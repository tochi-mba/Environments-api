"""Fixed values shared across the service.

Anything here is a protocol detail rather than a tunable: changing one changes the wire
format or on-disk layout, so it lives away from settings on purpose.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum

SERVICE_NAME = "environments-api"
PROBLEM_JSON = "application/problem+json"

HEADER_AUTHORIZATION = "Authorization"
# Superseded by ``Authorization: Bearer``; still accepted on its own for one release.
HEADER_USER_TOKEN = "X-Keyring-User-Token"
HEADER_PROFILE = "X-Keyring-Profile"
HEADER_API_KEY = "X-API-Key"
HEADER_REQUEST_ID = "X-Request-Id"

# The ASCII record separator. It frames the exit-code marker a shell prints after every
# command; a byte that essentially never appears in real output keeps the scan cheap and
# false positives rare (a random nonce makes them harmless).
FRAME_BYTE = b"\x1e"
FRAME_PATTERN = re.compile(rb"\x1e([0-9a-f]{32}):(-?\d{1,5})\x1e")
# A tail that could still grow into a frame; anything else after a frame byte is output.
PARTIAL_FRAME_PATTERN = re.compile(rb"\x1e(?:[0-9a-f]{0,31}|[0-9a-f]{32}(?::(?:-?\d{0,5})?)?)$")

ENVIRONMENT_FILE = "environment.json"
WORKSPACE_DIR = "workspace"
LOGS_DIR = "logs"
ACCOUNTS_DIR = "accounts"
QUOTAS_DIR = "quotas"
AUDIT_FILE = "audit.jsonl"

PROFILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
SERVICE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

REDACTION_TEMPLATE = "«redacted:{service}»"
COMMAND_HISTORY_LIMIT = 200
SHELL_DEAD_RESTART = "service_restarted"


class SandboxTier(IntEnum):
    """Isolation tiers, ordered from weakest to strongest."""

    DIRECTORY = 1
    USER = 2
    NAMESPACE = 3

    @classmethod
    def parse(cls, value: str) -> SandboxTier:
        """Return the tier named by ``value`` (case-insensitive).

        Raises:
            ValueError: If ``value`` names no tier.
        """
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            names = ", ".join(t.name.lower() for t in cls)
            raise ValueError(f"unknown sandbox tier {value!r}; expected one of {names}") from exc

    @property
    def label(self) -> str:
        """The tier's wire name."""
        return self.name.lower()


class EnvironmentState(StrEnum):
    """Lifecycle of an environment."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ShellState(StrEnum):
    """Lifecycle of a shell process."""

    RUNNING = "running"
    CLOSED = "closed"
    DEAD = "dead"


class CommandState(StrEnum):
    """Lifecycle of a command run inside a shell."""

    RUNNING = "running"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    SHELL_DIED = "shell_died"
