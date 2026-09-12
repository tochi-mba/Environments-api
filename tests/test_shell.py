from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import procfs
from app.constants import CommandState, ShellState
from app.errors import SandboxError, ShellBusyError, ShellNotRunningError
from app.keyring.client import ResolvedCredential
from app.sandbox import ResourceLimits, SpawnRequest
from app.sandbox.directory import DirectorySandbox
from app.shells.shell import Shell, ShellSpec

BASH = "/bin/bash"


def state_of(shell: Shell) -> ShellState:
    """Re-read the state; keeps mypy from narrowing across a mutation."""
    return shell.state


def make_shell(tmp_path: Path, *, pty: bool = False, buffer_bytes: int = 1 << 20) -> Shell:
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    spec = ShellSpec(
        shell_id="sh_test0001",
        environment_id="env_test0001",
        cwd=".",
        pty=pty,
        logs_dir=logs,
        buffer_bytes=buffer_bytes,
        max_log_bytes=64,
    )
    request = SpawnRequest(
        environment_id=spec.environment_id,
        argv=[BASH],
        workspace=ws,
        cwd=ws,
        env={"PATH": os.environ["PATH"], "HOME": str(ws), "PS1": "", "TERM": "dumb"},
        limits=ResourceLimits(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return Shell.spawn(spec, DirectorySandbox(), request)


@pytest.fixture
def shell(tmp_path: Path) -> Iterator[Shell]:
    sh = make_shell(tmp_path)
    yield sh
    sh.close(grace_seconds=2)


def run(shell: Shell, command: str, timeout: float = 10, **kw: object) -> tuple[int | None, bytes]:
    record = shell.exec(command, **kw)  # type: ignore[arg-type]
    assert shell.wait_command(record, timeout), "command did not finish"
    assert record.output_end is not None
    return record.exit_code, shell.read_output(record.output_start, 1 << 20).data


def test_lifecycle(shell: Shell) -> None:
    assert shell.state is ShellState.RUNNING
    assert shell.pid > 0 and shell.pgid == shell.pid and shell.start_ticks > 0
    assert procfs.is_same_process(shell.pid, shell.start_ticks)
    code, out = run(shell, "echo hello")
    assert (code, out) == (0, b"hello\n")
    record = shell.commands[-1]
    assert record.state is CommandState.EXITED
    assert record.log_path.read_bytes() == b"hello\n"
    assert shell.current is None and shell.cursor == 6
    assert shell.to_dict()["commands_run"] == 1
    assert shell.find_command(record.id) is record
    assert shell.find_command("cmd_nope") is None
    shell.close(grace_seconds=2)
    assert state_of(shell) is ShellState.CLOSED and shell.dead_reason == "closed"
    assert not procfs.is_same_process(shell.pid, shell.start_ticks)
    shell.close(grace_seconds=1)  # idempotent
    with pytest.raises(ShellNotRunningError):
        shell.exec("true")
    with pytest.raises(ShellNotRunningError):
        shell.signal(signal.SIGTERM)
    with pytest.raises(ShellNotRunningError):
        shell.write_stdin(b"x")


def test_exit_codes(shell: Shell) -> None:
    assert run(shell, "false")[0] == 1
    assert run(shell, "sh -c 'exit 42'")[0] == 42
    assert run(shell, "sh -c 'kill -TERM $$'")[0] == 143
    assert run(shell, "this_command_does_not_exist 2>/dev/null")[0] == 127
    code, _out = run(shell, "echo 'unterminated")
    assert code != 0
    assert run(shell, "true")[0] == 0  # the shell is still usable afterwards


def test_state_persists_between_commands(shell: Shell) -> None:
    run(shell, "mkdir -p sub && cd sub && export X=1")
    _code, out = run(shell, 'basename "$PWD"; echo $X')
    assert out == b"sub\n1\n"


def test_frame_byte_in_output_and_spoofed_nonce(shell: Shell) -> None:
    code, out = run(shell, "printf 'a\\036b'")
    assert (code, out) == (0, b"a\x1eb")
    spoof = "printf '\\036%s:0\\036' " + "0" * 32 + "; false"
    code, out = run(shell, spoof)
    assert code == 1
    assert out == b"\x1e" + b"0" * 32 + b":0\x1e"
    # A well-formed frame at the very end of the stream is held until more data settles it.
    code, out = run(shell, "printf '\\036'; sleep 0.05; printf 'tail'")
    assert (code, out) == (0, b"\x1etail")


def test_stdin_reaches_command(shell: Shell) -> None:
    record = shell.exec("read line; echo got=$line")
    assert shell.write_stdin(b"abc\n") == 4
    assert shell.wait_command(record, 10)
    assert shell.read_output(record.output_start, 100).data == b"got=abc\n"
    with pytest.raises(ShellBusyError):
        shell.write_stdin(b"x", target="tty")


def test_second_exec_is_refused_and_signal_interrupts(shell: Shell) -> None:
    record = shell.exec("sleep 30")
    with pytest.raises(ShellBusyError) as info:
        shell.exec("echo no")
    assert info.value.extra["command_id"] == record.id
    assert not shell.wait_idle(0.05)
    assert shell.signal(signal.SIGTERM) >= 1
    assert shell.wait_command(record, 10)
    assert record.state is CommandState.EXITED and record.exit_code == 143
    assert shell.wait_idle(1)
    assert shell.signal(signal.SIGTERM) == 0


def test_timeout_kills_children(shell: Shell) -> None:
    record = shell.exec("sleep 30", timeout_ms=200)
    assert shell.wait_command(record, 10)
    assert record.state is CommandState.TIMED_OUT
    assert record.exit_code == 137
    assert shell.state is ShellState.RUNNING
    assert run(shell, "echo alive")[1] == b"alive\n"


def test_timeout_escalates_to_the_shell(shell: Shell) -> None:
    record = shell.exec("while :; do :; done", timeout_ms=100)
    assert shell.wait_command(record, 10)
    assert record.state is CommandState.TIMED_OUT and record.exit_code is None
    assert shell.wait_exit(5)
    assert shell.state is ShellState.DEAD and shell.dead_reason == "signal:9"


def test_timeout_after_completion_is_ignored(shell: Shell) -> None:
    record = shell.exec("true", timeout_ms=100)
    assert shell.wait_command(record, 10)
    shell._on_timeout(record)
    shell._escalate_timeout(record)
    assert record.state is CommandState.EXITED
    later = shell.exec("sleep 5", timeout_ms=50)
    assert shell.wait_command(later, 10)
    assert later.state is CommandState.TIMED_OUT


def test_shell_dies_mid_command(shell: Shell) -> None:
    record = shell.exec("echo partial; kill -9 $$")
    assert shell.wait_command(record, 10)
    assert record.state is CommandState.SHELL_DIED and record.exit_code is None
    assert shell.wait_exit(5)
    assert shell.state is ShellState.DEAD and shell.dead_reason == "signal:9"
    assert shell.read_output(0, 100).data == b"partial\n"
    assert shell.to_dict()["state"] == "dead"


def test_exit_command_ends_shell_cleanly(shell: Shell) -> None:
    record = shell.exec("exit 7")
    assert shell.wait_command(record, 10)
    assert shell.wait_exit(5)
    assert shell.dead_reason == "exit:7" and record.state is CommandState.SHELL_DIED
    assert record.exit_code == 7


def test_close_kills_whole_tree(shell: Shell) -> None:
    record = shell.exec("(sleep 100 & sleep 100 & wait) & echo spawned; sleep 100")
    assert shell.wait_output(record.output_start, 10)
    deadline = time.monotonic() + 10
    pids: list[int] = []
    while time.monotonic() < deadline:
        pids = procfs.descendants(shell.pid, procfs.snapshot())
        if len(pids) >= 4:
            break
    assert len(pids) >= 4
    stats = {pid: procfs.read_stat(pid) for pid in pids}
    shell.close(grace_seconds=2)
    assert shell.state is ShellState.CLOSED
    for pid, stat in stats.items():
        assert stat is not None
        deadline = time.monotonic() + 5
        while procfs.is_same_process(pid, stat.start_ticks) and time.monotonic() < deadline:
            pass
        assert not procfs.is_same_process(pid, stat.start_ticks), f"{pid} survived close"


def test_close_escalates_to_sigkill(shell: Shell) -> None:
    record = shell.exec("trap '' TERM; echo ready; sleep 100")
    assert shell.wait_output(record.output_start, 10)
    shell.close(grace_seconds=0.2)
    assert shell.state is ShellState.CLOSED
    assert record.state is CommandState.SHELL_DIED


def test_buffer_rollover_and_log_cap(tmp_path: Path) -> None:
    shell = make_shell(tmp_path, buffer_bytes=16)
    try:
        _code, _ = run(shell, "printf '%s' 0123456789abcdefghij")
        chunk = shell.read_output(0, 100)
        assert chunk.dropped_bytes == 4 and chunk.data == b"456789abcdefghij"
        record = shell.commands[-1]
        assert record.log_bytes == 20 and not record.log_truncated
        run(shell, "head -c 100 /dev/zero | tr '\\0' x")
        record = shell.commands[-1]
        assert record.log_truncated and record.log_bytes == 64
        assert record.log_path.stat().st_size == 64
    finally:
        shell.close(2)


def test_redaction_never_leaks(shell: Shell) -> None:
    creds = [
        ResolvedCredential("github", {"GITHUB_TOKEN": "ghp_supersecretvalue"}),
        ResolvedCredential("npm", {"NPM_TOKEN": "npm_othersecret"}),
    ]
    record = shell.exec(
        "echo $GITHUB_TOKEN; echo $NPM_TOKEN | tr -d '\\n'; env | grep -c TOKEN", credentials=creds
    )
    assert shell.wait_command(record, 10)
    out = shell.read_output(record.output_start, 1000).data
    assert b"supersecret" not in out and b"othersecret" not in out
    assert out == "«redacted:github»\n«redacted:npm»2\n".encode()
    assert b"supersecret" not in record.log_path.read_bytes()
    # Credentials are scoped to the command they were resolved for.
    _code, out = run(shell, "echo [$GITHUB_TOKEN]")
    assert out == b"[]\n"


def test_stdin_write_when_shell_not_reading(shell: Shell, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.shells.shell.STDIN_WRITE_TIMEOUT", 0.2)
    big = "x" * (1 << 16)
    record = shell.exec("sleep 30")
    with pytest.raises(SandboxError, match="not accepting input"):
        shell.write_stdin(big.encode() * 4)
    shell.signal(signal.SIGKILL)
    assert shell.wait_command(record, 10)
    with pytest.raises(ShellBusyError, match="too large"):
        shell.exec("x" * (2 << 20))


def test_stdin_closed_after_exit(shell: Shell) -> None:
    record = shell.exec("exit 0")
    assert shell.wait_exit(5)
    assert record.state is CommandState.SHELL_DIED and record.exit_code == 0
    with pytest.raises(ShellNotRunningError):
        shell.write_stdin(b"x")


def test_exec_when_stdin_broken(shell: Shell) -> None:
    # Close our end of stdin out from under the shell to force the write error path.
    os.close(shell._stdin_fd)
    shell._stdin_fd = os.open(os.devnull, os.O_RDONLY)
    with pytest.raises(SandboxError):
        shell.exec("true")
    assert shell.commands[-1].state is CommandState.SHELL_DIED
    shell._stdin_fd = -1
    with pytest.raises(SandboxError, match="closed"):
        shell._write_stdin(b"true\n")


def test_pty_mode(tmp_path: Path) -> None:
    shell = make_shell(tmp_path, pty=True)
    try:
        code, out = run(
            shell, "[ -t 1 ] && echo tty-out; [ -t 0 ] || echo pipe-in; readlink /proc/self/fd/1"
        )
        assert code == 0
        assert out.startswith(b"tty-out\r\npipe-in\r\n/dev/pts/")
        record = shell.exec("read -r line < /dev/tty; echo got=$line")
        assert shell.write_stdin(b"viatty\n", target="tty") == 7
        assert shell.wait_command(record, 10)
        assert shell.read_output(record.output_start, 100).data == b"got=viatty\r\n"
        assert shell.to_dict()["pty"] is True
    finally:
        shell.close(2)
    assert shell.state is ShellState.CLOSED


def test_spawn_failure_cleans_up(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    spec = ShellSpec("sh_x", "env_x", ".", False, tmp_path, 1024, 1024)
    request = SpawnRequest(
        environment_id="env_x",
        argv=["/nonexistent/shell"],
        workspace=ws,
        cwd=ws,
        env={},
        limits=ResourceLimits(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with pytest.raises(SandboxError, match="could not start shell"):
        Shell.spawn(spec, DirectorySandbox(), request)


def test_mark_dead(shell: Shell) -> None:
    record = shell.exec("sleep 30")
    shell.mark_dead("service_restarted")
    assert shell.state is ShellState.DEAD and shell.dead_reason == "service_restarted"
    assert record.state is CommandState.SHELL_DIED
    os.killpg(shell.pgid, signal.SIGKILL)


def test_on_change_callback(tmp_path: Path) -> None:
    seen: list[str] = []
    ws = tmp_path / "workspace"
    ws.mkdir()
    spec = ShellSpec("sh_cb", "env_cb", ".", False, tmp_path, 1024, 1024)
    request = SpawnRequest(
        environment_id="env_cb",
        argv=[BASH],
        workspace=ws,
        cwd=ws,
        env={"PATH": os.environ["PATH"]},
        limits=ResourceLimits(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shell = Shell.spawn(
        spec, DirectorySandbox(), request, on_change=lambda s: seen.append(s.state.value)
    )
    run(shell, "true")
    shell.close(2)
    assert seen[0] == "running" and seen[-1] == "closed"
