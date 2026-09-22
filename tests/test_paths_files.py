from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from app.errors import NotFoundError, PathEscapeError, ValidationError
from app.files import FileService
from app.paths import relative_to_workspace, resolve_within


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    (ws / "sub" / "file.txt").write_text("hello")
    (ws / "etc-link").symlink_to("/etc")
    (ws / "passwd-link").symlink_to("/etc/passwd")
    (ws / "inner-link").symlink_to("sub")
    (ws / "dangling-out").symlink_to("/nonexistent/place")
    return ws


@pytest.mark.parametrize(
    "requested",
    [
        "../",
        "../../etc/passwd",
        "sub/../../x",
        "/etc/passwd",
        "/",
        "etc-link",
        "etc-link/passwd",
        "passwd-link",
        "dangling-out",
        "dangling-out/new.txt",
        "sub/../../ws-other",
        "a\0b",
    ],
)
def test_escapes_refused(workspace: Path, requested: str) -> None:
    with pytest.raises(PathEscapeError) as info:
        resolve_within(workspace, requested)
    assert info.value.extra["path"] == requested


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (".", "."),
        ("", "."),
        ("sub", "sub"),
        ("sub/file.txt", "sub/file.txt"),
        ("inner-link", "sub"),
        ("inner-link/file.txt", "sub/file.txt"),
        ("sub/../sub/file.txt", "sub/file.txt"),
        ("new/deep/file.txt", "new/deep/file.txt"),
    ],
)
def test_contained_paths_resolve(workspace: Path, requested: str, expected: str) -> None:
    resolved = resolve_within(workspace, requested)
    assert relative_to_workspace(workspace, resolved) == expected


def test_absolute_path_inside_workspace_is_allowed(workspace: Path) -> None:
    resolved = resolve_within(workspace, str(workspace / "sub"))
    assert relative_to_workspace(workspace, resolved) == "sub"


def test_symlink_planted_after_the_fact(workspace: Path) -> None:
    resolve_within(workspace, "later")
    (workspace / "later").symlink_to("/etc")
    with pytest.raises(PathEscapeError):
        resolve_within(workspace, "later/passwd")


@pytest.fixture
def files() -> FileService:
    return FileService(max_read_bytes=8, max_write_bytes=32)


def test_list_dir(workspace: Path, files: FileService) -> None:
    entries = {e.name: e for e in files.list_dir(workspace, ".")}
    assert entries["sub"].kind == "directory"
    assert entries["etc-link"].kind == "symlink"
    assert entries["sub"].path == "sub"
    inner = files.list_dir(workspace, "sub")
    assert [e.path for e in inner] == ["sub/file.txt"]
    assert inner[0].kind == "file" and inner[0].size == 5
    os.mkfifo(workspace / "fifo")
    assert {e.name: e.kind for e in files.list_dir(workspace, "")}["fifo"] == "other"
    with pytest.raises(NotFoundError):
        files.list_dir(workspace, "sub/file.txt")
    with pytest.raises(PathEscapeError):
        files.list_dir(workspace, "etc-link")


def test_list_dir_skips_vanishing_entries(
    workspace: Path, files: FileService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that disappears between listing and stat is skipped, not a 500.

    The listing works from a directory descriptor with ``os.stat(name, dir_fd=...)``, so
    that is what vanishes here. Patching ``Path.lstat`` would prove nothing: the listing
    never calls it, and the test would pass or fail on what the descriptor happened to see.
    """
    original = os.stat

    def flaky(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == "sub":
            raise OSError("gone")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", flaky)
    assert "sub" not in {e.name for e in files.list_dir(workspace, ".")}


def test_read_text_binary_and_truncation(workspace: Path, files: FileService) -> None:
    content = files.read(workspace, "sub/file.txt", offset=0, max_bytes=None)
    assert content.content == "hello" and content.encoding == "utf-8"
    assert not content.truncated and content.size == 5 and content.path == "sub/file.txt"
    (workspace / "big.txt").write_text("0123456789abcdef")
    big = files.read(workspace, "big.txt", offset=0, max_bytes=100)
    assert big.content == "01234567" and big.truncated
    tail = files.read(workspace, "big.txt", offset=8, max_bytes=4)
    assert tail.content == "89ab" and tail.truncated and tail.offset == 8
    end = files.read(workspace, "big.txt", offset=12, max_bytes=None)
    assert end.content == "cdef" and not end.truncated
    (workspace / "bin").write_bytes(b"\xff\xfe\x00")
    binary = files.read(workspace, "bin", offset=0, max_bytes=None)
    assert binary.encoding == "base64" and binary.content == "//4A"
    with pytest.raises(NotFoundError):
        files.read(workspace, "sub", offset=0, max_bytes=None)
    with pytest.raises(PathEscapeError):
        files.read(workspace, "passwd-link", offset=0, max_bytes=None)


def test_write_modes_and_limits(workspace: Path, files: FileService) -> None:
    assert files.write(workspace, "new/deep/a.txt", "abc", "utf-8", "overwrite", None) == 3
    assert (workspace / "new/deep/a.txt").read_text() == "abc"
    assert files.write(workspace, "new/deep/a.txt", "de", "utf-8", "append", None) == 5
    assert files.write(workspace, "new/deep/a.txt", "x", "utf-8", "overwrite", None) == 1
    assert files.write(workspace, "b.bin", "//4A", "base64", "overwrite", None) == 3
    assert (workspace / "b.bin").read_bytes() == b"\xff\xfe\x00"
    with pytest.raises(ValidationError, match="base64"):
        files.write(workspace, "c", "not base64!", "base64", "overwrite", None)
    with pytest.raises(ValidationError, match="encoding"):
        files.write(workspace, "c", "x", "latin-1", "overwrite", None)
    with pytest.raises(ValidationError, match="max_file_write_bytes"):
        files.write(workspace, "c", "x" * 33, "utf-8", "overwrite", None)
    with pytest.raises(ValidationError, match="mode"):
        files.write(workspace, "c", "x", "utf-8", "truncate", None)
    with pytest.raises(ValidationError, match="directory"):
        files.write(workspace, "sub", "x", "utf-8", "overwrite", None)
    with pytest.raises(ValidationError, match="directory"):
        files.write(workspace, ".", "x", "utf-8", "overwrite", None)
    with pytest.raises(PathEscapeError):
        files.write(workspace, "../escape.txt", "x", "utf-8", "overwrite", None)
    with pytest.raises(PathEscapeError):
        files.write(workspace, "etc-link/evil", "x", "utf-8", "overwrite", None)


def test_write_sets_owner(workspace: Path, files: FileService) -> None:
    me = (os.getuid(), os.getgid())
    assert files.write(workspace, "owned/dir/f.txt", "hi", "utf-8", "overwrite", me) == 2
    assert (workspace / "owned").stat().st_uid == me[0]
    assert (workspace / "owned/dir/f.txt").stat().st_uid == me[0]
