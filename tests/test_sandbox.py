from __future__ import annotations

import os
import resource
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.constants import SandboxTier
from app.errors import SandboxError
from app.sandbox import ResourceLimits, Sandbox, SpawnRequest, build_sandbox, highest_tier
from app.sandbox.detect import HostCapabilities, probe_host, unshare_probe_argv
from app.sandbox.directory import DirectorySandbox
from app.sandbox.namespace import NamespaceSandbox, running_as_root
from app.sandbox.user import UserSandbox
from app.sandbox.users import SystemUsers, user_name
from tests.conftest import requires_root, requires_unshare, requires_useradd, run_quiet


def _request(tmp_path: Path, argv: list[str], **kw: Any) -> SpawnRequest:
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    return SpawnRequest(
        environment_id="env_deadbeef0001",
        argv=argv,
        workspace=ws,
        cwd=ws,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(ws)},
        limits=ResourceLimits(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kw,
    )


def _run(sandbox: Sandbox, tmp_path: Path, script: str, **kw: Any) -> tuple[int, str]:
    req = _request(tmp_path, ["/bin/sh", "-c", script], **kw)
    sandbox.prepare(req.environment_id, tmp_path, req.workspace)
    proc = sandbox.spawn(req)
    out, _ = proc.communicate(timeout=60)
    return proc.returncode, out.decode(errors="replace")


def test_tier_parse_and_order() -> None:
    assert SandboxTier.parse("Namespace") is SandboxTier.NAMESPACE
    assert SandboxTier.DIRECTORY < SandboxTier.USER < SandboxTier.NAMESPACE
    assert SandboxTier.USER.label == "user"
    with pytest.raises(ValueError, match="unknown sandbox tier"):
        SandboxTier.parse("bogus")


def test_detector_matches_host() -> None:
    caps = probe_host()
    assert caps.is_root == (os.geteuid() == 0)
    assert caps.has_useradd == (shutil.which("useradd") is not None)
    assert caps.has_setpriv == (shutil.which("setpriv") is not None)
    expected = run_quiet(unshare_probe_argv(caps.is_root)).returncode == 0
    assert caps.unshare_works == expected
    assert caps.rootless_namespace == (expected and not caps.is_root)


def test_detector_injected_outcomes() -> None:
    def ok(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def fail(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, b"", b"Operation not permitted")

    def silent_fail(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 2, b"", b"")

    def boom(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 10)

    everything = lambda name: "/usr/bin/" + name  # noqa: E731
    nothing = lambda name: None  # noqa: E731
    caps = probe_host(run=ok, which=everything, euid=0)
    assert caps == HostCapabilities(True, True, True, True, "")
    assert highest_tier(caps) is SandboxTier.NAMESPACE
    caps = probe_host(run=fail, which=everything, euid=0)
    assert not caps.unshare_works and "not permitted" in caps.unshare_error
    assert highest_tier(caps) is SandboxTier.USER
    caps = probe_host(run=silent_fail, which=everything, euid=1000)
    assert caps.unshare_error == "exit 2"
    assert highest_tier(caps) is SandboxTier.DIRECTORY
    caps = probe_host(run=boom, which=everything, euid=1000)
    assert "probe failed" in caps.unshare_error
    caps = probe_host(run=ok, which=nothing, euid=0)
    assert caps.unshare_error == "unshare not found"
    assert highest_tier(caps) is SandboxTier.DIRECTORY
    caps = probe_host(run=ok, which=everything, euid=1000)
    assert highest_tier(caps) is SandboxTier.NAMESPACE and caps.rootless_namespace
    # Root without setpriv cannot drop privileges inside the namespace: not honest to
    # call that the namespace tier.
    caps = HostCapabilities(True, True, False, True)
    assert highest_tier(caps) is SandboxTier.USER


def test_build_sandbox_respects_minimum() -> None:
    caps = HostCapabilities(False, False, False, False, "unshare not found")
    assert isinstance(build_sandbox(caps, SandboxTier.DIRECTORY), DirectorySandbox)
    with pytest.raises(RuntimeError, match="ENVAPI_MIN_SANDBOX_TIER"):
        build_sandbox(caps, SandboxTier.USER)
    assert isinstance(
        build_sandbox(HostCapabilities(True, True, False, False), SandboxTier.USER), UserSandbox
    )
    ns = build_sandbox(HostCapabilities(False, False, False, True), SandboxTier.NAMESPACE)
    assert isinstance(ns, NamespaceSandbox)


def test_directory_tier_runs_with_limits(tmp_path: Path) -> None:
    sandbox = DirectorySandbox()
    assert sandbox.tier is SandboxTier.DIRECTORY
    assert sandbox.owner("env_x") is None
    req = _request(tmp_path, ["/bin/bash", "-c", "pwd; ulimit -t; ulimit -f; echo $HOME"])
    req.limits = ResourceLimits(cpu_seconds=123, file_size_bytes=4096 * 1024)
    proc = sandbox.spawn(req)
    out, _ = proc.communicate(timeout=30)
    lines = out.decode().splitlines()
    assert lines[0] == str(req.workspace.resolve()) or lines[0] == str(req.workspace)
    assert lines[1] == "123"
    assert lines[2] == "4096"
    assert lines[3] == str(req.workspace)
    assert proc.returncode == 0
    sandbox.teardown("env_x")


def test_limits_apply_in_child(tmp_path: Path) -> None:
    _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    limits = ResourceLimits(nproc=min(hard, 4096) if hard != resource.RLIM_INFINITY else 4096)
    req = _request(tmp_path, ["/bin/bash", "-c", "ulimit -u"])
    req.limits = limits
    proc = DirectorySandbox().spawn(req)
    out, _ = proc.communicate(timeout=30)
    assert out.decode().strip() == str(limits.nproc)


def test_memory_limit_is_enforced(tmp_path: Path) -> None:
    script = "python3 -c 'x = bytearray(600 * 1024 * 1024)' 2>/dev/null; echo rc=$?"
    req = _request(tmp_path, ["/bin/sh", "-c", script])
    req.limits = ResourceLimits(memory_bytes=256 * 1024 * 1024)
    proc = DirectorySandbox().spawn(req)
    out, _ = proc.communicate(timeout=60)
    assert out.decode().strip() != "rc=0"


class FakeRun:
    def __init__(self, rc: int = 0, stderr: bytes = b"") -> None:
        self.calls: list[list[str]] = []
        self.rc = rc
        self.stderr = stderr

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.rc, b"", self.stderr)


def test_system_users_error_paths() -> None:
    failing = FakeRun(rc=1, stderr=b"nope")
    with pytest.raises(SandboxError, match=r"useradd .* failed: nope"):
        SystemUsers(run=failing).ensure("env_ffffffffffff", "/nowhere")
    lying = FakeRun(rc=0)
    with pytest.raises(SandboxError, match="user is missing"):
        SystemUsers(run=lying).ensure("env_ffffffffffff", "/nowhere")
    users = SystemUsers(run=lying)
    users.remove("env_ffffffffffff")
    assert len(lying.calls) == 1
    assert user_name("env_abc") == "envapi_abc"


@requires_root
@requires_useradd
def test_user_tier_drops_privileges(tmp_path: Path) -> None:
    sandbox = UserSandbox()
    assert sandbox.tier is SandboxTier.USER
    env_id = "env_deadbeef0001"
    try:
        _rc, out = _run(
            sandbox, tmp_path, "id -u; id -G; touch ok && echo wrote; touch /root/x 2>&1"
        )
        uid, gid = sandbox.owner(env_id) or (0, 0)
        lines = out.splitlines()
        assert lines[0] == str(uid) and uid != 0
        assert lines[1] == str(gid)
        assert lines[2] == "wrote"
        assert "ermission denied" in lines[3]
        assert (tmp_path / "workspace" / "ok").stat().st_uid == uid
        # A second spawn reuses the user; the lookup path is exercised.
        _rc, out = _run(sandbox, tmp_path, "id -u")
        assert out.strip() == str(uid)
        assert oct(tmp_path.stat().st_mode & 0o777) == "0o711"
    finally:
        sandbox.teardown(env_id)
    assert sandbox.owner(env_id) is None


@requires_root
@requires_useradd
def test_user_tier_spawn_creates_missing_user(tmp_path: Path) -> None:
    sandbox = UserSandbox()
    req = _request(tmp_path, ["/bin/sh", "-c", "id -u"])
    try:
        proc = sandbox.spawn(req)
        out, _ = proc.communicate(timeout=30)
        assert out.strip() != b"0"
    finally:
        sandbox.teardown(req.environment_id)


@requires_unshare
def test_namespace_tier_isolates(tmp_path: Path) -> None:
    rootless = not running_as_root()
    sandbox = NamespaceSandbox(rootless=rootless)
    assert sandbox.tier is SandboxTier.NAMESPACE
    env_id = "env_deadbeef0001"
    script = (
        "echo pid=$$; ls /proc | grep -c '^[0-9]'; "
        "touch /etc/envapi_probe 2>&1; touch ok && echo wrote; "
        "touch /tmp/t && echo tmp_ok; id -u"
    )
    try:
        rc, out = _run(sandbox, tmp_path, script)
        lines = out.splitlines()
        assert rc == 0, out
        assert lines[0] == "pid=1"
        assert int(lines[1]) <= 4
        assert "Read-only file system" in lines[2]
        assert lines[3] == "wrote"
        assert lines[4] == "tmp_ok"
        if rootless:
            assert lines[5] == "0"
            assert sandbox.owner(env_id) is None
        else:
            owner = sandbox.owner(env_id)
            assert owner is not None and lines[5] == str(owner[0]) and owner[0] != 0
            assert oct(tmp_path.stat().st_mode & 0o777) == "0o711"
    finally:
        sandbox.teardown(env_id)


@requires_unshare
def test_namespace_tier_without_network(tmp_path: Path) -> None:
    sandbox = NamespaceSandbox(rootless=not running_as_root())
    try:
        rc, out = _run(sandbox, tmp_path, "cat /proc/net/dev | tail -n +3 | wc -l", network=False)
        assert rc == 0
        assert int(out.strip()) <= 1  # loopback at most, and it is down
    finally:
        sandbox.teardown("env_deadbeef0001")


@requires_unshare
@requires_root
def test_namespace_tier_spawn_creates_missing_user(tmp_path: Path) -> None:
    sandbox = NamespaceSandbox(rootless=False)
    req = _request(tmp_path, ["/bin/sh", "-c", "id -u"])
    try:
        proc = sandbox.spawn(req)
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0 and out.strip() != b"0"
    finally:
        sandbox.teardown(req.environment_id)


def test_namespace_rootless_prepare_is_noop(tmp_path: Path) -> None:
    sandbox = NamespaceSandbox(rootless=True, users=SystemUsers(run=FakeRun()))
    sandbox.prepare("env_x", tmp_path, tmp_path)
    assert sandbox.owner("env_x") is None
    sandbox.teardown("env_x")
