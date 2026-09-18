from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator

import pytest

from app import procfs
from app.audit import AuditLog
from app.constants import SHELL_DEAD_RESTART, EnvironmentState, ShellState
from app.environments.models import EnvironmentLimits, EnvironmentRecord, ShellRecord
from app.environments.quotas import Quotas, QuotaStore
from app.environments.service import EnvironmentService, NetworkDisabledError
from app.environments.store import EnvironmentStore, safe_segment
from app.errors import (
    EnvironmentArchivedError,
    NotFoundError,
    QuotaExceededError,
    ShellNotRunningError,
    ValidationError,
)
from app.keyring.auth import Caller
from app.keyring.client import ResolvedCredential
from app.preferences import Preferences
from app.sandbox.directory import DirectorySandbox
from app.settings import Settings
from tests.conftest import Clock

ALICE = Caller("alice", "personal", "tok-a")
ALICE_WORK = Caller("alice", "work", "tok-a")
BOB = Caller("bob", "personal", "tok-b")


def build_service(settings: Settings, clock: Clock) -> EnvironmentService:
    store = EnvironmentStore(settings.root)
    quotas = QuotaStore(settings.root, Quotas.from_settings(settings))
    audit = AuditLog(settings.root / "audit.jsonl")
    service = EnvironmentService(settings, store, quotas, DirectorySandbox(), audit, clock=clock)
    service.startup()
    return service


@pytest.fixture
def service(settings: Settings, clock: Clock) -> Iterator[EnvironmentService]:
    svc = build_service(settings, clock)
    yield svc
    svc.shutdown()


def test_create_layout_and_views(service: EnvironmentService, settings: Settings) -> None:
    record = service.create(ALICE, "first", {"team": "a"}, ["github"], None, None)
    env_dir = settings.root / "accounts" / "alice" / "personal" / record.id
    assert (env_dir / "environment.json").exists()
    assert (env_dir / "workspace").is_dir() and (env_dir / "logs").is_dir()
    assert oct((env_dir / "environment.json").stat().st_mode & 0o777) == "0o600"
    view = service.environment_view(record)
    assert view["sandbox_tier"] == "directory" and view["shells_running"] == 0
    assert "workspace" not in view
    assert service.get(ALICE, record.id) is record
    assert [r.id for r in service.list_environments(ALICE)] == [record.id]
    assert service.list_environments(ALICE, label="team=a") and not service.list_environments(
        ALICE, label="team=b"
    )
    assert service.list_environments(ALICE, label="team") and not service.list_environments(
        ALICE, label="other"
    )
    assert service.list_environments(ALICE, profile="work") == []
    assert service.list_environments(ALICE, state=EnvironmentState.ARCHIVED) == []
    assert service.list_all() == [record]
    assert service.sandbox_tier == "directory"


def test_isolation_between_accounts(service: EnvironmentService) -> None:
    record = service.create(ALICE, "mine", {}, [], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    assert service.list_environments(BOB) == []
    for call in (
        lambda: service.get(BOB, record.id),
        lambda: service.delete(BOB, record.id),
        lambda: service.reset(BOB, record.id),
        lambda: service.usage(BOB, record.id),
        lambda: service.summary(BOB, record.id),
        lambda: service.open_shell(BOB, record.id, ".", {}, False),
        lambda: service.list_shells(BOB, record.id),
        lambda: service.get_shell(BOB, shell.id),
        lambda: service.close_shell(BOB, shell.id),
        lambda: service.exec(BOB, shell.id, "true", None, []),
        lambda: service.signal_shell(BOB, shell.id, signal.SIGTERM),
        lambda: service.write_stdin(BOB, shell.id, b"x", "stdin"),
        lambda: service.list_processes(BOB, record.id),
        lambda: service.list_shell_processes(BOB, shell.id),
        lambda: service.signal_process(BOB, record.id, shell.pid, signal.SIGTERM),
        lambda: service.workspace_for(BOB, record.id),
        lambda: service.note_write(BOB, record.id, "x", 1),
    ):
        with pytest.raises(NotFoundError):
            call()
    with pytest.raises(NotFoundError):
        service.get_shell(ALICE, "sh_nope")
    assert service.get(ALICE, record.id) is record
    assert procfs.is_same_process(shell.pid, shell.start_ticks)


def test_quotas_named_in_errors(service: EnvironmentService) -> None:
    service.create(ALICE, "one", {}, [], None, None)
    service.create(ALICE, "two", {}, [], None, None)
    with pytest.raises(QuotaExceededError) as info:
        service.create(ALICE, "three", {}, [], None, None)
    assert info.value.extra == {"limit": "max_environments_per_profile", "current": 2, "maximum": 2}
    service.create(ALICE_WORK, "three", {}, [], None, None)
    with pytest.raises(QuotaExceededError) as info:
        service.create(Caller("alice", "invented", "tok-a"), "four", {}, [], None, None)
    assert info.value.extra["limit"] == "max_environments_per_account"
    record = service.create(BOB, "b", {}, [], None, None)
    service.open_shell(BOB, record.id, ".", {}, False)
    service.open_shell(BOB, record.id, ".", {}, False)
    with pytest.raises(QuotaExceededError) as info:
        service.open_shell(BOB, record.id, ".", {}, False)
    assert info.value.extra["limit"] == "max_shells_per_environment"
    with pytest.raises(QuotaExceededError) as info:
        service.create(BOB, "c", {}, [], None, EnvironmentLimits(max_memory_bytes=10**15))
    assert info.value.extra["limit"] == "max_memory_bytes"


def test_disk_quota_refuses_new_work(service: EnvironmentService, settings: Settings) -> None:
    record = service.create(ALICE, "disk", {}, [], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    service._quotas.set_overrides("alice", {"max_disk_bytes": 1024})
    workspace, _ = service.workspace_for(ALICE, record.id)
    (workspace / "big").write_bytes(b"x" * 8192)
    report = service.reap()
    assert report.usage[record.id] >= 8192
    for call in (
        lambda: service.open_shell(ALICE, record.id, ".", {}, False),
        lambda: service.exec(ALICE, shell.id, "true", None, []),
        lambda: service.note_write(ALICE, record.id, "f", 1),
    ):
        with pytest.raises(QuotaExceededError) as info:
            call()
        assert info.value.extra["limit"] == "max_disk_bytes"
    usage = service.usage(ALICE, record.id)
    assert usage["disk"]["max_bytes"] == 1024 and usage["disk"]["workspace_bytes"] >= 8192
    assert usage["shells"]["running"] == 1
    service.reset(ALICE, record.id)
    assert service.usage(ALICE, record.id)["disk"]["workspace_bytes"] == 0


def test_network_switch(settings: Settings, clock: Clock) -> None:
    settings = settings.model_copy(update={"allow_network": False})
    service = build_service(settings, clock)
    try:
        record = service.create(ALICE, "offline", {}, [], None, None)
        assert record.network is False
        with pytest.raises(NetworkDisabledError):
            service.create(ALICE, "online", {}, [], True, None)
    finally:
        service.shutdown()


def test_shell_open_validation(service: EnvironmentService) -> None:
    record = service.create(ALICE, "v", {}, [], None, None)
    with pytest.raises(ValidationError):
        service.open_shell(ALICE, record.id, ".", {"bad-name": "x"}, False)
    with pytest.raises(ValidationError):
        service.open_shell(ALICE, record.id, "missing-dir", {}, False)
    workspace, _ = service.workspace_for(ALICE, record.id)
    (workspace / "sub").mkdir()
    shell = service.open_shell(ALICE, record.id, "sub", {"FOO": "bar"}, False)
    command = service.exec(
        ALICE, shell.id, "basename $PWD; echo $FOO; echo $ENVAPI_ENVIRONMENT_ID", None, []
    )
    assert shell.wait_command(command, 10)
    out = shell.read_output(command.output_start, 1000).data
    assert out == f"sub\nbar\n{record.id}\n".encode()
    assert shell.to_dict()["cwd"] == "sub"
    assert service.list_shells(ALICE, record.id) == [shell]


def test_exec_credentials_and_command_retrieval(service: EnvironmentService) -> None:
    record = service.create(ALICE, "c", {}, ["github"], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    creds = [ResolvedCredential("github", {"GITHUB_TOKEN": "ghp_secretvalue"})]
    command = service.exec(ALICE, shell.id, "echo $GITHUB_TOKEN", None, creds)
    assert shell.wait_command(command, 10)
    view = service.get_command(ALICE, command.id, 0, 1000)
    assert view.output == "«redacted:github»\n".encode() and not view.output_truncated
    small = service.get_command(ALICE, command.id, 2, 3)
    assert small.output_truncated and small.output_offset == 2
    with pytest.raises(NotFoundError):
        service.get_command(BOB, command.id, 0, 10)
    with pytest.raises(NotFoundError):
        service.get_command(ALICE, "cmd_missing", 0, 10)
    # Persisted alongside the log once finished, so it survives the shell.
    deadline = time.monotonic() + 5
    while command.id not in service._persisted_commands and time.monotonic() < deadline:
        pass
    service.close_shell(ALICE, shell.id)
    service._shells.clear()
    persisted = service.get_command(ALICE, command.id, 0, 1000)
    assert persisted.command["exit_code"] == 0 and persisted.output == view.output
    with pytest.raises(NotFoundError):
        service.exec(ALICE, shell.id, "true", None, [])
    with pytest.raises(ShellNotRunningError):
        shell.exec("true")  # closed, nothing ever runs in it again


def test_signal_stdin_and_processes(service: EnvironmentService) -> None:
    record = service.create(ALICE, "p", {}, [], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    command = service.exec(
        ALICE, shell.id, "sleep 30 & echo started; read x; echo got=$x; wait", None, []
    )
    assert shell.wait_output(command.output_start, 10)
    deadline = time.monotonic() + 10
    procs = service.list_processes(ALICE, record.id)
    while len(procs) < 2 and time.monotonic() < deadline:
        procs = service.list_processes(ALICE, record.id)
    pids = {p.pid: p for p in procs}
    assert shell.pid in pids and pids[shell.pid].shell_id == shell.id
    sleeper = next(p for p in procs if "sleep" in p.cmdline)
    assert sleeper.ppid == shell.pid and sleeper.rss_bytes >= 0
    assert service.list_shell_processes(ALICE, shell.id)
    assert service.signal_process(ALICE, record.id, sleeper.pid, signal.SIGKILL) == shell.id
    assert service.write_stdin(ALICE, shell.id, b"hi\n", "stdin") == 3
    assert shell.wait_command(command, 10)
    assert b"got=hi\n" in shell.read_output(command.output_start, 1000).data
    with pytest.raises(NotFoundError):
        service.signal_process(ALICE, record.id, os.getpid(), signal.SIGTERM)
    with pytest.raises(NotFoundError):
        service.signal_process(ALICE, record.id, 2**22 - 1, signal.SIGTERM)
    other = service.create(BOB, "other", {}, [], None, None)
    other_shell = service.open_shell(BOB, other.id, ".", {}, False)
    with pytest.raises(NotFoundError):
        service.signal_process(ALICE, record.id, other_shell.pid, signal.SIGTERM)
    assert service.signal_shell(ALICE, shell.id, signal.SIGTERM) == 0
    summary = service.summary(ALICE, record.id)
    assert summary["shells"][0]["id"] == shell.id
    assert summary["recent_commands"][0]["id"] == command.id
    assert any(p["pid"] == shell.pid for p in summary["processes"])


def test_delete_kills_everything(service: EnvironmentService, settings: Settings) -> None:
    record = service.create(ALICE, "d", {}, [], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    command = service.exec(ALICE, shell.id, "sleep 100 & sleep 100 & echo up; wait", None, [])
    assert shell.wait_output(command.output_start, 10)
    stat = procfs.read_stat(shell.pid)
    assert stat is not None
    service.delete(ALICE, record.id)
    assert not (settings.root / "accounts" / "alice" / "personal" / record.id).exists()
    assert not procfs.is_same_process(shell.pid, stat.start_ticks)
    assert shell.state is ShellState.CLOSED
    with pytest.raises(NotFoundError):
        service.get(ALICE, record.id)
    with pytest.raises(NotFoundError):
        service.get_shell(ALICE, shell.id)


def test_reset_closes_shells_and_revives_archived(
    service: EnvironmentService, clock: Clock
) -> None:
    record = service.create(ALICE, "r", {}, [], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    workspace, _ = service.workspace_for(ALICE, record.id)
    (workspace / "keep.txt").write_text("x")
    service.reset(ALICE, record.id)
    assert shell.state is ShellState.CLOSED
    assert not (workspace / "keep.txt").exists()
    assert [s.state for s in record.shells] == [ShellState.CLOSED]
    (workspace / "again.txt").write_text("x")
    clock.now += 10**6
    report = service.reap()
    assert report.environments_archived == 1 and record.state is EnvironmentState.ARCHIVED
    assert not (workspace / "again.txt").exists()
    for call in (
        lambda: service.open_shell(ALICE, record.id, ".", {}, False),
        lambda: service.workspace_for(ALICE, record.id),
    ):
        with pytest.raises(EnvironmentArchivedError):
            call()
    assert service.list_environments(ALICE, state=EnvironmentState.ARCHIVED) == [record]
    service.reset(ALICE, record.id)
    assert service.get(ALICE, record.id).state is EnvironmentState.ACTIVE
    assert record.archived_at is None
    assert service.open_shell(ALICE, record.id, ".", {}, False).state is ShellState.RUNNING


def test_reaper_closes_idle_shells_and_prunes_logs(
    service: EnvironmentService, clock: Clock, settings: Settings
) -> None:
    record = service.create(ALICE, "idle", {}, [], None, None)
    idle = service.open_shell(ALICE, record.id, ".", {}, False)
    busy = service.open_shell(ALICE, record.id, ".", {}, False)
    command = service.exec(ALICE, busy.id, "sleep 30", None, [])
    for _ in range(3):
        c = service.exec(ALICE, idle.id, "head -c 4000 /dev/zero", None, [])
        assert idle.wait_command(c, 10)
    clock.now += settings.shell_idle_ttl_seconds + 1
    record.last_activity_at = clock.now
    report = service.reap()
    assert report.shells_closed == 1 and idle.state is ShellState.CLOSED
    assert busy.state is ShellState.RUNNING and report.environments_archived == 0
    assert report.logs_pruned >= 1
    assert len(list(service._store.logs(record).glob("*.log"))) < 3
    assert idle.commands[0].id not in {p.stem for p in service._store.logs(record).glob("*.log")}
    service.signal_shell(ALICE, busy.id, signal.SIGKILL)
    assert busy.wait_command(command, 10)
    # A record deleted mid-pass is skipped rather than resurrected.
    service.delete(ALICE, record.id)
    assert service.reap().usage == {}


def test_reaper_uses_idle_ttls_stamped_at_create(settings: Settings, clock: Clock) -> None:
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": "settings-api-token-for-environments-01",
            "environment_idle_ttl_seconds": 86_400,
        }
    )
    service = build_service(settings, clock)
    try:
        prefs = Preferences(
            environment_idle_ttl_seconds=60,
            shell_idle_ttl_seconds=15,
            max_environments_per_profile=5,
            default_profile="personal",
        )
        record = service.create(ALICE, "short", {}, [], None, None, prefs)
        clock.now += 61
        report = service.reap()
        assert report.environments_archived == 1
        assert service.get(ALICE, record.id).state is EnvironmentState.ARCHIVED
    finally:
        service.shutdown()


def test_create_without_settings_api_does_not_stamp(service: EnvironmentService) -> None:
    prefs = Preferences(
        environment_idle_ttl_seconds=60,
        shell_idle_ttl_seconds=15,
        max_environments_per_profile=1,
        default_profile="personal",
    )
    first = service.create(ALICE, "one", {}, [], None, None, prefs)
    second = service.create(ALICE, "two", {}, [], None, None, prefs)
    assert first.environment_idle_ttl_seconds is None
    assert first.shell_idle_ttl_seconds is None
    assert second.id != first.id


def test_create_with_settings_api_stamps_ttls_and_honours_the_lower_cap(
    settings: Settings, clock: Clock
) -> None:
    settings = settings.model_copy(
        update={
            "settings_api_base_url": "https://settings.test",
            "settings_api_token": "settings-api-token-for-environments-01",
            "max_environments_per_profile": 2,
        }
    )
    service = build_service(settings, clock)
    try:
        prefs = Preferences(
            environment_idle_ttl_seconds=60,
            shell_idle_ttl_seconds=15,
            max_environments_per_profile=1,
            default_profile="personal",
        )
        record = service.create(ALICE, "one", {}, [], None, None, prefs)
        assert record.environment_idle_ttl_seconds == 60
        assert record.shell_idle_ttl_seconds == 15
        loaded = EnvironmentRecord.model_validate_json(
            (
                settings.root / "accounts" / "alice" / "personal" / record.id / "environment.json"
            ).read_text(encoding="utf-8")
        )
        assert loaded.environment_idle_ttl_seconds == 60
        assert loaded.shell_idle_ttl_seconds == 15
        with pytest.raises(QuotaExceededError) as caught:
            service.create(ALICE, "two", {}, [], None, None, prefs)
        assert caught.value.extra["limit"] == "max_environments_per_profile"
        assert caught.value.extra["maximum"] == 1
        later = Preferences(
            environment_idle_ttl_seconds=120,
            shell_idle_ttl_seconds=30,
            max_environments_per_profile=5,
            default_profile="work",
        )
        other = service.create(ALICE_WORK, "work", {}, [], None, None, later)
        assert record.environment_idle_ttl_seconds == 60
        assert other.environment_idle_ttl_seconds == 120
    finally:
        service.shutdown()


def test_restart_reconciles_orphans(settings: Settings, clock: Clock) -> None:
    service = build_service(settings, clock)
    record = service.create(ALICE, "restart", {}, [], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    command = service.exec(ALICE, shell.id, "sleep 100 & echo up; sleep 100", None, [])
    assert shell.wait_output(command.output_start, 10)
    deadline = time.monotonic() + 10
    children = procfs.descendants(shell.pid, procfs.snapshot())
    while len(children) < 2 and time.monotonic() < deadline:
        children = procfs.descendants(shell.pid, procfs.snapshot())
    identities = {pid: procfs.read_stat(pid) for pid in [shell.pid, *children]}
    # Simulate the process dying without cleanup: forget the shells, drop the reader.
    shell._on_change = None
    service._shells.clear()
    del service

    reborn = build_service(settings, clock)
    try:
        loaded = reborn.get(ALICE, record.id)
        assert loaded.state is EnvironmentState.ACTIVE
        stored = loaded.shells[0]
        assert stored.state is ShellState.DEAD and stored.dead_reason == SHELL_DEAD_RESTART
        for pid, stat in identities.items():
            assert stat is not None
            wait_until = time.monotonic() + 5
            while procfs.is_same_process(pid, stat.start_ticks) and time.monotonic() < wait_until:
                pass
            assert not procfs.is_same_process(pid, stat.start_ticks), f"{pid} survived restart"
        assert reborn.list_shells(ALICE, record.id) == []
        view = reborn.environment_view(loaded)
        assert view["shells"][0]["dead_reason"] == "service_restarted"
    finally:
        reborn.shutdown()
    shell.close(1)


def test_restart_leaves_recycled_pid_alone(settings: Settings, clock: Clock) -> None:
    import subprocess

    store = EnvironmentStore(settings.root)
    victim = subprocess.Popen(["sleep", "30"])
    stat = procfs.read_stat(victim.pid)
    assert stat is not None
    now = clock()
    record = EnvironmentRecord(
        id="env_recycled0001",
        account_id="alice",
        profile="personal",
        name="ghost",
        sandbox_tier="directory",
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        shells=[
            ShellRecord(
                id="sh_ghost",
                pid=victim.pid,
                pgid=victim.pid,
                start_ticks=stat.start_ticks + 1,  # same pid, a different process
                state=ShellState.RUNNING,
                created_at=now,
            ),
            ShellRecord(
                id="sh_gone",
                pid=2**22 - 1,
                pgid=2**22 - 1,
                start_ticks=1,
                state=ShellState.RUNNING,
                created_at=now,
            ),
            ShellRecord(
                id="sh_closed",
                pid=1,
                pgid=1,
                start_ticks=1,
                state=ShellState.CLOSED,
                created_at=now,
            ),
        ],
    )
    store.create_dirs(record)
    store.save(record)
    try:
        service = build_service(settings, clock)
        loaded = service.get(ALICE, record.id)
        assert all(s.state is not ShellState.RUNNING for s in loaded.shells)
        assert loaded.shells[0].dead_reason == SHELL_DEAD_RESTART
        assert loaded.shells[2].state is ShellState.CLOSED and loaded.shells[2].dead_reason is None
        assert victim.poll() is None, "a recycled pid must never be killed"
        service.shutdown()
    finally:
        victim.kill()
        victim.wait()


def test_orphan_kill_handles_vanished_group(settings: Settings, clock: Clock) -> None:
    import subprocess

    service = build_service(settings, clock)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    stat = procfs.read_stat(proc.pid)
    assert stat is not None
    ghost = ShellRecord(
        id="sh_g",
        pid=proc.pid,
        pgid=proc.pid,
        start_ticks=stat.start_ticks,
        state=ShellState.RUNNING,
        created_at=0,
    )
    proc.kill()
    proc.wait()
    service._kill_orphan(ghost)  # both the group and the pid are already gone
    service.shutdown()


def test_store_details(settings: Settings) -> None:
    assert safe_segment("plain-id_1.2") == "plain-id_1.2"
    assert safe_segment("weird/id").startswith("h_") and safe_segment("..").startswith("h_")
    store = EnvironmentStore(settings.root)
    bad = settings.root / "accounts" / "a" / "p" / "env_bad"
    bad.mkdir(parents=True)
    (bad / "environment.json").write_text("{not json")
    assert store.load_all() == []
    now = time.time()
    record = EnvironmentRecord(
        id="env_x",
        account_id="a",
        profile="p",
        name="n",
        sandbox_tier="directory",
        created_at=now,
        updated_at=now,
        last_activity_at=now,
    )
    store.create_dirs(record)
    assert store.read_command_json(record, "cmd_none") is None
    (store.logs(record) / "cmd_list.json").write_text("[1]")
    assert store.read_command_json(record, "cmd_list") is None
    (store.logs(record) / "a.log").write_bytes(b"x" * 100)
    (store.logs(record) / "a.json").write_text("{}")
    assert store.prune_logs(record, 10) == 1 and not (store.logs(record) / "a.json").exists()
    assert store.prune_logs(record, 10) == 0
    ws, logs = store.disk_usage(record)
    assert ws == 0 and logs > 0


def test_quota_store_validation(settings: Settings) -> None:
    quotas = QuotaStore(settings.root, Quotas.from_settings(settings))
    assert quotas.effective("nobody") == quotas.defaults
    assert (
        quotas.set_overrides("x", {"max_shells_per_environment": 9}).max_shells_per_environment == 9
    )
    assert quotas.overrides("x") == {"max_shells_per_environment": 9}
    assert quotas.set_overrides("x", {}) == quotas.defaults and quotas.overrides("x") == {}
    for bad in (
        {"nope": 1},
        {"max_shells_per_environment": 0},
        {"max_shells_per_environment": True},
        {"max_cpu_seconds": "5"},
    ):
        with pytest.raises(ValidationError):
            quotas.set_overrides("x", bad)
    (settings.root / "quotas" / "y.json").write_text("[1,2]")
    assert quotas.overrides("y") == {}
    (settings.root / "quotas" / "z.json").write_text("not json")
    assert quotas.overrides("z") == {}


def test_on_shell_change_ignores_unknown(service: EnvironmentService) -> None:
    record = service.create(ALICE, "cb", {}, [], None, None)
    shell = service.open_shell(ALICE, record.id, ".", {}, False)
    record.shells.clear()
    service._on_shell_change(shell)  # shell record missing: nothing to persist
    shell.environment_id = "env_missing"
    service._on_shell_change(shell)  # environment missing: nothing to persist
    shell.environment_id = record.id
