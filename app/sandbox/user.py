"""Tier 2: a system user per environment.

Ordinary filesystem permissions then do the isolating: the workspace is owned by the
user, everything else is whatever the host grants to "other", and ``RLIMIT_NPROC`` finally
means something because it counts per uid.
"""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

from app.constants import SandboxTier
from app.sandbox.protocol import SpawnRequest
from app.sandbox.users import SystemUsers


def drop_privileges(uid: int, gid: int, request: SpawnRequest) -> None:
    """Become ``uid:gid`` for good, then finish the child setup. Runs after fork."""
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    request.child_setup()


def chown_tree(path: Path, uid: int, gid: int) -> None:
    """Give ``uid:gid`` everything under ``path`` without following symlinks out of it."""
    os.chown(path, uid, gid, follow_symlinks=False)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)


class UserSandbox:
    """Drops to a per-environment user before exec."""

    def __init__(self, users: SystemUsers | None = None) -> None:
        """Manage users through ``users`` (a default :class:`SystemUsers` if omitted)."""
        self._users = users or SystemUsers()

    @property
    def tier(self) -> SandboxTier:
        """Always :attr:`SandboxTier.USER`."""
        return SandboxTier.USER

    def prepare(self, environment_id: str, environment_dir: Path, workspace: Path) -> None:
        """Create the user and hand it the workspace."""
        uid, gid = self._users.ensure(environment_id, str(workspace))
        # The user must traverse the environment folder to reach the workspace but must not
        # list it, and the record next to it stays the service's.
        environment_dir.chmod(0o711)
        chown_tree(workspace, uid, gid)

    def owner(self, environment_id: str) -> tuple[int, int] | None:
        """The environment's user, if it has been prepared."""
        return self._users.lookup(environment_id)

    def spawn(self, request: SpawnRequest) -> subprocess.Popen[bytes]:
        """Start ``argv`` as the environment's user."""
        ids = self._users.lookup(request.environment_id)
        if ids is None:
            ids = self._users.ensure(request.environment_id, str(request.workspace))
        uid, gid = ids
        return subprocess.Popen(
            request.argv,
            cwd=request.cwd,
            env=request.env,
            stdin=request.stdin,
            stdout=request.stdout,
            stderr=request.stderr,
            start_new_session=True,
            preexec_fn=functools.partial(drop_privileges, uid, gid, request),
            close_fds=True,
        )

    def teardown(self, environment_id: str) -> None:
        """Remove the user."""
        self._users.remove(environment_id)
