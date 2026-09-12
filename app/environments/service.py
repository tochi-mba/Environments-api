"""The orchestrator: environments, their shells, quotas, and the reaper's work.

Every public method takes the :class:`Caller` and refuses, with a 404, anything the
caller's account does not own. That single check is the isolation between accounts.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app import processes, procfs
from app.audit import AuditLog
from app.constants import (
    ENV_VAR_PATTERN,
    SHELL_DEAD_RESTART,
    CommandState,
    EnvironmentState,
    ShellState,
)
from app.environments.models import EnvironmentLimits, EnvironmentRecord, ShellRecord
from app.environments.quotas import Quotas, QuotaStore
from app.environments.store import EnvironmentStore
from app.errors import (
    ConflictError,
    EnvironmentArchivedError,
    NotFoundError,
    QuotaExceededError,
    ValidationError,
)
from app.keyring.auth import Caller
from app.keyring.client import ResolvedCredential
from app.paths import relative_to_workspace, resolve_within
from app.sandbox import ResourceLimits, Sandbox, SpawnRequest
from app.settings import Settings
from app.shells.shell import CommandRecord, Shell, ShellSpec, new_id

log = structlog.get_logger(__name__)

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class NetworkDisabledError(ConflictError):
    """The deployment disables network egress; an environment cannot ask for it."""

    code = "network_disabled"
    title = "Network egress is disabled"


@dataclass(slots=True)
class ReapReport:
    """What one reaper pass did."""

    shells_closed: int = 0
    environments_archived: int = 0
    logs_pruned: int = 0
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandView:
    """A command plus as much of its logged output as was asked for."""

    command: dict[str, Any]
    output: bytes
    output_offset: int
    output_truncated: bool


class EnvironmentService:
    """Owns the records, the live shells and the locks around them."""

    def __init__(
        self,
        settings: Settings,
        store: EnvironmentStore,
        quotas: QuotaStore,
        sandbox: Sandbox,
        audit: AuditLog,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the collaborators; call :meth:`startup` before serving."""
        self._settings = settings
        self._store = store
        self._quotas = quotas
        self._sandbox = sandbox
        self._audit = audit
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, EnvironmentRecord] = {}
        self._shells: dict[str, Shell] = {}
        self._usage: dict[str, int] = {}
        self._persisted_commands: set[str] = set()

    # ----- lifecycle -----------------------------------------------------------------

    @property
    def sandbox_tier(self) -> str:
        """The active sandbox tier's name."""
        return self._sandbox.tier.label

    def startup(self) -> None:
        """Load every record and reconcile shells that outlived the previous process."""
        with self._lock:
            for record in self._store.load_all():
                self._records[record.id] = record
                self._reconcile(record)
        log.info("environments_loaded", count=len(self._records))

    def _reconcile(self, record: EnvironmentRecord) -> None:
        changed = False
        for shell in record.shells:
            if shell.state is not ShellState.RUNNING:
                continue
            # The pid alone proves nothing after a restart; only a matching start time
            # says this is the process we spawned and not a stranger who got its number.
            if procfs.is_same_process(shell.pid, shell.start_ticks):
                self._kill_orphan(shell)
                log.warning("orphan_shell_killed", environment_id=record.id, shell_id=shell.id)
            else:
                log.info("orphan_shell_gone", environment_id=record.id, shell_id=shell.id)
            shell.state = ShellState.DEAD
            shell.dead_reason = SHELL_DEAD_RESTART
            changed = True
        if changed:
            self._store.save(record)

    def _kill_orphan(self, shell: ShellRecord) -> None:
        victims = set(procfs.descendants(shell.pid, procfs.snapshot()))
        if shell.pgid == shell.pid:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(shell.pgid, signal.SIGKILL)
        victims.add(shell.pid)
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue

    def shutdown(self) -> None:
        """Close every live shell; records are already on disk."""
        with self._lock:
            shells = list(self._shells.values())
        for shell in shells:
            shell.close(self._settings.shell_close_grace_seconds)

    # ----- helpers -------------------------------------------------------------------

    def _owned(self, caller: Caller, environment_id: str) -> EnvironmentRecord:
        record = self._records.get(environment_id)
        if record is None or record.account_id != caller.account_id:
            raise NotFoundError(
                f"environment {environment_id} not found", environment_id=environment_id
            )
        return record

    def _owned_shell(self, caller: Caller, shell_id: str) -> tuple[EnvironmentRecord, Shell]:
        shell = self._shells.get(shell_id)
        if shell is None:
            raise NotFoundError(f"shell {shell_id} not found", shell_id=shell_id)
        record = self._owned_or_none(caller, shell.environment_id)
        if record is None:
            raise NotFoundError(f"shell {shell_id} not found", shell_id=shell_id)
        return record, shell

    def _owned_or_none(self, caller: Caller, environment_id: str) -> EnvironmentRecord | None:
        record = self._records.get(environment_id)
        if record is None or record.account_id != caller.account_id:
            return None
        return record

    def _live_shells(self, environment_id: str) -> list[Shell]:
        return [
            s
            for s in self._shells.values()
            if s.environment_id == environment_id and s.state is ShellState.RUNNING
        ]

    def _all_shells(self, environment_id: str) -> list[Shell]:
        return [s for s in self._shells.values() if s.environment_id == environment_id]

    def _touch(self, record: EnvironmentRecord) -> None:
        now = self._clock()
        record.last_activity_at = now
        record.updated_at = now

    def _check_disk(self, record: EnvironmentRecord, quotas: Quotas) -> None:
        used = self._usage.get(record.id, 0)
        if used > quotas.max_disk_bytes:
            raise QuotaExceededError("max_disk_bytes", used, quotas.max_disk_bytes)

    def _effective_limits(self, record: EnvironmentRecord, quotas: Quotas) -> ResourceLimits:
        limits = record.limits
        return ResourceLimits(
            nproc=limits.max_processes_per_shell or quotas.max_processes_per_shell,
            memory_bytes=limits.max_memory_bytes or quotas.max_memory_bytes,
            file_size_bytes=limits.max_file_size_bytes or quotas.max_file_size_bytes,
            cpu_seconds=limits.max_cpu_seconds or quotas.max_cpu_seconds,
        )

    def _on_shell_change(self, shell: Shell) -> None:
        with self._lock:
            record = self._records.get(shell.environment_id)
            if record is None:
                return
            stored = next((s for s in record.shells if s.id == shell.id), None)
            if stored is None:
                return
            if (stored.state, stored.dead_reason) != (shell.state, shell.dead_reason):
                stored.state = shell.state
                stored.dead_reason = shell.dead_reason
                record.updated_at = self._clock()
                self._store.save(record)
            for command in shell.commands:
                if (
                    command.state is not CommandState.RUNNING
                    and command.id not in self._persisted_commands
                ):
                    self._store.write_command_json(record, command.id, command.to_dict())
                    self._persisted_commands.add(command.id)

    # ----- environments --------------------------------------------------------------

    def create(
        self,
        caller: Caller,
        name: str,
        labels: dict[str, str],
        credentials: list[str],
        network: bool | None,
        limits: EnvironmentLimits | None,
    ) -> EnvironmentRecord:
        """Create an environment under the caller's account and profile."""
        quotas = self._quotas.effective(caller.account_id)
        wants_network = self._settings.allow_network if network is None else network
        if wants_network and not self._settings.allow_network:
            raise NetworkDisabledError("ENVAPI_ALLOW_NETWORK is false on this deployment")
        limits = limits or EnvironmentLimits()
        for field_name, requested in limits.model_dump(exclude_none=True).items():
            maximum = int(getattr(quotas, field_name))
            if requested > maximum:
                raise QuotaExceededError(field_name, requested, maximum)
        with self._lock:
            mine = [r for r in self._records.values() if r.account_id == caller.account_id]
            in_profile = [r for r in mine if r.profile == caller.profile]
            if len(in_profile) >= quotas.max_environments_per_profile:
                raise QuotaExceededError(
                    "max_environments_per_profile",
                    len(in_profile),
                    quotas.max_environments_per_profile,
                )
            if len(mine) >= quotas.max_environments_per_account:
                raise QuotaExceededError(
                    "max_environments_per_account", len(mine), quotas.max_environments_per_account
                )
            now = self._clock()
            record = EnvironmentRecord(
                id=new_id("env"),
                account_id=caller.account_id,
                profile=caller.profile,
                name=name,
                labels=labels,
                credentials=credentials,
                network=wants_network,
                limits=limits,
                sandbox_tier=self._sandbox.tier.label,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
            self._store.create_dirs(record)
            self._sandbox.prepare(
                record.id, self._store.environment_dir(record), self._store.workspace(record)
            )
            self._store.save(record)
            self._records[record.id] = record
        self._audit.record(
            "environment.create",
            caller.account_id,
            environment_id=record.id,
            profile=caller.profile,
            name=name,
        )
        return record

    def list_environments(
        self,
        caller: Caller,
        profile: str | None = None,
        state: EnvironmentState | None = None,
        label: str | None = None,
    ) -> list[EnvironmentRecord]:
        """The caller's environments, optionally filtered. ``label`` is ``key`` or ``key=value``."""
        key, _, value = (label or "").partition("=")
        with self._lock:
            result = []
            for record in self._records.values():
                if record.account_id != caller.account_id:
                    continue
                if profile is not None and record.profile != profile:
                    continue
                if state is not None and record.state is not state:
                    continue
                if label and (key not in record.labels or (value and record.labels[key] != value)):
                    continue
                result.append(record)
        return sorted(result, key=lambda r: r.created_at)

    def list_all(self) -> list[EnvironmentRecord]:
        """Every environment, for operators."""
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.created_at)

    def get(self, caller: Caller, environment_id: str) -> EnvironmentRecord:
        """One environment."""
        with self._lock:
            return self._owned(caller, environment_id)

    def environment_view(self, record: EnvironmentRecord) -> dict[str, Any]:
        """The API representation, with live shell counts."""
        with self._lock:
            live = self._live_shells(record.id)
            data = record.model_dump(mode="json")
        data["shells"] = [s.model_dump(mode="json") for s in record.shells]
        data["shells_running"] = len(live)
        data["workspace"] = str(self._store.workspace(record))
        data["disk_bytes"] = self._usage.get(record.id)
        return data

    def delete(self, caller: Caller, environment_id: str) -> None:
        """Kill everything in the environment and remove its folder."""
        with self._lock:
            record = self._owned(caller, environment_id)
            shells = self._all_shells(environment_id)
            del self._records[environment_id]
        for shell in shells:
            shell.close(self._settings.shell_close_grace_seconds)
        with self._lock:
            for shell in shells:
                self._shells.pop(shell.id, None)
            self._sandbox.teardown(record.id)
            self._store.delete(record)
            self._usage.pop(record.id, None)
        self._audit.record("environment.delete", caller.account_id, environment_id=record.id)

    def reset(self, caller: Caller, environment_id: str) -> EnvironmentRecord:
        """Close every shell and wipe the workspace; an archived environment becomes active."""
        with self._lock:
            record = self._owned(caller, environment_id)
            shells = self._all_shells(environment_id)
        for shell in shells:
            shell.close(self._settings.shell_close_grace_seconds)
        with self._lock:
            for shell in shells:
                self._shells.pop(shell.id, None)
            self._store.wipe_workspace(record)
            self._store.logs(record).mkdir(exist_ok=True)
            self._sandbox.prepare(
                record.id, self._store.environment_dir(record), self._store.workspace(record)
            )
            record.state = EnvironmentState.ACTIVE
            record.archived_at = None
            record.shells = [s for s in record.shells if s.state is not ShellState.RUNNING]
            self._touch(record)
            self._store.save(record)
            self._usage[record.id] = 0
        self._audit.record("environment.reset", caller.account_id, environment_id=record.id)
        return record

    def usage(self, caller: Caller, environment_id: str) -> dict[str, Any]:
        """Disk, shells and processes against the effective limits (a fresh disk scan)."""
        with self._lock:
            record = self._owned(caller, environment_id)
            quotas = self._quotas.effective(caller.account_id)
            live = self._live_shells(environment_id)
            mine = [r for r in self._records.values() if r.account_id == caller.account_id]
            in_profile = [r for r in mine if r.profile == record.profile]
        workspace_bytes, logs_bytes = self._store.disk_usage(record)
        with self._lock:
            self._usage[record.id] = workspace_bytes + logs_bytes
        return {
            "environment_id": record.id,
            "disk": {
                "workspace_bytes": workspace_bytes,
                "logs_bytes": logs_bytes,
                "total_bytes": workspace_bytes + logs_bytes,
                "max_bytes": quotas.max_disk_bytes,
            },
            "shells": {"running": len(live), "max": quotas.max_shells_per_environment},
            "processes": {"count": len(processes.list_processes(live))},
            "environments": {
                "in_profile": len(in_profile),
                "max_per_profile": quotas.max_environments_per_profile,
                "in_account": len(mine),
                "max_per_account": quotas.max_environments_per_account,
            },
            "limits": dataclasses.asdict(self._effective_limits(record, quotas)),
        }

    def summary(self, caller: Caller, environment_id: str) -> dict[str, Any]:
        """Environment, shells, processes and recent commands in one call."""
        with self._lock:
            record = self._owned(caller, environment_id)
            shells = self._all_shells(environment_id)
        commands = sorted(
            (c for s in shells for c in s.commands), key=lambda c: c.started_at, reverse=True
        )[:20]
        return {
            "environment": self.environment_view(record),
            "shells": [s.to_dict() for s in shells],
            "processes": [p.to_dict() for p in processes.list_processes(shells)],
            "recent_commands": [c.to_dict() for c in commands],
        }

    def workspace_for(
        self, caller: Caller, environment_id: str
    ) -> tuple[Path, tuple[int, int] | None]:
        """The workspace path and the owner files should be given, after the ownership check."""
        with self._lock:
            record = self._owned(caller, environment_id)
            if record.state is EnvironmentState.ARCHIVED:
                raise EnvironmentArchivedError(
                    f"environment {record.id} is archived; reset it first"
                )
            return self._store.workspace(record), self._sandbox.owner(record.id)

    def note_write(self, caller: Caller, environment_id: str, path: str, size: int) -> None:
        """Record a file write: activity for the reaper, an entry for the audit log."""
        with self._lock:
            record = self._owned(caller, environment_id)
            quotas = self._quotas.effective(caller.account_id)
            self._check_disk(record, quotas)
            self._usage[record.id] = self._usage.get(record.id, 0) + size
            self._touch(record)
        self._audit.record(
            "file.write", caller.account_id, environment_id=environment_id, path=path, bytes=size
        )

    # ----- shells --------------------------------------------------------------------

    def open_shell(
        self, caller: Caller, environment_id: str, cwd: str, env: dict[str, str], pty: bool
    ) -> Shell:
        """Start a shell in the environment."""
        for key in env:
            if not ENV_VAR_PATTERN.match(key):
                raise ValidationError(f"invalid environment variable name {key!r}", name=key)
        with self._lock:
            record = self._owned(caller, environment_id)
            if record.state is EnvironmentState.ARCHIVED:
                raise EnvironmentArchivedError(
                    f"environment {record.id} is archived; reset it first"
                )
            quotas = self._quotas.effective(caller.account_id)
            self._check_disk(record, quotas)
            live = self._live_shells(environment_id)
            if len(live) >= quotas.max_shells_per_environment:
                raise QuotaExceededError(
                    "max_shells_per_environment", len(live), quotas.max_shells_per_environment
                )
            workspace = self._store.workspace(record)
            cwd_path = resolve_within(workspace, cwd)
            if not cwd_path.is_dir():
                raise ValidationError(f"cwd {cwd!r} is not a directory", cwd=cwd)
            spawn_env = {
                "PATH": DEFAULT_PATH,
                "HOME": str(workspace),
                "LANG": "C.UTF-8",
                "TERM": "xterm-256color" if pty else "dumb",
                "PS1": "",
                "PS2": "",
                "HISTFILE": "",
                "ENVAPI_ENVIRONMENT_ID": record.id,
                **env,
            }
            spec = ShellSpec(
                shell_id=new_id("sh"),
                environment_id=record.id,
                cwd=relative_to_workspace(workspace, cwd_path),
                pty=pty,
                logs_dir=self._store.logs(record),
                buffer_bytes=quotas.max_output_buffer_bytes,
                max_log_bytes=quotas.max_command_log_bytes,
            )
            request = SpawnRequest(
                environment_id=record.id,
                argv=[self._settings.shell_binary],
                workspace=workspace,
                cwd=cwd_path,
                env=spawn_env,
                limits=self._effective_limits(record, quotas),
                stdin=-1,
                stdout=-1,
                stderr=-1,
                network=record.network,
            )
            shell = Shell.spawn(
                spec, self._sandbox, request, on_change=self._on_shell_change, clock=self._clock
            )
            self._shells[shell.id] = shell
            record.shells.append(
                ShellRecord(
                    id=shell.id,
                    pid=shell.pid,
                    pgid=shell.pgid,
                    start_ticks=shell.start_ticks,
                    state=shell.state,
                    created_at=shell.created_at,
                    cwd=spec.cwd,
                    pty=pty,
                )
            )
            self._touch(record)
            self._store.save(record)
        self._audit.record(
            "shell.open",
            caller.account_id,
            environment_id=record.id,
            shell_id=shell.id,
            pid=shell.pid,
        )
        return shell

    def list_shells(self, caller: Caller, environment_id: str) -> list[Shell]:
        """Every shell of the environment still known to this process, live or not."""
        with self._lock:
            self._owned(caller, environment_id)
            return sorted(self._all_shells(environment_id), key=lambda s: s.created_at)

    def get_shell(self, caller: Caller, shell_id: str) -> Shell:
        """One shell, after the ownership check."""
        with self._lock:
            return self._owned_shell(caller, shell_id)[1]

    def close_shell(self, caller: Caller, shell_id: str) -> Shell:
        """SIGTERM the shell's group, SIGKILL after the grace period."""
        with self._lock:
            record, shell = self._owned_shell(caller, shell_id)
        shell.close(self._settings.shell_close_grace_seconds)
        self._audit.record(
            "shell.close", caller.account_id, environment_id=record.id, shell_id=shell.id
        )
        return shell

    def exec(
        self,
        caller: Caller,
        shell_id: str,
        command: str,
        timeout_ms: int | None,
        credentials: list[ResolvedCredential],
    ) -> CommandRecord:
        """Run ``command`` in the shell; the record returns immediately."""
        with self._lock:
            record, shell = self._owned_shell(caller, shell_id)
            quotas = self._quotas.effective(caller.account_id)
            self._check_disk(record, quotas)
            self._touch(record)
        command_record = shell.exec(command, timeout_ms=timeout_ms, credentials=credentials)
        self._audit.record(
            "shell.exec",
            caller.account_id,
            environment_id=record.id,
            shell_id=shell.id,
            command_id=command_record.id,
            command=command[:512],
            credentials=[c.service for c in credentials],
        )
        return command_record

    def signal_shell(self, caller: Caller, shell_id: str, sig: int) -> int:
        """Signal the current command's process tree."""
        with self._lock:
            record, shell = self._owned_shell(caller, shell_id)
        count = shell.signal(sig)
        self._audit.record(
            "shell.signal",
            caller.account_id,
            environment_id=record.id,
            shell_id=shell.id,
            signal=sig,
            processes=count,
        )
        return count

    def write_stdin(self, caller: Caller, shell_id: str, data: bytes, target: str) -> int:
        """Feed bytes to the shell."""
        with self._lock:
            record, shell = self._owned_shell(caller, shell_id)
            self._touch(record)
        written = shell.write_stdin(data, target)
        self._audit.record(
            "shell.stdin",
            caller.account_id,
            environment_id=record.id,
            shell_id=shell.id,
            bytes=written,
        )
        return written

    def get_command(
        self, caller: Caller, command_id: str, offset: int, max_bytes: int
    ) -> CommandView:
        """A command, live or persisted, with output read back from its log."""
        with self._lock:
            mine = [r for r in self._records.values() if r.account_id == caller.account_id]
            for shell in self._shells.values():
                if any(r.id == shell.environment_id for r in mine):
                    live = shell.find_command(command_id)
                    if live is not None:
                        return self._command_view(live.to_dict(), live.log_path, offset, max_bytes)
            for record in mine:
                data = self._store.read_command_json(record, command_id)
                if data is not None:
                    return self._command_view(
                        data, self._store.logs(record) / f"{command_id}.log", offset, max_bytes
                    )
        raise NotFoundError(f"command {command_id} not found", command_id=command_id)

    @staticmethod
    def _command_view(
        data: dict[str, Any], log_path: Path, offset: int, max_bytes: int
    ) -> CommandView:
        try:
            with log_path.open("rb") as handle:
                handle.seek(offset)
                output = handle.read(max_bytes)
                truncated = bool(handle.read(1))
        except OSError:
            output, truncated = b"", False
        return CommandView(data, output, offset, truncated)

    # ----- processes -----------------------------------------------------------------

    def list_processes(self, caller: Caller, environment_id: str) -> list[processes.ProcessInfo]:
        """Every live process in the environment."""
        with self._lock:
            self._owned(caller, environment_id)
            shells = self._live_shells(environment_id)
        return processes.list_processes(shells)

    def list_shell_processes(self, caller: Caller, shell_id: str) -> list[processes.ProcessInfo]:
        """Just one shell's tree."""
        with self._lock:
            _, shell = self._owned_shell(caller, shell_id)
        return processes.list_processes([shell])

    def signal_process(self, caller: Caller, environment_id: str, pid: int, sig: int) -> str:
        """Signal one pid, provided its ancestry leads to a shell of this environment."""
        with self._lock:
            self._owned(caller, environment_id)
            shells = self._live_shells(environment_id)
        owner = processes.signal_process(pid, sig, shells)
        self._audit.record(
            "process.signal",
            caller.account_id,
            environment_id=environment_id,
            shell_id=owner,
            pid=pid,
            signal=sig,
        )
        return owner

    # ----- quotas --------------------------------------------------------------------

    def quotas_for(self, account_id: str) -> dict[str, Any]:
        """Effective quotas and the stored overrides."""
        return {
            "account_id": account_id,
            "effective": self._quotas.effective(account_id).to_dict(),
            "overrides": self._quotas.overrides(account_id),
            "defaults": self._quotas.defaults.to_dict(),
        }

    def set_quotas(
        self, operator: Caller, account_id: str, overrides: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace an account's overrides."""
        self._quotas.set_overrides(account_id, overrides)
        self._audit.record(
            "quota.set", operator.account_id, target_account_id=account_id, overrides=overrides
        )
        return self.quotas_for(account_id)

    # ----- reaper --------------------------------------------------------------------

    def reap(self) -> ReapReport:
        """Close idle shells, archive idle environments, prune logs, refresh disk usage."""
        now = self._clock()
        report = ReapReport()
        with self._lock:
            records = list(self._records.values())
            shells = list(self._shells.values())
        quota_cache: dict[str, Quotas] = {}

        def quotas_of(record: EnvironmentRecord) -> Quotas:
            if record.account_id not in quota_cache:
                quota_cache[record.account_id] = self._quotas.effective(record.account_id)
            return quota_cache[record.account_id]

        by_env = {r.id: r for r in records}
        for shell in shells:
            record = by_env.get(shell.environment_id)
            if record is None or shell.state is not ShellState.RUNNING:
                continue
            ttl = quotas_of(record).shell_idle_ttl_seconds
            if shell.current is None and now - shell.last_activity > ttl:
                shell.close(self._settings.shell_close_grace_seconds)
                self._audit.record(
                    "shell.reap", record.account_id, environment_id=record.id, shell_id=shell.id
                )
                report.shells_closed += 1
        for record in records:
            quotas = quotas_of(record)
            with self._lock:
                if record.id not in self._records:
                    continue
                live = self._live_shells(record.id)
                last = max([record.last_activity_at, *(s.last_activity for s in live)])
                idle = not live and now - last > quotas.environment_idle_ttl_seconds
                if record.state is EnvironmentState.ACTIVE and idle:
                    self._archive(record, now)
                    report.environments_archived += 1
                report.logs_pruned += self._store.prune_logs(record, quotas.max_command_log_bytes)
                workspace_bytes, logs_bytes = self._store.disk_usage(record)
                self._usage[record.id] = workspace_bytes + logs_bytes
                report.usage[record.id] = workspace_bytes + logs_bytes
        return report

    def _archive(self, record: EnvironmentRecord, now: float) -> None:
        for shell in self._all_shells(record.id):
            self._shells.pop(shell.id, None)
        self._store.wipe_workspace(record)
        self._store.wipe_logs(record)
        record.state = EnvironmentState.ARCHIVED
        record.archived_at = now
        record.updated_at = now
        self._store.save(record)
        self._audit.record("environment.archive", record.account_id, environment_id=record.id)
        log.info("environment_archived", environment_id=record.id)
