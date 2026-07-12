# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Shared fixture material for the matrix-engine tests.

One representative ``matrix.yml`` exercising every schema feature:
podman flavor, a capability contract, command + pytest phases with
scopes and ``expect-add``, per-slot extra packages, an arch skip, and a
nix-style slot override.
"""

from __future__ import annotations

from pathlib import Path

from terok_util.matrix import MatrixConfig, load_config

FULL_MATRIX_YML = """\
image-prefix: terok-fixture-test
flavor: podman
expect: [podman, nft, internet]
groups: [test, stories]

slots:
  debian13:
    extra-packages: [openssh-client, dbus]
  podman:
  alpine:
    skip:
      arches: [aarch64, arm64]
      reason: no musl aarch64 wheels for the TUI grammar stack
  nix:
    groups: [test, docs]
    expect: []
    phases:
      - name: unit tests
        scope: unit
        pytest: tests/unit -v -p no:tach

phases:
  - name: integration tests without hooks
    scope: integ
    pytest: tests/integration/ -v --tb=short -m "not needs_hooks"
  - name: install hooks
    run:
      - stack-under-test setup
    expect-add: [hooks]
  - name: integration tests with hooks
    scope: integ
    pytest: tests/integration/ -v --tb=short -m "needs_hooks"
  - name: diagnostics
    run:
      - stack-under-test config 2>&1
    tolerate-failure: true
"""


def write_config(tmp_path: Path, text: str = FULL_MATRIX_YML) -> Path:
    """Materialise a matrix.yml under the conventional repo layout."""
    containers = tmp_path / "tests" / "containers"
    containers.mkdir(parents=True, exist_ok=True)
    config_path = containers / "matrix.yml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def load_fixture(tmp_path: Path, text: str = FULL_MATRIX_YML) -> MatrixConfig:
    """Write and load the fixture config in one step."""
    return load_config(write_config(tmp_path, text))


def minimal_yml(
    flavor: str = "podman",
    slot: str = "debian13",
    head: str = "",
    slot_options: str = "",
) -> str:
    """The smallest valid matrix.yml, with hook points for one-off tweaks.

    Args:
        flavor: Containerfile family to declare.
        slot: The single slot to declare.
        head: Extra repo-level lines (e.g. ``"expect: [dbus-daemon]\n"``).
        slot_options: Indented option lines under the slot.
    """
    return (
        f"image-prefix: t\nflavor: {flavor}\n{head}"
        f"slots:\n  {slot}:\n{slot_options}"
        "phases:\n  - name: all\n    pytest: tests/ -v\n"
    )
