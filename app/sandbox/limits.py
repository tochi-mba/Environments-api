"""POSIX resource limits applied in the child before exec."""

from __future__ import annotations

import resource
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Per-process caps. ``None`` leaves the inherited limit alone."""

    nproc: int | None = None
    memory_bytes: int | None = None
    file_size_bytes: int | None = None
    cpu_seconds: int | None = None

    def apply(self) -> None:
        """Lower the calling process's limits. Meant to run as a ``preexec_fn``."""
        for name, value in (
            (resource.RLIMIT_NPROC, self.nproc),
            (resource.RLIMIT_AS, self.memory_bytes),
            (resource.RLIMIT_FSIZE, self.file_size_bytes),
            (resource.RLIMIT_CPU, self.cpu_seconds),
        ):
            if value is not None:
                resource.setrlimit(name, (value, value))
