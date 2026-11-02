# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Privileged scanners use trusted settings outside the analyzed checkout."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from terok_util.yaml import load

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
SCANNER_HOST = "https://sonarcloud.io"
SCANNER_API = "https://api.sonarcloud.io"
UNTRUSTED_HOST = "https://scanner.invalid"
FAKE_TOKEN = "test-only-sonar-token"


@pytest.mark.parametrize("name", ["fleet-sonar.yml", "fleet-sonar-pr.yml"])
def test_scanner_uses_preserved_trusted_settings(name: str, tmp_path: Path) -> None:
    """A PR's hostile properties never become the secret-bearing scanner's config."""
    steps = load((WORKFLOWS / name).read_text())["jobs"]["sonar"]["steps"]
    trusted = steps[0]
    preserve = steps[1]
    scanner = steps[-1]
    checkout_index = next(
        index
        for index, step in enumerate(steps[2:], start=2)
        if step.get("uses", "").startswith("actions/checkout@")
    )
    prepare_reports = steps[checkout_index + 1]
    assert prepare_reports["name"] == "Prepare report directory"
    assert trusted["name"] == "Checkout trusted scanner settings"
    assert trusted["with"]["repository"] == "${{ github.repository }}"
    assert trusted["with"]["persist-credentials"] is False
    expected_ref = (
        "${{ github.event.repository.default_branch }}"
        if name == "fleet-sonar-pr.yml"
        else "${{ github.event.pull_request.base.sha || github.sha }}"
    )
    assert trusted["with"]["ref"] == expected_ref
    assert "SONAR_TOKEN" not in trusted.get("env", {})
    assert "SONAR_TOKEN" not in preserve.get("env", {})

    workspace = tmp_path / "checkout"
    trusted_dir = workspace / trusted["with"]["path"]
    trusted_dir.mkdir(parents=True)
    settings = "sonar.projectKey=trusted-project\nsonar.sources=src\n"
    (trusted_dir / "sonar-project.properties").write_text(settings)
    runner_temp = tmp_path / "runner"
    subprocess.run(
        ["bash", "-e", "-c", preserve["run"]],
        cwd=workspace,
        env=os.environ | {"RUNNER_TEMP": str(runner_temp)},
        check=True,
    )
    # The source checkout replaces everything in the workspace. The trusted
    # settings must survive outside it, including a PR's hostile default file.
    shutil.rmtree(workspace)
    workspace.mkdir()
    (workspace / "sonar-project.properties").write_text(
        f"sonar.host.url={UNTRUSTED_HOST}\n"
        f"sonar.scanner.apiBaseUrl={UNTRUSTED_HOST}\n"
        "sonar.scanner.javaExePath=./steal-token\n"
    )
    # A tracked reports symlink must not redirect later artifact extraction
    # into the preserved trusted settings directory.
    reports = workspace / "reports"
    reports.symlink_to(runner_temp / "terok-sonar", target_is_directory=True)
    subprocess.run(["bash", "-e", "-c", prepare_reports["run"]], cwd=workspace, check=True)
    assert reports.is_dir() and not reports.is_symlink()
    (reports / "sonar-project.properties").write_text(
        f"sonar.host.url={UNTRUSTED_HOST}\nsonar.scanner.javaExePath=./steal-token\n"
    )
    args = scanner["with"]["args"].replace("${{ runner.temp }}", str(runner_temp))
    for key in ("PR_NUMBER", "PR_REF", "PR_BASE", "PR_SHA"):
        args = args.replace("${{ env." + key + " }}", "fixture")
    properties = dict(arg.removeprefix("-D").split("=", 1) for arg in shlex.split(args))
    selected = Path(properties["project.settings"])
    assert not selected.is_relative_to(workspace)
    assert selected.read_text() == settings
    assert properties["sonar.host.url"] == SCANNER_HOST
    assert properties["sonar.scanner.apiBaseUrl"] == SCANNER_API
    assert scanner["env"]["SONAR_TOKEN"] == "${{ secrets.SONAR_TOKEN }}"

    # Exercise the actual argument list with a fake-token scanner stand-in.
    # It checks the selected config, never contacts either endpoint.
    command = """import os, pathlib, sys
props = dict(arg.removeprefix('-D').split('=', 1) for arg in sys.argv[1:])
assert os.environ['SONAR_TOKEN'] == 'test-only-sonar-token'
assert pathlib.Path(props['project.settings']).read_text() == os.environ['EXPECTED_SETTINGS']
assert props['sonar.host.url'] == os.environ['EXPECTED_HOST']
assert props['sonar.scanner.apiBaseUrl'] == os.environ['EXPECTED_API']
"""
    subprocess.run(
        [sys.executable, "-c", command, *shlex.split(args)],
        cwd=workspace,
        env=os.environ
        | {
            "SONAR_TOKEN": FAKE_TOKEN,
            "EXPECTED_SETTINGS": settings,
            "EXPECTED_HOST": SCANNER_HOST,
            "EXPECTED_API": SCANNER_API,
        },
        check=True,
    )
