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


def test_read_slot_skips_reads_the_report(tmp_path: Path) -> None:
    """The runner reads a slot's per-reason skip counts; missing means empty."""
    from terok_util.matrix.cli import _read_slot_skips

    (tmp_path / "manjaro.skips.json").write_text('{"needs_podman": 12, "needs_loopback": 6}')
    assert _read_slot_skips(tmp_path, "manjaro") == {"needs_podman": 12, "needs_loopback": 6}
    assert _read_slot_skips(tmp_path, "fedora44") == {}


def test_read_slot_skips_rejects_malformed_counts(tmp_path: Path) -> None:
    """A null, non-numeric, or negative count reads as empty, not a crash."""
    from terok_util.matrix.cli import _read_slot_skips

    for bad in ('{"needs_krun": null}', '{"needs_krun": "lots"}', '{"needs_krun": -3}', "[1, 2]"):
        (tmp_path / "bad.skips.json").write_text(bad)
        assert _read_slot_skips(tmp_path, "bad") == {}


def test_within_slot_skip_block_lists_reasons_by_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The SKIPPED block shows each slot's reasons, most-skipped first."""
    from terok_util.matrix.cli import _print_within_slot_skips

    _print_within_slot_skips({"manjaro": {"needs_podman": 12, "needs_x86": 6}})
    out = capsys.readouterr().out
    assert "SKIPPED" in out
    assert "manjaro" in out
    assert out.index("12x needs_podman") < out.index("6x needs_x86")


def test_within_slot_skip_block_silent_when_nothing_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No within-slot skips means no block at all."""
    from terok_util.matrix.cli import _print_within_slot_skips

    _print_within_slot_skips({})
    assert capsys.readouterr().out == ""


def test_failure_verdict_flags_suspected_host_network_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed slot with a network hint is annotated in both verdict and summary."""
    from terok_util.matrix.cli import _print_summary, _print_verdict
    from terok_util.matrix.config import load_config
    from terok_util.matrix.runner import SlotResult

    config = load_config(write_config(tmp_path))
    hinted = SlotResult(
        passed=False, observed="6.0.2", network_hint="Could not resolve host: github.com"
    )

    _print_verdict(config, "debian13", hinted)
    _print_summary(config, [], [], ["debian13"], {"debian13": hinted}, {})
    out = capsys.readouterr().out

    assert "debian13: FAIL" in out
    assert out.count("suspected host network error: Could not resolve host: github.com") == 2
    assert "a rerun may clear them" in out
