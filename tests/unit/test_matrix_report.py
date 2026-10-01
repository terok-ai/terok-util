# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Structured matrix progress survives the temporary engine workspace."""

import json

import pytest

from terok_util.matrix import cli
from terok_util.matrix.runner import SlotResult
from unit.matrix_fixtures import write_config


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Exercise the real matrix walk without invoking host tools or containers."""
    config = write_config(tmp_path)
    report = tmp_path / "reports" / "matrix.json"
    monkeypatch.setattr(cli, "_teardown", lambda config: None)
    monkeypatch.setattr(cli, "_warn_kernel_keyring", lambda: None)
    monkeypatch.setattr(cli, "_host_confines_pasta", lambda: False)
    monkeypatch.setattr(cli, "_skip_reason", lambda config, name: "")
    monkeypatch.setattr(cli, "build_image", lambda *a, **k: True)
    monkeypatch.setattr(cli, "run_slot", lambda *a, **k: SlotResult(True, "5.4.2"))
    return ["--config", str(config), "--report", str(report)], report


@pytest.mark.parametrize("jobs", [1, 3])
def test_reports_verdicts_without_parsing_output(engine, monkeypatch, jobs):
    """Build failures, skips, test failures, and successes remain distinct."""
    args, path = engine
    monkeypatch.setattr(cli, "build_image", lambda config, name, *a, **k: name != "alpine")
    monkeypatch.setattr(
        cli, "_skip_reason", lambda config, name: "unsupported host" if name == "nix" else ""
    )
    monkeypatch.setattr(
        cli,
        "run_slot",
        lambda config, name, *a, **k: SlotResult(
            name != "podman",
            "5.4.2",
            "DNS unavailable" if name == "podman" else None,
        ),
    )

    assert cli.main([*args, "--jobs", str(jobs)]) == 1
    report = json.loads(path.read_text())
    assert report["schema_version"] == 1
    assert report["state"] == "failed"
    assert report["exit_code"] == 1
    assert report["slots"]["debian13"]["state"] == "passed"
    assert report["slots"]["podman"]["network_hint"] == "DNS unavailable"
    assert report["slots"]["alpine"]["state"] == "build_failed"
    assert report["slots"]["nix"]["reason"] == "unsupported host"
    assert report["duration"] >= 0


def test_publishes_progress_and_preserves_skip_counts(engine, monkeypatch):
    """The live snapshot advances before a slot finishes, and keeps its artifacts."""
    args, path = engine

    def build(config, name, scratch, **kwargs):
        assert json.loads(path.read_text())["slots"][name]["state"] == "building"
        return True

    def run(config, name, scratch, **kwargs):
        assert json.loads(path.read_text())["slots"][name]["state"] == "testing"
        (scratch / f"{name}.skips.json").write_text(
            json.dumps({"matrix": {"no KVM": 2}, "pytest": {}})
        )
        return SlotResult(True, "5.4.2")

    monkeypatch.setattr(cli, "build_image", build)
    monkeypatch.setattr(cli, "run_slot", run)
    assert cli.main([*args, "debian13", "unavailable"]) == 0
    report = json.loads(path.read_text())
    assert report["slots"]["debian13"]["skips"]["matrix"] == {"no KVM": 2}
    assert report["slots"]["unavailable"]["state"] == "skipped"
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize(
    "error, code, state", [(KeyboardInterrupt, 130, "cancelled"), (OSError, 2, "error")]
)
def test_interrupted_report_keeps_finished_slots(engine, monkeypatch, error, code, state):
    """An unfinished slot is never presented as a passing test."""
    args, path = engine

    def run(config, name, *a, **k):
        if name == "podman":
            raise error("stopped")
        return SlotResult(True)

    monkeypatch.setattr(cli, "run_slot", run)
    assert cli.main(args) == code
    report = json.loads(path.read_text())
    assert report["state"] == state
    assert report["slots"]["debian13"]["state"] == "passed"
    assert report["slots"]["podman"]["state"] == state


def test_build_only_does_not_claim_tests_passed(engine):
    """A successful image build is reported as built, not as a passing test."""
    args, path = engine
    assert cli.main([*args, "--build-only"]) == 0
    assert {slot["state"] for slot in json.loads(path.read_text())["slots"].values()} == {"built"}


def test_teardown_failure_does_not_publish_a_successful_matrix(engine, monkeypatch):
    """Keep passing slot verdicts, but report a host cleanup error for the matrix."""
    args, path = engine

    def failed_teardown(config):
        raise OSError("container cleanup failed")

    monkeypatch.setattr(cli, "_teardown", failed_teardown)
    assert cli.main([*args, "debian13"]) == 2
    report = json.loads(path.read_text())
    assert report["state"] == "error"
    assert report["exit_code"] == 2
    assert report["error"] == "container cleanup failed"
    assert report["slots"]["debian13"]["state"] == "passed"


def test_teardown_interruption_keeps_finished_slots(engine, monkeypatch):
    """Interrupted cleanup cancels the matrix without discarding completed slot results."""
    args, path = engine

    def interrupted_teardown(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_teardown", interrupted_teardown)
    with pytest.raises(KeyboardInterrupt):
        cli.main([*args, "debian13"])
    report = json.loads(path.read_text())
    assert report["state"] == "cancelled"
    assert report["exit_code"] == cli.EXIT_INTERRUPTED
    assert report["slots"]["debian13"]["state"] == "passed"
