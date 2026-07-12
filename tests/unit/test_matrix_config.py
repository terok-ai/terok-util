# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""matrix.yml loading — schema acceptance, defaults, and typo rejection."""

from __future__ import annotations

from pathlib import Path

import pytest

from terok_util.matrix import MatrixConfigError, load_config
from unit.matrix_fixtures import load_fixture, write_config


def test_full_config_round_trips(tmp_path: Path) -> None:
    """Every schema feature of the fixture lands in the dataclasses."""
    config = load_fixture(tmp_path)

    assert config.image_prefix == "terok-fixture-test"
    assert config.flavor == "podman"
    assert config.expect == ("podman", "nft", "internet")
    assert config.poetry_groups == ("test", "stories")
    assert list(config.slots) == ["debian13", "podman", "alpine", "nix"]
    assert config.slots["debian13"].extra_packages == ("openssh-client", "dbus")
    assert config.slots["alpine"].skip_arches == ("aarch64", "arm64")
    assert "musl" in config.slots["alpine"].skip_reason
    assert [phase.name for phase in config.phases] == [
        "integration tests without hooks",
        "install hooks",
        "integration tests with hooks",
        "diagnostics",
    ]
    assert config.phases[1].expect_add == ("hooks",)
    assert config.phases[3].tolerate_failure


def test_repo_root_derived_from_config_location(tmp_path: Path) -> None:
    """<root>/tests/containers/matrix.yml implies <root> and the fragments dir."""
    config = load_fixture(tmp_path)

    assert config.repo_root == tmp_path.resolve()
    assert config.containers_dir == (tmp_path / "tests" / "containers").resolve()


def test_slot_overrides_fall_back_to_repo_level(tmp_path: Path) -> None:
    """Slot-level poetry-groups/expect/phases override; absent = repo-level."""
    config = load_fixture(tmp_path)

    assert config.slot_poetry_groups("debian13") == ("test", "stories")
    assert config.slot_poetry_groups("nix") == ("test", "docs")
    assert config.slot_expect("podman") == ("podman", "nft", "internet")
    assert config.slot_expect("nix") == ()
    assert [phase.name for phase in config.slot_phases("nix")] == ["unit tests"]
    assert config.slot_phases("debian13") == config.phases


def test_defaults_for_minimal_config(tmp_path: Path) -> None:
    """A minimal matrix.yml gets the conventional defaults."""
    config = load_fixture(
        tmp_path,
        "image-prefix: t\nflavor: dbus\nslots:\n  debian13:\n"
        "phases:\n  - name: all\n    pytest: tests/ -v\n",
    )

    assert config.poetry_groups == ("test",)
    assert config.expect == ()
    assert config.slots["debian13"].extra_packages == ()


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("image-prefix: t\nflavor: podman\nslots:\n  atari800:\n", "unknown slot"),
        ("image-prefix: t\nflavor: podmann\nslots:\n  debian13:\n", "unknown flavor"),
        ("image-prefix: t\nflavor: podman\nslots:\n  debian13:\nbogus-key: 1\n", "unknown key"),
        (
            "image-prefix: t\nflavor: podman\nslots:\n  debian13:\nphases:\n  - name: broken\n",
            "exactly one of",
        ),
        (
            "image-prefix: t\nflavor: podman\nslots:\n  debian13:\n"
            "phases:\n  - name: both\n    pytest: tests/\n    run: [true]\n",
            "exactly one of",
        ),
        (
            "image-prefix: t\nflavor: podman\nslots:\n  debian13:\n"
            "phases:\n  - name: p\n    pytest: tests/\n    scope: e2e\n",
            "unknown scope",
        ),
        ("flavor: podman\nslots:\n  debian13:\n", "missing required key 'image-prefix'"),
        (
            "image-prefix: t\nflavor: podman\nslots:\n  debian13:\n    extra-packages: 5\n",
            "must be a list",
        ),
        (
            "image-prefix: t\nflavor: podman\nslots:\n  debian13:\n    skip: [aarch64]\n",
            "must be a mapping",
        ),
        ("image-prefix: t\nflavor: podman\nslots:\n  debian13:\nphases: [5]\n", "list of mappings"),
        ("[not, a, mapping]", "mapping at top level"),
        (
            "image-prefix: t\nflavor: podman\nslots:\n  debian13:\n"
            'phases:\n  - name: d\n    run: [true]\n    tolerate-failure: "no"\n',
            "must be a boolean",
        ),
    ],
)
def test_bad_configs_are_rejected(tmp_path: Path, mutation: str, match: str) -> None:
    """Typos and contradictions fail loudly instead of silently skipping work."""
    with pytest.raises(MatrixConfigError, match=match):
        load_config(write_config(tmp_path, mutation))


def test_installer_sniffed_from_lockfile(tmp_path: Path) -> None:
    """uv.lock at the repo root flips the installer; its absence means poetry."""
    assert load_fixture(tmp_path).installer == "poetry"
    (tmp_path / "uv.lock").touch()
    assert load_fixture(tmp_path).installer == "uv"
