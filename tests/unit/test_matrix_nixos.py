# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The NixOS slot boots the real system without disguising its non-FHS layout."""

from dataclasses import replace
from pathlib import Path

import pytest

from terok_util.matrix import runner
from terok_util.matrix.catalog import BOOT_TARGET, SLOT_SERVICE, SLOTS
from terok_util.matrix.cli import _skip_reason
from terok_util.matrix.inner import boot_units, inner_script, outer_script
from unit.matrix_fixtures import load_fixture, minimal_yml


def test_nixos_requires_boot_but_nix_packaging_does_not(tmp_path: Path) -> None:
    """A missing boot runtime is an explicit slot skip, not simulated NixOS."""
    config = load_fixture(tmp_path, minimal_yml(slot="nixos"))

    assert "requires --krun" in _skip_reason(config, "nixos")
    assert not _skip_reason(replace(config, krun=True), "nixos")
    assert SLOTS["nixos"].may_boot_systemd("dbus", krun=True)
    assert not SLOTS["nixos"].may_boot_systemd("podman", krun=False)
    assert not SLOTS["nix"].requires_boot


def test_nixos_boot_runs_activation_without_an_fhs_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mandatory /init boot must not fall back to an unactivated container."""
    config = replace(load_fixture(tmp_path, minimal_yml(slot="nixos")), krun=True)

    def unexpected_probe(*args: object, **kwargs: object) -> None:
        pytest.fail("NixOS runtime files do not exist until activation")

    monkeypatch.setattr(runner.subprocess, "run", unexpected_probe)
    assert runner._boots_systemd(config, "nixos")
    argv = runner._run_argv(config, "nixos", tmp_path, boots_systemd=True)
    assert argv[-4:] == [
        SLOTS["nixos"].boot_init,
        f"--unit={BOOT_TARGET}",
        "--show-status=no",
        "--log-level=warning",
    ]
    assert "KRUN_INIT_PID1=1" in argv
    assert "--init" not in argv


def test_nixos_scripts_use_system_shell_and_login_environment(tmp_path: Path) -> None:
    """The harness uses NixOS's normal environment rather than adding FHS links."""
    config = replace(load_fixture(tmp_path, minimal_yml(slot="nixos")), krun=True)
    shell = SLOTS["nixos"].bash_path

    outer = outer_script(config, "nixos", boots_systemd=True)
    inner = inner_script(config, "nixos")
    service = boot_units("nixos")[SLOT_SERVICE]
    assert outer.startswith(f"#!{shell}\n")
    assert inner.startswith(f"#!{shell}\n")
    assert "must boot systemd as PID 1" in outer
    assert "exec su - testrunner" in outer
    assert "rootless podman preflight" in inner
    assert f"ExecStart={shell} -l -o pipefail" in service
    assert f"ExecStopPost={shell}" in service


def test_nixos_image_is_a_system_closure_not_the_nix_packaging_image(tmp_path: Path) -> None:
    """Real NixOS activation, security wrappers and native tools reach the image."""
    config = load_fixture(tmp_path, minimal_yml(slot="nixos"))
    image = runner.render_containerfile(config, "nixos")

    assert 'modulesPath + "/virtualisation/docker-image.nix"' in image
    assert "config.system.build.tarball" in image
    assert "FROM scratch" in image
    assert "virtualisation.podman.enable = true" in image
    assert "subUidRanges" in image and "subGidRanges" in image
    assert "nftables" in image and "util-linux" in image
    assert "ln -s" not in image
    for tool in ("nsenter", "nft", "python3", "dnsmasq"):
        assert f"/usr/bin/{tool}" not in image
        assert f"/usr/sbin/{tool}" not in image


@pytest.mark.parametrize("flavor", ["podman", "dbus"])
def test_nixos_preserves_runtime_dns(tmp_path: Path, flavor: str) -> None:
    """NixOS must leave the runtime-supplied resolver configuration untouched."""
    config = load_fixture(tmp_path, minimal_yml(flavor=flavor, slot="nixos"))
    image = runner.render_containerfile(config, "nixos")

    assert "networking.resolvconf.enable = false;" in image
    assert "networking.useHostResolvConf" not in image
    assert 'environment.etc."resolv.conf"' not in image


@pytest.mark.parametrize("flavor", ["podman", "dbus"])
def test_nixos_builder_provides_its_tarball_decompressor(tmp_path: Path, flavor: str) -> None:
    """Extraction must not depend on xz being available in the base image."""
    config = load_fixture(tmp_path, minimal_yml(flavor=flavor, slot="nixos"))
    image = runner.render_containerfile(config, "nixos")
    builder, runtime = image.split("FROM scratch", maxsplit=1)

    nixpkgs = "-I nixpkgs=https://github.com/NixOS/nixpkgs/archive/${NIXPKGS_REV}.tar.gz"
    xz_build = "'<nixpkgs>' -A xz.bin -o /tmp/xz"
    extraction = "tar --use-compress-program=/tmp/xz-bin/bin/xz -xf"
    assert builder.count(nixpkgs) == 2
    assert builder.index(xz_build) < builder.index(extraction)
    assert "/tmp/xz" not in runtime


@pytest.mark.parametrize("flavor", ["podman", "dbus"])
def test_nixos_provides_native_dbus_build_and_keyring_libraries(
    tmp_path: Path, flavor: str
) -> None:
    """Complete suites can build D-Bus bindings and load keyutils without FHS links."""
    config = load_fixture(tmp_path, minimal_yml(flavor=flavor, slot="nixos"))
    image = runner.render_containerfile(config, "nixos")

    assert "gcc pkg-config meson ninja patchelf dbus" in image
    assert 'NINJA = "${pkgs.ninja}/bin/ninja";' in image
    assert 'PKG_CONFIG_PATH = lib.makeSearchPathOutput "dev" "lib/pkgconfig"' in image
    assert "[ dbus glib libffi pcre2 ]" in image
    assert "LD_LIBRARY_PATH = lib.makeLibraryPath (with pkgs; [ dbus glib keyutils ]);" in image
    assert "ln -s" not in image
