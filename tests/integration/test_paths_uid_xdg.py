# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Path resolution under the uid the kernel actually sees.

[`host_uid`][terok_util.paths.host_uid] exists because ``os.geteuid()``
lies inside a user namespace, and every path resolver in
[`paths`][terok_util.paths] forks on its answer: ``host_uid() == 0``
means "real root" and sends config/state/runtime to ``/etc/terok``,
``/var/lib/terok``, ``/run/terok``; anything else means "an operator"
and sends them through the XDG chain.  Mocking ``geteuid`` cannot
falsify any of that — only a real ``/proc/self/uid_map`` can, and what
it contains depends on the distro, the container runtime, and how many
user namespaces the process is nested inside.

The uncomfortable finding this module pins down: under a *nested* user
namespace — the shape rootless podman's ``--userns=keep-id`` produces,
which is exactly how terok runs its agent containers — an unprivileged
process gets ``host_uid() == 0``.  The resolvers then take the root
branch and ignore ``XDG_*`` entirely, which is what defeats XDG-based
test isolation for every sibling package.  The tests that would fail on
that are marked ``xfail`` rather than quietly skipped: the behaviour is
wrong, we know it is wrong, and the marker is the record.
"""

from __future__ import annotations

import json
import os

import pytest

from terok_util.paths import host_uid

from .constants import (
    FHS_CONFIG_DIR,
    FHS_RUNTIME_DIR,
    FHS_STATE_DIR,
    IDENTITY_UID_MAP_ROW,
    NAMESPACE,
    UID_MAP_PATH,
)

pytestmark = pytest.mark.needs_host_features


# ── What this host actually is ─────────────────────────────────────────


def _uid_map_rows() -> list[tuple[int, int, int]]:
    """Parse ``/proc/self/uid_map`` into (inner, outer, length) rows."""
    if not UID_MAP_PATH.exists():
        return []
    rows: list[tuple[int, int, int]] = []
    for line in UID_MAP_PATH.read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) == 3:
            inner, outer, length = (int(p) for p in parts)
            rows.append((inner, outer, length))
    return rows


def _translate(euid: int, rows: list[tuple[int, int, int]]) -> int:
    """Apply the uid_map rows to *euid* the way the kernel would."""
    for inner, outer, length in rows:
        if inner <= euid < inner + length:
            return outer + (euid - inner)
    return euid


UID_MAP_ROWS = _uid_map_rows()
PROCESS_UID = os.geteuid()
IN_INITIAL_USERNS = UID_MAP_ROWS == [IDENTITY_UID_MAP_ROW]
RUNNING_AS_ROOT = PROCESS_UID == 0

#: The bug, stated as a predicate: an unprivileged process that
#: ``host_uid()`` nevertheless reports as uid 0.  True inside a
#: keep-id/nested-userns container, false on a plain host and inside a
#: plain (single-level) rootless container.
HOST_UID_CLAIMS_ROOT = not RUNNING_AS_ROOT and host_uid() == 0

_XDG_ISOLATION_DEFEATED = pytest.mark.xfail(
    HOST_UID_CLAIMS_ROOT,
    reason=(
        "known bug: under a nested user namespace /proc/self/uid_map's second "
        "column is the *parent* userns, not the initial one, so host_uid() "
        "reports 0 for an unprivileged process and every resolver takes the "
        "root branch, ignoring XDG_*"
    ),
    strict=False,
)

_needs_unprivileged = pytest.mark.skipif(
    RUNNING_AS_ROOT, reason="asserts the non-root branch; this process is uid 0"
)
_needs_root = pytest.mark.skipif(
    not RUNNING_AS_ROOT, reason="asserts the root branch; this process is not uid 0"
)
_needs_uid_map = pytest.mark.skipif(
    not UID_MAP_ROWS, reason="no /proc/self/uid_map (not a Linux user-namespace kernel)"
)


# ── The probe the children run ─────────────────────────────────────────

_RESOLVE_PROBE = """
import json, os
from terok_util.paths import (
    config_file_paths,
    host_uid,
    namespace_config_dir,
    namespace_runtime_dir,
    namespace_state_dir,
)

print(json.dumps({
    "euid": os.geteuid(),
    "host_uid": host_uid(),
    "config": str(namespace_config_dir()),
    "state": str(namespace_state_dir()),
    "runtime": str(namespace_runtime_dir()),
    "config_files": [str(p) for _, p in config_file_paths()],
}))
"""


# ── host_uid(): what it promises, and where it stops being true ────────


@_needs_uid_map
def test_host_uid_applies_the_uid_map_translation() -> None:
    """host_uid() is exactly geteuid() run through /proc/self/uid_map.

    The one claim that holds on every kernel and every nesting depth —
    the docstring's own description of the algorithm, checked against an
    independent implementation of it over the *live* map.  If a future
    refactor swaps the columns or drops the range arithmetic, this is
    the test that notices, on whatever the slot's uid_map happens to be.
    """
    assert host_uid() == _translate(PROCESS_UID, UID_MAP_ROWS)


@pytest.mark.skipif(not IN_INITIAL_USERNS, reason="process is inside a user namespace")
def test_host_uid_is_geteuid_in_the_initial_userns() -> None:
    """With the identity map in play there is nothing to translate."""
    assert host_uid() == PROCESS_UID


@_needs_unprivileged
@_XDG_ISOLATION_DEFEATED
def test_unprivileged_process_is_not_reported_as_root() -> None:
    """An operator who is not root must never be handed uid 0.

    This is the contract every caller of
    [`host_uid`][terok_util.paths.host_uid] actually relies on — the
    resolvers, and the ``AUTH EXTERNAL`` peer credentials the helper was
    written for.  It holds on a bare host and in a single-level rootless
    container; it fails under nesting, where the map's second column is
    a parent-userns uid rather than an initial-userns one.  Non-strict
    xfail: the same test must stay green on the hosts where the bug does
    not bite, and turn red the day someone fixes the nested case.
    """
    assert host_uid() != 0, f"geteuid()={PROCESS_UID} but host_uid()=0; uid_map rows={UID_MAP_ROWS}"


# ── The blast radius: which directories the resolvers pick ─────────────


@_needs_root
def test_root_lands_on_fhs_paths_and_ignores_xdg(child_json, tmp_path) -> None:
    """Real root uses the FHS tree even when XDG_* is exported.

    Not a bug — a root daemon has no business writing under some
    operator's ``~/.config``.  Asserted here because it is the *other*
    half of the fork, and because a root run is precisely what the
    root-in-container matrix slots give us for free.
    """
    result = child_json(
        _RESOLVE_PROBE,
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
    )

    assert result["config"] == str(FHS_CONFIG_DIR)
    assert result["state"] == str(FHS_STATE_DIR)
    assert result["runtime"] == str(FHS_RUNTIME_DIR)
    # Root reads the system config file only — no user layer to merge.
    assert result["config_files"] == [str(FHS_CONFIG_DIR / "config.yml")]


@_needs_unprivileged
@_XDG_ISOLATION_DEFEATED
def test_unprivileged_honours_the_xdg_chain(child_json, tmp_path) -> None:
    """XDG_* redirects config, state and runtime for a non-root process.

    The property every sibling's test suite leans on to keep its
    fixtures off the operator's real filesystem.  When ``host_uid()``
    claims root (see the module docstring) all three land in ``/etc``,
    ``/var/lib`` and ``/run`` instead — which is the mechanism behind
    the ten terok-shield unit tests that fail inside terok's own agent
    containers.
    """
    cfg, data, run = tmp_path / "cfg", tmp_path / "data", tmp_path / "run"
    result = child_json(
        _RESOLVE_PROBE,
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(cfg),
            "XDG_DATA_HOME": str(data),
            "XDG_RUNTIME_DIR": str(run),
        },
    )

    assert result["config"] == str(cfg / NAMESPACE)
    assert result["state"] == str(data / NAMESPACE)
    assert result["runtime"] == str(run / NAMESPACE)
    # The user layer is appended above the system one, highest priority last.
    assert result["config_files"] == [
        str(FHS_CONFIG_DIR / "config.yml"),
        str(cfg / NAMESPACE / "config.yml"),
    ]


@_needs_unprivileged
@_XDG_ISOLATION_DEFEATED
def test_unprivileged_falls_back_to_home_without_xdg(child_json, tmp_path) -> None:
    """With no XDG_* exported, a non-root process falls back under $HOME.

    The distro's ``platformdirs`` is what answers here, so this is a
    genuine per-slot question: an image that ships a patched or ancient
    platformdirs, or none at all (the import is optional), would place
    these elsewhere.
    """
    home = tmp_path / "home"
    home.mkdir()
    result = child_json(_RESOLVE_PROBE, {"HOME": str(home)})

    assert result["config"] == str(home / ".config" / NAMESPACE)
    assert result["state"] == str(home / ".local" / "share" / NAMESPACE)
    assert result["runtime"] == str(home / ".local" / "state" / NAMESPACE)


@_needs_unprivileged
def test_the_child_sees_the_same_uid_story_as_the_test_process(child_json, tmp_path) -> None:
    """A fresh process inherits the namespace, not just the env.

    Cheap, but it is the assumption the three tests above rest on: the
    probe child is subject to the same uid_map as the session that
    spawned it, so what it reports about paths is about *this* slot and
    not about some sanitised subprocess environment.  Printed as JSON so
    a failing matrix log carries the evidence.
    """
    result = child_json(_RESOLVE_PROBE, {"HOME": str(tmp_path)})

    assert result["euid"] == PROCESS_UID
    assert result["host_uid"] == host_uid(), json.dumps(result)
