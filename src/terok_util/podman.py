# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Podman helpers shared by the terok packages.

Exposes helpers that spell podman arguments correctly for the host's
podman release: user-namespace mapping for rootless invocations and the
force-pull flag for image builds.
"""

from __future__ import annotations

import functools
import os
import subprocess  # nosec B404

_KEEP_ID_UID_MIN = (4, 3)
"""First podman release that understands ``keep-id:uid=…`` customization."""

_PULL_POLICY_MIN = (4, 0)
"""First podman release whose ``build --pull`` accepts a policy string."""

_DEV_ID = 1000
"""UID/GID of the conventional ``dev`` user in terok container images."""

_ID_SPAN = 65536
"""Container ID range the explicit maps cover — the standard rootless
allotment (the user's own ID plus 65535 subordinate IDs)."""


def podman_userns_args() -> list[str]:
    """Return user namespace args for rootless podman so UID 1000 maps correctly.

    Maps the host user to container UID/GID 1000, the conventional non-root
    ``dev`` user in terok container images.  Returns an empty list when
    running as root — root podman runs in the host user namespace and
    has no need to remap.

    Podman releases older than 4.3 predate the ``keep-id:uid=…``
    customization; for those the same mapping is spelled out as explicit
    ``--uidmap``/``--gidmap`` triples, assuming the standard 65536-ID
    subordinate allotment.
    """
    if os.geteuid() == 0:
        return []
    if _podman_version() >= _KEEP_ID_UID_MIN:
        return [f"--userns=keep-id:uid={_DEV_ID},gid={_DEV_ID}"]
    mappings = (
        f"0:1:{_DEV_ID}",  # container 0..dev-1  <- subordinate IDs
        f"{_DEV_ID}:0:1",  # container dev       <- the host user itself
        f"{_DEV_ID + 1}:{_DEV_ID + 1}:{_ID_SPAN - _DEV_ID - 1}",  # the rest, identity
    )
    return [arg for flag in ("--uidmap", "--gidmap") for m in mappings for arg in (flag, m)]


def podman_pull_always_args() -> list[str]:
    """Return the ``podman build`` flags that force re-pulling base images.

    Podman 4.0 turned ``--pull`` into a policy option (``--pull=always``);
    older releases take ``--pull`` as a boolean and reject the policy
    form outright, spelling the same request ``--pull-always`` instead.
    """
    if _podman_version() >= _PULL_POLICY_MIN:
        return ["--pull=always"]
    return ["--pull-always"]


@functools.cache
def _podman_version() -> tuple[int, int]:
    """Podman client version as ``(major, minor)``.

    Any probe failure — podman missing, unresponsive, or an unparseable
    version string — reports the modern baseline, so the caller's own
    podman invocation (not this probe) produces the user-facing error.
    """
    try:
        out = subprocess.run(  # nosec B603 B607
            ["podman", "version", "--format", "{{.Client.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        major, minor = out.strip().split(".")[:2]
        return (int(major), int(minor))
    except (OSError, subprocess.SubprocessError, ValueError):
        return _KEEP_ID_UID_MIN


__all__ = ["podman_pull_always_args", "podman_userns_args"]
