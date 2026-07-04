# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Host-side slot execution — image assembly, build, run, prune.

The shared Containerfile templates ship inside this package; a consuming
repo customises them declaratively (``extra-packages`` becomes the
``EXTRA_PACKAGES`` build arg) or, for genuinely repo-specific image
tweaks, drops a fragment next to its ``matrix.yml``
(``fragments/Containerfile.<slot>``) that the engine appends verbatim.

Process output streams straight to the operator's terminal (podman build
and the test run are long and live); all narration around it belongs to
[`cli`][terok_util.matrix.cli].
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed-argv podman shellouts
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from jinja2 import Environment, PackageLoader, StrictUndefined

from terok_util.security import sanitize_tty

from .catalog import OWNERSHIP_LABEL, RESULTS_MOUNT, SLOTS, SOURCE_MOUNT, UV_IMAGE_TAG, SlotKind
from .config import MatrixConfig
from .inner import inner_script, outer_script


@dataclass(frozen=True)
class SlotResult:
    """Outcome of one slot run: pass/fail plus the observed version."""

    passed: bool
    observed: str = "?"


# The templates are Jinja: shared blocks (ARG/label header, uv bootstrap,
# rootless-podman setup) live once as _*.j2 partials and are composed per
# slot via {% include %}.  StrictUndefined turns a typo'd variable into a
# render error instead of silently empty output.
_TEMPLATES = Environment(
    loader=PackageLoader("terok_util.matrix", "containerfiles"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,  # nosec B701 - renders Containerfiles, not HTML
)


def render_containerfile(config: MatrixConfig, slot_name: str) -> str:
    """Shared template plus the repo's optional fragment, ready to build."""
    kind = SLOTS[slot_name].kind
    flavor = "nix" if kind is SlotKind.NIX else config.flavor
    rendered = _TEMPLATES.get_template(f"{flavor}/Containerfile.{slot_name}").render(
        uv_tag=UV_IMAGE_TAG
    )
    fragment = config.containers_dir / "fragments" / f"Containerfile.{slot_name}"
    if fragment.is_file():
        rendered += "\n" + fragment.read_text(encoding="utf-8")
    return rendered


def build_image(
    config: MatrixConfig, slot_name: str, results_dir: Path, no_cache: bool = False
) -> bool:
    """Assemble and build the slot image; the repo root is the build context."""
    containerfile = results_dir / f"Containerfile.{slot_name}"
    containerfile.write_text(render_containerfile(config, slot_name), encoding="utf-8")
    argv = [
        "podman",
        "build",
        *(["--no-cache"] if no_cache else []),
        "--build-arg",
        f"IMAGE_PREFIX={config.image_prefix}",
        "--build-arg",
        f"EXTRA_PACKAGES={' '.join(config.slots[slot_name].extra_packages)}",
        "-t",
        f"{config.image_prefix}:{slot_name}",
        "-f",
        str(containerfile),
        str(config.repo_root),
    ]
    return subprocess.run(argv, check=False).returncode == 0  # nosec B603


def run_slot(
    config: MatrixConfig, slot_name: str, results_dir: Path, scope: str = "all"
) -> SlotResult:
    """Run one slot's test container and collect its observed version."""
    _write_scripts(config, slot_name, results_dir, scope)
    status = subprocess.run(  # nosec B603
        _run_argv(config, slot_name, results_dir), check=False
    ).returncode
    return SlotResult(passed=status == 0, observed=_observed_version(slot_name, results_dir))


def prune_dangling(config: MatrixConfig) -> int:
    """Prune dangling generations of exactly this harness's images.

    Idle CPU/IO priority when available — small per-run increments instead
    of an hours-long backlog.  Returns the number of pruned image records.
    """
    argv = [
        "podman",
        "image",
        "prune",
        "-f",
        "--filter",
        f"label={OWNERSHIP_LABEL}={config.image_prefix}",
    ]
    for wrapper in (["ionice", "-c3"], ["nice", "-n19"]):
        if which(wrapper[0]):
            argv = wrapper + argv
    pruned = subprocess.run(argv, check=False, capture_output=True, text=True)  # nosec B603
    return len(pruned.stdout.splitlines())


# ── Assembly details ───────────────────────────────────────────────


def _write_scripts(config: MatrixConfig, slot_name: str, results_dir: Path, scope: str) -> None:
    """Drop the generated outer/inner scripts where the container mounts them."""
    outer = results_dir / f"outer-{slot_name}.sh"
    outer.write_text(outer_script(config, slot_name), encoding="utf-8")
    inner = results_dir / f"inner-{slot_name}.sh"
    inner.write_text(inner_script(config, slot_name, scope), encoding="utf-8")


def _run_argv(config: MatrixConfig, slot_name: str, results_dir: Path) -> list[str]:
    """The ``podman run`` command line for one slot."""
    spec = SLOTS[slot_name]
    argv = [
        "podman",
        "run",
        "--rm",
        "--replace",
        "--name",
        f"{config.image_prefix}-{slot_name}",
        "-e",
        "TERM=xterm",
    ]
    if spec.kind is SlotKind.NIX:
        argv += ["--security-opt", "label=disable"]
    elif config.flavor == "podman":
        # Privileged gives the outer container the capabilities nested
        # rootless podman needs; tests still run as the uid-1000 user.
        argv += [
            "--privileged",
            "--security-opt",
            "label=disable",
            "--device",
            "/dev/fuse:rw",
            "-e",
            "container=podman",
        ]
    argv += [
        "-v",
        f"{config.repo_root}:{SOURCE_MOUNT}:ro,Z",
        "-v",
        f"{results_dir}:{RESULTS_MOUNT}:rw,Z",
        f"{config.image_prefix}:{slot_name}",
        "bash",
        f"{RESULTS_MOUNT}/outer-{slot_name}.sh",
    ]
    return argv


def _observed_version(slot_name: str, results_dir: Path) -> str:
    """What the inner script recorded — may be missing if it died early."""
    stem = "python" if SLOTS[slot_name].kind is SlotKind.NIX else "podman"
    try:
        recorded = (results_dir / f"{slot_name}.{stem}-version").read_text(encoding="utf-8")
    except OSError:
        return "?"
    # The file is written inside the test container - sanitize before it
    # reaches the operator's terminal via the summary.
    return sanitize_tty(recorded.strip()) or "?"
