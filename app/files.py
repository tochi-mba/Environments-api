"""Confined file retrieval and optimistic, atomic file mutations."""

from __future__ import annotations

import base64
import codecs
import fnmatch
import hashlib
import os
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.errors import ConflictError, NotFoundError, ValidationError
from app.file_edits import apply_patch, replace_unique, unified_diff
from app.file_safety import check_match, current_etag, metadata, open_file, parent_fd, relative_path


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
    """A bounded read with a validator and an exact byte continuation."""

    path: str
    size: int
    offset: int
    content: str
    encoding: str
    truncated: bool
    is_binary: bool
    etag: str
    next_offset: int


@dataclass(frozen=True, slots=True)
class Mutation:
    """A committed file representation and, for edits, its reviewable diff."""

    path: str
    size: int
    etag: str
    diff: str = ""
    applied_hunks: list[int] = field(default_factory=list)
    rejected_hunks: list[int] = field(default_factory=list)


def _kind(st: os.stat_result, is_link: bool) -> str:
    if is_link:
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


class FileService:
    """Resolve containment, refuse symlinks and serialize mutations before touching files."""

    def __init__(self, max_read_bytes: int, max_write_bytes: int) -> None:
        """Cap read windows and submitted writes; API mutations share one lock."""
        self._max_read = max_read_bytes
        self._max_write = max_write_bytes
        self._lock = threading.RLock()

    def list_dir(
        self, workspace: Path, requested: str, glob: str | None = None, depth: int = 0
    ) -> list[DirEntry]:
        """List recursively to a bounded depth without following symbolic links."""
        relative = relative_path(workspace, requested)
        with parent_fd(workspace, relative) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise NotFoundError("The path is not a directory")
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                return self._list(descriptor, relative, glob, depth)
            finally:
                os.close(descriptor)

    def _list(self, descriptor: int, relative: str, glob: str | None, depth: int) -> list[DirEntry]:
        rows: list[DirEntry] = []
        for name in sorted(os.listdir(descriptor)):
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            path = name if relative == "." else f"{relative}/{name}"
            kind = _kind(info, stat.S_ISLNK(info.st_mode))
            if glob is None or fnmatch.fnmatchcase(path, glob) or fnmatch.fnmatchcase(name, glob):
                rows.append(DirEntry(name, path, kind, info.st_size, info.st_mtime))
            if kind == "directory" and depth > 0:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                try:
                    rows.extend(self._list(child, path, glob, depth - 1))
                finally:
                    os.close(child)
        return rows

    def read(
        self, workspace: Path, requested: str, offset: int, max_bytes: int | None
    ) -> FileContent:
        """Read a byte window using a whole-file encoding decision and a strong ETag.

        UTF-8 pages end at character boundaries. Offsets inside a character and windows
        too small for one character are refused instead of returning corrupt text.
        """
        relative = relative_path(workspace, requested)
        limit = min(max_bytes if max_bytes is not None else self._max_read, self._max_read)
        with parent_fd(workspace, relative) as (parent, name), open_file(parent, name) as handle:
            etag, binary, size = metadata(handle)
            handle.seek(offset)
            data = handle.read(limit)
            if binary:
                text = base64.b64encode(data).decode("ascii")
            else:
                decoder = codecs.getincrementaldecoder("utf-8")()
                try:
                    text = decoder.decode(data, final=offset + len(data) >= size)
                except UnicodeDecodeError as exc:
                    raise ValidationError("Read offset is inside a UTF-8 character") from exc
                data = data[: len(data) - len(decoder.getstate()[0])]
                if not data and offset < size:
                    raise ValidationError("Increase max_bytes to fit at least one UTF-8 character")
        next_offset = offset + len(data)
        return FileContent(
            relative,
            size,
            offset,
            text,
            "base64" if binary else "utf-8",
            next_offset < size,
            binary,
            etag,
            next_offset,
        )

    def _data(self, content: str, encoding: str) -> bytes:
        if encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except ValueError as exc:
                raise ValidationError("content is not valid base64") from exc
        elif encoding == "utf-8":
            data = content.encode("utf-8")
        else:
            raise ValidationError("unsupported encoding")
        if len(data) > self._max_write:
            raise ValidationError(
                f"content exceeds max_file_write_bytes ({self._max_write})", limit=self._max_write
            )
        return data

    def _commit(
        self,
        parent: int,
        name: str,
        data: bytes,
        owner: tuple[int, int] | None,
        expected: str | None,
    ) -> str:
        check_match(expected, current_etag(parent, name))
        temporary = f".lucy-write-{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                if owner is not None:
                    os.fchown(handle.fileno(), *owner)
            # A shell may have changed the file while the new content was being written.
            check_match(expected, current_etag(parent, name))
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            if os.path.exists(f"/proc/self/fd/{parent}/{temporary}"):
                os.unlink(temporary, dir_fd=parent)
        return f'"{hashlib.sha256(data).hexdigest()}"'

    def write_result(
        self,
        workspace: Path,
        requested: str,
        content: str,
        encoding: str,
        mode: str,
        owner: tuple[int, int] | None,
        if_match: str | None = None,
    ) -> Mutation:
        """Atomically write a file, creating parents and optionally comparing its ETag."""
        data = self._data(content, encoding)
        if mode not in ("overwrite", "append"):
            raise ValidationError("unsupported mode")
        relative = relative_path(workspace, requested)
        if relative == "." or (workspace / relative).is_dir():
            raise ValidationError("The requested path is a directory")
        with self._lock, parent_fd(workspace, relative, create=True, owner=owner) as (parent, name):
            expected = if_match
            if mode == "append" and current_etag(parent, name) is not None:
                with open_file(parent, name) as handle:
                    previous = handle.read(self._max_write + 1)
                    check_match(if_match, f'"{hashlib.sha256(previous).hexdigest()}"')
                    expected = f'"{hashlib.sha256(previous).hexdigest()}"'
                    data = self._data(base64.b64encode(previous + data).decode("ascii"), "base64")
            etag = self._commit(parent, name, data, owner, expected)
        return Mutation(relative, len(data), etag)

    def write(
        self,
        workspace: Path,
        requested: str,
        content: str,
        encoding: str,
        mode: str,
        owner: tuple[int, int] | None,
        if_match: str | None = None,
    ) -> int:
        """Write a file and return its size; retained for existing in-process callers."""
        return self.write_result(
            workspace, requested, content, encoding, mode, owner, if_match
        ).size

    def edit(
        self,
        workspace: Path,
        requested: str,
        old_string: str,
        new_string: str,
        owner: tuple[int, int] | None,
        if_match: str | None = None,
    ) -> Mutation:
        """Replace one exact occurrence, returning the committed diff and ETag."""
        with self._lock:
            content = self._editable(workspace, requested, if_match)
            new = replace_unique(content.content, old_string, new_string)
            changed = self.write_result(
                workspace, requested, new, "utf-8", "overwrite", owner, content.etag
            )
            return Mutation(
                changed.path,
                changed.size,
                changed.etag,
                unified_diff(requested, content.content, new),
            )

    def patch(
        self,
        workspace: Path,
        requested: str,
        patch: str,
        owner: tuple[int, int] | None,
        if_match: str | None = None,
    ) -> Mutation:
        """Apply matching unified hunks; rejected hunks remain visible in the response."""
        with self._lock:
            content = self._editable(workspace, requested, if_match)
            new, applied, rejected = apply_patch(content.content, patch)
            changed = self.write_result(
                workspace, requested, new, "utf-8", "overwrite", owner, content.etag
            )
            return Mutation(
                changed.path,
                changed.size,
                changed.etag,
                unified_diff(requested, content.content, new),
                applied,
                rejected,
            )

    def _editable(self, workspace: Path, requested: str, if_match: str | None) -> FileContent:
        # Editing is bounded by the write limit, independently of a deployment's read page.
        relative = relative_path(workspace, requested)
        with parent_fd(workspace, relative) as (parent, name), open_file(parent, name) as handle:
            etag, binary, size = metadata(handle)
            check_match(if_match, etag)
            if binary or size > self._max_write:
                raise ValidationError(
                    "Editing requires a UTF-8 file within max_file_write_bytes",
                    size=size,
                    is_binary=binary,
                )
            text = handle.read().decode("utf-8")
        return FileContent(relative, size, 0, text, "utf-8", False, False, etag, size)

    def mkdir(self, workspace: Path, requested: str, owner: tuple[int, int] | None) -> str:
        """Create a directory and its parents without following links.

        Existing directories succeed.
        """
        relative = relative_path(workspace, requested)
        with self._lock, parent_fd(workspace, f"{relative}/.keep", create=True, owner=owner):
            pass
        return relative

    def delete(
        self,
        workspace: Path,
        requested: str,
        recursive: bool = False,
        if_match: str | None = None,
    ) -> str:
        """Delete a file or directory; deleting the workspace root is always refused."""
        relative = relative_path(workspace, requested)
        if relative == ".":
            raise ValidationError("Use environment reset to clear the workspace root")
        with self._lock, parent_fd(workspace, relative) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if if_match is not None:
                    raise ValidationError("Directory deletion does not support If-Match")
                if recursive:
                    shutil.rmtree(name, dir_fd=parent)
                else:
                    os.rmdir(name, dir_fd=parent)
            else:
                check_match(if_match, current_etag(parent, name))
                os.unlink(name, dir_fd=parent)
        return relative

    def transfer(
        self,
        workspace: Path,
        source: str,
        destination: str,
        move: bool,
        owner: tuple[int, int] | None,
        if_match: str | None = None,
    ) -> Mutation:
        """Copy or move a bounded regular file; existing destinations are never overwritten."""
        source_path = relative_path(workspace, source)
        destination_path = relative_path(workspace, destination)
        with self._lock, parent_fd(workspace, source_path) as (source_fd, source_name):
            with open_file(source_fd, source_name) as handle:
                etag, _, size = metadata(handle)
                check_match(if_match, etag)
                if size > self._max_write:
                    raise ValidationError("File exceeds max_file_write_bytes", size=size)
                data = handle.read()
            with parent_fd(workspace, destination_path, create=True, owner=owner) as (
                dest_fd,
                dest_name,
            ):
                try:
                    descriptor = os.open(
                        dest_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=dest_fd,
                    )
                except FileExistsError as exc:
                    raise ConflictError("Destination already exists; choose a new path") from exc
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    if owner is not None:
                        os.fchown(output.fileno(), *owner)
                if move:
                    check_match(etag, current_etag(source_fd, source_name))
                    os.unlink(source_name, dir_fd=source_fd)
        return Mutation(destination_path, size, etag)
