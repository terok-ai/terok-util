# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The consumer's declarative surface — ``tests/containers/matrix.yml``.

A consuming repo declares *what* its matrix is (image prefix, Containerfile
flavor, slot selection, capability contract, test phases); the engine owns
*how* a matrix runs.  This module is the schema and its loader — parse,
validate against the slot catalog, and hand the runner a frozen
[`MatrixConfig`][terok_util.matrix.config.MatrixConfig].

Unknown keys and unknown slot names are hard errors: the file is small and
hand-written, so a typo silently ignored would mean a slot or phase quietly
not running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent

from terok_util.yaml import YAMLError, load as yaml_load

from ._files import read_regular
from .catalog import FLAVORS, SLOTS

_MAX_DECLARATION_BYTES = 64 * 1024
_MAX_YAML_EVENTS = 4096
_MAX_YAML_DEPTH = 16
_IMAGE_PREFIX = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_PACKAGE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.:@=~<>-]*")


class MatrixConfigError(ValueError):
    """A ``matrix.yml`` failed validation."""


#: Pytest-phase scopes selectable from the CLI (``--unit-only`` / ``--integ-only``).
SCOPES = ("unit", "integ")


@dataclass(frozen=True)
class Phase:
    """One step of the in-container test flow.

    A phase either runs shell commands (``run``) or a pytest invocation
    (``pytest``) — never both.  Command phases abort the slot on failure
    (later phases depend on their effect, e.g. installed hooks); pytest
    phases record the failure and continue, so one run surfaces every
    failing suite.

    Args:
        name: Human-readable heading printed before the phase.
        run: Shell commands executed in order (command phase).
        pytest: Arguments to ``pytest`` (pytest phase).
        scope: Optional ``unit``/``integ`` tag; scope-filtered runs execute
            only pytest phases carrying the requested tag.
        expect_add: Capability names appended to ``TEROK_EXPECT`` after the
            phase succeeds (e.g. ``hooks`` once installed).
        tolerate_failure: Run the commands best-effort (diagnostics).
    """

    name: str
    run: tuple[str, ...] = ()
    pytest: str | None = None
    scope: str | None = None
    expect_add: tuple[str, ...] = ()
    tolerate_failure: bool = False


@dataclass(frozen=True)
class SlotConfig:
    """Per-slot choices a repo makes on top of the catalog facts.

    Args:
        extra_packages: Distro packages appended to the shared image's
            base set (the ``EXTRA_PACKAGES`` build arg).
        skip_arches: Host architectures (``uname -m``) the slot is skipped
            on, with ``skip_reason`` explaining why.
        skip_reason: Human-readable reason shown for the skip.
        groups: Override of the repo-level dependency groups.
        expect: Override of the repo-level capability contract.
        phases: Override of the repo-level phase list.
    """

    extra_packages: tuple[str, ...] = ()
    skip_arches: tuple[str, ...] = ()
    skip_reason: str = ""
    groups: tuple[str, ...] | None = None
    expect: tuple[str, ...] | None = None
    phases: tuple[Phase, ...] | None = None


@dataclass(frozen=True)
class MatrixConfig:
    """A repo's whole matrix declaration, resolved and validated.

    Args:
        image_prefix: Image/container name prefix and prune-label value.
        flavor: Shared Containerfile family (``podman`` or ``dbus``).
        groups: Dependency groups synced before testing.
        expect: ``TEROK_EXPECT`` capability contract; empty = not exported.
        slots: Selected slots in declaration order.
        phases: Repo-level test flow.
        containers_dir: Directory of the ``matrix.yml`` (fragments live here).
        repo_root: Build context and bind-mounted source tree.
        host_confines_pasta: The host's AppArmor ``passt`` profile is the
            distro's, so a nested pasta reached through a symlink loses its
            netns; slots with such a pasta are skipped.  Probed once in
            ``main``; never set under ``krun``.
        krun: Run each slot as a libkrun microVM (its own kernel) instead of a
            shared-kernel container.  A run-mode set from ``--krun``, not from
            ``matrix.yml`` — the CLI injects it after the declaration loads.
    """

    image_prefix: str
    flavor: str
    groups: tuple[str, ...]
    expect: tuple[str, ...]
    slots: dict[str, SlotConfig]
    phases: tuple[Phase, ...]
    containers_dir: Path
    repo_root: Path
    krun: bool = False
    host_confines_pasta: bool = False

    def slot_groups(self, name: str) -> tuple[str, ...]:
        """Dependency groups effective for slot ``name``."""
        override = self.slots[name].groups
        return self.groups if override is None else override

    def slot_expect(self, name: str) -> tuple[str, ...]:
        """Capability contract effective for slot ``name``."""
        override = self.slots[name].expect
        return self.expect if override is None else override

    def slot_phases(self, name: str) -> tuple[Phase, ...]:
        """Test flow effective for slot ``name``."""
        override = self.slots[name].phases
        return self.phases if override is None else override


def load_config(path: Path) -> MatrixConfig:
    """Load and validate a ``matrix.yml``.

    Args:
        path: The config file; its grandparent directory is taken as the
            repo root (``<root>/tests/containers/matrix.yml``). Host aliases
            through that root are resolved; repository paths must not be links.

    Raises:
        MatrixConfigError: On unknown keys, unknown slots, or a phase that
            is neither a command phase nor a pytest phase.
    """
    try:
        root = path.parent.parent.parent
        repo_root = root.resolve()
        path = repo_root / path.relative_to(root)
        text = read_regular(path, _MAX_DECLARATION_BYTES)
        _validate_yaml_structure(text)
        raw = yaml_load(text)
    except (OSError, UnicodeError, YAMLError) as error:
        raise MatrixConfigError(f"{path}: cannot read matrix declaration: {error}") from error
    if not isinstance(raw, dict):
        raise MatrixConfigError(f"{path}: expected a YAML mapping at top level")

    data = _Section(dict(raw), where=str(path))
    flavor = data.string("flavor")
    if flavor not in FLAVORS:
        raise MatrixConfigError(f"{path}: unknown flavor '{flavor}'; known: {list(FLAVORS)}")
    config = MatrixConfig(
        image_prefix=data.string("image-prefix"),
        flavor=flavor,
        groups=data.strings("groups", default=("test",)),
        expect=data.strings("expect", default=()),
        slots=_parse_slots(data.mapping("slots"), where=str(path)),
        phases=_parse_phases(data.list_of_maps("phases"), where=str(path)),
        containers_dir=path.parent,
        repo_root=repo_root,
    )
    data.reject_leftovers()
    if not _IMAGE_PREFIX.fullmatch(config.image_prefix):
        raise MatrixConfigError(f"{path}: 'image-prefix' must be a simple lowercase image name")
    return config


# ── Parse helpers ──────────────────────────────────────────────────


def _validate_yaml_structure(text: str) -> None:
    """Bound declaration structure before YAML constructs values or alias graphs."""
    depth = 0
    for count, event in enumerate(YAML(typ="safe", pure=True).parse(text), start=1):
        if count > _MAX_YAML_EVENTS:
            raise MatrixConfigError("Matrix declaration exceeds the YAML event limit")
        if isinstance(event, AliasEvent):
            raise MatrixConfigError("Matrix declarations do not allow YAML aliases")
        if isinstance(event, CollectionStartEvent):
            depth += 1
            if depth > _MAX_YAML_DEPTH:
                raise MatrixConfigError("Matrix declaration exceeds the YAML nesting limit")
        elif isinstance(event, CollectionEndEvent):
            depth -= 1


def _parse_slots(raw: dict[str, Any], where: str) -> dict[str, SlotConfig]:
    """Validate slot names against the catalog and parse their options."""
    unknown = set(raw) - set(SLOTS)
    if unknown:
        raise MatrixConfigError(
            f"{where}: unknown slot(s) {sorted(unknown)}; known: {sorted(SLOTS)}"
        )
    slots: dict[str, SlotConfig] = {}
    for name, options in raw.items():
        if options is not None and not isinstance(options, dict):
            raise MatrixConfigError(f"{where}: slot {name} must be a mapping or null")
        section = _Section(dict(options or {}), where=f"{where}: slot {name}")
        skip = _Section(section.mapping("skip"), where=f"{where}: slot {name}: skip")
        slots[name] = SlotConfig(
            extra_packages=section.strings("extra-packages", default=()),
            skip_arches=skip.strings("arches", default=()),
            skip_reason=skip.string("reason", default=""),
            groups=section.strings_or_none("groups"),
            expect=section.strings_or_none("expect"),
            phases=_parse_slot_phases(section, where=f"{where}: slot {name}"),
        )
        skip.reject_leftovers()
        section.reject_leftovers()
        if any(not _PACKAGE_TOKEN.fullmatch(package) for package in slots[name].extra_packages):
            raise MatrixConfigError(f"{where}: slot {name}: extra-packages must be package tokens")
    return slots


def _parse_slot_phases(section: _Section, where: str) -> tuple[Phase, ...] | None:
    """Parse a slot-level ``phases`` override, if declared."""
    if "phases" not in section.data:
        return None
    return _parse_phases(section.list_of_maps("phases"), where=where)


def _parse_phases(raw: list[dict[str, Any]], where: str) -> tuple[Phase, ...]:
    """Parse the ordered phase list."""
    phases = []
    for entry in raw:
        section = _Section(dict(entry), where=where)
        phase = Phase(
            name=section.string("name"),
            run=section.strings("run", default=()),
            pytest=section.string("pytest", default="") or None,
            scope=section.string("scope", default="") or None,
            expect_add=section.strings("expect-add", default=()),
            tolerate_failure=section.boolean("tolerate-failure", default=False),
        )
        section.reject_leftovers()
        if bool(phase.run) == bool(phase.pytest):
            raise MatrixConfigError(
                f"{where}: phase '{phase.name}' must have exactly one of 'run' or 'pytest'"
            )
        if phase.scope is not None and phase.scope not in SCOPES:
            raise MatrixConfigError(
                f"{where}: phase '{phase.name}' has unknown scope '{phase.scope}'"
            )
        phases.append(phase)
    return tuple(phases)


#: Sentinel distinguishing "key absent" from any real YAML value.
_MISSING = object()


@dataclass
class _Section:
    """One mapping being consumed key-by-key; leftovers are typos."""

    data: dict[str, Any]
    where: str = ""

    def __post_init__(self) -> None:
        """Reject structured or non-string keys before formatting diagnostics."""
        if any(not isinstance(key, str) for key in self.data):
            raise MatrixConfigError(f"{self.where}: mapping keys must be strings")

    def string(self, key: str, default: str | None = None) -> str:
        """Pop a string value; required when no default is given."""
        value = self.data.pop(key, _MISSING)
        if value is _MISSING:
            if default is None:
                raise MatrixConfigError(f"{self.where}: missing required key '{key}'")
            return default
        if not isinstance(value, str):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a string")
        return value

    def strings(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        """Pop a list-of-strings value."""
        found = self.strings_or_none(key)
        return default if found is None else found

    def strings_or_none(self, key: str) -> tuple[str, ...] | None:
        """Pop a list-of-strings value, ``None`` when absent."""
        value = self.data.pop(key, _MISSING)
        if value is _MISSING:
            return None
        if not isinstance(value, list):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a list")
        if any(not isinstance(item, str) for item in value):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a list of strings")
        return tuple(value)

    def boolean(self, key: str, default: bool) -> bool:
        """Pop a boolean value - a quoted "false" must not sneak in as truthy."""
        value = self.data.pop(key, _MISSING)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a boolean")
        return value

    def mapping(self, key: str) -> dict[str, Any]:
        """Pop a nested mapping, empty when absent."""
        value = self.data.pop(key, {})
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a mapping")
        return _Section(dict(value), where=f"{self.where}: {key}").data

    def list_of_maps(self, key: str) -> list[dict[str, Any]]:
        """Pop a list of mappings, empty when absent."""
        value = self.data.pop(key, [])
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a list of mappings")
        return [dict(item) for item in value]

    def reject_leftovers(self) -> None:
        """Fail on any key nothing consumed — it is a typo."""
        if self.data:
            raise MatrixConfigError(f"{self.where}: unknown key(s) {sorted(self.data)}")
