"""Process isolation: three tiers, the strongest the host allows."""

from app.sandbox.detect import HostCapabilities, build_sandbox, highest_tier, probe_host
from app.sandbox.limits import ResourceLimits
from app.sandbox.protocol import Sandbox, SpawnRequest

__all__ = [
    "HostCapabilities",
    "ResourceLimits",
    "Sandbox",
    "SpawnRequest",
    "build_sandbox",
    "highest_tier",
    "probe_host",
]
