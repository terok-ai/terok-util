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

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terok_util.yaml import load as yaml_load

from .catalog import FLAVORS, SLOTS


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
        pytest: Arguments to ``poetry run pytest`` (pytest phase).
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
        poetry_groups: Override of the repo-level dependency groups.
        expect: Override of the repo-level capability contract.
        phases: Override of the repo-level phase list.
    """

    extra_packages: tuple[str, ...] = ()
    skip_arches: tuple[str, ...] = ()
    skip_reason: str = ""
    poetry_groups: tuple[str, ...] | None = None
    expect: tuple[str, ...] | None = None
    phases: tuple[Phase, ...] | None = None


@dataclass(frozen=True)
class MatrixConfig:
    """A repo's whole matrix declaration, resolved and validated.

    Args:
        image_prefix: Image/container name prefix and prune-label value.
        flavor: Shared Containerfile family (``podman`` or ``dbus``).
        poetry_groups: Dependency groups installed before testing.
        expect: ``TEROK_EXPECT`` capability contract; empty = not exported.
        slots: Selected slots in declaration order.
        phases: Repo-level test flow.
        containers_dir: Directory of the ``matrix.yml`` (fragments live here).
        repo_root: Build context and bind-mounted source tree.
    """

    image_prefix: str
    flavor: str
    poetry_groups: tuple[str, ...]
    expect: tuple[str, ...]
    slots: dict[str, SlotConfig]
    phases: tuple[Phase, ...]
    containers_dir: Path
    repo_root: Path

    def slot_poetry_groups(self, name: str) -> tuple[str, ...]:
        """Dependency groups effective for slot ``name``."""
        override = self.slots[name].poetry_groups
        return self.poetry_groups if override is None else override

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
            repo root (``<root>/tests/containers/matrix.yml``).

    Raises:
        MatrixConfigError: On unknown keys, unknown slots, or a phase that
            is neither a command phase nor a pytest phase.
    """
    raw = yaml_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MatrixConfigError(f"{path}: expected a YAML mapping at top level")

    data = _Section(dict(raw), where=str(path))
    flavor = data.string("flavor")
    if flavor not in FLAVORS:
        raise MatrixConfigError(f"{path}: unknown flavor '{flavor}'; known: {list(FLAVORS)}")
    config = MatrixConfig(
        image_prefix=data.string("image-prefix"),
        flavor=flavor,
        poetry_groups=data.strings("poetry-groups", default=("test",)),
        expect=data.strings("expect", default=()),
        slots=_parse_slots(data.mapping("slots"), where=str(path)),
        phases=_parse_phases(data.list_of_maps("phases"), where=str(path)),
        containers_dir=path.parent.resolve(),
        repo_root=path.parent.parent.parent.resolve(),
    )
    data.reject_leftovers()
    return config


# ── Parse helpers ──────────────────────────────────────────────────


def _parse_slots(raw: dict[str, Any], where: str) -> dict[str, SlotConfig]:
    """Validate slot names against the catalog and parse their options."""
    unknown = set(raw) - set(SLOTS)
    if unknown:
        raise MatrixConfigError(
            f"{where}: unknown slot(s) {sorted(unknown)}; known: {sorted(SLOTS)}"
        )
    slots: dict[str, SlotConfig] = {}
    for name, options in raw.items():
        section = _Section(dict(options or {}), where=f"{where}: slot {name}")
        skip = _Section(section.mapping("skip"), where=f"{where}: slot {name}: skip")
        slots[name] = SlotConfig(
            extra_packages=section.strings("extra-packages", default=()),
            skip_arches=skip.strings("arches", default=()),
            skip_reason=skip.string("reason", default=""),
            poetry_groups=section.strings_or_none("poetry-groups"),
            expect=section.strings_or_none("expect"),
            phases=_parse_slot_phases(section, where=f"{where}: slot {name}"),
        )
        skip.reject_leftovers()
        section.reject_leftovers()
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

    def string(self, key: str, default: str | None = None) -> str:
        """Pop a string value; required when no default is given."""
        value = self.data.pop(key, _MISSING)
        if value is _MISSING:
            if default is None:
                raise MatrixConfigError(f"{self.where}: missing required key '{key}'")
            return default
        return str(value)

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
        return tuple(str(item) for item in value)

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
        value = self.data.pop(key, None) or {}
        if not isinstance(value, dict):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a mapping")
        return dict(value)

    def list_of_maps(self, key: str) -> list[dict[str, Any]]:
        """Pop a list of mappings, empty when absent."""
        value = self.data.pop(key, None) or []
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise MatrixConfigError(f"{self.where}: '{key}' must be a list of mappings")
        return [dict(item) for item in value]

    def reject_leftovers(self) -> None:
        """Fail on any key nothing consumed — it is a typo."""
        if self.data:
            raise MatrixConfigError(f"{self.where}: unknown key(s) {sorted(self.data)}")
