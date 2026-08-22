# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Host-side assembly — shared templates, fragments, and podman argv."""

from __future__ import annotations

from pathlib import Path

import pytest

from terok_util.matrix.catalog import OWNERSHIP_LABEL, SLOTS, UV_IMAGE_TAG, SlotKind
from terok_util.matrix.runner import _run_argv, render_containerfile
from unit.matrix_fixtures import load_fixture, minimal_yml


def test_every_catalog_slot_has_a_template_per_flavor(tmp_path: Path) -> None:
    """The catalog and the packaged Containerfiles must not drift apart."""
    for name, spec in SLOTS.items():
        # The nix slot's template is flavor-independent; any declared flavor
        # exercises it.
        flavors = ("podman",) if spec.kind is SlotKind.NIX else ("podman", "dbus")
        for flavor in flavors:
            config = load_fixture(tmp_path / f"{name}-{flavor}", minimal_yml(flavor, name))
            rendered = render_containerfile(config, name)
            assert rendered.startswith("# SPDX-FileCopyrightText"), (flavor, name)
            assert 'LABEL "io.terok.matrix-test"="${IMAGE_PREFIX}"' in rendered, (flavor, name)
            assert "{%" not in rendered and "{{" not in rendered, (flavor, name)
            if spec.kind is not SlotKind.NIX:
                assert "$EXTRA_PACKAGES" in rendered, (flavor, name)


def test_shared_blocks_render_with_their_slot_knobs(tmp_path: Path) -> None:
    """The Jinja partials produce the per-slot variants of the shared blocks."""
    config = load_fixture(tmp_path)

    alpine = render_containerfile(config, "alpine")
    # AppArmor pasta workaround is base behavior for the podman flavor's
    # non-systemd slots, and musl images need an explicit bash login shell.
    assert 'default_rootless_network_cmd = "slirp4netns"' in alpine
    assert "useradd -m -s /bin/bash testrunner" in alpine
    assert f"ghcr.io/astral-sh/uv:{UV_IMAGE_TAG}" in alpine

    debian13 = render_containerfile(config, "debian13")
    assert "default_rootless_network_cmd" not in debian13
    assert 'driver = "overlay"' in debian13

    mageia_config = load_fixture(tmp_path / "m", minimal_yml(slot="mageia"))
    mageia = render_containerfile(mageia_config, "mageia")
    assert 'driver = "vfs"' in mageia
    assert "UV_PYTHON_INSTALL_DIR=/opt/uv/python" in mageia


def test_missing_template_error_is_catchable_as_oserror(tmp_path: Path) -> None:
    """TemplateNotFound must stay in the OSError family cli.main catches.

    The flavor is validated at load time, so this can only happen through
    drift (a catalog slot without a packaged template); the CLI turns
    OSError into a friendly exit-2 - lock in that jinja2's exception
    still qualifies.
    """
    from dataclasses import replace

    doctored = replace(load_fixture(tmp_path), flavor="no-such-flavor")

    with pytest.raises(OSError):
        render_containerfile(doctored, "debian13")


def test_fragment_is_appended_verbatim(tmp_path: Path) -> None:
    """A repo-local fragments/Containerfile.<slot> extends the shared image."""
    config = load_fixture(tmp_path)
    fragment_dir = config.containers_dir / "fragments"
    fragment_dir.mkdir()
    quirk = 'RUN printf "[network]\\n" >> /etc/containers/containers.conf'
    (fragment_dir / "Containerfile.debian13").write_text(quirk + "\n", encoding="utf-8")

    rendered = render_containerfile(config, "debian13")

    assert rendered.rstrip().endswith(quirk)
    assert quirk not in render_containerfile(config, "podman")


def test_run_argv_matches_the_flavor_and_kind(tmp_path: Path) -> None:
    """Nested-podman slots get privileged+fuse; nix and dbus stay unprivileged."""
    config = load_fixture(tmp_path)
    results = tmp_path / "results"

    podman_argv = _run_argv(config, "debian13", results)
    assert "--privileged" in podman_argv
    assert "/dev/fuse:rw" in podman_argv
    assert podman_argv[-2:] == ["bash", "/results/outer-debian13.sh"]
    # ``:z`` (shared SELinux label), not ``:Z``: /src and /results are shared
    # across parallel slots, so a private label would race (see _run_argv).
    assert f"{config.repo_root}:/src:ro,z" in podman_argv
    assert f"{results}:/results:rw,z" in podman_argv

    nix_argv = _run_argv(config, "nix", results)
    assert "--privileged" not in nix_argv
    assert "--security-opt" in nix_argv

    dbus_config = load_fixture(tmp_path / "dbus", minimal_yml(flavor="dbus"))
    dbus_argv = _run_argv(dbus_config, "debian13", results)
    assert "--privileged" not in dbus_argv
    assert "--security-opt" not in dbus_argv


def test_run_argv_injects_krun_runtime_and_kvm_when_enabled(tmp_path: Path) -> None:
    """--krun boots the slot as a libkrun microVM (its own kernel) with /dev/kvm."""
    from dataclasses import replace

    config = load_fixture(tmp_path)
    results = tmp_path / "results"

    assert "--runtime" not in _run_argv(config, "debian13", results)

    krun_argv = _run_argv(replace(config, krun=True), "debian13", results)
    assert krun_argv[krun_argv.index("--runtime") + 1] == "krun"
    assert "/dev/kvm:rw" in krun_argv
    # passt networking: without it libkrun's TSI mode has no working in-guest loopback.
    assert krun_argv[krun_argv.index("--annotation") + 1] == "krun.use_passt=1"


def test_inner_script_signals_kernel_isolation_only_under_krun(tmp_path: Path) -> None:
    """The in-container env carries TEROK_KERNEL_ISOLATED only when the slot runs under krun."""
    from dataclasses import replace

    from terok_util.matrix.inner import inner_script

    config = load_fixture(tmp_path)
    assert "TEROK_KERNEL_ISOLATED" not in inner_script(config, "debian13")
    assert "export TEROK_KERNEL_ISOLATED=1" in inner_script(replace(config, krun=True), "debian13")


def test_krun_outer_nudges_the_guest_clock(tmp_path: Path) -> None:
    """Under krun the outer script advances the clock so build mtimes aren't 'future'."""
    from dataclasses import replace

    from terok_util.matrix.inner import outer_script

    config = load_fixture(tmp_path)
    assert "date -s" not in outer_script(config, "debian13")  # crun shares the host clock
    assert "date -s '+2 seconds'" in outer_script(replace(config, krun=True), "debian13")


def test_krun_outer_recreates_dev_std_symlinks(tmp_path: Path) -> None:
    """Under krun the outer script restores /dev/stdin so ``podman build -f -`` works."""
    from dataclasses import replace

    from terok_util.matrix.inner import outer_script

    config = load_fixture(tmp_path)
    assert "/dev/stdin" not in outer_script(config, "debian13")  # crun's /dev already has it
    krun = outer_script(replace(config, krun=True), "debian13")
    assert "ln -s /proc/self/fd/0 /dev/stdin" in krun


def test_krun_outer_mounts_dev_mqueue_for_nested_podman(tmp_path: Path) -> None:
    """Under krun the nested-podman slots mount /dev/mqueue, which crun needs to
    create containers — the microVM's minimal /dev omits it."""
    from dataclasses import replace

    from terok_util.matrix.inner import outer_script

    config = load_fixture(tmp_path)
    krun = replace(config, krun=True)

    assert "mount -t mqueue mqueue /dev/mqueue" in outer_script(krun, "debian13")
    # not on crun (its /dev already has it), nor on a krun slot that runs no nested podman.
    assert "/dev/mqueue" not in outer_script(config, "debian13")
    assert "/dev/mqueue" not in outer_script(krun, "nix")


def test_krun_podman_slot_ext4_disk_for_store_and_short_tmpdir(tmp_path: Path) -> None:
    """Under krun the store binds to a loop-ext4 and TMPDIR is that (short) mount, not the workspace."""
    from dataclasses import replace

    from terok_util.matrix.inner import inner_script, outer_script

    config = load_fixture(tmp_path)
    krun = replace(config, krun=True)

    outer = outer_script(krun, "debian13")
    assert "mkfs.ext4" in outer
    assert "mount -o loop /krun-disk.img /kd" in outer
    assert "mount --bind /kd/store /home/testrunner/.local/share/containers" in outer
    # The rootless runroot binds to the ext4 too (podman 4.x subuid-chowns its
    # runroot resolv.conf under keep-id, which virtiofs squashes); path stays put.
    assert 'mount --bind /kd/run /run/user/"$(id -u testrunner)"' in outer
    # No workspace bind: fuse-overlayfs drives the context overlay on virtiofs.
    assert "mount --bind /kd/workspace" not in outer
    # TMPDIR is the short mount point itself (buildah RUN-step rootfs on ext4;
    # short so pytest's TMPDIR-rooted AF_UNIX sockets stay under 107 bytes).
    assert "export TMPDIR=/kd\n" in inner_script(krun, "debian13")

    assert "mkfs.ext4" not in outer_script(config, "debian13")  # crun overlay works direct
    assert "TMPDIR=/kd" not in inner_script(config, "debian13")
    assert "mkfs.ext4" not in outer_script(krun, "nix")  # nix runs no nested podman
    dbus = load_fixture(tmp_path / "dbus", minimal_yml(flavor="dbus"))
    assert "mkfs.ext4" not in outer_script(replace(dbus, krun=True), "debian13")


def test_run_argv_stamps_the_ownership_label_explicitly(tmp_path: Path) -> None:
    """Teardown's container sweep must not lean on image-label inheritance."""
    config = load_fixture(tmp_path)

    argv = _run_argv(config, "debian13", tmp_path / "results")

    label_flag = argv.index("--label")
    assert argv[label_flag + 1] == f"{OWNERSHIP_LABEL}={config.image_prefix}"


def test_network_error_signature_flags_host_failures_not_test_logic() -> None:
    """DNS/connection-layer lines are flagged; ordinary test output (and bare
    'connection refused') is not, so an intentional refused-assertion isn't mislabelled."""
    from terok_util.matrix.runner import _network_error_signature

    assert _network_error_signature(
        "fatal: unable to access 'https://github.com/x': Could not resolve host: github.com"
    )
    assert _network_error_signature("WARNING: fetch ...: temporary error (try again later)")
    assert _network_error_signature(
        "socket.gaierror: [Errno -3] Temporary failure in name resolution"
    )
    assert _network_error_signature("  Connection timed out after 30000 ms") is not None

    assert _network_error_signature("assert 200 == 502") is None
    assert (
        _network_error_signature("E   ConnectionRefusedError: [Errno 111] Connection refused")
        is None
    )
    # A git auth/403 failure is deterministic, not a host-network blip: the bare
    # "fatal: unable to access" prefix must not flag it (a real DNS failure still
    # matches on its "could not resolve host" reason, tested above).
    assert (
        _network_error_signature(
            "fatal: unable to access 'https://github.com/x': The requested URL returned error: 403"
        )
        is None
    )
    # the returned hint is the sanitized, truncated line
    hit = _network_error_signature("\x1b[31mCould not resolve host: proxy.local\x1b[0m")
    assert hit is not None
    assert "Could not resolve host" in hit
    assert "\x1b" not in hit
