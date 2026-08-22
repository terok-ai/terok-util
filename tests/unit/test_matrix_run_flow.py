# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The full matrix walk and host-side subprocess plumbing, podman mocked out.

[`runner`][terok_util.matrix.runner] is exercised against a recorded
``subprocess.run``; [`cli`][terok_util.matrix.cli] against stubbed runner
functions — between them every orchestration branch (pass, fail, skip,
build failure, teardown, interrupt, keyring warning) runs without a
container host.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from terok_util.matrix import cli, runner
from unit.matrix_fixtures import load_fixture, write_config


class RecordedRun:
    """Stand-in for ``subprocess.run`` that records argv and scripts a result."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Record the argv and return the scripted completed process."""
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


# ── runner: build / run / prune ────────────────────────────────────


def test_build_image_argv_and_rendered_containerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build gets the args, the tag, and the assembled Containerfile."""
    config = load_fixture(tmp_path)
    recorded = RecordedRun()
    monkeypatch.setattr(runner.subprocess, "run", recorded)
    results = tmp_path / "results"
    results.mkdir()

    assert runner.build_image(config, "debian13", results, no_cache=True)

    (argv,) = recorded.calls
    assert argv[:4] == ["podman", "build", "--pull=newer", "--no-cache"]
    assert "IMAGE_PREFIX=terok-fixture-test" in argv
    assert "EXTRA_PACKAGES=openssh-client dbus" in argv
    assert "terok-fixture-test:debian13" in argv
    assert argv[-1] == str(config.repo_root)
    written = (results / "Containerfile.debian13").read_text(encoding="utf-8")
    assert "$EXTRA_PACKAGES" in written


def test_build_image_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero podman build becomes False, never an exception."""
    config = load_fixture(tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", RecordedRun(returncode=1))
    results = tmp_path / "results"
    results.mkdir()

    assert not runner.build_image(config, "debian13", results)


def test_run_slot_writes_scripts_and_reads_the_observed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripts land in the results dir; the recorded version comes back."""
    config = load_fixture(tmp_path)
    recorded = RecordedPopen([])
    monkeypatch.setattr(runner.subprocess, "Popen", recorded)
    results = tmp_path / "results"
    results.mkdir()
    (results / "debian13.podman-version").write_text("5.4.2\n", encoding="utf-8")

    result = runner.run_slot(config, "debian13", results)

    assert result.passed
    assert result.observed == "5.4.2"
    assert result.network_hint is None
    assert (results / "outer-debian13.sh").exists()
    assert (results / "inner-debian13.sh").exists()
    (argv,) = recorded.calls
    assert argv[:2] == ["podman", "run"]


def test_run_slot_missing_version_file_reads_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slot that died before recording its version reports '?', not a crash."""
    config = load_fixture(tmp_path)
    monkeypatch.setattr(runner.subprocess, "Popen", RecordedPopen([], returncode=1))
    results = tmp_path / "results"
    results.mkdir()

    result = runner.run_slot(config, "debian13", results)

    assert not result.passed
    assert result.observed == "?"


def test_run_slot_flags_suspected_host_network_error_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing slot whose output shows a DNS/connection error gets a network_hint."""
    config = load_fixture(tmp_path)
    dns_line = "fatal: unable to access 'https://github.com/x': Could not resolve host: github.com"
    monkeypatch.setattr(runner.subprocess, "Popen", RecordedPopen([dns_line], returncode=1))
    results = tmp_path / "results"
    results.mkdir()

    result = runner.run_slot(config, "debian13", results)

    assert not result.passed
    assert result.network_hint is not None
    assert "Could not resolve host" in result.network_hint


def test_run_slot_does_not_flag_a_network_line_when_it_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network blip a passing slot recovered from is not flagged as the cause."""
    config = load_fixture(tmp_path)
    blip = "warning: Could not resolve host: cdn.example (retrying)"
    monkeypatch.setattr(runner.subprocess, "Popen", RecordedPopen([blip], returncode=0))
    results = tmp_path / "results"
    results.mkdir()
    (results / "debian13.podman-version").write_text("5.4.2\n", encoding="utf-8")

    assert runner.run_slot(config, "debian13", results).network_hint is None


def test_prune_targets_only_this_harness_and_niceness_wraps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prune filters on the ownership label and runs at idle priority."""
    config = load_fixture(tmp_path)
    recorded = RecordedRun(stdout="id1\nid2\n")
    monkeypatch.setattr(runner.subprocess, "run", recorded)
    monkeypatch.setattr(runner, "which", lambda cmd: f"/usr/bin/{cmd}")

    assert runner.prune_dangling(config) == 2

    (argv,) = recorded.calls
    assert argv[:4] == ["nice", "-n19", "ionice", "-c3"]
    assert "label=io.terok.matrix-test=terok-fixture-test" in argv


def test_prune_without_niceness_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No nice/ionice on the host - the prune still runs, unwrapped."""
    config = load_fixture(tmp_path)
    recorded = RecordedRun()
    monkeypatch.setattr(runner.subprocess, "run", recorded)
    monkeypatch.setattr(runner, "which", lambda cmd: None)

    assert runner.prune_dangling(config) == 0

    assert recorded.calls[0][0] == "podman"


def test_prune_failure_surfaces_stderr_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing prune warns with stderr's gist — a leak reported beats one hidden."""
    config = load_fixture(tmp_path)
    recorded = RecordedRun(returncode=125, stderr="Error: layer store exploded\n")
    monkeypatch.setattr(runner.subprocess, "run", recorded)
    monkeypatch.setattr(runner, "which", lambda cmd: None)

    assert runner.prune_dangling(config) == 0

    err = capsys.readouterr().err
    assert "WARNING: image prune failed" in err
    assert "layer store exploded" in err


def test_prune_blocked_by_external_container_names_the_cure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 'image is in use' case points at superbuild's matrix-clean."""
    config = load_fixture(tmp_path)
    stderr = "Error: image used by 1234abcd: image is in use by a container: consider force removal"
    monkeypatch.setattr(runner.subprocess, "run", RecordedRun(returncode=125, stderr=stderr))
    monkeypatch.setattr(runner, "which", lambda cmd: None)

    assert runner.prune_dangling(config) == 0

    err = capsys.readouterr().err
    assert "external (buildah) build leftover" in err
    assert "superbuild's matrix-clean" in err


# ── runner: teardown sweep ─────────────────────────────────────────


def test_sweep_removes_only_this_harness_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep lists by ownership label and force-removes what it finds."""
    config = load_fixture(tmp_path)
    recorded = RecordedRun(stdout="id1\nid2\n")
    monkeypatch.setattr(runner.subprocess, "run", recorded)

    assert runner.sweep_containers(config) == 2

    listing, removal = recorded.calls
    assert listing[:3] == ["podman", "ps", "-aq"]
    assert "label=io.terok.matrix-test=terok-fixture-test" in listing
    assert removal == ["podman", "rm", "-f", "-t", "0", "id1", "id2"]


def test_sweep_with_nothing_leftover_skips_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean host means one listing call and no ``podman rm``."""
    config = load_fixture(tmp_path)
    recorded = RecordedRun(stdout="")
    monkeypatch.setattr(runner.subprocess, "run", recorded)

    assert runner.sweep_containers(config) == 0

    assert len(recorded.calls) == 1


def test_external_storage_leftovers_are_named_not_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only storage-state externals are reported; nothing gets an rm call."""
    recorded = RecordedRun(
        stdout="abc123 storage terok-util-buildah\ndef456 running unrelated-service\n"
    )
    monkeypatch.setattr(runner.subprocess, "run", recorded)

    assert runner.external_storage_leftovers() == ["terok-util-buildah"]

    (listing,) = recorded.calls
    assert listing[:2] == ["podman", "ps"]
    assert "--external" in listing


# ── cli: the matrix walk ───────────────────────────────────────────


@pytest.fixture
def stubbed_host(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the runner layer under the CLI with scriptable stubs."""
    journal: dict[str, Any] = {
        "built": [],
        "ran": [],
        "pruned": 0,
        "swept": 0,
        "external": [],
        "fail_build": set(),
        "fail_run": set(),
    }

    def fake_build(config: Any, name: str, results: Path, no_cache: bool = False) -> bool:
        journal["built"].append(name)
        return name not in journal["fail_build"]

    def fake_run(config: Any, name: str, results: Path, scope: str = "all") -> runner.SlotResult:
        journal["ran"].append(name)
        passed = name not in journal["fail_run"]
        return runner.SlotResult(passed=passed, observed="5.4.2" if passed else "?")

    def fake_prune(config: Any) -> int:
        journal["pruned"] += 1
        return 3

    def fake_sweep(config: Any) -> int:
        journal["swept"] += 1
        return 0

    monkeypatch.setattr(cli, "build_image", fake_build)
    monkeypatch.setattr(cli, "run_slot", fake_run)
    monkeypatch.setattr(cli, "prune_dangling", fake_prune)
    monkeypatch.setattr(cli, "sweep_containers", fake_sweep)
    monkeypatch.setattr(cli, "external_storage_leftovers", lambda: journal["external"])
    monkeypatch.setenv("CONTAINERS_CONF", "/nonexistent/containers.conf")
    return journal


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return ["--config", str(write_config(tmp_path)), *extra]


def test_walk_passes_and_prunes(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Green slots: build all, run all, summary, prune, exit 0."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")

    assert cli.main(_args(tmp_path)) == 0

    assert stubbed_host["built"] == ["debian13", "podman", "alpine", "nix"]
    assert stubbed_host["ran"] == ["debian13", "podman", "alpine", "nix"]
    assert stubbed_host["pruned"] == 1
    out = capsys.readouterr().out
    assert "===== Matrix Summary =====" in out
    assert out.count("PASS") >= 4
    assert "pruned 3 image record(s)" in out


def test_walk_reports_failures_with_exit_1(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing slot is summarised as FAIL and fails the run."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    stubbed_host["fail_run"].add("podman")

    assert cli.main(_args(tmp_path)) == 1

    assert "FAIL: podman" in capsys.readouterr().out


def test_walk_skips_arch_limited_slots(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On aarch64 the fixture's alpine slot is skipped, not failed."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "aarch64")

    assert cli.main(_args(tmp_path)) == 0

    assert stubbed_host["built"] == ["debian13", "podman", "nix"]
    assert "alpine" not in stubbed_host["ran"]
    assert "SKIP: alpine" in capsys.readouterr().out


def test_walk_records_build_failures_and_keeps_going(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One bad image build fails that slot only; the rest still run."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    stubbed_host["fail_build"].add("debian13")

    assert cli.main(_args(tmp_path)) == 1

    assert "debian13" not in stubbed_host["ran"]
    assert stubbed_host["ran"] == ["podman", "alpine", "nix"]
    assert "FAIL (image build failed)" in capsys.readouterr().err


def test_build_only_still_prunes_the_retagged_generations(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--build-only runs nothing — but it retags images, so teardown still owes a prune."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")

    assert cli.main(_args(tmp_path, "--build-only")) == 0

    assert stubbed_host["ran"] == []
    assert stubbed_host["pruned"] == 1


def test_keep_dangling_skips_the_teardown(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--keep-dangling leaves teardown hygiene to the operator."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")

    assert cli.main(_args(tmp_path, "--keep-dangling")) == 0

    assert stubbed_host["pruned"] == 0
    assert stubbed_host["swept"] == 0


def test_interrupt_still_tears_down_and_exits_130(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C mid-walk sweeps and prunes anyway — the 40-80 GB stranding path."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")

    def interrupted(*_args: Any, **_kwargs: Any) -> runner.SlotResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_slot", interrupted)

    assert cli.main(_args(tmp_path)) == 130

    assert stubbed_host["swept"] == 1
    assert stubbed_host["pruned"] == 1
    assert "Interrupted" in capsys.readouterr().err


def test_teardown_names_external_storage_leftovers(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """buildah leftovers are named with a pointer to superbuild, never removed here."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    stubbed_host["external"].append("terok-util-buildah")

    assert cli.main(_args(tmp_path)) == 0

    err = capsys.readouterr().err
    assert "terok-util-buildah" in err
    assert "superbuild's matrix-clean" in err


def test_version_mismatch_is_a_warning_not_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_host: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An observed podman differing from the pin warns but stays PASS."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        cli,
        "run_slot",
        lambda *a, **k: runner.SlotResult(passed=True, observed="9.9.9"),
    )

    assert cli.main(_args(tmp_path, "debian13")) == 0

    out = capsys.readouterr().out
    assert "WARNING: expected podman 5.4.2, got podman 9.9.9" in out
    assert "PASS" in out


# ── cli: the wall-time closer ──────────────────────────────────────


def _scripted_clock(monkeypatch: pytest.MonkeyPatch, *readings: float) -> None:
    """Script the walk's clock seam: one reading per ``_monotonic_now`` call."""
    clock = iter(readings)
    monkeypatch.setattr(cli, "_monotonic_now", lambda: next(clock))


def test_wall_time_is_the_runs_last_line(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wall-time line closes the run, after the summary and the prune."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    _scripted_clock(monkeypatch, 100.0, 100.0 + 12 * 60 + 34)

    assert cli.main(_args(tmp_path)) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[-1] == "Matrix wall time: 0:12:34"
    assert out.index("===== Matrix Summary =====") < out.index("pruned") < out.index("wall time")


def test_wall_time_survives_an_interrupt(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C still reports how long the run lived — it rides the teardown finally."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cli, "run_slot", _raise_interrupt)
    _scripted_clock(monkeypatch, 0.0, 42.0)

    assert cli.main(_args(tmp_path)) == 130

    assert "Matrix wall time: 0:00:42" in capsys.readouterr().out


def _raise_interrupt(*_args: Any, **_kwargs: Any) -> runner.SlotResult:
    raise KeyboardInterrupt


# ── cli: keyring preflight ─────────────────────────────────────────


def test_keyring_warning_fires_without_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A containers.conf without keyring=false earns the EDQUOT warning."""
    conf = tmp_path / "containers.conf"
    conf.write_text("[containers]\n", encoding="utf-8")
    monkeypatch.setenv("CONTAINERS_CONF", str(conf))

    cli._warn_keyring()

    assert "kernel keyring is not disabled" in capsys.readouterr().out


def test_keyring_warning_respects_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """keyring = false (with comment/spacing noise) silences the warning."""
    conf = tmp_path / "containers.conf"
    conf.write_text("[containers]\nkeyring = false  # quota\n", encoding="utf-8")
    monkeypatch.setenv("CONTAINERS_CONF", str(conf))

    cli._warn_keyring()

    assert capsys.readouterr().out == ""


def test_keyring_warning_when_no_conf_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No containers.conf anywhere: warn (the default keeps keyrings on)."""
    monkeypatch.delenv("CONTAINERS_CONF", raising=False)

    cli._warn_keyring()

    assert "kernel keyring" in capsys.readouterr().out


def test_run_matrix_via_main_uses_a_results_tempdir(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() provisions the shared results dir world-writable and cleans up."""
    seen: list[Path] = []
    original = cli._run_matrix

    def spy(config: Any, targets: list[str], args: Any, results_dir: Path) -> int:
        seen.append(results_dir)
        assert results_dir.exists()
        return original(config, targets, args, results_dir)

    monkeypatch.setattr(cli, "_run_matrix", spy)
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")

    assert cli.main(_args(tmp_path)) == 0

    assert not seen[0].exists()


def test_dbus_flavor_prints_no_version_strings(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dbus flavor never reported podman versions; headings stay bare."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    config_path = write_config(
        tmp_path,
        "image-prefix: t\nflavor: dbus\nslots:\n  debian13:\n"
        "phases:\n  - name: all\n    pytest: tests/ -v\n",
    )

    assert cli.main(["--config", str(config_path)]) == 0

    out = capsys.readouterr().out
    assert "expected podman" not in out
    assert "==> Testing debian13\n" in out


def test_walk_errors_are_reported_not_raised(
    tmp_path: Path,
    stubbed_host: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An OSError out of the walk (e.g. missing template) exits 2, no traceback."""
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")

    def broken_build(*_args: Any, **_kwargs: Any) -> bool:
        raise FileNotFoundError("containerfiles/podman/Containerfile.atari800")

    monkeypatch.setattr(cli, "build_image", broken_build)

    assert cli.main(_args(tmp_path)) == 2

    assert "Error:" in capsys.readouterr().err


def test_scope_flags_are_mutually_exclusive() -> None:
    """--unit-only and --integ-only cannot combine."""
    with pytest.raises(SystemExit):
        cli._parse_args(["--unit-only", "--integ-only"])


class RecordedPopen:
    """Stand-in for ``subprocess.Popen`` that scripts streamed output."""

    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.lines = lines
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> RecordedPopen:
        """Record the argv and hand back self as the process object."""
        self.calls.append(list(argv))
        return self

    def __enter__(self) -> RecordedPopen:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    @property
    def stdout(self) -> Any:
        """The scripted output lines, newline-terminated like a real pipe."""
        return iter(f"{line}\n" for line in self.lines)

    def wait(self) -> int:
        """The scripted exit code."""
        return self.returncode


def test_run_slot_with_line_prefix_streams_tagged_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A line_prefix tags every streamed line — live, attributable output
    for concurrent slots instead of buffered-away logs."""
    config = load_fixture(tmp_path)
    monkeypatch.setattr(runner.subprocess, "Popen", RecordedPopen(["hello", "world"]))
    results = tmp_path / "results"
    results.mkdir()

    result = runner.run_slot(config, "debian13", results, line_prefix="[debian13] ")

    assert result.passed
    out = capsys.readouterr().out
    assert "[debian13] hello" in out
    assert "[debian13] world" in out


def test_matrix_parallel_jobs_tags_lines_and_keeps_the_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """-j N streams tagged output per slot and reports the same summary a
    serial run would."""
    write_config(tmp_path)

    def fake_run_slot(config, name, results_dir, scope="all", line_prefix=None):
        assert line_prefix is not None, "concurrent slots must tag their lines"
        sys.stdout.write(f"{line_prefix}log-of-{name}\n")
        return runner.SlotResult(passed=name != "podman", observed="5.0.0")

    monkeypatch.setattr(cli, "run_slot", fake_run_slot)
    monkeypatch.setattr(cli, "_build_images", lambda *a, **k: set())
    monkeypatch.setattr(cli, "prune_dangling", lambda config: 0)
    monkeypatch.setattr(cli, "sweep_containers", lambda config: 0)
    monkeypatch.setattr(cli, "external_storage_leftovers", lambda: [])
    monkeypatch.setattr(cli, "_skip_reason", lambda config, name: "")

    rc = cli.main(["--config", str(tmp_path / "tests" / "containers" / "matrix.yml"), "-j", "3"])
    out = capsys.readouterr().out

    assert rc == 1  # podman slot scripted to fail
    assert "log-of-debian13" in out
    assert "==> debian13: PASS" in out
    assert "==> podman: FAIL" in out
    # every slot line carries its tag, aligned to the longest slot name
    for line in out.splitlines():
        if "log-of-debian13" in line:
            assert line.startswith("[debian13")
