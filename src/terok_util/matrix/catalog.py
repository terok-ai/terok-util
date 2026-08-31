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
# matrix run; EXPECT_ENV carries the comma-separated capability list;
# KERNEL_ISOLATED_ENV is set only when the slot runs under krun (its own
# kernel), so tests marked ``needs_own_kernel`` — untestable under a shared
# kernel, where the carrier's AppArmor/sysctl/netns state leaks in — run.
MATRIX_ENV = "TEROK_MATRIX"
EXPECT_ENV = "TEROK_EXPECT"
KERNEL_ISOLATED_ENV = "TEROK_KERNEL_ISOLATED"
# The slot name, exported into every matrix container so the runtime-aware
# skip plugin can write its per-reason skip counts to a slot-named file the
# runner aggregates into the closing SKIPPED summary.
SLOT_ENV = "TEROK_SLOT"

# The podman OCI-runtime name that boots a slot as a libkrun microVM (its
# own kernel).  The name is a site-defined ``[engine.runtimes]`` entry; this
# is the conventional one and the default the ``--krun`` flag selects.
KRUN_RUNTIME = "krun"

# crun-krun annotation that switches libkrun from its default TSI networking
# (Transparent Socket Impersonation — socket calls proxied to the host, so
# the guest has no real stack and 127.0.0.1 never loops back inside it) to a
# real virtio-net stack backed by passt.  Without it the story tests'
# in-guest broker<->upstream loopback hops are refused.  passt ships with
# rootless podman, so the host already has it.
KRUN_PASST_ANNOTATION = "krun.use_passt=1"

# Under krun the slot's rootfs is virtiofs, which root-squashes the subuid
# ``mkdir``/``chown`` rootless podman does as it unpacks/runs images
# (virtiofsd runs as the host user).  A loop-mounted ext4 image is a
# guest-kernel-owned filesystem where those ids resolve; the nested-podman
# store and buildah's per-``RUN``-step rootfs (``TMPDIR``) live on it.  The
# mount point is ``TMPDIR`` itself and is deliberately short: pytest hangs
# ``tmp_path`` off ``TMPDIR``, and the socket tests would blow the 107-byte
# ``AF_UNIX`` limit under a long prefix.  The image lives in the container's
# own (``--rm``-cleaned) rootfs, sparse — the size is a ceiling against a
# runaway pull, not an allocation.
KRUN_DISK_IMG = "/krun-disk.img"
KRUN_DISK_SIZE = "16G"
KRUN_DISK_MOUNT = "/kd"

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

    def runs_nested_podman(self, flavor: str) -> bool:
        """Whether this slot runs nested rootless podman under *flavor*.

        True only for a container-kind slot on the ``podman`` flavor: the nix
        slot has no podman inside, and the dbus flavor runs none.  This gates
        the resolv.conf fix and, under krun, the device/disk/tmp setup.
        """
        return flavor == "podman" and self.kind is SlotKind.CONTAINER


# Expected podman versions are pinned to the exact distro-shipped point
# release.  A mismatch is surfaced as a WARNING, never a failure — distro
# point releases are routine; the warning is a nudge to refresh the pin.
SLOTS: dict[str, SlotSpec] = {
    "debian12": SlotSpec(expected_podman="4.3.1"),
    "ubuntu2404": SlotSpec(expected_podman="4.9.3"),
    "ubuntu2604": SlotSpec(expected_podman="5.7.0"),
    "debian13": SlotSpec(expected_podman="5.4.2"),
    "fedora43": SlotSpec(expected_podman="5.8.4"),
    "fedora44": SlotSpec(expected_podman="5.8.4"),
    "podman": SlotSpec(expected_podman="latest", user="podman"),
    "alpine": SlotSpec(expected_podman="5.3.2", non_systemd=True),
    "void": SlotSpec(expected_podman="latest", non_systemd=True),
    "mageia": SlotSpec(expected_podman="4.9.5"),
    "manjaro": SlotSpec(expected_podman="6.1.0"),
    "nix": SlotSpec(kind=SlotKind.NIX),
}
