# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`terok_util.fs`][terok_util.fs] — directory + sensitive-file helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from terok_util.fs import ensure_dir, ensure_dir_writable, write_config_file, write_sensitive_file


class TestEnsureDir:
    """[`ensure_dir`][terok_util.fs.ensure_dir] is idempotent and creates parents."""

    def test_creates_missing_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c"
        ensure_dir(target)
        assert target.is_dir()

    def test_idempotent_on_existing_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "exists"
        target.mkdir()
        ensure_dir(target)  # must not raise
        assert target.is_dir()


class TestEnsureDirWritable:
    """[`ensure_dir_writable`][terok_util.fs.ensure_dir_writable] raises ``SystemExit`` on failure."""

    def test_creates_and_returns(self, tmp_path: Path) -> None:
        target = tmp_path / "subdir"
        ensure_dir_writable(target, label="data")
        assert target.is_dir()
        assert os.access(target, os.W_OK | os.X_OK)

    def test_existing_directory_passes(self, tmp_path: Path) -> None:
        target = tmp_path / "already-here"
        target.mkdir()
        ensure_dir_writable(target, label="data")  # no raise

    def test_path_is_a_file_exits(self, tmp_path: Path) -> None:
        """A regular file at the target path triggers the mkdir-fails branch.

        ``mkdir(exist_ok=True)`` raises ``FileExistsError`` when the path
        is a non-directory, which the helper wraps as ``SystemExit`` via
        the catch-all in the first block.  The message names the label
        and the path so the operator can find the offending file.
        """
        target = tmp_path / "not-a-dir"
        target.write_text("hello")
        with pytest.raises(SystemExit, match="not writable"):
            ensure_dir_writable(target, label="data")

    def test_unwritable_dir_exits(self, tmp_path: Path) -> None:
        """A directory with no write bit is reported with the chown-fix hint."""
        if os.geteuid() == 0:
            pytest.skip("root bypasses mode checks; test only meaningful as non-root")
        target = tmp_path / "ro"
        target.mkdir()
        os.chmod(target, 0o555)
        try:
            with pytest.raises(SystemExit, match="not writable"):
                ensure_dir_writable(target, label="data")
        finally:
            # Restore so pytest can clean up tmp_path.
            os.chmod(target, 0o755)


class TestEnsureDirWritablePrivate:
    """``private=True`` creates the missing directories ``0o700``."""

    def test_creates_missing_directories_private(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b"
        ensure_dir_writable(target, label="project", private=True)
        assert _mode(tmp_path / "a") == 0o700
        assert _mode(target) == 0o700

    def test_existing_directory_keeps_its_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "shared"
        target.mkdir()
        os.chmod(target, 0o755)
        ensure_dir_writable(target, label="project", private=True)
        assert _mode(target) == 0o755


class TestWriteConfigFile:
    """[`write_config_file`][terok_util.fs.write_config_file] writes operator config."""

    def test_new_file_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "terok" / "projects" / "demo" / "project.yml"
        write_config_file(path, "project:\n  name: demo\n")
        assert path.read_text() == "project:\n  name: demo\n"
        assert _mode(path) == 0o600
        for directory in (tmp_path / "terok", path.parent.parent, path.parent):
            assert _mode(directory) == 0o700

    def test_existing_file_keeps_its_mode(self, tmp_path: Path) -> None:
        """The operator's permissions stay; only the content changes."""
        path = tmp_path / "config.yml"
        path.write_text("old: 1\n")
        os.chmod(path, 0o644)
        write_config_file(path, "new: 2\n")
        assert path.read_text() == "new: 2\n"
        assert _mode(path) == 0o644

    def test_existing_directory_keeps_its_mode(self, tmp_path: Path) -> None:
        os.chmod(tmp_path, 0o755)
        write_config_file(tmp_path / "config.yml", "a: 1\n")
        assert _mode(tmp_path) == 0o755

    def test_symlink_keeps_pointing_at_its_target(self, tmp_path: Path) -> None:
        """A dotfiles-managed config stays a link; the target gets the new text."""
        target = tmp_path / "dotfiles" / "config.yml"
        target.parent.mkdir()
        target.write_text("old: 1\n")
        link = tmp_path / "config.yml"
        link.symlink_to(target)
        write_config_file(link, "new: 2\n")
        assert link.is_symlink()
        assert target.read_text() == "new: 2\n"

    def test_dangling_symlink_creates_its_target_private(self, tmp_path: Path) -> None:
        target = tmp_path / "dotfiles" / "config.yml"
        target.parent.mkdir()
        link = tmp_path / "config.yml"
        link.symlink_to(target)
        write_config_file(link, "a: 1\n")
        assert link.is_symlink()
        assert target.read_text() == "a: 1\n"
        assert _mode(target) == 0o600

    def test_hard_link_sees_the_new_text(self, tmp_path: Path) -> None:
        """The file is rewritten in place, not replaced, so it stays the same file."""
        path = tmp_path / "config.yml"
        path.write_text("old: 1\n")
        twin = tmp_path / "twin.yml"
        os.link(path, twin)
        write_config_file(path, "new: 2\n")
        assert twin.read_text() == "new: 2\n"


def _mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


class TestWriteSensitiveFile:
    """[`write_sensitive_file`][terok_util.fs.write_sensitive_file] writes with 0o600 + atomicity."""

    def test_creates_file_with_0600(self, tmp_path: Path) -> None:
        """New file gets mode 0o600."""
        target = tmp_path / "secrets" / "creds.json"
        assert write_sensitive_file(target, '{"key": "val"}\n') is True
        assert target.read_text() == '{"key": "val"}\n'
        assert oct(target.stat().st_mode & 0o777) == oct(0o600)

    def test_parent_dir_is_0700(self, tmp_path: Path) -> None:
        """Parent directory is hardened to 0o700."""
        target = tmp_path / "secrets" / "creds.json"
        write_sensitive_file(target, "{}\n")
        assert oct(target.parent.stat().st_mode & 0o777) == oct(0o700)

    def test_existing_file_not_overwritten(self, tmp_path: Path) -> None:
        """Returns False and leaves existing content untouched."""
        target = tmp_path / "existing.json"
        target.write_text("original")
        assert write_sensitive_file(target, "overwrite") is False
        assert target.read_text() == "original"

    def test_symlinked_parent_refused(self, tmp_path: Path) -> None:
        """Refuses to follow a symlinked parent directory."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        target = link_dir / "creds"
        with pytest.raises(RuntimeError, match="symlinked"):
            write_sensitive_file(target, "secret")
