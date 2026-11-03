# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Read bounded workload files without following links or waiting on devices."""

from __future__ import annotations

import os
import stat
from pathlib import Path

MAX_RESULT_BYTES = 1024 * 1024


def slot_output_dir(workspace: Path, slot: str) -> Path:
    """The only host directory a slot may write through its results mount."""
    return workspace / "outputs" / slot


def read_regular(path: Path, limit: int = MAX_RESULT_BYTES) -> str:
    """Read a small regular UTF-8 file; reject symlinks in every path component.

    Raises:
        OSError: The path is absent, linked, special, or exceeds the size limit.
        UnicodeError: The file is not UTF-8.
    """
    path = path.absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise OSError(f"Not a bounded regular file: {path}")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise OSError(f"File exceeds {limit} bytes: {path}")
    return content.decode("utf-8")
