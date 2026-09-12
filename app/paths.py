"""Workspace path containment.

Every path a caller names is resolved with symlinks followed and only then checked for
containment. The other order (check the text, then resolve) is the classic symlink escape:
``safe/link`` looks contained and points at ``/etc``.
"""

from __future__ import annotations

from pathlib import Path

from app.errors import PathEscapeError


def resolve_within(workspace: Path, requested: str) -> Path:
    """Resolve ``requested`` (relative to ``workspace``, or absolute) and prove it stays inside.

    A non-existent final component is allowed so files can be created, but everything that
    does exist along the way is resolved for real, including symlinks planted by a shell
    after the environment was made.

    Raises:
        PathEscapeError: If the resolved path is not the workspace or below it.
    """
    root = workspace.resolve()
    if "\0" in requested:
        raise PathEscapeError("path contains a NUL byte", path=requested)
    candidate = Path(requested) if requested.startswith("/") else root / requested
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise PathEscapeError(f"{requested!r} resolves outside the workspace", path=requested)
    return resolved


def relative_to_workspace(workspace: Path, resolved: Path) -> str:
    """The workspace-relative form of an already-contained path, ``"."`` for the root."""
    rel = resolved.relative_to(workspace.resolve())
    return "." if str(rel) == "." else rel.as_posix()
