"""An append-only record of every privileged action.

Keyring keeps an audit log of its own privileged actions; a service that executes
arbitrary commands for remote callers needs the same, and this is it. One JSON object per
line so it can be tailed, grepped, and shipped.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class AuditLog:
    """Thread-safe JSON-lines writer."""

    def __init__(self, path: Path) -> None:
        """Append to ``path``, creating it and its parents as needed."""
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, account_id: str, **fields: Any) -> None:
        """Append one event."""
        entry = {"ts": time.time(), "action": action, "account_id": account_id, **fields}
        line = json.dumps(entry, separators=(",", ":"), default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        log.info("audit", **entry)

    def tail(self, limit: int, account_id: str | None = None) -> list[dict[str, Any]]:
        """The most recent ``limit`` events, optionally for one account only."""
        with self._lock:
            if not self._path.exists():
                return []
            lines = self._path.read_text(encoding="utf-8").splitlines()
        kept: deque[dict[str, Any]] = deque(maxlen=limit)
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if account_id is None or entry.get("account_id") == account_id:
                kept.append(entry)
        return list(kept)
