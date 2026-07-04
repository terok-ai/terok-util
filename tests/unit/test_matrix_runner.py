# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Host-side assembly — shared templates, fragments, and podman argv."""

from __future__ import annotations

from pathlib import Path

import pytest

from terok_util.matrix.catalog import SLOTS, UV_IMAGE_TAG, SlotKind
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
    assert f"{config.repo_root}:/src:ro,Z" in podman_argv

    nix_argv = _run_argv(config, "nix", results)
    assert "--privileged" not in nix_argv
    assert "--security-opt" in nix_argv

    dbus_config = load_fixture(tmp_path / "dbus", minimal_yml(flavor="dbus"))
    dbus_argv = _run_argv(dbus_config, "debian13", results)
    assert "--privileged" not in dbus_argv
    assert "--security-opt" not in dbus_argv
