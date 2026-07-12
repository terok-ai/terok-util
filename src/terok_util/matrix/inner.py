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
    MATRIX_ENV,
    PYTHON_VERSION,
    RESULTS_MOUNT,
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
    lines = [
        "#!/bin/bash",
        "set -e -o pipefail",
        "",
        f"cp -a {SOURCE_MOUNT} {WORKSPACE_DIR}",
        f"chown -R {spec.user}:{spec.user} {WORKSPACE_DIR}",
    ]
    if spec.kind is SlotKind.CONTAINER:
        lines += _init_system_proof(slot_name)
    if config.flavor == "podman" and spec.kind is SlotKind.CONTAINER:
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
    lines = [f"export {MATRIX_ENV}=1"]
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
