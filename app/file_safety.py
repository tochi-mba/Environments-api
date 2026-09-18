"""Descriptor-relative file access that never follows a caller-controlled symlink."""

from __future__ import annotations

import codecs
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote

from app.errors import NotFoundError, PathEscapeError, PreconditionError, ValidationError
from app.paths import relative_to_workspace, resolve_within


def relative_path(workspace: Path, requested: str) -> str:
    """Canonicalise separators and reject aliases through links before resolving."""
    requested = unquote(requested).replace("\\", "/")
    root = workspace.resolve()
    candidate = Path(requested) if Path(requested).is_absolute() else root / requested
    resolved = resolve_within(root, requested)
    # Inspect the spelling too: resolving first must not turn an in-root symlink into
    # permission to follow it. Descriptor walks repeat this check without a race.
    for part in (candidate, *candidate.parents):
        if part == root:
            break
        if part.is_symlink():
            raise PathEscapeError("File operations do not follow symbolic links")
    return relative_to_workspace(root, resolved)


@contextmanager
def parent_fd(
    workspace: Path,
    relative: str,
    *,
    create: bool = False,
    owner: tuple[int, int] | None = None,
) -> Iterator[tuple[int, str]]:
    """Open each parent with O_NOFOLLOW; retain its descriptor until the action ends."""
    parts = Path(relative).parts
    if not parts:
        parts = (".",)
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                    if owner is not None:
                        os.chown(part, *owner, dir_fd=descriptor, follow_symlinks=False)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    except FileNotFoundError as exc:
        raise NotFoundError("The file or its parent directory does not exist") from exc
    except OSError as exc:
        raise ValidationError("File operation refused; check its type and permissions") from exc
    finally:
        os.close(descriptor)


@contextmanager
def open_file(descriptor: int, name: str) -> Iterator[BinaryIO]:
    """Open a regular file without waiting on a named pipe or following a link."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotFoundError("The path is not a regular file")
    with os.fdopen(fd, "rb") as handle:
        yield handle


def metadata(handle: BinaryIO) -> tuple[str, bool, int]:
    """Hash and classify the entire file incrementally, independently of the read window."""
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    binary = False
    size = 0
    handle.seek(0)
    while chunk := handle.read(64 * 1024):
        digest.update(chunk)
        size += len(chunk)
        if not binary:
            try:
                decoder.decode(chunk)
                binary = b"\0" in chunk
            except UnicodeDecodeError:
                binary = True
    if not binary:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            binary = True
    handle.seek(0)
    return f'"{digest.hexdigest()}"', binary, size


def current_etag(descriptor: int, name: str) -> str | None:
    """Return the current representation validator, or None for an absent file."""
    try:
        with open_file(descriptor, name) as handle:
            return metadata(handle)[0]
    except FileNotFoundError:
        return None


def check_match(expected: str | None, actual: str | None) -> None:
    """Apply HTTP strong If-Match comparison, including lists and the existence wildcard."""
    if expected is None:
        return
    candidates = [part.strip() for part in expected.split(",")]
    if actual is not None and (expected.strip() == "*" or actual in candidates):
        return
    raise PreconditionError("The file changed since you read it; read it again and reapply")
