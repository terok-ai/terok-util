# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Generate the scripts that run inside a slot's test container.

Two scripts per slot, both written to the shared results mount so the
container executes real files — the historical ``bash -c`` string with
its three levels of quote-escaping is gone:

* the **outer** script runs as root: copy the read-only source mount into
  a writable workspace, prove the init system matches the slot's
  contract, then drop to the slot's test user;
* the **inner** script runs as the test user: export the capability
  contract, bootstrap a Python 3.12 venv plus uv, sync the repo's
  locked dependency groups, and walk the configured phases.

Command phases abort the slot on failure (``set -e``); pytest phases
record the first failing exit code and keep going, so a single run
surfaces every failing suite.
"""

from __future__ import annotations

from .catalog import (
    EXPECT_ENV,
    KERNEL_ISOLATED_ENV,
    KRUN_DISK_IMG,
    KRUN_DISK_MOUNT,
    KRUN_DISK_SIZE,
    MATRIX_ENV,
    PYTHON_VERSION,
    RESULTS_MOUNT,
    SLOT_ENV,
    SLOTS,
    SOURCE_MOUNT,
    UV_MANAGED_PYTHON_DIR,
    WORKSPACE_DIR,
    SlotKind,
)
from .config import MatrixConfig

#: Uid baked into every slot image for the non-root test user.
TEST_UID = 1000


def outer_script(config: MatrixConfig, slot_name: str) -> str:
    """Root-side container entry: workspace prep, init-system proof, user drop."""
    spec = SLOTS[slot_name]
    lines = ["#!/bin/bash", "set -e -o pipefail", ""]
    if config.krun:
        lines += _krun_dev_std_symlinks()
        lines += _krun_clock_skew_guard()
        if spec.runs_nested_podman(config.flavor):
            lines += _krun_relax_devices()
            lines += _krun_mount_mqueue()
            lines += _krun_real_disk(spec.user)
    lines += [
        f"cp -a {SOURCE_MOUNT} {WORKSPACE_DIR}",
        f"chown -R {spec.user}:{spec.user} {WORKSPACE_DIR}",
    ]
    if spec.kind is SlotKind.CONTAINER:
        lines += _init_system_proof(slot_name)
    if spec.runs_nested_podman(config.flavor):
        lines += _resolv_conf_strip()
    lines += [
        "",
        f"install -m 0755 {RESULTS_MOUNT}/inner-{slot_name}.sh /tmp/inner.sh",
        *_user_drop(spec.user, spec.kind),
        "",
    ]
    return "\n".join(lines)


def inner_script(config: MatrixConfig, slot_name: str, scope: str = "all") -> str:
    """Test-user-side flow: env contract, venv + deps, configured phases."""
    spec = SLOTS[slot_name]
    lines = ["#!/bin/bash", "set -e -o pipefail", ""]
    if spec.kind is SlotKind.CONTAINER:
        lines += ["export XDG_RUNTIME_DIR=/run/user/$(id -u)"]
    if config.krun and spec.runs_nested_podman(config.flavor):
        lines += _krun_tmpdir_export()
    lines += _env_contract(config, slot_name)
    lines += ["", f"cd {WORKSPACE_DIR}", ""]
    if spec.kind is SlotKind.NIX:
        lines += _nix_python_report(slot_name)
        lines += _plain_venv_bootstrap(f"python{PYTHON_VERSION}")
    else:
        if config.flavor == "podman":
            lines += _podman_report_and_preflight(slot_name)
        lines += _uv_or_venv_bootstrap()
    lines += _uv_sync(config.slot_groups(slot_name))
    lines += _phase_walk(config, slot_name, scope)
    return "\n".join(lines) + "\n"


# ── Outer building blocks ──────────────────────────────────────────


def _krun_clock_skew_guard() -> list[str]:
    """Advance the guest clock so build tools never see fs mtimes as ``future``.

    Under krun the container filesystem is virtiofs, which stamps file mtimes
    with the *host* clock, but the microVM guest's wall clock lags it (tens of
    ms — a plain offset, not a timezone).  A just-written file then looks
    future-dated to strict build tools: meson aborts with ``Clock skew
    detected`` and, for example, ``dbus-python`` fails to build.  Nudge the
    guest clock a couple of seconds ahead of the fs clock so every file reads
    as past.  Root-only (the outer script runs as root); best-effort so a
    read-only clock never aborts the slot.
    """
    return [
        'echo "--- krun: nudging the guest clock past the virtiofs mtime clock ---"',
        "date -s '+2 seconds' >/dev/null 2>&1 || true",
        "",
    ]


def _krun_dev_std_symlinks() -> list[str]:
    """Give the krun guest the standard ``/dev/std*`` fd symlinks.

    A crun container inherits ``/dev/stdin`` → ``/proc/self/fd/0`` (and the
    stdout/stderr/fd siblings) from the runtime's device setup, but the
    libkrun microVM boots a bare devtmpfs without them.  ``podman build
    -f -`` then cannot stat ``/dev/stdin`` — buildah falls back to
    resolving it against the build context (``stat <context>/dev/stdin``)
    and dies with exit 125.  Recreate the POSIX symlinks, best-effort and
    only when absent — no ``chmod``/``mknod`` on the guest ``/dev``, which
    upsets libkrun's device model.
    """
    links = {"stdin": 0, "stdout": 1, "stderr": 2}
    lines = [
        'echo "--- krun: ensuring /dev/std* fd symlinks (bare microVM devtmpfs lacks them) ---"'
    ]
    lines += [
        f"[ -e /dev/{name} ] || ln -s /proc/self/fd/{fd} /dev/{name} 2>/dev/null || true"
        for name, fd in links.items()
    ]
    lines += ["[ -e /dev/fd ] || ln -s /proc/self/fd /dev/fd 2>/dev/null || true", ""]
    return lines


def _krun_relax_devices() -> list[str]:
    """0666 the ``/dev`` nodes rootless podman needs — the microVM leaves them root-only.

    The libkrun guest kernel creates device nodes root-only (no udev in the
    microVM to relax them), so the rootless test user cannot open them:
    ``/dev/fuse`` (fuse-overlayfs — ``failed to open /dev/fuse: Permission
    denied``) and ``/dev/net/tun`` (pasta/slirp networking for a build's
    ``RUN`` — ``Failed to open() /dev/net/tun: Permission denied``).  podman
    surfaces both only as a bare ``exit status 1``/``125``.  A crun container
    inherits these at 0666 from its runtime; recreate that.  Individual
    device nodes (not the ``/dev`` mount) are safe for libkrun; best-effort
    so a read-only or absent node never aborts the slot.
    """
    return [
        'echo "--- krun: relaxing /dev/fuse + /dev/net/tun for rootless podman ---"',
        "chmod 0666 /dev/fuse /dev/net/tun 2>/dev/null || true",
        "",
    ]


def _krun_mount_mqueue() -> list[str]:
    """Mount ``/dev/mqueue`` — the microVM's minimal ``/dev`` omits it.

    crun sets up a POSIX message-queue mount at ``/dev/mqueue`` for every
    container it creates; when the nested-podman host's own ``/dev`` has no
    ``/dev/mqueue`` to stat, container creation aborts (``cannot stat
    /dev/mqueue: No such file or directory``), surfaced only as a bare
    ``exit status 1``/``125`` from ``podman build``/``run``.  A crun
    container inherits this mount from its runtime; the libkrun guest does
    not, so mount it once here.  Best-effort so an already-mounted or
    unsupported node never aborts the slot.
    """
    return [
        'echo "--- krun: mounting /dev/mqueue for nested podman ---"',
        "mkdir -p /dev/mqueue && mount -t mqueue mqueue /dev/mqueue 2>/dev/null || true",
        "",
    ]


def _krun_real_disk(user: str) -> list[str]:
    """Loop-mount one ext4 disk under krun for the two paths that need it.

    krun's rootfs is virtiofs, which root-squashes the subuid ``mkdir``/
    ``chown`` that rootless podman does as it unpacks and runs images
    (virtiofsd runs as the host user and can't set those ids).  Three things
    hit that wall, all fixed by living on a guest-kernel-owned ext4:

    * the nested-podman **store** — the image-unpack untar
      (``.pivot_root: permission denied``);
    * **build tmp** — buildah runs each ``RUN`` step in a throwaway
      container whose rootfs it scaffolds under ``GetTempDir()`` (``TMPDIR``,
      via [`_krun_tmpdir_export`][terok_util.matrix.inner._krun_tmpdir_export]);
      on virtiofs that ``mkdir …/mnt/rootfs`` is squashed.
    * the rootless **runroot** (``$XDG_RUNTIME_DIR/containers``, separate from
      the store) — podman 4.x subuid-chowns each container's runroot files
      (e.g. ``resolv.conf``) under ``keep-id``, squashed on virtiofs (podman
      ≥5 skips that write when ``/etc/resolv.conf`` is volume-mounted, so only
      the 4.x slots tripped it).

    The build **workspace** is deliberately *not* bound here: the build's
    read-only context overlay is driven by fuse-overlayfs, which works over
    plain virtiofs, so the repo copy stays on the rootfs.  The mount point is
    short (``/kd``) on purpose — ``TMPDIR`` is it, and pytest hangs its
    ``tmp_path`` (and the Unix sockets tests bind there) off ``TMPDIR``; a
    long prefix pushes those past the 107-byte ``AF_UNIX`` limit.

    The image is sparse and lives in the container's own (``--rm``-cleaned)
    rootfs; no journal (throwaway).  Needs ``e2fsprogs`` in the image.
    ``install -d`` as root chowns only its leaf, so name ~/.local and
    ~/.local/share too or the test user can't create a sibling like
    ~/.local/share/terok (shield state).
    """
    disk, home = KRUN_DISK_MOUNT, f"/home/{user}"
    store = f"{home}/.local/share/containers"
    return [
        'echo "--- krun: loop-ext4 for the store + build tmp + runroot (virtiofs squashes subuid mkdir) ---"',
        f"truncate -s {KRUN_DISK_SIZE} {KRUN_DISK_IMG}",
        f"mkfs.ext4 -qF -O ^has_journal -E lazy_itable_init=1 {KRUN_DISK_IMG}",
        f"mkdir -p {disk}",
        f"mount -o loop {KRUN_DISK_IMG} {disk}",
        f"mkdir -p {disk}/store",
        f"chown {user}:{user} {disk} {disk}/store",
        # ``install -d`` (as root) chowns only the leaf, leaving ~/.local and
        # ~/.local/share root-owned; the test user must own them too, or a
        # sibling like ~/.local/share/terok (shield state) can't be created.
        f"install -d -o {user} -g {user} {home}/.local {home}/.local/share {store}",
        f"mount --bind {disk}/store {store}",
        # Bind the rootless runroot ($XDG_RUNTIME_DIR/containers) to the ext4
        # too — its path (/run/user/<uid>) stays put so XDG + the 107-byte
        # AF_UNIX limit are unaffected, but the subuid runroot writes now land
        # on the guest-kernel-owned ext4 instead of the squashing virtiofs.
        f"install -d -o {user} -g {user} -m 0700 {disk}/run",
        f'mount --bind {disk}/run /run/user/"$(id -u {user})"',
        "",
    ]


def _krun_tmpdir_export() -> list[str]:
    """Point ``TMPDIR`` at the ext4 disk so buildah's RUN-step rootfs lands there.

    Runs in the inner (test-user) script.  buildah scaffolds each ``RUN``
    step's throwaway-container rootfs under ``GetTempDir()`` (``TMPDIR`` or
    ``/var/tmp``); on virtiofs the subuid ``mkdir …/mnt/rootfs`` is squashed,
    so it must be the ext4 mount from
    [`_krun_real_disk`][terok_util.matrix.inner._krun_real_disk].  The mount
    point *is* ``TMPDIR`` (not a subdir) and is short so pytest's
    ``TMPDIR``-rooted Unix sockets stay under the 107-byte ``AF_UNIX`` limit.
    """
    return [f"export TMPDIR={KRUN_DISK_MOUNT}"]


def _init_system_proof(slot_name: str) -> list[str]:
    """Record PID1; hard-fail non-systemd slots if systemd sneaks back in."""
    on_systemd_present = ['    echo "systemd: present"']
    if SLOTS[slot_name].non_systemd:
        on_systemd_present += [
            f"    echo \"FATAL: '{slot_name}' is a non-systemd slot but systemd was detected\" >&2",
            "    exit 1",
        ]
    return [
        "",
        "# Non-systemd slots must run on a genuinely systemd-free host; fail",
        "# loudly if a future base image regresses that.  Other slots just",
        "# record their init system in the log.",
        'echo "--- init system: PID1=$(cat /proc/1/comm 2>/dev/null || echo unknown) ---"',
        "if command -v systemctl >/dev/null 2>&1 || [ -d /run/systemd/system ]; then",
        *on_systemd_present,
        "else",
        '    echo "systemd: absent - non-systemd host confirmed"',
        "fi",
    ]


def _resolv_conf_strip() -> list[str]:
    """Drop IPv6 zone-ID nameservers the nested resolvers cannot use."""
    return [
        "",
        "# Strip IPv6 zone-ID nameservers - they reference host interfaces",
        "# (e.g. eno1) that don't exist inside the container, causing dig to",
        "# reject the entire resolv.conf.  Fixed upstream in podman 5.4+",
        "# (https://github.com/containers/common/pull/2233).",
        "# Remove once we drop < 5.4 support.",
        "cp /etc/resolv.conf /tmp/resolv.conf.clean",
        "grep -v '^nameserver.*%' /tmp/resolv.conf.clean > /etc/resolv.conf",
    ]


def _user_drop(user: str, kind: SlotKind) -> list[str]:
    """Hand off to the test user — ``su`` normally, ``os.setuid`` on nix.

    The bare ``nixos/nix`` image ships no SUID ``su``/``runuser`` (those are
    NixOS security-wrapper concerns), so the nix slot switches users via
    Python, which needs no such infrastructure.
    """
    if kind is SlotKind.CONTAINER:
        return [f"exec su - {user} -c /tmp/inner.sh"]
    return [
        f"exec python{PYTHON_VERSION} - <<'PYEOF'",
        "import os",
        "os.setgroups([])",
        f"os.setgid({TEST_UID})",
        f"os.setuid({TEST_UID})",
        f"os.environ.update(HOME='/home/{user}', USER='{user}', LOGNAME='{user}')",
        "os.execvp('/tmp/inner.sh', ['/tmp/inner.sh'])",
        "PYEOF",
    ]


# ── Inner building blocks ──────────────────────────────────────────


def _env_contract(config: MatrixConfig, slot_name: str) -> list[str]:
    """The ``TEROK_MATRIX`` marker and the ``TEROK_EXPECT`` contract.

    Every slot kind gets these — a nix run is still a matrix run; a slot
    that wants no contract declares ``expect: []`` in its matrix.yml.
    """
    lines = [f"export {MATRIX_ENV}=1", f"export {SLOT_ENV}={slot_name}"]
    if config.krun:
        # This slot runs under krun (its own kernel), so tests gated on
        # ``needs_own_kernel`` can trust that no shared-kernel carrier state
        # reaches them.
        lines.append(f"export {KERNEL_ISOLATED_ENV}=1")
    expect = config.slot_expect(slot_name)
    if expect:
        lines += [
            "# Image-capability contract only: phase state (e.g. hooks) is",
            "# appended once the phase that provides it has succeeded.",
            f"export {EXPECT_ENV}={','.join(expect)}",
        ]
    return lines


def _podman_report_and_preflight(slot_name: str) -> list[str]:
    """Capture the observed podman version and prove rootless podman works."""
    return [
        'echo "--- podman version ---"',
        "if command -v podman >/dev/null 2>&1; then",
        "    podman_ver_line=$(podman --version 2>&1 | head -n1)",
        '    echo "$podman_ver_line"',
        f'    echo "${{podman_ver_line##* }}" > {RESULTS_MOUNT}/{slot_name}.podman-version',
        "else",
        '    echo "podman not available"',
        f"    : > {RESULTS_MOUNT}/{slot_name}.podman-version",
        "fi",
        "",
        'echo "--- rootless podman preflight ---"',
        'podman info --format "podman={{.Version.Version}} storage={{.Store.GraphDriverName}}" \\',
        '    || { echo "FATAL: rootless podman not functional" >&2; exit 1; }',
        "",
    ]


def _nix_python_report(slot_name: str) -> list[str]:
    """Show and record the nix-wrapped interpreter under test."""
    return [
        'echo "--- nix-wrapped python ---"',
        f"which python{PYTHON_VERSION}",
        f"python{PYTHON_VERSION} --version",
        f"python{PYTHON_VERSION} --version | awk '{{print $2}}' > {RESULTS_MOUNT}/{slot_name}.python-version",
        "",
    ]


def _uv_or_venv_bootstrap() -> list[str]:
    """Fast uv venv when the image ships uv, stdlib venv otherwise."""
    return [
        *_venv_scrub_and_python_home(),
        *_uv_no_python_downloads(),
        "if command -v uv >/dev/null 2>&1; then",
        f"    uv venv --python {PYTHON_VERSION} .venv",
        "else",
        f"    python{PYTHON_VERSION} -m venv .venv 2>/dev/null \\",
        "        || python3 -m venv .venv",
        "fi",
        ". .venv/bin/activate",
        "",
        'echo "--- python version ---"',
        "python --version",
        "",
        *_venv_uv(),
    ]


def _venv_uv() -> list[str]:
    """Make uv usable from the active venv when the image lacks it.

    uv needs no isolation from the project venv: it is a single static
    binary with no importable modules the project's own build-isolation
    stack could race against.
    """
    return [
        "if ! command -v uv >/dev/null 2>&1; then",
        "    pip install --quiet uv",
        "fi",
    ]


def _uv_no_python_downloads() -> list[str]:
    """Pin uv to the interpreter under test, before uv runs at all.

    A silently downloaded CPython would defeat the per-distro python
    matrix, so the export must precede even ``uv venv``.
    """
    return ["export UV_PYTHON_DOWNLOADS=never"]


def _venv_scrub_and_python_home() -> list[str]:
    """Reset harness venv state that does not survive the workspace copy.

    The source tree is copied wholesale, so a checkout's in-project
    ``.venv`` arrives with entry-point shebangs pointing at absolute
    paths from the original machine — scrub it; the harness always
    builds its own.  The managed-interpreter home must be re-exported
    because ``su - <test user>`` wipes the image ENV that declared it
    (see ``UV_MANAGED_PYTHON_DIR``); without it, distros whose image
    provisions Python via uv re-download it per run or fail outright.
    """
    return [
        "rm -rf .venv",
        f"export UV_PYTHON_INSTALL_DIR={UV_MANAGED_PYTHON_DIR}",
    ]


def _plain_venv_bootstrap(python: str) -> list[str]:
    """Stdlib venv on a fixed interpreter — the nix wrapper must stay in play."""
    return [
        "rm -rf .venv",
        "# The venv inherits the wrapper's sys.path scrubbing, which is the",
        "# wrapped-Python failure mode this slot exists to exercise.",
        f"{python} -m venv .venv",
        ". .venv/bin/activate",
        "",
        *_uv_no_python_downloads(),
        *_venv_uv(),
    ]


def _uv_sync(groups: tuple[str, ...]) -> list[str]:
    """Install the repo with exactly the configured dependency groups.

    ``--no-default-groups`` keeps the matrix declarative: runtime
    dependencies plus precisely the listed groups, regardless of what the
    repo's ``[tool.uv] default-groups`` says.  ``--active`` targets the
    venv the bootstrap just activated.
    """
    flags = " ".join(f"--group {group}" for group in groups)
    return [
        f"uv sync --locked --active --no-default-groups {flags}".rstrip(),
        'echo "--- deps installed ---"',
    ]


def _phase_walk(config: MatrixConfig, slot_name: str, scope: str) -> list[str]:
    """Render the configured phases; aggregate pytest failures into one rc."""
    lines = ["", "_rc=0"]
    expect_nonempty = bool(config.slot_expect(slot_name))
    for phase in config.slot_phases(slot_name):
        if phase.pytest and scope != "all" and phase.scope != scope:
            continue
        lines += ["", 'echo ""', f'echo "--- {phase.name} ---"']
        if phase.pytest:
            lines += [
                # The sync targets the activated venv, so bare ``pytest``
                # resolves there.
                f"pytest {phase.pytest} \\",
                '    || { _prc=$?; if [ "$_rc" -eq 0 ]; then _rc=$_prc; fi; }',
            ]
        else:
            suffix = " || true" if phase.tolerate_failure else ""
            lines += [f"{command}{suffix}" for command in phase.run]
        if phase.expect_add:
            added = ",".join(phase.expect_add)
            grown = f"${{{EXPECT_ENV}}},{added}" if expect_nonempty else added
            lines += [
                "# The phase above succeeded, so from here on the absence of",
                "# what it provides would be a real breakage - the contract grows.",
                f"export {EXPECT_ENV}={grown}",
            ]
            expect_nonempty = True
    lines += ["", "exit $_rc"]
    return lines
