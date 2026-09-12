"""Per-environment system users, for tiers that drop privileges."""

from __future__ import annotations

import pwd
import subprocess
from collections.abc import Callable

from app.errors import SandboxError

RunFn = Callable[..., "subprocess.CompletedProcess[bytes]"]


def user_name(environment_id: str) -> str:
    """The system user an environment's processes run as."""
    return "envapi_" + environment_id.removeprefix("env_")


class SystemUsers:
    """Create and remove system users through ``useradd``/``userdel``."""

    def __init__(self, run: RunFn = subprocess.run) -> None:
        """Use ``run`` to invoke the user-management binaries (injected for tests)."""
        self._run = run

    def lookup(self, environment_id: str) -> tuple[int, int] | None:
        """``(uid, gid)`` of the environment's user, or ``None`` if it does not exist."""
        try:
            entry = pwd.getpwnam(user_name(environment_id))
        except KeyError:
            return None
        return entry.pw_uid, entry.pw_gid

    def ensure(self, environment_id: str, home: str) -> tuple[int, int]:
        """Create the environment's user if needed and return its ids.

        Raises:
            SandboxError: If ``useradd`` fails.
        """
        existing = self.lookup(environment_id)
        if existing is not None:
            return existing
        name = user_name(environment_id)
        result = self._run(
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--home-dir",
                home,
                "--shell",
                "/usr/sbin/nologin",
                "--user-group",
                name,
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise SandboxError(f"useradd {name} failed: {result.stderr.decode(errors='replace')}")
        created = self.lookup(environment_id)
        if created is None:
            raise SandboxError(f"useradd {name} reported success but the user is missing")
        return created

    def remove(self, environment_id: str) -> None:
        """Delete the environment's user; a missing user is not an error."""
        if self.lookup(environment_id) is None:
            return
        self._run(["userdel", user_name(environment_id)], capture_output=True, check=False)
