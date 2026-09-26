# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Host lookup follows the current environment without implicit search locations."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from terok_util import find_host_tool, host_path, host_tools_source, require_host_tool


def executable(directory: Path, name: str = "host-tool") -> Path:
    """Create an executable fixture without invoking a host dependency."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("fixture", encoding="utf-8")
    tool.chmod(0o700)
    return tool


def test_path_lookup_follows_each_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changes to PATH take effect immediately, without cached executable bindings."""
    first = executable(tmp_path / "first")
    second = executable(tmp_path / "second")
    monkeypatch.setenv("PATH", os.pathsep.join((str(first.parent), str(second.parent))))
    assert require_host_tool(first.name) == str(first)
    monkeypatch.setenv("PATH", str(second.parent))
    assert require_host_tool(first.name) == str(second)


def test_lookup_preserves_symlink_spelling(tmp_path: Path) -> None:
    """Profile directory and executable symlinks keep their user-facing names."""
    tool = executable(tmp_path / "store")
    alias = tool.with_name("alias")
    alias.symlink_to(tool)
    profile = tmp_path / "profile"
    profile.symlink_to(tool.parent, target_is_directory=True)
    assert require_host_tool(alias.name, path=str(profile)) == str(profile / alias.name)


@pytest.mark.parametrize("path", ["", ":", ".", "bin", ":.:bin:"])
def test_lookup_never_searches_relative_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Empty and relative entries cannot expose executables from the cwd."""
    tool = executable(tmp_path)
    executable(tmp_path / "bin")
    monkeypatch.chdir(tmp_path)
    assert host_path(path) == ""
    assert find_host_tool(tool.name, path=path) is None
    assert find_host_tool(f"./{tool.name}", path=str(tmp_path)) is None
    assert find_host_tool(f"bin/{tool.name}", path=str(tmp_path)) is None


@pytest.mark.parametrize("path", [None, ""])
def test_missing_and_empty_path_have_no_default(
    monkeypatch: pytest.MonkeyPatch, path: str | None
) -> None:
    """Neither a missing nor an empty PATH falls back to system directories."""
    monkeypatch.delenv("PATH", raising=False)
    assert host_path(path) == ""
    assert find_host_tool("sh", path=path) is None


def test_explicit_absolute_tool_does_not_need_path(tmp_path: Path) -> None:
    """An operator's absolute override is honored without resolving its symlink."""
    tool = executable(tmp_path)
    assert require_host_tool(str(tool), path="") == str(tool)
    tool.chmod(0o600)
    assert find_host_tool(str(tool), path=str(tmp_path)) is None
    assert find_host_tool(str(tmp_path), path=str(tmp_path)) is None


def test_filtered_lookup_reports_actual_search_path(tmp_path: Path) -> None:
    """Missing tools produce a useful diagnostic with no fabricated locations."""
    path = os.pathsep.join((".", "", str(tmp_path), "relative"))
    assert host_path(path) == str(tmp_path)
    with pytest.raises(FileNotFoundError, match="Host executable 'absent'") as caught:
        require_host_tool("absent", path=path)
    assert f"PATH={str(tmp_path)!r}" in str(caught.value)


def test_copied_source_runs_without_site_packages(tmp_path: Path) -> None:
    """The exact installed source works with isolated Python and no site imports."""
    tool = executable(tmp_path / "profile")
    source = tmp_path / "_host_tools.py"
    source.write_text(host_tools_source(), encoding="utf-8")
    script = (
        "import runpy, sys; "
        "tools = runpy.run_path(sys.argv[1]); "
        "print(tools['require_host_tool'](sys.argv[2])); "
        "assert not any(n.startswith('terok') for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(source), tool.name],
        env={"PATH": str(tool.parent)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == str(tool)
