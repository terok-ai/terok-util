# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

{ lib, pkgs, modulesPath, ... }:
{
  imports = [ (modulesPath + "/virtualisation/docker-image.nix") ];
  system.stateVersion = "26.05";
  documentation.enable = false;
  networking.useDHCP = false;
  networking.resolvconf.enable = false;
  services.dbus.enable = true;

  # Native Python extensions and ctypes libraries retain their Nix store paths.
  environment.variables = {
    # Build isolation's wheel-provided Ninja expects an FHS loader.
    NINJA = "${pkgs.ninja}/bin/ninja";
    PKG_CONFIG_PATH = lib.makeSearchPathOutput "dev" "lib/pkgconfig"
      (with pkgs; [ dbus glib libffi pcre2 ]);
    LD_LIBRARY_PATH = lib.makeLibraryPath (with pkgs; [ dbus glib keyutils ]);
  };

  users.groups.testrunner.gid = 1000;
  users.users.testrunner = {
    isNormalUser = true;
    uid = 1000;
    group = "testrunner";
    shell = pkgs.bashInteractive;
    # Fit inside the outer rootless container's user-namespace mapping.
    subUidRanges = [ { startUid = 1; count = 999; } { startUid = 1001; count = 64535; } ];
    subGidRanges = [ { startGid = 1; count = 999; } { startGid = 1001; count = 64535; } ];
  };

{% if flavor == "podman" %}
  virtualisation.podman.enable = true;
  virtualisation.containers = {
    containersConf.settings = {
      engine.cgroup_manager = "cgroupfs";
      engine.events_logger = "file";
      containers.keyring = false;
    };
    storage.settings.storage.options.overlay = {
      mount_program = "${pkgs.fuse-overlayfs}/bin/fuse-overlayfs";
      mountopt = "nodev,fsync=0";
    };
  };
  environment.etc."containers/registries.conf.d/99-matrix-mirrors.conf".text = ''
    [[registry]]
    prefix = "docker.io"
    location = "docker.io"
    [[registry.mirror]]
    location = "mirror.gcr.io"
    [[registry.mirror]]
    location = "public.ecr.aws/docker"
  '';
{% endif %}

  environment.systemPackages = with pkgs; [
    python312 uv gitMinimal openssh curl cacert
    util-linux e2fsprogs
    gcc pkg-config meson ninja patchelf dbus
{% if flavor == "podman" %}
    nftables dnsmasq bind
{% endif %}
    bashInteractive coreutils findutils gnugrep gnused gawk gnutar gzip xz
  ] ++ map (name: pkgs.${name})
    (lib.filter (name: name != "") (lib.splitString " " (builtins.getEnv "EXTRA_PACKAGES")));
}
