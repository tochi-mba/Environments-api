"""Error branches and races that the main suites cannot reach naturally."""

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import processes, procfs
from app.audit import AuditLog
from app.constants import CommandState, EnvironmentState, ShellState
from app.environments.models import EnvironmentLimits, EnvironmentRecord, ShellRecord
from app.environments.service import EnvironmentService
from app.environments.store import EnvironmentStore
from app.errors import DomainError, NotFoundError, SandboxError
from app.main import create_app
from app.sandbox.namespace import NamespaceSandbox
from app.settings import Settings
from app.shells.shell import Shell
from tests.conftest import NO_SANDBOX, Clock, auth_headers, requires_unshare
from tests.fake_keyring import FakeKeyring
from tests.test_service import ALICE, build_service
from tests.test_shell import make_shell, run


def test_problem_without_instance() -> None:
    body = DomainError("x").to_problem()
    assert "instance" not in body and body["status"] == 500


def test_audit_tail_edge_cases(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    assert audit.tail(10) == []
    audit.record("a", "acct")
    with (tmp_path / "audit.jsonl").open("a") as log:
        log.write("not json\n")
    audit.record("b", "other")
    assert [e["action"] for e in audit.tail(10)] == ["a", "b"]
    assert [e["action"] for e in audit.tail(10, "acct")] == ["a"]
    assert [e["action"] for e in audit.tail(1)] == ["b"]


def test_store_failure_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EnvironmentStore(tmp_path)
    now = time.time()
    record = EnvironmentRecord(
        id="env_s",
        account_id="a",
        profile="p",
        name="n",
        sandbox_tier="directory",
        created_at=now,
        updated_at=now,
        last_activity_at=now,
    )
    store.create_dirs(record)

    def fail_replace(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        store.save(record)
    monkeypatch.undo()
    assert not list(store.environment_dir(record).glob(".environment-*"))
    store.save(record)
    (store.logs(record) / "x.log").write_bytes(b"1234")
    original_stat = Path.stat

    def flaky_stat(self: Path, **kw: Any) -> os.stat_result:
        if self.name == "x.log":
            raise OSError("gone")
        return original_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert store.prune_logs(record, 1) == 0
    monkeypatch.undo()
    original_lstat = os.lstat

    def flaky_lstat(path: Any, *a: Any, **kw: Any) -> os.stat_result:
        if str(path).endswith("x.log"):
            raise OSError("gone")
        return original_lstat(path, *a, **kw)

    monkeypatch.setattr(os, "lstat", flaky_lstat)
    assert store.disk_usage(record) == (0, 0)


def test_log_writer_cap_across_chunks(tmp_path: Path) -> None:
    shell = make_shell(tmp_path)
    try:
        run(shell, "head -c 100 /dev/zero | tr '\\0' x; sleep 0.05; echo more")
        record = shell.commands[-1]
        assert record.log_truncated and record.log_path.stat().st_size == 64
        shell.mark_dead("test")  # no command running: nothing to finish
        assert shell.state is ShellState.DEAD
    finally:
        os.killpg(shell.pgid, signal.SIGKILL)


def test_write_stdin_blocking_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = make_shell(tmp_path)
    try:
        r, w = os.pipe()
        os.set_blocking(w, False)
        with pytest.raises(BlockingIOError):
            while True:
                os.write(w, b"x" * 65536)
        shell._stdin_fd = w
        monkeypatch.setattr("app.shells.shell.STDIN_WRITE_TIMEOUT", 0.2)
        monkeypatch.setattr(select, "select", lambda *a, **k: ([], [w], []))
        with pytest.raises(SandboxError, match="not accepting input"):
            shell._write_stdin(b"y")
        os.close(r)
        os.close(w)
    finally:
        os.killpg(shell.pgid, signal.SIGKILL)


def test_signal_descendants_tolerates_exited_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = make_shell(tmp_path)
    try:
        gone = subprocess.Popen(["true"])
        gone.wait()
        monkeypatch.setattr("app.shells.shell.procfs.descendants", lambda *a: [gone.pid])
        assert shell.signal(signal.SIGTERM) == 0
    finally:
        shell.close(1)


def test_processes_races(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = make_shell(tmp_path)
    try:
        monkeypatch.setattr("app.processes.procfs.snapshot", lambda: {})
        assert processes.list_processes([shell]) == []
        assert processes.owning_shell(shell.pid, [shell]) is shell
        monkeypatch.undo()
        assert processes.owning_shell(1, [shell]) is None
        monkeypatch.setattr("app.processes.owning_shell", lambda pid, shells: shell)
        with pytest.raises(NotFoundError, match="already exited"):
            processes.signal_process(2**22 - 1, signal.SIGTERM, [shell])
    finally:
        shell.close(1)


def test_service_edges(settings: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    service = build_service(settings, clock)
    try:
        record = service.create(
            ALICE,
            "lim",
            {},
            [],
            None,
            EnvironmentLimits(max_cpu_seconds=10, max_memory_bytes=1 << 30),
        )
        assert service.usage(ALICE, record.id)["limits"]["cpu_seconds"] == 10
        shell = service.open_shell(ALICE, record.id, ".", {}, False)
        command = service.exec(ALICE, shell.id, "echo x", None, [])
        assert shell.wait_command(command, 10)
        service.close_shell(ALICE, shell.id)
        assert shell.state is ShellState.CLOSED
        # A closed shell still registered is skipped by the reaper's shell pass.
        assert service.reap().shells_closed == 0
        # A pruned log leaves the command retrievable, just without output.
        command.log_path.unlink()
        view = service.get_command(ALICE, command.id, 0, 10)
        assert view.output == b"" and not view.output_truncated
        # Archiving drops the dead shell from the registry.
        clock.now += settings.environment_idle_ttl_seconds + 1
        assert service.reap().environments_archived == 1
        assert shell.id not in service._shells
        assert service.get(ALICE, record.id).state is EnvironmentState.ARCHIVED
        # A record deleted while the reaper is mid-pass is skipped, not resurrected.
        other = service.create(ALICE, "gone", {}, [], None, None)
        real_effective = service._quotas.effective

        def delete_then_effective(account_id: str) -> Any:
            if other.id in service._records:
                service.delete(ALICE, other.id)
            return real_effective(account_id)

        monkeypatch.setattr(service._quotas, "effective", delete_then_effective)
        assert other.id not in service.reap().usage
    finally:
        service.shutdown()


def test_reconcile_skips_group_kill_for_non_leader(settings: Settings, clock: Clock) -> None:
    store = EnvironmentStore(settings.root)
    victim = subprocess.Popen(["sleep", "30"])
    stat = procfs.read_stat(victim.pid)
    assert stat is not None
    now = clock()
    record = EnvironmentRecord(
        id="env_nonleader01",
        account_id="alice",
        profile="personal",
        name="n",
        sandbox_tier="directory",
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        shells=[
            ShellRecord(
                id="sh_n",
                pid=victim.pid,
                pgid=os.getpgid(0),
                start_ticks=stat.start_ticks,
                state=ShellState.RUNNING,
                created_at=now,
            )
        ],
    )
    store.create_dirs(record)
    store.save(record)
    try:
        service = build_service(settings, clock)
        assert victim.wait(timeout=10) == -signal.SIGKILL
        service.shutdown()
    finally:
        victim.kill()


@requires_unshare
def test_namespace_rootless_spawn(tmp_path: Path) -> None:
    from tests.test_sandbox import _run

    sandbox = NamespaceSandbox(rootless=True)
    rc, out = _run(sandbox, tmp_path, "id -u; touch ok && echo wrote")
    assert rc == 0 and out.splitlines() == ["0", "wrote"]


async def test_reaper_loop_runs_and_survives_errors(
    settings: Settings, keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings.model_copy(update={"reaper_interval_seconds": 0.01})
    calls: list[int] = []
    seen = asyncio.Event()
    loop = asyncio.get_running_loop()

    def fake_reap(self: EnvironmentService) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("one bad pass")
        # Signal on the pass after the empty one, so the loop has visibly gone round
        # again with nothing to report before the app is shut down.
        if len(calls) >= 4:
            loop.call_soon_threadsafe(seen.set)
        from app.environments.service import ReapReport

        return ReapReport(shells_closed=1) if len(calls) == 2 else ReapReport()

    monkeypatch.setattr(EnvironmentService, "reap", fake_reap)
    app = create_app(settings, keyring_transport=keyring.transport(), capabilities=NO_SANDBOX)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(seen.wait(), 10)
    assert len(calls) >= 4


async def test_keyring_clients_start_without_a_fetch_and_close_with_the_app(
    settings: Settings, keyring: FakeKeyring
) -> None:
    app = create_app(settings, keyring_transport=keyring.transport(), capabilities=NO_SANDBOX)
    async with app.router.lifespan_context(app):
        jwks, credentials = app.state.jwks, app.state.credentials
        assert keyring.fetches == 0 and not jwks._client.is_closed
    assert jwks._client.is_closed and credentials._client._http.is_closed


async def test_schema_validation_over_http(client: httpx.AsyncClient, keyring: FakeKeyring) -> None:
    alice = auth_headers(keyring, "alice")
    for body in (
        {"name": "x", "labels": {"bad key!": "v"}},
        {"name": "x", "credentials": ["Bad Service"]},
    ):
        response = await client.post("/v1/environments", json=body, headers=alice)
        assert response.status_code == 422, body
    response = await client.post("/v1/environments", json={"name": "ok"}, headers=alice)
    env = response.json()
    response = await client.post(
        f"/v1/environments/{env['id']}/shells", json={"env": {"1BAD": "x"}}, headers=alice
    )
    assert response.status_code == 422
    response = await client.post(f"/v1/environments/{env['id']}/shells", json={}, headers=alice)
    sid = response.json()["id"]
    response = await client.get(f"/v1/shells/{sid}/output", headers=alice)
    assert response.json()["data"] == "" and response.json()["shell_state"] == "running"


def test_command_state_names() -> None:
    assert json.loads(json.dumps(CommandState.EXITED)) == "exited"
    assert Shell.__name__ == "Shell"
