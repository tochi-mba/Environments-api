"""Find out what the host can do and pick the strongest tier it supports.

Detection runs real probes rather than trusting configuration: a Docker seccomp profile
can leave ``unshare`` on disk but make it useless, and only trying tells the difference.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from app.constants import SandboxTier
from app.sandbox.directory import DirectorySandbox
from app.sandbox.namespace import NamespaceSandbox
from app.sandbox.protocol import Sandbox
from app.sandbox.user import UserSandbox

log = structlog.get_logger(__name__)

RunFn = Callable[..., "subprocess.CompletedProcess[bytes]"]
WhichFn = Callable[[str], str | None]

# The probe does what the real tier does; if a hardened mount table cannot be built in a
# fresh namespace, the tier would be a lie.
PROBE_SCRIPT = "mount --make-rprivate / && mount -o remount,bind,ro / && [ -d /proc/1 ]"


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    """What the probes found."""

    is_root: bool
    has_useradd: bool
    has_setpriv: bool
    unshare_works: bool
    unshare_error: str = ""

    @property
    def rootless_namespace(self) -> bool:
        """Whether the namespace tier would use a user namespace rather than real root."""
        return self.unshare_works and not self.is_root


def unshare_probe_argv(is_root: bool) -> list[str]:
    """The exact command used to decide whether the namespace tier is available."""
    argv = ["unshare"]
    if not is_root:
        argv += ["--user", "--map-root-user"]
    argv += ["--mount", "--pid", "--fork", "--mount-proc", "--", "/bin/sh", "-c", PROBE_SCRIPT]
    return argv


def probe_host(
    run: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
    euid: int | None = None,
) -> HostCapabilities:
    """Probe the host. ``run``, ``which`` and ``euid`` are injectable for tests."""
    is_root = (os.geteuid() if euid is None else euid) == 0
    has_useradd = which("useradd") is not None and which("userdel") is not None
    has_setpriv = which("setpriv") is not None
    unshare_works = False
    error = ""
    if which("unshare") is None:
        error = "unshare not found"
    else:
        try:
            result = run(unshare_probe_argv(is_root), capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"probe failed to run: {exc}"
        else:
            unshare_works = result.returncode == 0
            if not unshare_works:
                error = (
                    result.stderr.decode(errors="replace").strip() or f"exit {result.returncode}"
                )
    return HostCapabilities(is_root, has_useradd, has_setpriv, unshare_works, error)


def highest_tier(caps: HostCapabilities) -> SandboxTier:
    """The strongest tier ``caps`` supports."""
    if caps.unshare_works and (not caps.is_root or (caps.has_useradd and caps.has_setpriv)):
        return SandboxTier.NAMESPACE
    if caps.is_root and caps.has_useradd:
        return SandboxTier.USER
    return SandboxTier.DIRECTORY


def build_sandbox(caps: HostCapabilities, minimum: SandboxTier) -> Sandbox:
    """Construct the strongest available tier, refusing to go below ``minimum``.

    Raises:
        RuntimeError: If the host cannot provide ``minimum``. Failing at boot beats
            discovering months later that everything ran on ``directory``.
    """
    tier = highest_tier(caps)
    if tier < minimum:
        raise RuntimeError(
            f"host supports sandbox tier {tier.label!r} but ENVAPI_MIN_SANDBOX_TIER is "
            f"{minimum.label!r} (root={caps.is_root}, useradd={caps.has_useradd}, "
            f"setpriv={caps.has_setpriv}, unshare={caps.unshare_works}"
            + (f": {caps.unshare_error}" if caps.unshare_error else "")
            + ")"
        )
    log.info("sandbox_selected", tier=tier.label, capabilities=caps)
    if tier is SandboxTier.NAMESPACE:
        return NamespaceSandbox(rootless=caps.rootless_namespace)
    if tier is SandboxTier.USER:
        return UserSandbox()
    return DirectorySandbox()
