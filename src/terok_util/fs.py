# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Filesystem helpers for directory creation, writability checks, and config and secret files."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def ensure_dir(path: Path) -> None:
    """Create a directory (and parents) if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)


def ensure_dir_writable(path: Path, label: str, *, private: bool = False) -> None:
    """Create *path* if needed and verify it is writable, or exit with an error.

    With *private*, the directories this creates get mode ``0o700``;
    directories that exist already keep theirs.
    """
    try:
        if private:
            _make_private_dirs(path)
        else:
            path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise SystemExit(f"{label} directory is not writable: {path} ({e})")
    if not path.is_dir():
        raise SystemExit(f"{label} path is not a directory: {path}")
    if not os.access(path, os.W_OK | os.X_OK):
        uid = os.getuid()
        gid = os.getgid()
        raise SystemExit(
            f"{label} directory is not writable: {path}\n"
            f"Fix permissions for the user running this process (uid={uid}, gid={gid}). "
            f"Example: sudo chown -R {uid}:{gid} {path}"
        )


def write_config_file(path: Path, text: str) -> None:
    """Write *text* to the operator-owned config file at *path*.

    An existing file is rewritten in place, so what the operator set up
    stays: a symlink still points where it did, and the file keeps its
    mode and owner.  A new file is created with mode ``0o600``, and the
    directories created for it with ``0o700``, so config is private by
    default.
    """
    _make_private_dirs(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())


def _make_private_dirs(path: Path) -> None:
    """Create *path* and its missing parents with mode ``0o700``; existing ones keep theirs."""
    missing = []
    for directory in (path, *path.parents):
        if directory.exists():
            break
        missing.append(directory)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


def write_sensitive_file(path: Path, content: str) -> bool:
    """Atomically create *path* with mode ``0o600`` and write *content*.

    Returns ``True`` if the file was created, ``False`` if it already existed.
    Parent directories are created with mode ``0o700``.

    Refuses to operate if *path.parent* is a symbolic link — chmod would
    otherwise follow the link target.  Opens the file with ``O_NOFOLLOW``
    so a planted symlink at the final path cannot redirect the write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked directory for sensitive file: {path.parent}")
    os.chmod(path.parent, 0o700)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    except FileExistsError:
        return False
    try:
        # ``os.write`` may write fewer bytes than requested; loop until the
        # whole buffer lands.  On any failure, unlink the partially-written
        # file so the next call sees a clean state rather than a half-baked
        # secret.
        view = memoryview(content.encode())
        while view:
            view = view[os.write(fd, view) :]
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        raise
    finally:
        os.close(fd)
    return True


__all__ = ["ensure_dir", "ensure_dir_writable", "write_config_file", "write_sensitive_file"]
