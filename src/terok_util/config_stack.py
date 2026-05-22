# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Resolves layered configuration by deep-merging ordered scopes.

Domain-agnostic: no terok service dependencies.

Terminology
-----------
- **Scope**: a single config layer (e.g. "global", "project", "preset", "cli").
- **Stack**: an ordered list of scopes, lowest-priority first.
- **deep_merge**: recursive dict merge with ``_inherit`` support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_INHERIT = "_inherit"


@dataclass(frozen=True)
class ConfigScope:
    """A single layer in the config stack."""

    level: str
    source: Path | None
    data: dict


class ConfigStack:
    """Ordered collection of config scopes, lowest-priority first.

    Usage::

        stack = ConfigStack()
        stack.push(ConfigScope("global", global_path, global_data))
        stack.push(ConfigScope("project", proj_path, proj_data))
        resolved = stack.resolve()
    """

    def __init__(self) -> None:
        """Initialise an empty config stack."""
        self._scopes: list[ConfigScope] = []

    def push(self, scope: ConfigScope) -> None:
        """Append a scope (higher priority than all previous)."""
        self._scopes.append(scope)

    def resolve(self) -> dict:
        """Deep-merge all scopes in order and return the result."""
        result: dict = {}
        for scope in self._scopes:
            result = deep_merge(result, scope.data)
        return result

    def resolve_section(self, key: str) -> dict:
        """Resolve only a single top-level section across all scopes.

        Respects the same semantics as
        [`resolve`][terok_util.config_stack.ConfigStack.resolve] — in
        particular, ``None`` values trigger deletion via
        [`deep_merge`][terok_util.config_stack.deep_merge].

        Returns ``{}`` when the highest-priority scope has a non-dict
        value for *key* (e.g. ``services: tcp`` instead of
        ``services: {mode: tcp}``).  Callers can call
        [`resolve`][terok_util.config_stack.ConfigStack.resolve] and
        inspect the raw shape if they need to distinguish "missing"
        from "wrong-shape", but ``resolve_section`` is contract-typed
        as a mapping accessor and so coerces non-mappings to empty.
        """
        wrapper: dict = {}
        for scope in self._scopes:
            if key in scope.data:
                wrapper = deep_merge(wrapper, {key: scope.data[key]})
        section = wrapper.get(key, {})
        return section if isinstance(section, dict) else {}

    @property
    def scopes(self) -> list[ConfigScope]:
        """Read-only access to the scope list (for diagnostics)."""
        return list(self._scopes)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_yaml_scope(level: str, path: Path) -> ConfigScope:
    """Load a YAML file into a [`ConfigScope`][terok_util.config_stack.ConfigScope].

    Returns an empty-data scope when the file is missing or empty.

    Raises
    ------
    ValueError
        If the parsed YAML is not a mapping.
        [`ConfigScope`][terok_util.config_stack.ConfigScope] ``data``
        must be a ``dict`` because
        [`ConfigStack.resolve`][terok_util.config_stack.ConfigStack.resolve]
        and [`deep_merge`][terok_util.config_stack.deep_merge] operate
        on mappings.
    """
    if path.is_file():
        from ruamel.yaml import YAML  # lazy import; declared runtime dep

        text = path.read_text(encoding="utf-8")
        # Blank file → empty scope (``yaml.load("") is None``, but be explicit).
        if not text.strip():
            data: object = {}
        else:
            yaml = YAML(typ="safe")
            data = yaml.load(text)
            if data is None:  # whitespace-only or pure comments
                data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top-level value must be a mapping, got {type(data).__name__}; "
            f"ConfigScope.data fed to ConfigStack.resolve / deep_merge requires a dict"
        )
    return ConfigScope(level=level, source=path, data=data)


def load_json_scope(level: str, path: Path) -> ConfigScope:
    """Load a JSON file into a [`ConfigScope`][terok_util.config_stack.ConfigScope].

    Returns an empty-data scope when the file is missing or empty.

    Raises
    ------
    ValueError
        If the parsed JSON is not a mapping.
        [`ConfigScope`][terok_util.config_stack.ConfigScope] ``data``
        must be a ``dict`` because
        [`ConfigStack.resolve`][terok_util.config_stack.ConfigStack.resolve]
        and [`deep_merge`][terok_util.config_stack.deep_merge] operate
        on mappings.
    """
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        # ``json.loads("")`` raises; blank files map to an empty scope.
        data: object = json.loads(text) if text.strip() else {}
    else:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top-level value must be a mapping, got {type(data).__name__}; "
            f"ConfigScope.data fed to ConfigStack.resolve / deep_merge requires a dict"
        )
    return ConfigScope(level=level, source=path, data=data)


# ---------------------------------------------------------------------------
# Merge engine
# ---------------------------------------------------------------------------


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a **new** dict.

    Rules
    -----
    * Dicts are merged recursively by default.
    * A ``None`` value in *override* **deletes** the corresponding key.
    * A bare ``"_inherit"`` string keeps the base value unchanged
      (equivalent to omitting the key, but explicit).
    * Lists in *override* replace the base list wholesale **unless** the
      list contains the sentinel string ``"_inherit"``, in which case the
      sentinel is replaced by the base list elements (splice).
    * A dict in *override* that contains ``_inherit: true`` keeps all
      parent keys and overlays the rest (the ``_inherit`` key itself is
      stripped from the result).
    """
    merged: dict = {}

    all_keys = set(base) | set(override)
    for key in all_keys:
        if key in override:
            ov = override[key]
            # None → delete
            if ov is None:
                continue
            # Bare _inherit string → keep base value (explicit no-op)
            if ov == _INHERIT:
                if key in base:
                    merged[key] = base[key]
                continue
            bv = base.get(key)
            if isinstance(ov, dict) and isinstance(bv, dict):
                merged[key] = _merge_dicts(bv, ov)
            elif isinstance(ov, list) and isinstance(bv, list):
                merged[key] = _merge_lists(bv, ov)
            elif isinstance(ov, dict) and ov.get(_INHERIT) is True:
                # _inherit with no dict parent — strip sentinel, use rest
                merged[key] = {k: v for k, v in ov.items() if k != _INHERIT}
            else:
                merged[key] = ov
        else:
            # key only in base
            merged[key] = base[key]
    return merged


def _merge_dicts(base: dict, override: dict) -> dict:
    """Merge two dicts, respecting ``_inherit: true``."""
    if override.get(_INHERIT) is True:
        # Keep parent, overlay rest (strip sentinel)
        cleaned = {k: v for k, v in override.items() if k != _INHERIT}
        return deep_merge(base, cleaned)
    return deep_merge(base, override)


def _merge_lists(base: list, override: list) -> list:
    """Merge two lists, splicing base at ``_inherit`` sentinels."""
    if _INHERIT not in override:
        return list(override)
    result: list = []
    for item in override:
        if item == _INHERIT:
            result.extend(base)
        else:
            result.append(item)
    return result


__all__ = [
    "ConfigScope",
    "ConfigStack",
    "deep_merge",
    "load_json_scope",
    "load_yaml_scope",
]
