"""Code that runs in the forked child before exec, measured through multiprocessing."""

from __future__ import annotations

import multiprocessing
import os
import resource
import subprocess
from pathlib import Path

import pytest

from app.sandbox import ResourceLimits, SpawnRequest
from app.sandbox.user import drop_privileges
from tests.conftest import requires_root


def _request(tmp_path: Path, tty: int | None) -> SpawnRequest:
    return SpawnRequest(
        environment_id="env_child",
        argv=["true"],
        workspace=tmp_path,
        cwd=tmp_path,
        env={},
        limits=ResourceLimits(
            cpu_seconds=600, file_size_bytes=1 << 30, memory_bytes=1 << 31, nproc=512
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        controlling_tty=tty,
    )


def _tty_child(slave: int, tmp_path: Path) -> None:
    os.setsid()
    _request(tmp_path, slave).child_setup()
    assert os.open("/dev/tty", os.O_RDWR) >= 0
    assert resource.getrlimit(resource.RLIMIT_CPU) == (600, 600)
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (512, 512)


def _drop_child(uid: int, gid: int, tmp_path: Path) -> None:
    drop_privileges(uid, gid, _request(tmp_path, None))
    assert os.getuid() == uid and os.getgid() == gid and os.getgroups() == []
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == (1 << 30, 1 << 30)


def _run_child(target: object, *args: object) -> None:
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=target, args=args)  # type: ignore[arg-type]
    proc.start()
    proc.join(30)
    assert proc.exitcode == 0


def test_child_setup_claims_tty_and_limits(tmp_path: Path) -> None:
    master, slave = os.openpty()
    try:
        _run_child(_tty_child, slave, tmp_path)
    finally:
        os.close(master)
        os.close(slave)


def test_child_setup_without_tty(tmp_path: Path) -> None:
    def child() -> None:
        _request(tmp_path, None).child_setup()
        assert resource.getrlimit(resource.RLIMIT_AS) == (1 << 31, 1 << 31)

    _run_child(child)


@requires_root
def test_drop_privileges(tmp_path: Path) -> None:
    import pwd

    nobody = pwd.getpwnam("nobody")
    _run_child(_drop_child, nobody.pw_uid, nobody.pw_gid, tmp_path)


def test_limits_apply_in_process_noop() -> None:
    ResourceLimits().apply()  # nothing set, nothing changed


@pytest.mark.parametrize("value", [None, resource.getrlimit(resource.RLIMIT_FSIZE)[1]])
def test_limits_apply_current_hard_value(value: int | None) -> None:
    # Setting a limit to its own hard value is a no-op, so this is safe in the test process.
    ResourceLimits(file_size_bytes=value).apply()
