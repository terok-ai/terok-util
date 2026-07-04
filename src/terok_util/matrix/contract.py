# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The consumer half of the matrix capability contract.

The engine exports ``TEROK_EXPECT`` (a comma-separated capability list
from ``matrix.yml``) inside every matrix test container — see
[`inner`][terok_util.matrix.inner].  A consuming repo's integration
conftest owns the other half: a probe per capability it knows how to
verify, handed to
[`check_capability_contract`][terok_util.matrix.contract.check_capability_contract]
at session start.  Inside the matrix the harness built the image, so a
declared-but-missing capability means the slot is broken and must fail
up front — not dissolve into skips that read as green.

Typical conftest wiring::

    _CAPABILITY_PROBES = {"podman": lambda: binary_on_path("podman"), ...}

    def pytest_sessionstart(session):
        if broken := check_capability_contract(_CAPABILITY_PROBES):
            pytest.exit(broken, returncode=3)

The probe *maps* rightly differ per repo (shield probes its own hook
chain, clearance only D-Bus); the protocol — env var, separator,
unknown-name rejection — lives here so it cannot drift apart again.
"""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Callable, Mapping

from .catalog import EXPECT_ENV

# dnsmasq/nft install into sbin on several distros while the test user's
# PATH may omit those dirs — probes search with them appended.
_SBIN_DIRS = ("/usr/sbin", "/sbin", "/usr/local/sbin")


def check_capability_contract(probes: Mapping[str, Callable[[], bool]]) -> str | None:
    """Verify ``TEROK_EXPECT`` against *probes*; return the failure, if any.

    Args:
        probes: Capability name to presence probe, the repo-specific half
            of the contract.

    Returns:
        A human-readable failure message when the contract names unknown
        capabilities or a declared capability is missing; ``None`` when
        the contract holds (or none is declared — dev-machine runs).
    """
    expected = {c for c in os.environ.get(EXPECT_ENV, "").split(",") if c}
    if not expected:
        return None
    unknown = expected - probes.keys()
    if unknown:
        return f"{EXPECT_ENV} names unknown capabilities: {sorted(unknown)}"
    missing = sorted(cap for cap in expected if not probes[cap]())
    if missing:
        return "matrix capability contract broken - expected but missing: " + ", ".join(missing)
    return None


def binary_on_path(name: str) -> bool:
    """Whether *name* resolves on the sbin-extended ``PATH``."""
    search = os.pathsep.join([os.environ.get("PATH", ""), *_SBIN_DIRS])
    return shutil.which(name, path=search) is not None


def tcp_reachable(ip: str, port: int, timeout: float = 5.0) -> bool:
    """Whether a TCP connection to ``ip:port`` succeeds within *timeout*."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False
