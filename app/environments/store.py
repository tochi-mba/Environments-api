"""On-disk layout and atomic persistence of environment records.

``ROOT/accounts/<account>/<profile>/<environment id>/`` holds ``environment.json``, the
``workspace/`` a shell lives in, and ``logs/`` with one file per command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import structlog

from app.constants import ACCOUNTS_DIR, ENVIRONMENT_FILE, LOGS_DIR, WORKSPACE_DIR
from app.environments.models import EnvironmentRecord

log = structlog.get_logger(__name__)

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_segment(value: str) -> str:
    """A path segment for an opaque id: used as-is when harmless, hashed otherwise."""
    if _SAFE_SEGMENT.match(value) and value not in (".", ".."):
        return value
    return "h_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def _mkdir_traversable(path: Path) -> None:
    # Sandboxed users must be able to traverse down to their workspace, never list.
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o711)


class EnvironmentStore:
    """Knows where everything lives and writes records atomically."""

    def __init__(self, root: Path) -> None:
        """Keep everything under ``root``."""
        self.root = root
        _mkdir_traversable(root)
        _mkdir_traversable(root / ACCOUNTS_DIR)

    def environment_dir(self, record: EnvironmentRecord) -> Path:
        """The environment's folder."""
        return (
            self.root
            / ACCOUNTS_DIR
            / safe_segment(record.account_id)
            / safe_segment(record.profile)
            / record.id
        )

    def workspace(self, record: EnvironmentRecord) -> Path:
        """Where shells run."""
        return self.environment_dir(record) / WORKSPACE_DIR

    def logs(self, record: EnvironmentRecord) -> Path:
        """Where command output is kept."""
        return self.environment_dir(record) / LOGS_DIR

    def create_dirs(self, record: EnvironmentRecord) -> None:
        """Make the environment's folders."""
        env_dir = self.environment_dir(record)
        _mkdir_traversable(env_dir.parent.parent)
        _mkdir_traversable(env_dir.parent)
        _mkdir_traversable(env_dir)
        self.workspace(record).mkdir(exist_ok=True)
        self.workspace(record).chmod(0o755)
        self.logs(record).mkdir(exist_ok=True)

    def save(self, record: EnvironmentRecord) -> None:
        """Write ``environment.json`` atomically (write, fsync, rename)."""
        env_dir = self.environment_dir(record)
        env_dir.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=env_dir, prefix=".environment-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, env_dir / ENVIRONMENT_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load_all(self) -> list[EnvironmentRecord]:
        """Every record on disk; a corrupt one is logged and skipped rather than fatal."""
        records: list[EnvironmentRecord] = []
        for path in sorted((self.root / ACCOUNTS_DIR).glob(f"*/*/*/{ENVIRONMENT_FILE}")):
            try:
                records.append(EnvironmentRecord.model_validate_json(path.read_text()))
            except ValueError as exc:
                log.error("environment_record_corrupt", path=str(path), error=str(exc))
        return records

    def delete(self, record: EnvironmentRecord) -> None:
        """Remove the environment's folder entirely."""
        shutil.rmtree(self.environment_dir(record), ignore_errors=True)

    def wipe_workspace(self, record: EnvironmentRecord) -> None:
        """Empty the workspace, keeping the folder itself."""
        workspace = self.workspace(record)
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir()
        workspace.chmod(0o755)

    def wipe_logs(self, record: EnvironmentRecord) -> None:
        """Remove every command log."""
        logs = self.logs(record)
        shutil.rmtree(logs, ignore_errors=True)
        logs.mkdir()

    def disk_usage(self, record: EnvironmentRecord) -> tuple[int, int]:
        """``(workspace_bytes, logs_bytes)`` as allocated on disk."""
        return _tree_size(self.workspace(record)), _tree_size(self.logs(record))

    def prune_logs(self, record: EnvironmentRecord, max_bytes: int) -> int:
        """Delete the oldest logs until the total fits ``max_bytes``; returns how many."""
        logs = self.logs(record)
        entries = []
        for path in logs.glob("*.log"):
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, path))
        total = sum(size for _, size, _ in entries)
        removed = 0
        for _, size, path in sorted(entries):
            if total <= max_bytes:
                break
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    def write_command_json(
        self, record: EnvironmentRecord, command_id: str, payload: dict[str, object]
    ) -> None:
        """Persist a command's metadata next to its log."""
        path = self.logs(record) / f"{command_id}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

    def read_command_json(
        self, record: EnvironmentRecord, command_id: str
    ) -> dict[str, object] | None:
        """A persisted command's metadata, or ``None``."""
        path = self.logs(record) / f"{command_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            total += st.st_blocks * 512
    return total
