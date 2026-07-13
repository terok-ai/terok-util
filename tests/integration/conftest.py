# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Fixtures and skip markers for terok-util's integration suite.

The unit suite already covers every branch of this library with mocks.
What it *cannot* cover is the part of the contract that only a real
kernel, a real uid, and a real interpreter can falsify — so this suite
stays deliberately small and keeps to exactly that:

* ``test_paths_uid_xdg`` — user-namespace uid translation and the
  root/non-root fork in [`paths`][terok_util.paths].  Mocking
  ``os.geteuid`` proves nothing about what ``/proc/self/uid_map`` says
  under rootless podman.
* ``test_sensitive_file_kernel_effects`` — the permission and
  ``O_NOFOLLOW`` promises of [`fs`][terok_util.fs], observed as the
  kernel applies them (umask is process state; DAC bypass is a uid
  property; ``O_EXCL`` on a symlink is a VFS rule).
* ``test_import_laziness`` — the barrel's laziness on whatever Python
  the slot ships (3.12 / 3.13 / 3.14).  A stale ``sys.modules`` in the
  pytest process would hide a regression; a fresh child cannot.

Environment requirements are expressed with markers, not directory
placement:

- ``needs_host_features``: Linux kernel surfaces (``/proc``, umask, DAC).
- ``needs_unprivileged``: must run under a non-root uid.
- ``needs_root``: must run as real root.

None of it needs podman, which is why the matrix runs this repo on the
cheap ``dbus`` image flavor.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 — probe children are the point of this suite
import sys
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from terok_util.matrix import check_capability_contract

from .constants import CHILD_PATH, CHILD_TIMEOUT_S, CONTRACT_EXIT_CODE

# ── Matrix capability contract ─────────────────────────────────────────
# terok-util's integration tests reach for the kernel and the
# interpreter, and for nothing else — no D-Bus, no podman, no network.
# The probe map is therefore empty, and `expect:` in tests/containers/
# matrix.yml is correspondingly empty.  The wiring stays regardless: the
# day a capability *is* declared, this is what makes a slot that lacks it
# fail up front instead of dissolving into skips that read as green.
_CAPABILITY_PROBES: dict[str, Callable[[], bool]] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail the whole session when the matrix capability contract is broken."""
    if broken := check_capability_contract(_CAPABILITY_PROBES):
        pytest.exit(broken, returncode=CONTRACT_EXIT_CODE)


# ── Probe children ─────────────────────────────────────────────────────


@pytest.fixture
def child_json() -> Callable[[str, Mapping[str, str] | None], Any]:
    """Run a probe in a fresh interpreter; return the JSON it prints.

    Three of this suite's questions can only be asked of a *new* process:
    what a scrubbed ``XDG_*`` environment resolves to, what a hostile
    umask does to a mode, and which modules an import really pulls.  The
    fixture hands tests a one-liner for all three.

    The child's environment is replaced, not extended — ``PATH`` is the
    only inherited notion, and it is reinstated from a constant.  That is
    what lets a test say "with ``XDG_CONFIG_HOME`` unset" and mean it,
    whatever the operator (or the matrix container) had exported.  The
    interpreter is ``sys.executable``, so the child imports the same
    installed ``terok_util`` this session imported.
    """

    def run(code: str, env: Mapping[str, str] | None = None) -> Any:
        child_env = {"PATH": CHILD_PATH, **(env or {})}
        proc = subprocess.run(  # nosec B603 — fixed argv, no shell
            [sys.executable, "-c", code],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            pytest.fail(
                f"probe child exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    return run


@pytest.fixture
def real_uid() -> int:
    """The uid this test process actually runs under."""
    return os.geteuid()
