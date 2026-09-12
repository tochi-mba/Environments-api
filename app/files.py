"""Retrieval without a shell: list, read and write files in a workspace."""

from __future__ import annotations

import base64
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.errors import NotFoundError, ValidationError
from app.paths import relative_to_workspace, resolve_within


@dataclass(frozen=True, slots=True)
class DirEntry:
    """One row of a directory listing."""

    name: str
    path: str
    kind: str
    size: int
    mtime: float


@dataclass(frozen=True, slots=True)
class FileContent:
    """A (possibly truncated) read."""

    path: str
    size: int
    offset: int
    content: str
    encoding: str
    truncated: bool


def _kind(st: os.stat_result, is_link: bool) -> str:
    if is_link:
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


class FileService:
    """Every operation resolves and contains the path before touching the filesystem."""

    def __init__(self, max_read_bytes: int, max_write_bytes: int) -> None:
        """Cap reads at ``max_read_bytes`` and writes at ``max_write_bytes``."""
        self._max_read = max_read_bytes
        self._max_write = max_write_bytes

    def list_dir(self, workspace: Path, requested: str) -> list[DirEntry]:
        """List a directory, symlinks reported as such rather than followed."""
        target = resolve_within(workspace, requested)
        if not target.is_dir():
            raise NotFoundError(f"{requested!r} is not a directory", path=requested)
        entries: list[DirEntry] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            try:
                st = child.lstat()
            except OSError:
                continue
            is_link = stat.S_ISLNK(st.st_mode)
            entries.append(
                DirEntry(
                    name=child.name,
                    path=relative_to_workspace(workspace, target / child.name),
                    kind=_kind(st, is_link),
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
        return entries

    def read(
        self, workspace: Path, requested: str, offset: int, max_bytes: int | None
    ) -> FileContent:
        """Read up to ``max_bytes`` (capped) from ``offset``; text as UTF-8, else base64."""
        target = resolve_within(workspace, requested)
        if not target.is_file():
            raise NotFoundError(f"{requested!r} is not a file", path=requested)
        limit = min(max_bytes if max_bytes is not None else self._max_read, self._max_read)
        size = target.stat().st_size
        with target.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit)
        truncated = offset + len(data) < size
        try:
            text = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = base64.b64encode(data).decode("ascii")
            encoding = "base64"
        return FileContent(
            path=relative_to_workspace(workspace, target),
            size=size,
            offset=offset,
            content=text,
            encoding=encoding,
            truncated=truncated,
        )

    def write(
        self,
        workspace: Path,
        requested: str,
        content: str,
        encoding: str,
        mode: str,
        owner: tuple[int, int] | None,
    ) -> int:
        """Write a file, creating parents. ``mode`` is ``overwrite`` or ``append``.

        Returns:
            The file's size afterwards.
        """
        if encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except ValueError as exc:
                raise ValidationError("content is not valid base64") from exc
        elif encoding == "utf-8":
            data = content.encode("utf-8")
        else:
            raise ValidationError(f"unsupported encoding {encoding!r}")
        if len(data) > self._max_write:
            raise ValidationError(
                f"content exceeds max_file_write_bytes ({self._max_write})", limit=self._max_write
            )
        if mode not in ("overwrite", "append"):
            raise ValidationError(f"unsupported mode {mode!r}")
        target = resolve_within(workspace, requested)
        if target == workspace.resolve() or target.is_dir():
            raise ValidationError(f"{requested!r} is a directory", path=requested)
        created_dirs: list[Path] = []
        parent = target.parent
        while not parent.exists():
            created_dirs.append(parent)
            parent = parent.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab" if mode == "append" else "wb") as handle:
            handle.write(data)
        if owner is not None:
            for made in created_dirs:
                os.chown(made, *owner)
            os.chown(target, *owner)
        return target.stat().st_size
