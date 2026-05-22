# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Podman helpers shared by the terok packages.

Currently exposes a single helper that maps the host UID/GID into the
container user namespace for rootless podman invocations.
"""

from __future__ import annotations

import os


def podman_userns_args() -> list[str]:
    """Return user namespace args for rootless podman so UID 1000 maps correctly.

    Maps the host user to container UID/GID 1000, the conventional non-root
    ``dev`` user in terok container images.  Returns an empty list when
    running as root — root podman runs in the host user namespace and
    has no need to remap.
    """
    if os.geteuid() == 0:
        return []
    return ["--userns=keep-id:uid=1000,gid=1000"]


__all__ = ["podman_userns_args"]
