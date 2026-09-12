"""One persistent shell process and the commands run inside it.

The core is synchronous and thread-safe: a reader thread drains the shell's merged
stdout+stderr, and every state change happens under one condition variable so waiters
(``wait``, ``close``) are woken by events rather than by polling.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import select
import signal
import subprocess
import termios
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app import procfs
from app.constants import COMMAND_HISTORY_LIMIT, CommandState, ShellState
from app.errors import SandboxError, ShellBusyError, ShellNotRunningError
from app.keyring.client import ResolvedCredential
from app.sandbox.protocol import Sandbox, SpawnRequest
from app.shells.buffer import OutputChunk, RingBuffer
from app.shells.framing import Frame, FrameParser, exec_script, new_nonce
from app.shells.redact import Redactor

log = structlog.get_logger(__name__)

READ_SIZE = 65536
STDIN_WRITE_TIMEOUT = 5.0
# After killing a command's children on timeout, how long to wait for the frame before
# concluding the shell itself is stuck (a builtin loop has no children to kill).
TIMEOUT_ESCALATION_SECONDS = 1.0
MAX_COMMAND_BYTES = 1024 * 1024


def new_id(prefix: str) -> str:
    """A random, URL-safe identifier with a type prefix."""
    return f"{prefix}_{secrets.token_hex(6)}"


class _LogWriter:
    """Appends a command's output to its log file up to a cap."""

    def __init__(self, path: Path, cap: int) -> None:
        self._path = path
        self._cap = cap
        self._handle = path.open("wb")
        self.written = 0
        self.truncated = False

    def write(self, data: bytes) -> None:
        room = self._cap - self.written
        if room <= 0:
            self.truncated = self.truncated or bool(data)
            return
        if len(data) > room:
            self.truncated = True
            data = data[:room]
        self._handle.write(data)
        self._handle.flush()
        self.written += len(data)

    def close(self) -> None:
        self._handle.close()


@dataclass(slots=True)
class CommandRecord:
    """A command's lifecycle and where its output went."""

    id: str
    shell_id: str
    environment_id: str
    command: str
    state: CommandState
    started_at: float
    output_start: int
    log_path: Path
    timeout_ms: int | None
    nonce: str = field(repr=False)
    log: _LogWriter = field(repr=False)
    exit_code: int | None = None
    finished_at: float | None = None
    output_end: int | None = None
    log_bytes: int = 0
    log_truncated: bool = False
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The API representation; the nonce never leaves the process."""
        return {
            "id": self.id,
            "shell_id": self.shell_id,
            "environment_id": self.environment_id,
            "command": self.command,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_start": self.output_start,
            "output_end": self.output_end,
            "log_bytes": self.log_bytes,
            "log_truncated": self.log_truncated,
            "timeout_ms": self.timeout_ms,
        }


@dataclass(frozen=True, slots=True)
class ShellSpec:
    """What a shell is opened with."""

    shell_id: str
    environment_id: str
    cwd: str
    pty: bool
    logs_dir: Path
    shell_binary: str
    buffer_bytes: int
    max_log_bytes: int


class Shell:
    """A running shell process. Construct through :meth:`spawn`."""

    def __init__(
        self,
        spec: ShellSpec,
        proc: subprocess.Popen[bytes],
        read_fd: int,
        stdin_fd: int,
        tty_fd: int | None,
        on_change: Callable[[Shell], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wrap an already-started process; :meth:`spawn` does the starting."""
        self.spec = spec
        self._clock = clock
        self.id = spec.shell_id
        self.environment_id = spec.environment_id
        self._proc = proc
        self._read_fd = read_fd
        self._stdin_fd = stdin_fd
        self._tty_fd = tty_fd
        self._on_change = on_change
        self.pid = proc.pid
        self._lineage: list[int] | None = None
        stat = procfs.read_stat(proc.pid)
        self.pgid = stat.pgid if stat else os.getpgid(proc.pid)
        self.start_ticks = stat.start_ticks if stat else 0
        self.state = ShellState.RUNNING
        self.dead_reason: str | None = None
        self.exit_status: int | None = None
        self.created_at = clock()
        self.last_activity = self.created_at
        self._cond = threading.Condition()
        self._buffer = RingBuffer(spec.buffer_bytes)
        self._current: CommandRecord | None = None
        self._redactor: Redactor | None = None
        self._closing = False
        self.commands: deque[CommandRecord] = deque(maxlen=COMMAND_HISTORY_LIMIT)
        self._timers: list[threading.Timer] = []
        self._reader = threading.Thread(
            target=self._read_loop, name=f"reader-{self.id}", daemon=True
        )
        self._reader.start()

    @classmethod
    def spawn(
        cls,
        spec: ShellSpec,
        sandbox: Sandbox,
        request: SpawnRequest,
        on_change: Callable[[Shell], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> Shell:
        """Start the process described by ``request`` and wrap it.

        Stdout and stderr are merged at the OS level so ordering is preserved. With
        ``pty``, they go to a pseudo-terminal that is also the controlling tty; stdin stays
        a pipe so the shell remains non-interactive and never echoes what we feed it.
        """
        stdin_r, stdin_w = os.pipe()
        # Non-blocking so a write to a shell that has stopped reading fails at the deadline
        # instead of blocking the caller (and the shell's lock) until the pipe drains.
        os.set_blocking(stdin_w, False)
        tty_master: int | None = None
        slave: int | None = None
        if spec.pty:
            tty_master, slave = os.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            out_r, out_w = tty_master, slave
            request.controlling_tty = slave
        else:
            out_r, out_w = os.pipe()
        request.stdin = stdin_r
        request.stdout = out_w
        request.stderr = out_w
        try:
            proc = sandbox.spawn(request)
        except (OSError, subprocess.SubprocessError) as exc:
            for fd in (stdin_r, stdin_w, out_r, out_w):
                os.close(fd)
            raise SandboxError(f"could not start shell: {exc}") from exc
        finally:
            request.stdin = subprocess.DEVNULL
            request.stdout = subprocess.DEVNULL
            request.stderr = subprocess.DEVNULL
        os.close(stdin_r)
        os.close(out_w)
        return cls(spec, proc, out_r, stdin_w, tty_master if spec.pty else None, on_change, clock)

    # ----- reading -------------------------------------------------------------------

    def _read_loop(self) -> None:
        parser = FrameParser()
        while True:
            try:
                chunk = os.read(self._read_fd, READ_SIZE)
            except OSError:
                # A pty master raises EIO once every slave handle is closed; either way the
                # stream is over.
                chunk = b""
            if not chunk:
                break
            with self._cond:
                self._handle(parser.feed(chunk))
                self._cond.notify_all()
            self._notify_change()
        with self._cond:
            self._handle([parser.flush()])
            self._on_exit()
            self._cond.notify_all()
        self._notify_change()

    def _handle(self, events: list[bytes | Frame]) -> None:
        for event in events:
            if isinstance(event, Frame):
                current = self._current
                if current is not None and event.nonce == current.nonce:
                    self._finish(current, event.exit_code)
                else:
                    # A frame we did not ask for is just output that happens to look like one.
                    self._emit(event.raw())
            else:
                self._emit(event)

    def _emit(self, data: bytes) -> None:
        if not data:
            return
        if self._redactor is not None:
            data = self._redactor.feed(data)
        self._buffer.append(data)
        current = self._current
        if current is not None:
            current.log.write(data)
        self.last_activity = self._clock()

    def _finish(
        self, record: CommandRecord, exit_code: int | None, shell_died: bool = False
    ) -> None:
        if self._redactor is not None:
            tail = self._redactor.flush()
            self._redactor = None
            self._emit(tail)
        record.exit_code = exit_code
        record.finished_at = self._clock()
        record.output_end = self._buffer.end
        record.log_bytes = record.log.written
        record.log_truncated = record.log.truncated
        record.log.close()
        if record.timed_out:
            record.state = CommandState.TIMED_OUT
        elif shell_died:
            record.state = CommandState.SHELL_DIED
        else:
            record.state = CommandState.EXITED
        self._current = None
        self.last_activity = self._clock()

    def _on_exit(self) -> None:
        self.exit_status = self._proc.wait()
        if self._current is not None:
            # A clean `exit N` still tells us N; a signal death tells us nothing.
            code = self.exit_status if self.exit_status >= 0 else None
            self._finish(self._current, code, shell_died=True)
        if self._closing:
            self.state = ShellState.CLOSED
            self.dead_reason = "closed"
        else:
            self.state = ShellState.DEAD
            self.dead_reason = (
                f"signal:{-self.exit_status}"
                if self.exit_status < 0
                else f"exit:{self.exit_status}"
            )
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        for fd in (self._read_fd, self._stdin_fd, self._tty_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._stdin_fd = -1

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change(self)

    # ----- commands ------------------------------------------------------------------

    def exec(
        self,
        command: str,
        timeout_ms: int | None = None,
        credentials: list[ResolvedCredential] | None = None,
    ) -> CommandRecord:
        """Start ``command``; returns immediately with its record.

        Raises:
            ShellNotRunningError: The shell has exited.
            ShellBusyError: A command is still running; a shell is serial by nature.
            SandboxError: The shell stopped accepting input.
        """
        if len(command.encode()) > MAX_COMMAND_BYTES:
            raise ShellBusyError("command is too large", limit=MAX_COMMAND_BYTES)
        with self._cond:
            if self.state is not ShellState.RUNNING:
                raise ShellNotRunningError(f"shell {self.id} is {self.state.value}")
            if self._current is not None:
                raise ShellBusyError(
                    f"shell {self.id} is running command {self._current.id}",
                    command_id=self._current.id,
                )
            env: dict[str, str] = {}
            secrets_map: dict[str, str] = {}
            for credential in credentials or []:
                env.update(credential.env)
                for secret in credential.secrets:
                    secrets_map[secret] = credential.service
            command_id = new_id("cmd")
            record = CommandRecord(
                id=command_id,
                shell_id=self.id,
                environment_id=self.environment_id,
                command=command,
                state=CommandState.RUNNING,
                started_at=self._clock(),
                output_start=self._buffer.end,
                log_path=self.spec.logs_dir / f"{command_id}.log",
                timeout_ms=timeout_ms,
                nonce=new_nonce(),
                log=_LogWriter(self.spec.logs_dir / f"{command_id}.log", self.spec.max_log_bytes),
            )
            self._redactor = Redactor(secrets_map) if secrets_map else None
            self._current = record
            self.commands.append(record)
            self.last_activity = record.started_at
            try:
                self._write_stdin(exec_script(command, record.nonce, env))
            except SandboxError:
                self._finish(record, None, shell_died=True)
                raise
            if timeout_ms is not None:
                timer = threading.Timer(timeout_ms / 1000, self._on_timeout, args=(record,))
                timer.daemon = True
                self._timers.append(timer)
                timer.start()
            self._cond.notify_all()
        self._notify_change()
        return record

    def _on_timeout(self, record: CommandRecord) -> None:
        with self._cond:
            if self._current is not record or record.state is not CommandState.RUNNING:
                return
            record.timed_out = True
            killed = self._signal_descendants(signal.SIGKILL)
            log.info("command_timeout", shell_id=self.id, command_id=record.id, killed=killed)
            timer = threading.Timer(
                TIMEOUT_ESCALATION_SECONDS, self._escalate_timeout, args=(record,)
            )
            timer.daemon = True
            self._timers.append(timer)
            timer.start()

    def _escalate_timeout(self, record: CommandRecord) -> None:
        with self._cond:
            if self._current is not record or record.state is not CommandState.RUNNING:
                return
            # Nothing to kill below the shell yet the frame never came: the shell itself is
            # busy (a builtin loop, a blocked read). Only killing it ends the command.
            log.warning("command_timeout_escalated", shell_id=self.id, command_id=record.id)
            self._kill_group(signal.SIGKILL)

    def _write_stdin(self, data: bytes) -> None:
        fd = self._stdin_fd
        if fd < 0:
            raise SandboxError("shell stdin is closed")
        deadline = time.monotonic() + STDIN_WRITE_TIMEOUT
        view = memoryview(data)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise SandboxError("shell is not accepting input")
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise SandboxError(f"shell stdin is closed: {exc}") from exc
            view = view[written:]

    def write_stdin(self, data: bytes, target: str = "stdin") -> int:
        """Write raw bytes to the shell's stdin, or to its tty when ``target`` is ``tty``."""
        with self._cond:
            if self.state is not ShellState.RUNNING:
                raise ShellNotRunningError(f"shell {self.id} is {self.state.value}")
            if target == "tty":
                if self._tty_fd is None:
                    raise ShellBusyError("shell has no tty", code_hint="open with pty=true")
                return os.write(self._tty_fd, data)
            self._write_stdin(data)
            self.last_activity = self._clock()
            return len(data)

    # ----- observation ---------------------------------------------------------------

    @property
    def current(self) -> CommandRecord | None:
        """The running command, if any."""
        return self._current

    @property
    def cursor(self) -> int:
        """Offset one past the newest output byte."""
        return self._buffer.end

    def read_output(self, cursor: int, max_bytes: int) -> OutputChunk:
        """Output from ``cursor``; see :class:`RingBuffer`."""
        return self._buffer.read(cursor, max_bytes)

    def wait_output(self, cursor: int, timeout: float) -> bool:
        """Block until there is output past ``cursor``, the command ends, or the shell exits."""
        with self._cond:
            return self._cond.wait_for(
                lambda: (
                    self._buffer.end > cursor
                    or self._current is None
                    or self.state is not ShellState.RUNNING
                ),
                timeout,
            )

    def wait_idle(self, timeout: float) -> bool:
        """Block until no command is running or the shell has exited; ``False`` on timeout."""
        with self._cond:
            return self._cond.wait_for(
                lambda: self._current is None or self.state is not ShellState.RUNNING, timeout
            )

    def wait_command(self, record: CommandRecord, timeout: float) -> bool:
        """Block until ``record`` finished; ``False`` on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: record.state is not CommandState.RUNNING, timeout)

    def wait_exit(self, timeout: float) -> bool:
        """Block until the process has exited and the reader drained; ``False`` on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self.state is not ShellState.RUNNING, timeout)

    def find_command(self, command_id: str) -> CommandRecord | None:
        """A command from this shell's in-memory history."""
        for record in self.commands:
            if record.id == command_id:
                return record
        return None

    # ----- signals -------------------------------------------------------------------

    def lineage(self, snap: dict[int, procfs.ProcStat] | None = None) -> list[int]:
        """Pids from the root process down to the shell itself.

        Under the namespace tier the root is ``unshare`` and the shell is its only child;
        under the others the two coincide. A shell inside a PID namespace cannot report
        its host pid, so this walks ``/proc`` down through single-child wrappers until it
        reaches a process named like the shell binary, and remembers the answer.
        """
        if self._lineage is not None:
            return self._lineage
        snap = snap if snap is not None else procfs.snapshot()
        shell_comm = os.path.basename(self.spec.shell_binary)[:15]
        chain = [self.pid]
        while chain[-1] in snap and snap[chain[-1]].comm != shell_comm:
            children = [p.pid for p in snap.values() if p.ppid == chain[-1]]
            if len(children) != 1:
                return chain
            chain.append(children[0])
        if chain[-1] in snap:
            self._lineage = chain
        return chain

    @property
    def shell_pid(self) -> int:
        """The shell process itself (a host pid), as opposed to the root process."""
        return self.lineage()[-1]

    def _signal_descendants(self, sig: int) -> int:
        snap = procfs.snapshot()
        # The shell and whatever sits between it and the root process (unshare, setpriv)
        # are not the command; a signal meant for the command must leave them alone.
        lineage = set(self.lineage(snap))
        pids = [pid for pid in procfs.descendants(self.pid, snap) if pid not in lineage]
        killed = 0
        for pid in pids:
            try:
                os.kill(pid, sig)
                killed += 1
            except ProcessLookupError:
                continue
        return killed

    def signal(self, sig: int) -> int:
        """Send ``sig`` to every process below the shell (the current command's tree).

        Returns:
            How many processes were signalled.
        """
        with self._cond:
            if self.state is not ShellState.RUNNING:
                raise ShellNotRunningError(f"shell {self.id} is {self.state.value}")
            return self._signal_descendants(sig)

    def _kill_group(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, sig)
        # Anything that called setsid() left the group; the tree walk still finds it.
        self._signal_descendants(sig)

    def close(self, grace_seconds: float) -> None:
        """SIGTERM the process group, SIGKILL whatever is left after ``grace_seconds``."""
        with self._cond:
            if self.state is not ShellState.RUNNING:
                return
            self._closing = True
            self._kill_group(signal.SIGTERM)
            if not self._cond.wait_for(lambda: self.state is not ShellState.RUNNING, grace_seconds):
                self._kill_group(signal.SIGKILL)
                self._cond.wait_for(lambda: self.state is not ShellState.RUNNING, grace_seconds)
        self._notify_change()

    def mark_dead(self, reason: str) -> None:
        """Record a shell that is known to be gone without a process to wait for."""
        with self._cond:
            self.state = ShellState.DEAD
            self.dead_reason = reason
            if self._current is not None:
                self._finish(self._current, None, shell_died=True)
            self._cond.notify_all()

    def to_dict(self) -> dict[str, Any]:
        """The API representation."""
        with self._cond:
            current = self._current
            return {
                "id": self.id,
                "environment_id": self.environment_id,
                "state": self.state.value,
                "dead_reason": self.dead_reason,
                "pid": self.pid,
                "shell_pid": self.shell_pid,
                "pgid": self.pgid,
                "start_ticks": self.start_ticks,
                "pty": self.spec.pty,
                "cwd": self.spec.cwd,
                "created_at": self.created_at,
                "last_activity_at": self.last_activity,
                "current_command_id": current.id if current else None,
                "cursor": self._buffer.end,
                "commands_run": len(self.commands),
            }
