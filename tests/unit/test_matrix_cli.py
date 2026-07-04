# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""terok-matrix CLI — the podman-free surfaces (list, JSON, validation)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terok_util.matrix.cli import main
from unit.matrix_fixtures import write_config


def _config_args(tmp_path: Path) -> list[str]:
    return ["--config", str(write_config(tmp_path))]


def test_slots_json_emits_the_declared_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI derives strategy.matrix from here — order and content must match."""
    assert main([*_config_args(tmp_path), "--slots-json"]) == 0

    assert json.loads(capsys.readouterr().out) == ["debian13", "podman", "alpine", "nix"]


def test_slots_json_respects_an_explicit_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A slot argument narrows the JSON the same way it narrows a run."""
    assert main([*_config_args(tmp_path), "--slots-json", "alpine"]) == 0

    assert json.loads(capsys.readouterr().out) == ["alpine"]


def test_image_prefix_query_prints_the_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """External tooling (superbuild TUI) reads the prefix from here, not YAML regexes."""
    assert main([*_config_args(tmp_path), "--image-prefix"]) == 0

    assert capsys.readouterr().out.strip() == "terok-fixture-test"


def test_list_shows_slots_with_version_expectations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--list mirrors the historical run-matrix.sh --list output."""
    assert main([*_config_args(tmp_path), "--list"]) == 0

    out = capsys.readouterr().out
    assert "debian13 (expected podman 5.4.2)" in out
    assert "podman (podman latest, version pinned by upstream)" in out
    assert "nix (nix-wrapped Python)" in out


def test_list_respects_an_explicit_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--list narrows to the requested slots, same as --slots-json."""
    assert main([*_config_args(tmp_path), "--list", "alpine"]) == 0

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("alpine")


def test_unknown_slot_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A typo'd slot fails fast instead of running the wrong subset."""
    assert main([*_config_args(tmp_path), "atari800"]) == 2

    assert "unknown slot" in capsys.readouterr().err


def test_missing_config_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No matrix.yml means a friendly error, exit 2."""
    assert main(["--config", str(tmp_path / "nowhere.yml"), "--list"]) == 2

    assert "Error:" in capsys.readouterr().err
