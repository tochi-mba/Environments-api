"""Durable records. Environments survive a restart; shells do not, and the record says so."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.constants import EnvironmentState, ShellState


class ShellRecord(BaseModel):
    """What is remembered about a shell: enough to reconcile it after a restart."""

    id: str
    pid: int
    pgid: int
    start_ticks: int
    state: ShellState
    dead_reason: str | None = None
    created_at: float
    cwd: str = "."
    pty: bool = False


class EnvironmentLimits(BaseModel):
    """Per-environment caps, each bounded by the account's quota."""

    max_memory_bytes: int | None = None
    max_cpu_seconds: int | None = None
    max_file_size_bytes: int | None = None
    max_processes_per_shell: int | None = None


class EnvironmentRecord(BaseModel):
    """The contents of ``environment.json``."""

    id: str
    account_id: str
    profile: str
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    credentials: list[str] = Field(default_factory=list)
    network: bool = True
    limits: EnvironmentLimits = Field(default_factory=EnvironmentLimits)
    state: EnvironmentState = EnvironmentState.ACTIVE
    sandbox_tier: str
    created_at: float
    updated_at: float
    last_activity_at: float
    archived_at: float | None = None
    shells: list[ShellRecord] = Field(default_factory=list)
    # Stamped at create from settings-api. ``None`` on records created while settings-api
    # was off, so the reaper keeps using QuotaStore / deployment TTLs for those.
    environment_idle_ttl_seconds: float | None = None
    shell_idle_ttl_seconds: float | None = None

    def environment_idle_ttl(self, fallback: float) -> float:
        """Seconds of quiet before archive: stamped at create, otherwise ``fallback``."""
        if self.environment_idle_ttl_seconds is None:
            return fallback
        return self.environment_idle_ttl_seconds

    def shell_idle_ttl(self, fallback: float) -> float:
        """Seconds of quiet before a shell is closed: stamped at create, otherwise ``fallback``."""
        if self.shell_idle_ttl_seconds is None:
            return fallback
        return self.shell_idle_ttl_seconds
