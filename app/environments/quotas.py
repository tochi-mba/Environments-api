"""Quotas: deployment-wide defaults with admin-settable per-account overrides."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from app.constants import QUOTAS_DIR
from app.environments.store import safe_segment
from app.errors import ValidationError
from app.settings import Settings


@dataclass(frozen=True, slots=True)
class Quotas:
    """Every limit the service enforces."""

    max_environments_per_profile: int
    max_environments_per_account: int
    max_shells_per_environment: int
    max_processes_per_shell: int
    max_memory_bytes: int
    max_file_size_bytes: int
    max_cpu_seconds: int
    max_disk_bytes: int
    max_output_buffer_bytes: int
    max_command_log_bytes: int
    environment_idle_ttl_seconds: float
    shell_idle_ttl_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Quotas:
        """The deployment defaults."""
        return cls(**{f.name: getattr(settings, f.name) for f in fields(cls)})

    def with_overrides(self, overrides: dict[str, Any]) -> Quotas:
        """A copy with ``overrides`` applied."""
        values = asdict(self)
        values.update(validate_overrides(overrides))
        return Quotas(**values)

    def to_dict(self) -> dict[str, Any]:
        """Plain mapping for the API."""
        return asdict(self)


QUOTA_NAMES = frozenset(f.name for f in fields(Quotas))


def validate_overrides(overrides: dict[str, Any]) -> dict[str, int | float]:
    """Check that ``overrides`` only names real quotas with positive numeric values."""
    clean: dict[str, int | float] = {}
    for name, value in overrides.items():
        if name not in QUOTA_NAMES:
            raise ValidationError(f"unknown quota {name!r}", quota=name)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise ValidationError(f"quota {name!r} must be a positive number", quota=name)
        clean[name] = value
    return clean


class QuotaStore:
    """Per-account overrides at ``ROOT/quotas/<account>.json``."""

    def __init__(self, root: Path, defaults: Quotas) -> None:
        """Keep overrides under ``root`` on top of ``defaults``."""
        self._dir = root / QUOTAS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._defaults = defaults

    @property
    def defaults(self) -> Quotas:
        """The deployment-wide values."""
        return self._defaults

    def _path(self, account_id: str) -> Path:
        return self._dir / f"{safe_segment(account_id)}.json"

    def overrides(self, account_id: str) -> dict[str, int | float]:
        """The account's stored overrides (empty if none)."""
        try:
            data = json.loads(self._path(account_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return validate_overrides(data) if isinstance(data, dict) else {}

    def set_overrides(self, account_id: str, overrides: dict[str, Any]) -> Quotas:
        """Replace the account's overrides and return the effective quotas."""
        clean = validate_overrides(overrides)
        path = self._path(account_id)
        if clean:
            path.write_text(json.dumps(clean), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        return self.effective(account_id)

    def effective(self, account_id: str) -> Quotas:
        """Defaults with the account's overrides applied."""
        return self._defaults.with_overrides(self.overrides(account_id))
