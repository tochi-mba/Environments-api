"""Tier 1: a working directory, a minimal environment and rlimits.

Organisation, not a boundary: a process here can still ``cd /`` and read whatever the
service user can read. It exists so the service runs everywhere, and so the stronger tiers
have something to fall back to when ``ENVAPI_MIN_SANDBOX_TIER`` permits it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.constants import SandboxTier
from app.sandbox.protocol import SpawnRequest


class DirectorySandbox:
    """Spawns in the workspace with rlimits and nothing else."""

    @property
    def tier(self) -> SandboxTier:
        """Always :attr:`SandboxTier.DIRECTORY`."""
        return SandboxTier.DIRECTORY

    def prepare(self, environment_id: str, environment_dir: Path, workspace: Path) -> None:
        """Nothing to prepare: the store already created the folders."""

    def owner(self, environment_id: str) -> tuple[int, int] | None:
        """Processes run as the service user."""
        return None

    def spawn(self, request: SpawnRequest) -> subprocess.Popen[bytes]:
        """Start ``argv`` in a new session with the requested limits."""
        return subprocess.Popen(
            request.argv,
            cwd=request.cwd,
            env=request.env,
            stdin=request.stdin,
            stdout=request.stdout,
            stderr=request.stderr,
            start_new_session=True,
            preexec_fn=request.child_setup,
            close_fds=True,
        )

    def teardown(self, environment_id: str) -> None:
        """Nothing to release."""
