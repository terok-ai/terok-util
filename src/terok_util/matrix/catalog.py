# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Distro-slot catalog — facts about the shared matrix base images.

Everything in here is a property of a *slot* (the base image and how a
test container on it behaves), never of a consuming repository: which
Containerfile builds it, which podman the distro ships, whether the image
is systemd-free, and which user runs the tests.  Repo-specific choices
(slot selection, extra packages, test phases) live in each repo's
``matrix.yml`` — see [`config`][terok_util.matrix.config].

Two slot kinds exist:

* ``container`` — a regular distro image; tests run via ``su`` as the
  slot's test user.
* ``nix`` — the wrapped-Python oddball (no ``su``, no podman inside);
  the runner switches users via Python ``os.setuid`` and reports the
  Python version instead of a podman version.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# ── In-container geography (shared by every slot) ─────────────────

SOURCE_MOUNT = "/src"
WORKSPACE_DIR = "/workspace"
RESULTS_MOUNT = "/results"
PYTHON_VERSION = "3.12"

# Ownership label baked into every image layer so teardown can prune
# exactly this harness's dangling generations (value = the image prefix).
OWNERSHIP_LABEL = "io.terok.matrix-test"

# Env vars of the in-container capability contract: MATRIX_ENV marks a
# matrix run; EXPECT_ENV carries the comma-separated capability list.
MATRIX_ENV = "TEROK_MATRIX"
EXPECT_ENV = "TEROK_EXPECT"

# Shared Containerfile families a matrix.yml may select.
FLAVORS = ("podman", "dbus")

# The uv container image the templates copy the uv binary from, pinned to
# one minor so matrix runs stay reproducible while patches still flow.
UV_IMAGE_TAG = "0.11"

# Shared home of the image-provisioned uv-managed interpreter (see the
# ``_uv-python312.j2`` fragment).  Baked in at build time for distros
# whose system Python is too old; the inner script re-exports it because
# ``su - <test user>`` wipes container ENV — without the re-export uv
# cannot see the provisioned interpreter and fails outright (Python
# downloads are pinned off).
UV_MANAGED_PYTHON_DIR = "/opt/uv/python"


class SlotKind(StrEnum):
    """How a slot's test container is driven."""

    CONTAINER = "container"
    NIX = "nix"


@dataclass(frozen=True)
class SlotSpec:
    """Facts about one matrix slot's base image.

    Args:
        expected_podman: Distro-shipped podman version the slot is pinned
            to; ``"latest"`` for rolling images (and for slot kinds that
            never read it, like ``nix``).
        non_systemd: The runner hard-fails the slot if systemd is present —
            these slots exist to prove the systemd-free floor.
        user: Non-root user baked into the image (uid 1000).
        kind: Driving mode, see [`SlotKind`][terok_util.matrix.catalog.SlotKind].
    """

    expected_podman: str = "latest"
    non_systemd: bool = False
    user: str = "testrunner"
    kind: SlotKind = SlotKind.CONTAINER


# Expected podman versions are pinned to the exact distro-shipped point
# release.  A mismatch is surfaced as a WARNING, never a failure — distro
# point releases are routine; the warning is a nudge to refresh the pin.
SLOTS: dict[str, SlotSpec] = {
    "debian12": SlotSpec(expected_podman="4.3.1"),
    "ubuntu2404": SlotSpec(expected_podman="4.9.3"),
    "ubuntu2604": SlotSpec(expected_podman="5.7.0"),
    "debian13": SlotSpec(expected_podman="5.4.2"),
    "fedora43": SlotSpec(expected_podman="5.8.2"),
    "fedora44": SlotSpec(expected_podman="5.8.3"),
    "podman": SlotSpec(expected_podman="latest", user="podman"),
    "alpine": SlotSpec(expected_podman="5.3.2", non_systemd=True),
    "void": SlotSpec(expected_podman="latest", non_systemd=True),
    "mageia": SlotSpec(expected_podman="4.9.5"),
    "nix": SlotSpec(kind=SlotKind.NIX),
}
