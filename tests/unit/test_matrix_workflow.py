# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The shared workflow preserves an explicitly selected whole-run runtime."""

import os
import subprocess
from pathlib import Path

import pytest

from terok_util.yaml import load

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


@pytest.mark.parametrize("krun", [False, True])
@pytest.mark.parametrize("slot", ["fedora44", "nixos"])
def test_matrix_workflow_forwards_explicit_krun_for_every_slot(krun: bool, slot: str) -> None:
    """Neither selecting NixOS nor an FHS distro changes the chosen runtime."""
    workflow = load((WORKFLOWS / "fleet-test-matrix.yml").read_text())
    option = workflow["on"]["workflow_call"]["inputs"]["krun"]
    assert option["type"] == "boolean"
    assert option["default"] is False
    step = workflow["jobs"]["matrix"]["steps"][-1]
    assert step["env"]["KRUN"] == "${{ inputs.krun }}"

    result = subprocess.run(
        ["bash", "-c", 'uv() { printf "%s\\n" "$@"; }\n' + step["run"]],
        env=os.environ | {"KRUN": str(krun).lower(), "SLOT": slot},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        "run",
        "--no-sync",
        "terok-matrix",
        *(["--krun"] if krun else []),
        slot,
    ]


def test_util_matrix_workflow_installs_and_uses_its_own_engine() -> None:
    """The engine's own matrix does not need a published version of itself."""
    caller = load((WORKFLOWS / "test-matrix.yml").read_text())
    assert caller["on"]["workflow_dispatch"]["inputs"]["krun"]["default"] is False
    assert caller["jobs"]["matrix"]["uses"] == "./.github/workflows/fleet-test-matrix.yml"
    assert caller["jobs"]["matrix"]["with"]["krun"] == "${{ inputs.krun }}"

    shared = load((WORKFLOWS / "fleet-test-matrix.yml").read_text())
    for job in shared["jobs"].values():
        sync = next(step["run"] for step in job["steps"] if "uv sync" in step.get("run", ""))
        assert sync == "uv sync --locked --no-default-groups"
