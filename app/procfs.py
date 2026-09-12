"""Read process facts from ``/proc``.

The start time recorded here is the guard against PID reuse: a pid alone says nothing
about identity once the kernel recycles it, but a pid whose start time still matches what
we recorded is the same process we spawned.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROC = Path("/proc")
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


@dataclass(frozen=True, slots=True)
class ProcStat:
    """The parts of ``/proc/<pid>/stat`` the service cares about."""

    pid: int
    comm: str
    state: str
    ppid: int
    pgid: int
    utime_ticks: int
    stime_ticks: int
    start_ticks: int
    rss_bytes: int

    @property
    def cpu_seconds(self) -> float:
        """User plus system CPU time consumed so far."""
        return (self.utime_ticks + self.stime_ticks) / CLK_TCK

    def elapsed_seconds(self, now: float | None = None) -> float:
        """Wall-clock seconds since the process started."""
        started = boot_time() + self.start_ticks / CLK_TCK
        return max(0.0, (now if now is not None else time.time()) - started)


@cache
def boot_time() -> float:
    """Seconds since the epoch at which the host booted, from ``/proc/stat``."""
    for line in (PROC / "stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise RuntimeError("/proc/stat has no btime line")


def read_stat(pid: int, proc: Path = PROC) -> ProcStat | None:
    """Parse ``/proc/<pid>/stat``, or ``None`` if the process is gone."""
    try:
        raw = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm is parenthesised and may itself contain spaces or parentheses, so split on the
    # last closing parenthesis rather than on whitespace.
    head, _, tail = raw.rpartition(")")
    comm = head.split("(", 1)[1]
    fields = tail.split()
    return ProcStat(
        pid=pid,
        comm=comm,
        state=fields[0],
        ppid=int(fields[1]),
        pgid=int(fields[2]),
        utime_ticks=int(fields[11]),
        stime_ticks=int(fields[12]),
        start_ticks=int(fields[19]),
        rss_bytes=int(fields[21]) * PAGE_SIZE,
    )


def read_cmdline(pid: int, proc: Path = PROC) -> str:
    """The process's command line with NULs turned into spaces, or ``comm`` if unreadable."""
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")


def snapshot(proc: Path = PROC) -> dict[int, ProcStat]:
    """Every live process on the host, keyed by pid."""
    result: dict[int, ProcStat] = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            stat = read_stat(int(entry.name), proc)
            if stat is not None:
                result[stat.pid] = stat
    return result


def descendants(root_pid: int, snap: dict[int, ProcStat]) -> list[int]:
    """Every pid below ``root_pid`` in ``snap``, parents before children."""
    children: dict[int, list[int]] = {}
    for stat in snap.values():
        children.setdefault(stat.ppid, []).append(stat.pid)
    found: list[int] = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, []):
            found.append(child)
            frontier.append(child)
    return found


def is_same_process(pid: int, start_ticks: int, proc: Path = PROC) -> bool:
    """Whether ``pid`` is still the process that started at ``start_ticks``."""
    stat = read_stat(pid, proc)
    return stat is not None and stat.start_ticks == start_ticks and stat.state != "Z"
