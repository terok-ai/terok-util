# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Unit-test fixtures for terok-util.

The autouse [`_isolate_user_paths`][tests.unit.conftest._isolate_user_paths]
fixture redirects ``HOME`` and every ``XDG_*`` / ``TEROK_*`` knob to a
fresh tmp directory so that tests exercising default-path code paths
(e.g. [`namespace_state_dir()`][terok_util.paths.namespace_state_dir]
with no overrides) never land on the operator's real
``~/.config/terok`` / XDG state.
"""

from __future__ import annotations

import pytest

# Terok-specific env vars that override path resolution.  The autouse
# isolation fixture unsets each so resolution falls back through the
# tmp-rooted ``HOME`` / ``XDG_*`` chain — never to the operator's real
# state.  Kept in one place so a new ``TEROK_*_DIR`` knob added to any
# sibling only needs one edit here.
_TEROK_PATH_OVERRIDE_ENV_VARS = (
    "TEROK_CONFIG_DIR",
    "TEROK_STATE_DIR",
    "TEROK_RUNTIME_DIR",
    "TEROK_ROOT",
    "TEROK_CONFIG_FILE",
)


@pytest.fixture(autouse=True)
def _isolate_user_paths(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect ``HOME`` and every ``XDG_*`` / ``TEROK_*`` knob to a fresh tmp dir.

    Without this, tests that exercise default-config code paths fall
    through to the operator's real ``~/.config/terok`` and XDG state
    directories — silently passing on a clean machine and mutating
    those directories on a populated one.

    Uses ``tmp_path_factory`` rather than ``tmp_path`` so the fake home
    lives outside the per-test ``tmp_path`` — otherwise tests that
    iterate ``tmp_path`` looking for fixtures see a stray ``fake-home``
    entry.  The per-test ``monkeypatch`` undoes the env overrides at
    teardown, so tests that need different env state can layer their
    own ``setenv`` / ``delenv`` calls on top without leaking across
    cases.
    """
    fake_home = tmp_path_factory.mktemp("fake-home")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(fake_home / ".local" / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(fake_home / ".cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(fake_home / "run"))
    for var in _TEROK_PATH_OVERRIDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
