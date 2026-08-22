# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Runtime-aware skip plugin for matrix test runs.

A matrix slot runs its whole suite in one runtime — a shared-kernel
container (crun/runc) or a libkrun microVM (krun).  Some tests cannot run
in a given runtime *by construction*: a krun microVM has its own kernel
but cannot launch nested containers and has no host loopback stack, while
a shared-kernel container cannot give a test the isolated kernel it needs.
Such a test must skip in the runtime it cannot use — not fail — so a slot
reports green for what it actually can run.

This plugin auto-skips those tests, tagged by the marker that governs
them, and writes the per-reason skip counts to
``<results>/<slot>.skips.json`` when the run is inside the matrix (the
runner reads them into the closing ``SKIPPED`` summary).  The rules apply
off-matrix too — a developer box without krun skips ``needs_krun`` tests —
but no file is written there.

Skip rules (marker → skip when):

* ``needs_krun`` — skip unless the runtime is krun (own kernel).
* ``needs_loopback`` — skip under krun (its TSI intercepts loopback server
  sockets, so bind-then-connect tests cannot pass).
* ``needs_vm`` — skip inside any matrix container (needs a full VM/HW:
  real LSM enforcement, a non-nested podman); reserved for the VM backend.
* ``needs_x86`` — skip when the host is not ``x86_64``.

The podman / nested-container suite (``needs_podman``) is deliberately NOT
skipped on krun: exercising it under an own kernel is the reason a krun
slot exists.  A nested-podman failure there is a setup bug to fix (e.g. the
storage driver), not a skip.

The plugin is loaded through a ``pytest11`` entry point, so every repo
that depends on terok-util gets it without wiring a conftest.  It lives as
a top-level module (not under the ``matrix`` package) and imports its
catalog constants lazily inside the hooks: pytest loads a ``pytest11``
plugin at bootstrap, before coverage instrumentation starts, so pulling the
matrix package in at import time would hide that whole package's real
coverage behind a bootstrap-load artifact.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

#: Marker → one-line reason, for registration and the skip message.  The
#: marker name is also the key the runner aggregates in its summary.  Note
#: ``needs_podman`` is NOT here: running the podman/nested-container suite is
#: the whole point of a krun slot (its own kernel), so it must never skip on
#: krun — a nested-podman failure there is a setup bug to fix, not a skip.
_MARKER_HELP = {
    "needs_krun": "needs an own kernel (krun); skipped under a shared-kernel runtime",
    "needs_loopback": "needs a host loopback TCP stack; skipped under krun (TSI)",
    "needs_vm": "needs a full VM/HW (real LSM, non-nested podman); skipped in a matrix container",
    "needs_x86": "needs an x86_64 host",
}

#: Markers this plugin introduces and therefore registers.  ``needs_podman``
#: (and other capability markers) are declared by the consuming repos — this
#: plugin only adds a runtime skip rule for them, so it must not re-register.
_OWNED_MARKERS = ("needs_krun", "needs_loopback", "needs_vm", "needs_x86")

#: Where this run accumulates its skip counts, keyed on config, so the
#: session-finish hook can write them once per pytest invocation.
_COUNTS_KEY = pytest.StashKey[dict]()


#: PID 1's comm inside a libkrun microVM — a runtime fact true whether or not
#: the run is a matrix run, so the skip rules hold on a bare krun box too.
_KRUN_INIT_COMM = "init.krun"


def _pid1_comm() -> str:
    """PID 1's ``comm`` (``init.krun`` inside a libkrun microVM), or ``""``."""
    try:
        return Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _under_krun() -> bool:
    """Whether the runtime is a krun microVM (its own, LSM-less kernel).

    The matrix sets [`KERNEL_ISOLATED_ENV`][terok_util.matrix.catalog.KERNEL_ISOLATED_ENV]
    under krun; PID 1's ``comm`` is the fallback so the rules also hold
    outside the matrix (a dev box or CI on a krun microVM).
    """
    from terok_util.matrix.catalog import KERNEL_ISOLATED_ENV

    return os.environ.get(KERNEL_ISOLATED_ENV) == "1" or _pid1_comm() == _KRUN_INIT_COMM


def _in_matrix() -> bool:
    """Whether the run is inside a matrix test container."""
    from terok_util.matrix.catalog import MATRIX_ENV

    return os.environ.get(MATRIX_ENV) == "1"


def _skip_reason(marker_names: set[str]) -> str | None:
    """The governing marker to skip on in this runtime, or ``None`` to run.

    Order is deterministic, not priority: a test rarely carries two of
    these, and the first applicable rule names the skip.
    """
    if "needs_krun" in marker_names and not _under_krun():
        return "needs_krun"
    if "needs_loopback" in marker_names and _under_krun():
        return "needs_loopback"
    if "needs_vm" in marker_names and _in_matrix():
        return "needs_vm"
    if "needs_x86" in marker_names and platform.machine() != "x86_64":
        return "needs_x86"
    return None


def pytest_configure(config: pytest.Config) -> None:
    """Register the runtime markers this plugin owns for ``--strict-markers``."""
    for name in _OWNED_MARKERS:
        config.addinivalue_line("markers", f"{name}: {_MARKER_HELP[name]}")
    config.stash[_COUNTS_KEY] = {}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests the current runtime cannot run, counting them by reason."""
    counts = config.stash[_COUNTS_KEY]
    for item in items:
        reason = _skip_reason({m.name for m in item.iter_markers()})
        if reason is None:
            continue
        item.add_marker(pytest.mark.skip(reason=f"{reason}: {_MARKER_HELP[reason]}"))
        counts[reason] = counts.get(reason, 0) + 1


def pytest_sessionfinish(session: pytest.Session) -> None:
    """In-matrix, merge this invocation's skip counts into the slot's file.

    A slot runs several pytest phases (unit, integration) as separate
    invocations; each merges its counts so the runner sees the slot total.
    """
    from terok_util.matrix.catalog import RESULTS_MOUNT, SLOT_ENV, SLOTS

    slot = os.environ.get(SLOT_ENV)
    counts = session.config.stash[_COUNTS_KEY]
    # ``slot`` names the report file; accept only a known catalog slot so a
    # stray or crafted SLOT_ENV can never steer the write outside the mount.
    if slot not in SLOTS or not counts:
        return
    path = Path(RESULTS_MOUNT) / f"{slot}.skips.json"
    merged: dict[str, int] = {}
    if path.is_file():
        try:
            merged = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            merged = {}
    for reason, count in counts.items():
        merged[reason] = merged.get(reason, 0) + count
    try:
        path.write_text(json.dumps(merged, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # best-effort telemetry; never fail a green run over a report file
