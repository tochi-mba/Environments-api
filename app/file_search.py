"""Bounded literal text search with explicit skipped-file and truncation metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.errors import NotFoundError, ValidationError
from app.file_safety import metadata, open_file, parent_fd

if TYPE_CHECKING:
    from app.files import FileService


@dataclass(frozen=True, slots=True)
class SearchLine:
    """One matching or surrounding line, with a count when its text is clipped."""

    line: int
    text: str
    matched: bool
    original_chars: int


@dataclass(frozen=True, slots=True)
class SearchMatch:
    """One matching file and, in content mode, one matched line with context."""

    path: str
    lines: list[SearchLine]


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A search result whose totals and skipped files make every limit explicit."""

    matches: list[SearchMatch]
    total_matches: int
    truncated: bool
    skipped_binary: list[str]
    skipped_large: list[str]
    skipped_unavailable: list[str]


def search(
    files: FileService,
    workspace: Path,
    path: str,
    pattern: str,
    glob: str | None,
    depth: int,
    mode: Literal["files_with_matches", "content"],
    limit: int,
    before: int,
    after: int,
    max_file_bytes: int,
) -> SearchResult:
    """Search literal text; regular expressions cannot consume unbounded worker time."""
    if not pattern:
        raise ValidationError("pattern must not be empty")
    matches: list[SearchMatch] = []
    binary_paths: list[str] = []
    large_paths: list[str] = []
    unavailable: list[str] = []
    total = 0
    for entry in files.list_dir(workspace, path, glob, depth):
        if entry.kind != "file":
            continue
        try:
            with (
                parent_fd(workspace, entry.path) as (parent, name),
                open_file(parent, name) as handle,
            ):
                _, binary, size = metadata(handle)
                if binary:
                    binary_paths.append(entry.path)
                    continue
                if size > max_file_bytes:
                    large_paths.append(entry.path)
                    continue
                lines = handle.read().decode("utf-8").splitlines()
        except (NotFoundError, ValidationError, UnicodeDecodeError):
            unavailable.append(entry.path)
            continue
        for index, line in enumerate(lines):
            if pattern not in line:
                continue
            total += 1
            if len(matches) < limit:
                context = []
                if mode == "content":
                    context = [
                        SearchLine(i + 1, lines[i][:2000], i == index, len(lines[i]))
                        for i in range(max(0, index - before), min(len(lines), index + after + 1))
                    ]
                matches.append(SearchMatch(entry.path, context))
            if mode == "files_with_matches":
                break
    return SearchResult(
        matches, total, total > len(matches), binary_paths, large_paths, unavailable
    )
