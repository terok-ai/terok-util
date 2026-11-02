# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Task-owned filesystem entries cannot redirect host reads or writes."""

import os
import stat
from pathlib import Path

import pytest

from terok_util import (
    UnsafePathError,
    open_untrusted_directory,
    read_untrusted_bytes,
    read_untrusted_text,
    untrusted_file_lock,
    write_untrusted_bytes,
    write_untrusted_text,
)


def test_round_trip_creates_private_parents_and_replaces_inode(tmp_path: Path) -> None:
    """A replacement changes the inode, not the old file's contents or mode."""
    path = tmp_path / "task" / "config" / "file"
    write_untrusted_text(path, "first")
    with path.open() as old:
        write_untrusted_text(path, "second", mode=0o700)
        assert old.read() == "first"
        assert os.fstat(old.fileno()).st_ino != path.stat().st_ino
        assert stat.S_IMODE(os.fstat(old.fileno()).st_mode) == 0o600
    assert read_untrusted_text(path, max_bytes=6) == "second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
@pytest.mark.parametrize("operation", ["read", "write", "lock"])
def test_unsafe_leaf_is_refused(tmp_path: Path, kind: str, operation: str) -> None:
    """No operation follows a linked leaf or waits on a special file."""
    outside = tmp_path / "outside"
    outside.write_text("secret")
    path = tmp_path / "input"
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        path.hardlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    with pytest.raises(UnsafePathError):
        if operation == "read":
            read_untrusted_bytes(path, max_bytes=100)
        elif operation == "write":
            write_untrusted_bytes(path, b"changed")
        else:
            with untrusted_file_lock(path):
                pytest.fail("unsafe lock was acquired")
    assert outside.read_text() == "secret"


@pytest.mark.parametrize("depth", [0, 1])
@pytest.mark.parametrize("operation", ["read", "write", "lock"])
def test_ancestor_symlink_is_refused(tmp_path: Path, depth: int, operation: str) -> None:
    """Every ancestor, not just the immediate parent, must be a directory."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("secret")
    link = tmp_path / "task"
    link.symlink_to(outside, target_is_directory=True)
    path = link / "file" if not depth else link / "nested" / "file"
    with pytest.raises(UnsafePathError):
        if operation == "read":
            read_untrusted_text(path, max_bytes=100)
        elif operation == "write":
            write_untrusted_text(path, "changed")
        else:
            with untrusted_file_lock(path):
                pytest.fail("unsafe lock was acquired")
    assert sorted(p.name for p in outside.iterdir()) == ["file"]
    assert (outside / "file").read_text() == "secret"


def test_bounded_read_and_encoding(tmp_path: Path) -> None:
    """Oversize input is refused before decoding; exact limits are accepted."""
    path = tmp_path / "file"
    write_untrusted_bytes(path, b"\xff" * 4)
    assert read_untrusted_bytes(path, max_bytes=4) == b"\xff" * 4
    with pytest.raises(UnsafePathError, match="exceeds"):
        read_untrusted_text(path, max_bytes=3)
    with pytest.raises(UnicodeDecodeError):
        read_untrusted_text(path, max_bytes=4)
    with pytest.raises(ValueError):
        read_untrusted_bytes(path, max_bytes=-1)
    assert read_untrusted_text(path, max_bytes=4, encoding="latin-1") == "ÿ" * 4


def test_sparse_input_is_bounded(tmp_path: Path) -> None:
    """A large logical size does not cause an unbounded allocation."""
    path = tmp_path / "file"
    with path.open("wb") as handle:
        handle.truncate(1 << 30)
    with pytest.raises(UnsafePathError, match="exceeds"):
        read_untrusted_bytes(path, max_bytes=1024)


def test_missing_input_and_parent_traversal(tmp_path: Path) -> None:
    """Absence remains distinguishable from an unsafe path."""
    with pytest.raises(FileNotFoundError):
        read_untrusted_bytes(tmp_path / "missing", max_bytes=10)
    with pytest.raises(UnsafePathError, match="traversal"):
        write_untrusted_text(tmp_path / ".." / "escape", "no")


def test_directory_descriptor_survives_ancestor_swap(tmp_path: Path, monkeypatch) -> None:
    """A late parent replacement cannot redirect publication to another tree."""
    parent = tmp_path / "task"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "file"
    victim.write_text("secret")
    original_fsync = os.fsync

    def swap_parent(fd: int) -> None:
        original_fsync(fd)
        parent.rename(tmp_path / "original")
        parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(os, "fsync", swap_parent)
    write_untrusted_text(parent / "file", "new")
    assert victim.read_text() == "secret"
    assert (tmp_path / "original" / "file").read_text() == "new"


def test_leaf_swap_does_not_follow_target(tmp_path: Path, monkeypatch) -> None:
    """Replacing the destination after its check does not mutate its target."""
    path = tmp_path / "file"
    outside = tmp_path / "outside"
    outside.write_text("secret")
    replace = os.replace

    def plant_link(src, dst, **kwargs) -> None:
        path.symlink_to(outside)
        replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", plant_link)
    write_untrusted_text(path, "new")
    assert not path.is_symlink()
    assert outside.read_text() == "secret"


def test_failed_write_preserves_old_file_and_cleans_temporary(tmp_path: Path, monkeypatch) -> None:
    """Failure before publication leaves the existing file unchanged."""
    path = tmp_path / "file"
    path.write_text("old")

    def fail(_fd: int) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk failure"):
        write_untrusted_text(path, "new")
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]


def test_directory_context_and_lock(tmp_path: Path, monkeypatch) -> None:
    """Relative directories and private lock files work without truncation."""
    monkeypatch.chdir(tmp_path)
    with open_untrusted_directory(Path("nested"), create=True) as fd:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    lock = tmp_path / "nested" / "lock"
    with untrusted_file_lock(lock):
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    lock.write_text("keep")
    with untrusted_file_lock(lock):
        assert lock.read_text() == "keep"
