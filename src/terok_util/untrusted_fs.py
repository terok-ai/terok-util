# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Read and replace files in directories an untrusted task can modify.

Callers choose the path; these helpers do not accept an attacker-selected
absolute path as authorization. Every directory component is opened without
following links and held by descriptor. Reads are bounded and require a
single-link regular file. Writes publish a fresh inode instead of modifying
an existing one. Linux/POSIX descriptor-relative operations are required.

These operations prevent link redirection, not concurrent task edits. A task
can still replace its own files after publication. Keep authoritative host
state outside task-writable mounts.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path


def read_untrusted_text(path: Path, *, max_bytes: int, encoding: str = "utf-8") -> str:
    """Read at most *max_bytes* from a plain file, then decode it.

    Missing files raise ``FileNotFoundError``. Unsafe paths and oversized
    inputs raise [`UnsafePathError`][terok_util.untrusted_fs.UnsafePathError].
    Decoding errors propagate to the caller.
    """
    return read_untrusted_bytes(path, max_bytes=max_bytes).decode(encoding)


def read_untrusted_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read a bounded single-link regular file without following any path links."""
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    with open_untrusted_directory(path.parent) as parent:
        fd = _open_file(parent, path.name, os.O_RDONLY)
        try:
            _require_regular(os.fstat(fd), path)
            with os.fdopen(fd, "rb", closefd=False) as source:
                content = source.read(max_bytes + 1)
        finally:
            os.close(fd)
    if len(content) > max_bytes:
        raise UnsafePathError(f"File exceeds {max_bytes} bytes: {path}")
    return content


def write_untrusted_text(
    path: Path, text: str, *, mode: int = 0o600, encoding: str = "utf-8"
) -> None:
    """Encode *text* and atomically replace *path* with a fresh private inode."""
    write_untrusted_bytes(path, text.encode(encoding), mode=mode)


def write_untrusted_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Publish *content* without truncating or appending to a task-owned inode.

    Missing parents are created with mode ``0o700``. An observed symlink,
    non-regular file or hardlink at the destination is refused. A subsequent
    leaf replacement cannot redirect the write: the final rename replaces
    the directory entry itself. Only the new inode receives *mode*.
    """
    with open_untrusted_directory(path.parent, create=True) as parent:
        _check_destination(parent, path)
        name = f".terok-{secrets.token_hex(16)}.tmp"
        fd = _open_file(parent, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(content)
                target.flush()
                os.fchmod(target.fileno(), mode)
                os.fsync(target.fileno())
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=parent)


@contextlib.contextmanager
def untrusted_file_lock(path: Path) -> Iterator[None]:
    """Lock a plain file for cooperating host writers without following links.

    The file is created privately if missing and is never truncated. This
    advisory lock does not exclude a malicious task that ignores the lock
    or replaces its directory entry.
    """
    with open_untrusted_directory(path.parent, create=True) as parent:
        fd = _open_file(parent, path.name, os.O_RDWR | os.O_CREAT)
        try:
            _require_regular(os.fstat(fd), path)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def remove_untrusted_file(path: Path, *, missing_ok: bool = True) -> None:
    """Unlink a plain leaf through its pinned parent, never through an ancestor link.

    An observed symlink, hardlink or special file is refused. With
    *missing_ok*, an absent parent or leaf is already the requested result.
    """
    try:
        with open_untrusted_directory(path.parent) as parent:
            _check_destination(parent, path)
            os.unlink(path.name, dir_fd=parent)
    except FileNotFoundError:
        if not missing_ok:
            raise


@contextlib.contextmanager
def open_untrusted_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Hold a directory descriptor reached without following any ancestor link.

    Relative paths start at the process's current directory. Parent traversal
    components are refused. With *create*, missing directories get ``0o700``;
    existing directories retain their permissions. The yielded descriptor is
    borrowed and closes when the context exits.
    """
    if ".." in path.parts:
        raise UnsafePathError(f"Parent traversal is not allowed: {path}")
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with contextlib.ExitStack() as held:
        fd = os.open(absolute.anchor, flags)
        held.callback(os.close, fd)
        for component in absolute.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=fd)
            try:
                fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                _raise_unsafe_link(exc, path)
                raise
            held.callback(os.close, fd)
        yield fd


class UnsafePathError(OSError):
    """A task-owned input is linked, not regular, oversized or traverses a parent."""


def _open_file(parent: int, name: str, flags: int) -> int:
    """Open one leaf without following links or blocking on a FIFO."""
    if not name or name in (".", ".."):
        raise UnsafePathError(f"Expected a file name: {name!r}")
    try:
        return os.open(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=parent
        )
    except OSError as exc:
        _raise_unsafe_link(exc, Path(name))
        raise


def _raise_unsafe_link(exc: OSError, path: Path) -> None:
    """Give link and non-directory refusals one public exception type."""
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.EISDIR):
        raise UnsafePathError(f"Refusing unsafe path: {path}") from exc


def _require_regular(identity: os.stat_result, path: Path) -> None:
    """Refuse special files and multiply linked inodes."""
    if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
        raise UnsafePathError(f"Expected a single-link regular file: {path}")


def _check_destination(parent: int, path: Path) -> None:
    """Refuse an already unsafe leaf without opening it for modification."""
    try:
        identity = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    _require_regular(identity, path)


__all__ = [
    "UnsafePathError",
    "open_untrusted_directory",
    "read_untrusted_bytes",
    "read_untrusted_text",
    "remove_untrusted_file",
    "untrusted_file_lock",
    "write_untrusted_bytes",
    "write_untrusted_text",
]
