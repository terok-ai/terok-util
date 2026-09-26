# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Find host executables from the caller's current absolute PATH entries.

This module uses only the standard library. Setup may copy its source into
standalone programs without introducing an installed-package dependency.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def host_path(path: str | None = None) -> str:
    """Keep absolute search directories, without resolving symlinks or adding defaults.

    ``None`` reads the current environment. An absent or explicitly empty PATH
    permits no name lookup; empty and relative entries never search the cwd.
    """
    path = os.environ.get("PATH", "") if path is None else path
    return os.pathsep.join(entry for entry in path.split(os.pathsep) if os.path.isabs(entry))


def find_host_tool(name: str, *, path: str | None = None) -> str | None:
    """Find an executable by name or an explicitly supplied absolute path.

    Relative paths containing a slash are rejected. Returned paths retain the
    spelling selected by PATH, including symlinks, and are never cached.
    """
    if os.path.dirname(name) and not os.path.isabs(name):
        return None
    return shutil.which(name, path=host_path(path))


def require_host_tool(name: str, *, path: str | None = None) -> str:
    """Find an executable or raise ``FileNotFoundError`` with the searched PATH."""
    search_path = host_path(path)
    if executable := find_host_tool(name, path=search_path):
        return executable
    raise FileNotFoundError(f"Host executable {name!r} not found in PATH={search_path!r}")


def host_tools_source() -> str:
    """Return the standalone module source for installation alongside hook scripts."""
    return Path(__file__).read_text(encoding="utf-8")


__all__ = ["host_path", "find_host_tool", "require_host_tool", "host_tools_source"]
