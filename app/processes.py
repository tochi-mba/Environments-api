"""Live process inspection and the ownership guard for signals.

Signalling by pid is "kill any PID on the host, as a service" unless the target is proven
to belong to the environment. Proof is ancestry: walk the target's parents until a running
shell of the environment is reached, and confirm that shell's pid is still the process we
spawned (start time, not just pid).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app import procfs
from app.constants import ShellState
from app.errors import NotFoundError
from app.shells.shell import Shell


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """One live process in an environment."""

    pid: int
    ppid: int
    pgid: int
    state: str
    rss_bytes: int
    cpu_seconds: float
    elapsed_seconds: float
    cmdline: str
    shell_id: str

    def to_dict(self) -> dict[str, Any]:
        """The API representation."""
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "state": self.state,
            "rss_bytes": self.rss_bytes,
            "cpu_seconds": round(self.cpu_seconds, 3),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "cmdline": self.cmdline,
            "shell_id": self.shell_id,
        }


def _live_shells(shells: list[Shell]) -> list[Shell]:
    return [
        shell
        for shell in shells
        if shell.state is ShellState.RUNNING
        and procfs.is_same_process(shell.pid, shell.start_ticks)
    ]


def list_processes(shells: list[Shell]) -> list[ProcessInfo]:
    """Every live process under the given shells, each shell's own process included."""
    snap = procfs.snapshot()
    found: list[ProcessInfo] = []
    for shell in _live_shells(shells):
        for pid in [shell.pid, *procfs.descendants(shell.pid, snap)]:
            stat = snap.get(pid)
            if stat is None:
                continue
            found.append(
                ProcessInfo(
                    pid=stat.pid,
                    ppid=stat.ppid,
                    pgid=stat.pgid,
                    state=stat.state,
                    rss_bytes=stat.rss_bytes,
                    cpu_seconds=stat.cpu_seconds,
                    elapsed_seconds=stat.elapsed_seconds(),
                    cmdline=procfs.read_cmdline(pid) or stat.comm,
                    shell_id=shell.id,
                )
            )
    return found


def owning_shell(pid: int, shells: list[Shell]) -> Shell | None:
    """The shell whose process tree contains ``pid``, or ``None``."""
    by_pid = {shell.pid: shell for shell in _live_shells(shells)}
    snap = procfs.snapshot()
    current = pid
    seen: set[int] = set()
    while current > 1 and current not in seen:
        seen.add(current)
        if current in by_pid:
            return by_pid[current]
        stat = snap.get(current)
        if stat is None:
            return None
        current = stat.ppid
    return None


def signal_process(pid: int, sig: int, shells: list[Shell]) -> str:
    """Send ``sig`` to ``pid`` if, and only if, it belongs to one of ``shells``.

    Returns:
        The owning shell's id.

    Raises:
        NotFoundError: The process is not part of this environment (or no longer exists).
            A 403 here would confirm the pid's existence to a caller who must not learn it.
    """
    owner = owning_shell(pid, shells)
    if owner is None:
        raise NotFoundError(f"process {pid} is not part of this environment", pid=pid)
    try:
        os.kill(pid, sig)
    except ProcessLookupError as exc:
        raise NotFoundError(f"process {pid} has already exited", pid=pid) from exc
    return owner.id
