"""The descriptor half of file editing: every refusal the service makes before it writes.

The happy path is swept in `test_api.py`. What is pinned here is each edge the model is
expected to hit and recover from: a stale validator, a window inside a character, a binary
file offered for editing, a destination that already exists, a directory deleted without
saying so. Every one of them is a sentence the model can act on, never a 500.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from app.errors import (
    ConflictError,
    NotFoundError,
    PathEscapeError,
    PreconditionError,
    ValidationError,
)
from app.file_safety import check_match, open_file, parent_fd, relative_path
from app.file_search import SearchResult, search
from app.files import FileService

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from typing import BinaryIO


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "docs" / "note.txt").write_text("alpha\nbeta\ngamma\n")
    (ws / "docs" / "other.txt").write_text("nothing here\n")
    (ws / "bin.dat").write_bytes(b"\x00\x01\x02")
    (ws / "accent.txt").write_bytes("héllo".encode())
    return ws


@pytest.fixture
def files() -> FileService:
    return FileService(max_read_bytes=1024, max_write_bytes=64)


# --------------------------------------------------------------------------------------
# Reading windows
# --------------------------------------------------------------------------------------


class TestReadWindows:
    def test_a_binary_file_comes_back_base64_and_says_so(
        self, workspace: Path, files: FileService
    ) -> None:
        content = files.read(workspace, "bin.dat", offset=0, max_bytes=None)
        assert content.is_binary is True
        assert content.encoding == "base64"
        assert content.content == "AAEC"

    def test_an_offset_inside_a_character_is_refused(
        self, workspace: Path, files: FileService
    ) -> None:
        """`h` is one byte, `é` is two; offset 2 lands between them."""
        with pytest.raises(ValidationError, match="inside a UTF-8 character"):
            files.read(workspace, "accent.txt", offset=2, max_bytes=None)

    def test_a_window_too_small_for_one_character_is_refused(
        self, workspace: Path, files: FileService
    ) -> None:
        with pytest.raises(ValidationError, match="at least one UTF-8 character"):
            files.read(workspace, "accent.txt", offset=1, max_bytes=1)

    def test_a_window_ends_on_a_character_boundary(
        self, workspace: Path, files: FileService
    ) -> None:
        """Three bytes from offset 1 is `é` plus half of nothing: the page ends at `é`."""
        content = files.read(workspace, "accent.txt", offset=1, max_bytes=3)
        assert content.content == "él"
        assert content.next_offset == 4
        assert content.truncated is True

    def test_a_directory_is_not_a_file(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(NotFoundError, match="regular file"):
            files.read(workspace, "docs", offset=0, max_bytes=None)


# --------------------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------------------


class TestListing:
    def test_a_file_is_not_a_directory(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(NotFoundError, match="not a directory"):
            files.list_dir(workspace, "bin.dat")

    def test_depth_descends_and_a_glob_matches_either_the_name_or_the_path(
        self, workspace: Path, files: FileService
    ) -> None:
        by_name = {e.path for e in files.list_dir(workspace, ".", glob="*.txt", depth=1)}
        assert by_name == {"accent.txt", "docs/note.txt", "docs/other.txt"}
        by_path = {e.path for e in files.list_dir(workspace, ".", glob="docs/*.txt", depth=1)}
        assert by_path == {"docs/note.txt", "docs/other.txt"}

    def test_depth_zero_does_not_descend(self, workspace: Path, files: FileService) -> None:
        assert {e.path for e in files.list_dir(workspace, ".", depth=0)} == {
            "accent.txt",
            "bin.dat",
            "docs",
        }


# --------------------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------------------


class TestEditing:
    def test_a_binary_file_cannot_be_edited(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="UTF-8 file") as info:
            files.edit(workspace, "bin.dat", "a", "b", owner=None)
        assert info.value.extra["is_binary"] is True

    def test_a_file_over_the_write_limit_cannot_be_edited(
        self, workspace: Path, files: FileService
    ) -> None:
        (workspace / "big.txt").write_text("x" * 65)
        with pytest.raises(ValidationError, match="max_file_write_bytes") as info:
            files.edit(workspace, "big.txt", "x", "y", owner=None)
        assert info.value.extra["size"] == 65

    def test_a_stale_validator_refuses_the_edit_before_anything_changes(
        self, workspace: Path, files: FileService
    ) -> None:
        with pytest.raises(PreconditionError):
            files.edit(workspace, "docs/note.txt", "beta", "BETA", owner=None, if_match='"stale"')
        assert (workspace / "docs" / "note.txt").read_text() == "alpha\nbeta\ngamma\n"

    def test_an_ambiguous_edit_names_the_lines_and_changes_nothing(
        self, workspace: Path, files: FileService
    ) -> None:
        (workspace / "dup.txt").write_text("same\nsame\n")
        with pytest.raises(ValidationError) as info:
            files.edit(workspace, "dup.txt", "same", "x", owner=None)
        assert info.value.extra["occurrences"] == [1, 2]
        assert (workspace / "dup.txt").read_text() == "same\nsame\n"

    def test_an_edit_returns_the_diff_and_a_fresh_validator(
        self, workspace: Path, files: FileService
    ) -> None:
        before = files.read(workspace, "docs/note.txt", offset=0, max_bytes=None).etag
        result = files.edit(workspace, "docs/note.txt", "beta", "BETA", owner=None, if_match=before)
        assert "-beta" in result.diff
        assert "+BETA" in result.diff
        assert result.etag != before
        assert result.size == len("alpha\nBETA\ngamma\n")

    def test_a_patch_reports_which_hunks_landed_and_which_did_not(
        self, workspace: Path, files: FileService
    ) -> None:
        patch = "@@ -1 +1 @@\n-zzz\n+ZZZ\n@@ -3 +3 @@\n-gamma\n+GAMMA\n"
        result = files.patch(workspace, "docs/note.txt", patch, owner=None)
        assert result.applied_hunks == [2]
        assert result.rejected_hunks == [1]
        assert (workspace / "docs" / "note.txt").read_text() == "alpha\nbeta\nGAMMA\n"


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


class TestWriting:
    def test_an_unknown_mode_is_refused(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="unsupported mode"):
            files.write_result(workspace, "x.txt", "a", "utf-8", "truncate", owner=None)

    def test_an_unknown_encoding_is_refused(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="unsupported encoding"):
            files.write_result(workspace, "x.txt", "a", "latin-1", "overwrite", owner=None)

    def test_bad_base64_is_refused(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="not valid base64"):
            files.write_result(workspace, "x.txt", "!!!", "base64", "overwrite", owner=None)

    def test_content_over_the_limit_is_refused(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="exceeds max_file_write_bytes") as info:
            files.write_result(workspace, "x.txt", "x" * 65, "utf-8", "overwrite", owner=None)
        assert info.value.extra["limit"] == 64

    def test_writing_to_a_directory_path_is_refused(
        self, workspace: Path, files: FileService
    ) -> None:
        with pytest.raises(ValidationError, match="is a directory"):
            files.write_result(workspace, "docs", "a", "utf-8", "overwrite", owner=None)

    def test_append_keeps_what_was_there_and_checks_the_validator_against_it(
        self, workspace: Path, files: FileService
    ) -> None:
        first = files.write_result(workspace, "log.txt", "one\n", "utf-8", "overwrite", owner=None)
        with pytest.raises(PreconditionError):
            files.write_result(
                workspace, "log.txt", "two\n", "utf-8", "append", owner=None, if_match='"stale"'
            )
        appended = files.write_result(
            workspace, "log.txt", "two\n", "utf-8", "append", owner=None, if_match=first.etag
        )
        assert (workspace / "log.txt").read_text() == "one\ntwo\n"
        assert appended.size == 8

    def test_append_to_a_missing_file_creates_it(self, workspace: Path, files: FileService) -> None:
        files.write_result(workspace, "fresh.txt", "a", "utf-8", "append", owner=None)
        assert (workspace / "fresh.txt").read_text() == "a"

    def test_the_legacy_write_returns_the_size(self, workspace: Path, files: FileService) -> None:
        assert files.write(workspace, "s.txt", "abc", "utf-8", "overwrite", owner=None) == 3

    def test_a_write_that_races_a_shell_is_refused(
        self, workspace: Path, files: FileService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Between reading the validator and replacing the file, something else wrote it."""
        calls = {"n": 0}

        def racing(expected: str | None, actual: str | None) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                (workspace / "race.txt").write_text("someone else")
                raise PreconditionError("changed underneath")
            check_match(expected, actual)

        (workspace / "race.txt").write_text("mine")
        monkeypatch.setattr("app.files.check_match", racing)
        with pytest.raises(PreconditionError):
            files.write_result(workspace, "race.txt", "new", "utf-8", "overwrite", owner=None)
        assert (workspace / "race.txt").read_text() == "someone else"
        assert not [p for p in workspace.iterdir() if p.name.startswith(".lucy-write-")], (
            "the temporary file was cleaned up"
        )


# --------------------------------------------------------------------------------------
# Directories, deletion and transfer
# --------------------------------------------------------------------------------------


class TestMutations:
    def test_mkdir_creates_parents_and_tolerates_an_existing_directory(
        self, workspace: Path, files: FileService
    ) -> None:
        assert files.mkdir(workspace, "a/b/c", owner=None) == "a/b/c"
        assert (workspace / "a" / "b" / "c").is_dir()
        assert files.mkdir(workspace, "a/b/c", owner=None) == "a/b/c"

    def test_deleting_the_root_is_always_refused(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="environment reset"):
            files.delete(workspace, ".")

    def test_a_directory_needs_recursive_to_go_when_it_is_not_empty(
        self, workspace: Path, files: FileService
    ) -> None:
        with pytest.raises(ValidationError):
            files.delete(workspace, "docs")
        assert (workspace / "docs").is_dir()
        assert files.delete(workspace, "docs", recursive=True) == "docs"
        assert not (workspace / "docs").exists()

    def test_an_empty_directory_goes_without_recursive(
        self, workspace: Path, files: FileService
    ) -> None:
        (workspace / "empty").mkdir()
        assert files.delete(workspace, "empty") == "empty"

    def test_a_directory_does_not_take_if_match(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="If-Match"):
            files.delete(workspace, "docs", if_match='"x"')

    def test_a_file_delete_checks_its_validator(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(PreconditionError):
            files.delete(workspace, "bin.dat", if_match='"stale"')
        assert (workspace / "bin.dat").exists()

    def test_copy_never_overwrites_a_destination(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ConflictError, match="already exists"):
            files.transfer(workspace, "docs/note.txt", "docs/other.txt", move=False, owner=None)
        assert (workspace / "docs" / "other.txt").read_text() == "nothing here\n"

    def test_copy_refuses_a_file_over_the_write_limit(
        self, workspace: Path, files: FileService
    ) -> None:
        (workspace / "big.txt").write_text("x" * 65)
        with pytest.raises(ValidationError, match="exceeds max_file_write_bytes"):
            files.transfer(workspace, "big.txt", "copy.txt", move=False, owner=None)

    def test_copy_checks_the_validator_of_the_source(
        self, workspace: Path, files: FileService
    ) -> None:
        with pytest.raises(PreconditionError):
            files.transfer(
                workspace, "docs/note.txt", "copy.txt", move=False, owner=None, if_match='"stale"'
            )

    def test_move_creates_parents_and_removes_the_source(
        self, workspace: Path, files: FileService
    ) -> None:
        result = files.transfer(
            workspace, "docs/note.txt", "archive/deep/note.txt", move=True, owner=None
        )
        assert result.path == "archive/deep/note.txt"
        assert (workspace / "archive" / "deep" / "note.txt").read_text() == "alpha\nbeta\ngamma\n"
        assert not (workspace / "docs" / "note.txt").exists()

    def test_a_transfer_gives_the_copy_to_the_workspace_owner(
        self, workspace: Path, files: FileService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sandbox user, not the service, must own what the service writes for it."""
        owned: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: owned.append((uid, gid)))
        monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
        files.transfer(workspace, "docs/note.txt", "out/copy.txt", move=False, owner=(1000, 1000))
        assert owned == [(1000, 1000)]

    def test_transfer_from_a_directory_is_refused(
        self, workspace: Path, files: FileService
    ) -> None:
        with pytest.raises(NotFoundError, match="regular file"):
            files.transfer(workspace, "docs", "elsewhere", move=False, owner=None)


# --------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------


def run_search(files: FileService, workspace: Path, pattern: str, **overrides: Any) -> SearchResult:
    options: dict[str, Any] = {
        "path": ".",
        "glob": None,
        "depth": 5,
        "mode": "content",
        "limit": 100,
        "before": 0,
        "after": 0,
        "max_file_bytes": 1024,
    }
    options.update(overrides)
    return search(files, workspace, pattern=pattern, **options)


class TestSearch:
    def test_an_empty_pattern_is_refused(self, workspace: Path, files: FileService) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            run_search(files, workspace, "")

    def test_binary_and_oversized_files_are_skipped_and_named(
        self, workspace: Path, files: FileService
    ) -> None:
        (workspace / "huge.txt").write_text("beta " * 300)
        result = run_search(files, workspace, "beta", max_file_bytes=100)
        assert result.skipped_binary == ["bin.dat"]
        assert result.skipped_large == ["huge.txt"]
        assert [m.path for m in result.matches] == ["docs/note.txt"]

    def test_content_mode_carries_context_lines_and_marks_the_match(
        self, workspace: Path, files: FileService
    ) -> None:
        result = run_search(files, workspace, "beta", before=1, after=1)
        (match,) = result.matches
        assert [(line.line, line.text, line.matched) for line in match.lines] == [
            (1, "alpha", False),
            (2, "beta", True),
            (3, "gamma", False),
        ]

    def test_files_mode_reports_each_file_once(self, workspace: Path, files: FileService) -> None:
        (workspace / "docs" / "note.txt").write_text("beta\nbeta\nbeta\n")
        result = run_search(files, workspace, "beta", mode="files_with_matches")
        assert [m.path for m in result.matches] == ["docs/note.txt"]
        assert result.matches[0].lines == []
        assert result.truncated is False

    def test_the_limit_is_honest_about_what_it_left_out(
        self, workspace: Path, files: FileService
    ) -> None:
        (workspace / "docs" / "note.txt").write_text("beta\nbeta\nbeta\n")
        result = run_search(files, workspace, "beta", limit=2)
        assert len(result.matches) == 2
        assert result.total_matches == 3
        assert result.truncated is True

    def test_a_long_line_is_clipped_and_its_true_length_kept(
        self, workspace: Path, files: FileService
    ) -> None:
        (workspace / "long.txt").write_text("beta" + "x" * 3000 + "\n")
        result = run_search(files, workspace, "beta", glob="long.txt", max_file_bytes=10_000)
        (line,) = result.matches[0].lines
        assert len(line.text) == 2000
        assert line.original_chars == 3004

    def test_a_file_that_vanishes_mid_search_is_named_not_fatal(
        self, workspace: Path, files: FileService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def vanishing(descriptor: int, name: str) -> AbstractContextManager[BinaryIO]:
            if name == "other.txt":
                raise NotFoundError("gone")
            return open_file(descriptor, name)

        monkeypatch.setattr("app.file_search.open_file", vanishing)
        result = run_search(files, workspace, "nothing")
        assert result.skipped_unavailable == ["docs/other.txt"]
        assert result.matches == []


# --------------------------------------------------------------------------------------
# Descriptor safety
# --------------------------------------------------------------------------------------


class TestDescriptorSafety:
    def test_a_symlink_anywhere_in_the_spelling_is_refused_even_when_it_stays_inside(
        self, workspace: Path
    ) -> None:
        """Resolving first must not turn an in-root link into permission to follow it."""
        (workspace / "alias").symlink_to("docs")
        with pytest.raises(PathEscapeError, match="symbolic links"):
            relative_path(workspace, "alias/note.txt")

    def test_a_missing_parent_is_not_found(self, workspace: Path) -> None:
        with pytest.raises(NotFoundError, match="does not exist"), parent_fd(workspace, "nope/x"):
            pass

    def test_a_parent_that_is_a_file_is_refused_as_a_validation_error(
        self, workspace: Path
    ) -> None:
        with pytest.raises(ValidationError, match="refused"), parent_fd(workspace, "bin.dat/x"):
            pass

    def test_create_makes_parents_and_chowns_them_when_an_owner_is_given(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chowned: list[tuple[str, int, int]] = []

        def record(path: str, uid: int, gid: int, **kwargs: object) -> None:
            chowned.append((path, uid, gid))

        monkeypatch.setattr(os, "chown", record)
        with parent_fd(workspace, "made/deeper/file", create=True, owner=(1000, 1000)) as (_, name):
            assert name == "file"
        assert (workspace / "made" / "deeper").is_dir()
        assert chowned == [("made", 1000, 1000), ("deeper", 1000, 1000)]
