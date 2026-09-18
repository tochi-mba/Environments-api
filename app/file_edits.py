"""Exact content edits and validated single-file unified patches."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from app.errors import ValidationError

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)(?:\n)?$")


@dataclass(frozen=True, slots=True)
class Hunk:
    """A unified hunk with validated line counts."""

    start: int
    old: list[str]
    new: list[str]


def unified_diff(path: str, old: str, new: str) -> str:
    """Return an independently reviewable unified diff of one file."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), path, path
        )
    )


def replace_unique(old: str, before: str, after: str) -> str:
    """Replace exactly one occurrence; ambiguity never changes a file."""
    if not before:
        raise ValidationError("old_string must not be empty")
    starts = [match.start() for match in re.finditer(re.escape(before), old)]
    if len(starts) != 1:
        lines = [old.count("\n", 0, start) + 1 for start in starts]
        raise ValidationError(
            "No replacement was performed; old_string must match exactly once. "
            "Read the file again and include enough surrounding context.",
            occurrences=lines,
        )
    return old.replace(before, after, 1)


def parse_patch(patch: str) -> list[Hunk]:
    """Parse hunks before writing anything; filenames are labels, never filesystem paths."""
    lines = patch.splitlines(keepends=True)
    if len(lines) >= 2 and lines[0].startswith("--- ") and lines[1].startswith("+++ "):
        lines = lines[2:]
    hunks: list[Hunk] = []
    index = 0
    previous_end = 0
    while index < len(lines):
        match = _HEADER.match(lines[index])
        if match is None:
            raise ValidationError("Expected a unified diff hunk header; submit one file per patch")
        start, old_count, _, new_count = match.groups()
        old_size = int(old_count) if old_count is not None else 1
        new_size = int(new_count) if new_count is not None else 1
        position = int(start) if old_size == 0 else int(start) - 1
        if position < previous_end or position < 0:
            raise ValidationError("Patch hunks must be ordered and must not overlap")
        index += 1
        old: list[str] = []
        new: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith("\\ No newline at end of file"):
                raise ValidationError("A no-newline marker must immediately follow a patch line")
            if not line or line[0] not in " +-":
                raise ValidationError("Every patch line must start with space, plus or minus")
            payload = line[1:]
            if index + 1 < len(lines) and lines[index + 1].startswith(
                "\\ No newline at end of file"
            ):
                payload = payload.removesuffix("\n")
                index += 1
            if line[0] in " -":
                old.append(payload)
            if line[0] in " +":
                new.append(payload)
            index += 1
        if len(old) != old_size or len(new) != new_size:
            raise ValidationError("Patch hunk counts do not match its content")
        hunks.append(Hunk(position, old, new))
        previous_end = position + old_size
    if not hunks:
        raise ValidationError("The patch contains no hunks")
    return hunks


def apply_patch(text: str, patch: str) -> tuple[str, list[int], list[int]]:
    """Apply matching hunks and report unmatched hunk numbers (one-indexed)."""
    hunks = parse_patch(patch)
    source = text.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    applied: list[int] = []
    rejected: list[int] = []
    for number, hunk in enumerate(hunks, 1):
        if hunk.start > len(source) or source[hunk.start : hunk.start + len(hunk.old)] != hunk.old:
            rejected.append(number)
            continue
        output.extend(source[cursor : hunk.start])
        output.extend(hunk.new)
        cursor = hunk.start + len(hunk.old)
        applied.append(number)
    output.extend(source[cursor:])
    return "".join(output), applied, rejected
