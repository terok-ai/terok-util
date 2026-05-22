# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the generic config stack engine."""

from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from terok_util.config_stack import (
    ConfigScope,
    ConfigStack,
    deep_merge,
    load_json_scope,
    load_yaml_scope,
)

NONEXISTENT_DIR = Path("/nonexistent")
NONEXISTENT_CONFIG_YAML = NONEXISTENT_DIR / "config.yml"
NONEXISTENT_CONFIG_JSON = NONEXISTENT_DIR / "config.json"


@pytest.mark.parametrize(
    ("base", "override", "expected"),
    [
        ({"a": 1, "b": 2}, {"b": 3, "c": 4}, {"a": 1, "b": 3, "c": 4}),
        ({"x": {"a": 1, "b": 2}}, {"x": {"b": 3, "c": 4}}, {"x": {"a": 1, "b": 3, "c": 4}}),
        ({"a": 1, "b": 2, "c": 3}, {"b": None}, {"a": 1, "c": 3}),
        ({"items": [1, 2, 3]}, {"items": [4, 5]}, {"items": [4, 5]}),
        ({"items": ["a", "b"]}, {"items": ["_inherit", "c"]}, {"items": ["a", "b", "c"]}),
        (
            {"x": {"a": 1, "b": 2}},
            {"x": {"_inherit": True, "c": 3}},
            {"x": {"a": 1, "b": 2, "c": 3}},
        ),
        (
            {"a": 1, "b": [1, 2], "c": {"x": 1}},
            {"a": "_inherit", "b": "_inherit", "c": "_inherit"},
            {"a": 1, "b": [1, 2], "c": {"x": 1}},
        ),
        ({}, {"a": 1}, {"a": 1}),
        ({"a": 1}, {}, {"a": 1}),
        ({}, {}, {}),
        # _inherit on missing/non-dict base: sentinel must not leak into result
        ({}, {"x": {"_inherit": True, "a": 1}}, {"x": {"a": 1}}),
        ({"x": "scalar"}, {"x": {"_inherit": True, "a": 1}}, {"x": {"a": 1}}),
    ],
    ids=[
        "simple-override",
        "nested-merge",
        "delete-key",
        "replace-list",
        "inherit-list-prefix",
        "inherit-dict-keep-parent",
        "bare-inherit-keep-base",
        "empty-base",
        "empty-override",
        "both-empty",
        "inherit-missing-base",
        "inherit-non-dict-base",
    ],
)
def test_deep_merge(base: dict, override: dict, expected: dict) -> None:
    """deep_merge handles overrides, deletions, inheritance, and recursion."""
    base_before = copy.deepcopy(base)
    override_before = copy.deepcopy(override)
    assert deep_merge(base, override) == expected
    assert base == base_before
    assert override == override_before


class TestConfigStack:
    """Tests for ConfigScope and ConfigStack."""

    def test_single_scope(self) -> None:
        """Single scope resolves to its own data."""
        stack = ConfigStack()
        stack.push(ConfigScope("base", None, {"a": 1}))
        assert stack.resolve() == {"a": 1}

    def test_multi_level_chaining(self) -> None:
        """Higher-priority scopes override lower ones."""
        stack = ConfigStack()
        stack.push(ConfigScope("global", None, {"a": 1, "b": 1}))
        stack.push(ConfigScope("project", None, {"b": 2, "c": 2}))
        stack.push(ConfigScope("cli", None, {"c": 3, "d": 3}))
        assert stack.resolve() == {"a": 1, "b": 2, "c": 3, "d": 3}

    def test_section_resolution(self) -> None:
        """resolve_section merges only one top-level key."""
        stack = ConfigStack()
        stack.push(ConfigScope("global", None, {"agent": {"model": "haiku"}, "other": 1}))
        stack.push(ConfigScope("project", None, {"agent": {"model": "sonnet", "turns": 5}}))
        assert stack.resolve_section("agent") == {"model": "sonnet", "turns": 5}

    def test_section_deletion_via_none(self) -> None:
        """A higher-priority scope setting a section to None deletes it."""
        stack = ConfigStack()
        stack.push(ConfigScope("global", None, {"agent": {"model": "haiku"}}))
        stack.push(ConfigScope("project", None, {"agent": None}))
        assert stack.resolve_section("agent") == {}

    def test_empty_stack(self) -> None:
        """Empty stack resolves to empty dict."""
        assert ConfigStack().resolve() == {}

    def test_scopes_property(self) -> None:
        """Scopes property returns a copy of the scope list."""
        stack = ConfigStack()
        scopes = [ConfigScope("a", None, {}), ConfigScope("b", None, {})]
        for scope in scopes:
            stack.push(scope)
        assert stack.scopes == scopes
        # Mutating the returned list doesn't affect the stack
        stack.scopes.append(ConfigScope("c", None, {}))
        assert len(stack.scopes) == 2


def _yaml_dump(data: dict) -> str:
    """Minimal YAML dump for test fixtures."""
    from io import StringIO

    from ruamel.yaml import YAML

    buf = StringIO()
    YAML(typ="safe").dump(data, buf)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("loader", "suffix", "content", "expected"),
    [
        (load_yaml_scope, ".yml", _yaml_dump({"key": "value"}), {"key": "value"}),
        (load_json_scope, ".json", json.dumps({"key": "value"}), {"key": "value"}),
        (load_yaml_scope, ".yml", "", {}),
        (load_json_scope, ".json", "{}", {}),
    ],
    ids=["yaml", "json", "yaml-empty", "json-empty-object"],
)
def test_scope_loaders(
    loader: Callable[[str, Path], ConfigScope],
    suffix: str,
    content: str,
    expected: dict[str, object],
) -> None:
    """YAML/JSON scope loaders read files and normalize empty inputs."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / f"test{suffix}"
        path.write_text(content, encoding="utf-8")
        scope = loader("test", path)
    assert scope.level == "test"
    assert scope.source == path
    assert scope.data == expected


@pytest.mark.parametrize(
    ("loader", "suffix", "content"),
    [
        (load_yaml_scope, ".yml", "- item1\n- item2\n"),
        (load_json_scope, ".json", "[1, 2, 3]"),
    ],
    ids=["yaml-list", "json-list"],
)
def test_scope_loaders_reject_non_mapping(
    loader: Callable[[str, Path], ConfigScope],
    suffix: str,
    content: str,
) -> None:
    """Loaders raise ValueError when the top-level value is not a mapping."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / f"test{suffix}"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="top-level value must be a mapping"):
            loader("test", path)


@pytest.mark.parametrize(
    ("loader", "path"),
    [
        (load_yaml_scope, NONEXISTENT_CONFIG_YAML),
        (load_json_scope, NONEXISTENT_CONFIG_JSON),
    ],
    ids=["yaml-missing", "json-missing"],
)
def test_scope_loaders_missing_files(
    loader: Callable[[str, Path], ConfigScope],
    path: Path,
) -> None:
    """Missing config files are treated as empty scopes."""
    assert loader("missing", path).data == {}
