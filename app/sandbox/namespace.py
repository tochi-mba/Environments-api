"""Tier 3: mount and PID namespaces, layered on the user tier when running as root.

Inside the namespace the root filesystem is remounted read-only, the workspace is
bind-mounted back read-write, ``/tmp`` is a private tmpfs, ``/proc`` shows only the
environment's own processes, and (when network egress is disabled) there is no network
namespace to speak of. As root the process then drops to the environment's user via
``setpriv``; rootless, ``unshare --user --map-root-user`` provides the namespaces and the
mapped root is the service's own uid outside.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.constants import SandboxTier
from app.sandbox.protocol import SpawnRequest
from app.sandbox.user import chown_tree
from app.sandbox.users import SystemUsers

# Runs as root inside the fresh mount namespace, then execs the real command.
SETUP_SCRIPT = r"""
set -e
ws="$1"; cwd="$2"; uid="$3"; gid="$4"; shift 4
# Hold the workspace open: once /tmp is overmounted its path may no longer resolve, and
# the bind below goes through this descriptor rather than the path.
exec 3<"$ws"
mount --make-rprivate /
mount -o remount,bind,ro /
# Docker-style bind mounts (/etc/hosts and friends) are separate mounts that the root
# remount does not touch; best effort, since some (proc, sys, devpts) refuse.
while read -r _ mp _; do
  case "$mp" in
    /|/proc*|/sys*|/dev*) ;;
    *) mount -o remount,bind,ro "$mp" 2>/dev/null || true ;;
  esac
done < /proc/self/mounts
mount -t tmpfs -o size=256m,mode=1777 tmpfs /tmp
mkdir -p "$ws"
mount --no-canonicalize --bind /proc/self/fd/3 "$ws"
mount -o remount,bind,rw "$ws"
exec 3<&-
# The cwd Popen set refers to the pre-mount view of the tree, which is now read-only;
# re-enter it so the path resolves through the writable bind mount.
cd "$cwd"
if [ "$uid" != "0" ]; then
  exec setpriv --reuid "$uid" --regid "$gid" --clear-groups -- "$@"
fi
exec "$@"
"""


class NamespaceSandbox:
    """Spawns through ``unshare`` with a hardened mount table."""

    def __init__(self, rootless: bool, users: SystemUsers | None = None) -> None:
        """``rootless`` selects a user namespace instead of a real privilege drop."""
        self._rootless = rootless
        self._users = users or SystemUsers()

    @property
    def tier(self) -> SandboxTier:
        """Always :attr:`SandboxTier.NAMESPACE`."""
        return SandboxTier.NAMESPACE

    def prepare(self, environment_id: str, environment_dir: Path, workspace: Path) -> None:
        """As root, create the environment's user and hand it the workspace."""
        if self._rootless:
            return
        uid, gid = self._users.ensure(environment_id, str(workspace))
        environment_dir.chmod(0o711)
        chown_tree(workspace, uid, gid)

    def owner(self, environment_id: str) -> tuple[int, int] | None:
        """The environment's user when running as root, else the service's own."""
        if self._rootless:
            return None
        return self._users.lookup(environment_id)

    def spawn(self, request: SpawnRequest) -> subprocess.Popen[bytes]:
        """Start ``argv`` inside fresh namespaces."""
        uid, gid = 0, 0
        if not self._rootless:
            uid, gid = self._users.lookup(request.environment_id) or self._users.ensure(
                request.environment_id, str(request.workspace)
            )
        argv = ["unshare"]
        if self._rootless:
            argv += ["--user", "--map-root-user"]
        argv += ["--mount", "--pid", "--fork", "--mount-proc", "--kill-child"]
        if not request.network:
            argv.append("--net")
        argv += [
            "--",
            "/bin/sh",
            "-c",
            SETUP_SCRIPT,
            "envapi-setup",
            str(request.workspace),
            str(request.cwd),
            str(uid),
            str(gid),
            *request.argv,
        ]
        return subprocess.Popen(
            argv,
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
        """Remove the environment's user, if one was created."""
        if not self._rootless:
            self._users.remove(environment_id)


def running_as_root() -> bool:
    """Whether the service has real root, which decides how the namespace tier is built."""
    return os.geteuid() == 0
