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
  contract, bootstrap a Python 3.12 venv + Poetry, install the repo, and
  walk the configured phases.

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
        lines += _nix_python_report(slot_name) + _plain_venv_bootstrap(f"python{PYTHON_VERSION}")
    else:
        if config.flavor == "podman":
            lines += _podman_report_and_preflight(slot_name)
        lines += _uv_or_venv_bootstrap()
    lines += _poetry_install(config.slot_poetry_groups(slot_name))
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
        *_isolated_poetry(),
    ]


def _isolated_poetry() -> list[str]:
    """Install poetry into its own env, never the project venv.

    The project's own dependency set can include poetry's build-isolation
    stack (pre-commit pulls virtualenv, hence distlib/python_discovery);
    with poetry sharing the project venv, its parallel installer replaces
    those modules on disk while an in-flight sdist build env is importing
    them — observed as distlib/python_discovery ModuleNotFoundError
    mid-``poetry install`` on fast hosts.
    """
    return [
        "if command -v uv >/dev/null 2>&1; then",
        "    uv tool install -q poetry",
        '    export PATH="$HOME/.local/bin:$PATH"',
        "else",
        '    python3 -m venv "$HOME/.poetry-venv"',
        '    "$HOME/.poetry-venv/bin/pip" install --quiet --upgrade pip',
        '    "$HOME/.poetry-venv/bin/pip" install --quiet poetry',
        '    export PATH="$HOME/.poetry-venv/bin:$PATH"',
        "fi",
    ]


def _plain_venv_bootstrap(python: str) -> list[str]:
    """Stdlib venv on a fixed interpreter — the nix wrapper must stay in play."""
    return [
        "# The venv inherits the wrapper's sys.path scrubbing, which is the",
        "# wrapped-Python failure mode this slot exists to exercise.",
        f"{python} -m venv .venv",
        ". .venv/bin/activate",
        "",
        f'{python} -m venv "$HOME/.poetry-venv"',
        '"$HOME/.poetry-venv/bin/pip" install --quiet --upgrade pip',
        '"$HOME/.poetry-venv/bin/pip" install --quiet poetry',
        'export PATH="$HOME/.poetry-venv/bin:$PATH"',
    ]


def _poetry_install(groups: tuple[str, ...]) -> list[str]:
    """Install the repo with its configured dependency groups."""
    withs = (f"--with {group}" for group in groups)
    return [
        " ".join(["poetry install", *withs, "--no-interaction"]),
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
                f"poetry run pytest {phase.pytest} \\",
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
