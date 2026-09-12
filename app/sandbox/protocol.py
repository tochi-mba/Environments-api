"""The contract every sandbox tier implements."""

from __future__ import annotations

import fcntl
import subprocess
import termios
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from app.constants import SandboxTier
from app.sandbox.limits import ResourceLimits


@dataclass(slots=True)
class SpawnRequest:
    """Everything a tier needs to start one process for one environment."""

    environment_id: str
    argv: list[str]
    workspace: Path
    cwd: Path
    env: dict[str, str]
    limits: ResourceLimits
    stdin: int | IO[bytes]
    stdout: int | IO[bytes]
    stderr: int | IO[bytes]
    network: bool = True
    controlling_tty: int | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def child_setup(self) -> None:
        """Run in the child after fork: claim the tty (if any) and lower the rlimits.

        Popen already made the child a session leader, which is what ``TIOCSCTTY`` needs;
        without it programs that open ``/dev/tty`` for prompts would find nothing.
        """
        if self.controlling_tty is not None:
            fcntl.ioctl(self.controlling_tty, termios.TIOCSCTTY, 0)
        self.limits.apply()


class Sandbox(Protocol):
    """Spawns processes under one environment's constraints."""

    @property
    def tier(self) -> SandboxTier:
        """Which tier this is, for ``/health/ready`` and environment records."""
        ...

    def prepare(self, environment_id: str, environment_dir: Path, workspace: Path) -> None:
        """Do per-environment setup (create a user, fix ownership) before the first spawn."""
        ...

    def owner(self, environment_id: str) -> tuple[int, int] | None:
        """The ``(uid, gid)`` processes of this environment run as, if not the service's own."""
        ...

    def spawn(self, request: SpawnRequest) -> subprocess.Popen[bytes]:
        """Start the process in its own session and process group."""
        ...

    def teardown(self, environment_id: str) -> None:
        """Release per-environment resources once the environment is deleted."""
        ...
