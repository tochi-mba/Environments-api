from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app import procfs


def test_read_stat_self() -> None:
    stat = procfs.read_stat(os.getpid())
    assert stat is not None
    assert stat.pid == os.getpid()
    assert stat.ppid == os.getppid()
    assert stat.rss_bytes > 0
    assert stat.cpu_seconds >= 0
    assert stat.elapsed_seconds() >= 0
    assert stat.elapsed_seconds(now=0) == 0
    assert procfs.read_cmdline(os.getpid())


def test_read_stat_missing(tmp_path: Path) -> None:
    assert procfs.read_stat(2**22 - 1) is None
    assert procfs.read_cmdline(2**22 - 1) == ""
    assert procfs.read_stat(1, proc=tmp_path) is None


def test_comm_with_spaces_and_parens(tmp_path: Path) -> None:
    pid_dir = tmp_path / "42"
    pid_dir.mkdir()
    fields = ["S", "1", "42", "42", "0", "-1", "0", "0", "0", "0", "0", "7", "3"] + ["0"] * 6
    fields += ["12345", "0", "10"] + ["0"] * 30
    (pid_dir / "stat").write_text("42 (a (weird) name) " + " ".join(fields) + "\n")
    stat = procfs.read_stat(42, proc=tmp_path)
    assert stat is not None
    assert stat.comm == "a (weird) name"
    assert stat.ppid == 1 and stat.pgid == 42
    assert stat.start_ticks == 12345
    assert stat.rss_bytes == 10 * procfs.PAGE_SIZE
    assert stat.utime_ticks == 7 and stat.stime_ticks == 3
    (tmp_path / "notapid").mkdir()
    snap = procfs.snapshot(tmp_path)
    assert set(snap) == {42}


def test_boot_time_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stat").write_text("cpu 1 2 3\n")
    procfs.boot_time.cache_clear()
    monkeypatch.setattr(procfs, "PROC", tmp_path)
    with pytest.raises(RuntimeError):
        procfs.boot_time()
    monkeypatch.undo()
    procfs.boot_time.cache_clear()
    assert procfs.boot_time() > 0


def test_descendants_and_identity() -> None:
    child = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
    try:
        stat = procfs.read_stat(child.pid)
        assert stat is not None
        found: list[int] = []
        for _ in range(200):
            found = procfs.descendants(os.getpid(), procfs.snapshot())
            if len([p for p in found if p == child.pid]) == 1 and len(found) >= 2:
                break
        assert child.pid in found
        assert procfs.is_same_process(child.pid, stat.start_ticks)
        assert not procfs.is_same_process(child.pid, stat.start_ticks + 1)
    finally:
        child.kill()
        child.wait()
    assert not procfs.is_same_process(child.pid, stat.start_ticks)


def test_snapshot_skips_entries_that_are_not_processes_or_have_gone(tmp_path: Path) -> None:
    # A pid directory can vanish between listing /proc and reading its stat, and /proc holds
    # plenty that is not a pid at all. Neither may take the snapshot down.
    (tmp_path / "123").mkdir()
    (tmp_path / "self").mkdir()
    assert procfs.snapshot(proc=tmp_path) == {}
