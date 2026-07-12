# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Generated container scripts — env contract, phase walk, slot kinds."""

from __future__ import annotations

from pathlib import Path

from terok_util.matrix.inner import inner_script, outer_script
from unit.matrix_fixtures import load_fixture, minimal_yml


def test_outer_prepares_workspace_and_drops_to_user(tmp_path: Path) -> None:
    """Root side: copy source, chown, init proof, resolv fix, su hand-off."""
    outer = outer_script(load_fixture(tmp_path), "debian13")

    assert "cp -a /src /workspace" in outer
    # Consumer-authored phase commands may pipe; a failing early stage
    # must fail the phase.
    assert "set -e -o pipefail" in outer
    assert "chown -R testrunner:testrunner /workspace" in outer
    assert "PID1" in outer
    assert "grep -v '^nameserver.*%'" in outer
    assert "exec su - testrunner -c /tmp/inner.sh" in outer


def test_outer_hard_fails_systemd_on_non_systemd_slots(tmp_path: Path) -> None:
    """Alpine must abort when systemd sneaks back into the base image."""
    outer = outer_script(load_fixture(tmp_path), "alpine")

    assert "non-systemd slot but systemd was detected" in outer
    assert "exit 1" in outer
    # debian13 only records the init system.
    assert "was detected" not in outer_script(load_fixture(tmp_path), "debian13")


def test_outer_nix_uses_python_setuid_instead_of_su(tmp_path: Path) -> None:
    """The bare nix image has no SUID su; users switch via os.setuid."""
    outer = outer_script(load_fixture(tmp_path), "nix")

    assert outer.index("os.setgroups([])") < outer.index("os.setuid(1000)")
    assert "su -" not in outer
    # No podman inside: the resolv.conf strip does not apply.
    assert "resolv" not in outer


def test_inner_exports_the_capability_contract(tmp_path: Path) -> None:
    """TEROK_MATRIX + TEROK_EXPECT open the script; hooks join after setup."""
    inner = inner_script(load_fixture(tmp_path), "debian13")

    assert "export TEROK_MATRIX=1" in inner
    assert "export TEROK_EXPECT=podman,nft,internet" in inner
    assert "export TEROK_EXPECT=${TEROK_EXPECT},hooks" in inner
    assert inner.index("stack-under-test setup") < inner.index("${TEROK_EXPECT},hooks")


def test_inner_walks_phases_in_order_and_aggregates_pytest_failures(tmp_path: Path) -> None:
    """Pytest phases keep going on failure; command phases stay bare (set -e)."""
    inner = inner_script(load_fixture(tmp_path), "debian13")

    without = inner.index("integration tests without hooks")
    setup = inner.index("install hooks")
    with_hooks = inner.index("integration tests with hooks")
    assert without < setup < with_hooks
    assert inner.count('if [ "$_rc" -eq 0 ]; then _rc=$_prc; fi') == 2
    assert "stack-under-test config 2>&1 || true" in inner
    assert inner.rstrip().endswith("exit $_rc")


def test_inner_scope_filter_selects_tagged_pytest_phases(tmp_path: Path) -> None:
    """--integ-only style runs keep command phases, drop mismatched pytest."""
    config = load_fixture(tmp_path)

    integ = inner_script(config, "debian13", scope="integ")
    assert "needs_hooks" in integ
    assert "stack-under-test setup" in integ

    unit = inner_script(config, "debian13", scope="unit")
    assert "pytest tests/integration" not in unit
    assert "stack-under-test setup" in unit


def test_inner_podman_flavor_reports_and_preflights(tmp_path: Path) -> None:
    """The observed podman version lands in the results mount; preflight is fatal."""
    inner = inner_script(load_fixture(tmp_path), "debian13")

    assert "/results/debian13.podman-version" in inner
    assert "rootless podman not functional" in inner
    assert "uv venv --python 3.12 .venv" in inner
    assert "uv sync --locked --active --no-default-groups --group test --group stories" in inner


def test_inner_nix_slot_reports_python_with_its_declared_contract(tmp_path: Path) -> None:
    """Nix: python-version recording, plain venv; a nix run is still a matrix
    run, so TEROK_MATRIX is set - and the fixture's empty expect override
    keeps the capability contract off."""
    inner = inner_script(load_fixture(tmp_path), "nix")

    assert "/results/nix.python-version" in inner
    assert "python3.12 -m venv .venv" in inner
    assert "export TEROK_MATRIX=1" in inner
    assert "TEROK_EXPECT" not in inner
    assert "XDG_RUNTIME_DIR" not in inner
    assert "uv sync --locked --active --no-default-groups --group test --group docs" in inner


def test_inner_dbus_flavor_has_no_podman_machinery(tmp_path: Path) -> None:
    """A dbus-flavor repo gets contract + venv + phases, nothing podman."""
    config = load_fixture(tmp_path, minimal_yml(flavor="dbus", head="expect: [dbus-daemon]\n"))
    inner = inner_script(config, "debian13")

    assert "export TEROK_EXPECT=dbus-daemon" in inner
    assert "podman" not in inner
    assert "resolv" not in outer_script(config, "debian13")


def test_inner_syncs_locked_groups_and_runs_bare_pytest(tmp_path: Path) -> None:
    """Deps come from ``uv sync`` against the lockfile; pytest phases run bare.

    ``--no-default-groups`` keeps the install declarative (runtime deps +
    exactly the listed groups), and the sync targets the venv the bootstrap
    activated, so no runner prefix is needed.
    """
    inner = inner_script(load_fixture(tmp_path), "debian13")

    assert "uv sync --locked --active --no-default-groups --group test --group stories" in inner
    assert inner.index("export UV_PYTHON_DOWNLOADS=never") < inner.index("uv venv")
    assert inner.index("rm -rf .venv") < inner.index("uv venv")
    assert "export UV_PYTHON_INSTALL_DIR=/opt/uv/python" in inner
    assert "pytest tests/integration/ -v --tb=short" in inner
    assert "poetry" not in inner


def test_inner_nix_slot_installs_uv_into_the_wrapped_venv(tmp_path: Path) -> None:
    """The nix slot keeps its stdlib venv on the wrapped interpreter; uv rides
    inside that venv."""
    inner = inner_script(load_fixture(tmp_path), "nix")

    assert "python3.12 -m venv .venv" in inner
    assert "pip install --quiet uv" in inner
    assert "uv sync --locked --active --no-default-groups --group test --group docs" in inner


def test_inner_scrubs_stale_venv_and_finds_managed_python(tmp_path: Path) -> None:
    """Every slot kind scrubs a copied-in .venv and re-exports the managed
    interpreter home — image ENV does not survive the su drop, and a stale
    checkout venv carries dead absolute shebangs."""
    for slot in ("debian13", "nix"):
        inner = inner_script(load_fixture(tmp_path), slot)
        assert "rm -rf .venv" in inner
        assert "export UV_PYTHON_DOWNLOADS=never" in inner
    assert "export UV_PYTHON_INSTALL_DIR=/opt/uv/python" in inner_script(
        load_fixture(tmp_path), "debian13"
    )
