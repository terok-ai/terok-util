# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Host-side slot execution — image assembly, build, run, teardown.

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
import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import IO, cast

from jinja2 import Environment, PackageLoader, StrictUndefined

from terok_util.security import sanitize_tty

from .catalog import (
    OWNERSHIP_LABEL,
    RESULTS_MOUNT,
    SLOTS,
    SOURCE_MOUNT,
    UV_IMAGE_TAG,
    UV_MANAGED_PYTHON_DIR,
    SlotKind,
)
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
        uv_tag=UV_IMAGE_TAG,
        uv_python_dir=UV_MANAGED_PYTHON_DIR,
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
        # Base tags are moving targets (quay.io/podman/stable:latest,
        # distro :latest bases) but a cached FROM never re-checks the
        # registry -- the podman slot sat on 5.8.2 while quay shipped
        # newer.  --pull=newer is a digest check per build: pulls only
        # when the registry actually has a fresher base.
        "--pull=newer",
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
    config: MatrixConfig,
    slot_name: str,
    results_dir: Path,
    scope: str = "all",
    line_prefix: str | None = None,
) -> SlotResult:
    """Run one slot's test container and collect its observed version.

    With *line_prefix*, the container's combined output streams through
    this process line by line, each line tagged with the prefix — live
    and attributable when several slots run concurrently.  Each tagged
    line is emitted as a single ``write`` call so concurrent slots can
    interleave only between lines, never inside one.
    """
    _write_scripts(config, slot_name, results_dir, scope)
    argv = _run_argv(config, slot_name, results_dir)
    if line_prefix is None:
        status = subprocess.run(argv, check=False).returncode  # nosec B603
    else:
        with subprocess.Popen(  # nosec B603
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        ) as proc:
            stdout = cast(IO[str], proc.stdout)  # guaranteed non-None by stdout=PIPE
            for line in stdout:
                sys.stdout.write(f"{line_prefix}{line}")
                sys.stdout.flush()
            status = proc.wait()
    return SlotResult(passed=status == 0, observed=_observed_version(slot_name, results_dir))


# ── Teardown ───────────────────────────────────────────────────────
#
# Every rebuild retags the per-slot image, untagging the previous
# multi-GB generation — so by the end of any run the old generations are
# dangling, and only teardown stands between them and stranded disk.

# Leftover run containers are dead test containers; no stop grace period
# is owed before the kill.
_REMOVAL_GRACE_SECONDS = "0"

# podman's phrasing when a dangling image is pinned by a container this
# engine does not own (typically a buildah leftover of an interrupted
# ``podman build``).
_IMAGE_IN_USE_MARKER = "in use by a container"

# One line per external container: id, state, name — enough to name the
# storage-state leftovers in a warning.
_EXTERNAL_PS_FORMAT = "{{.ID}} {{.State}} {{.Names}}"

# The state podman reports for external (buildah) containers: storage
# with no runtime, silently pinning the image layers underneath.
_EXTERNAL_STORAGE_STATE = "storage"


def sweep_containers(config: MatrixConfig) -> int:
    """Remove leftover run containers owned by this harness.

    ``podman run --rm`` cleans up after itself — but an interrupted run
    leaves the container behind, and a leftover container pins its (by
    then dangling) image generation, immune to any prune.  The sweep is
    what lets the prune that follows actually reclaim the bytes.

    Returns:
        The number of containers removed.
    """
    listing = subprocess.run(  # nosec B603 B607 - fixed argv, podman from PATH by design
        ["podman", "ps", "-aq", "--filter", f"label={OWNERSHIP_LABEL}={config.image_prefix}"],
        check=False,
        capture_output=True,
        text=True,
    )
    container_ids = listing.stdout.split()
    if not container_ids:
        return 0
    subprocess.run(  # nosec B603 B607 - fixed argv, podman from PATH by design
        ["podman", "rm", "-f", "-t", _REMOVAL_GRACE_SECONDS, *container_ids],
        check=False,
        capture_output=True,
        text=True,
    )
    return len(container_ids)


def prune_dangling(config: MatrixConfig) -> int:
    """Prune dangling generations of exactly this harness's images.

    Idle CPU/IO priority when available — small per-run increments instead
    of an hours-long backlog.  A failed prune is warned about, never
    raised: teardown must always run to completion.  Returns the number
    of pruned image records.
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
    if pruned.returncode != 0:
        _warn_prune_failure(pruned.stderr)
    return len(pruned.stdout.splitlines())


def external_storage_leftovers() -> list[str]:
    """Name the external (buildah) containers stuck holding storage.

    These pin dangling image layers but were not created by this engine —
    removing them here could tear a live ``podman build`` out from under
    another process, so recovery is superbuild's job; the CLI only names
    them.  Returns the container names (or ids, for nameless entries).
    """
    listing = subprocess.run(  # nosec B603 B607 - fixed argv, podman from PATH by design
        ["podman", "ps", "-a", "--external", "--format", _EXTERNAL_PS_FORMAT],
        check=False,
        capture_output=True,
        text=True,
    )
    leftovers = []
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].lower() == _EXTERNAL_STORAGE_STATE:
            leftovers.append(sanitize_tty(fields[2] if len(fields) > 2 else fields[0]))
    return leftovers


def _warn_prune_failure(stderr: str) -> None:
    """One line saying why the prune failed — silence is how leaks last years."""
    if _IMAGE_IN_USE_MARKER in stderr:
        print(
            "WARNING: prune blocked by an external (buildah) build leftover"
            " — run superbuild's matrix-clean",
            file=sys.stderr,
        )
        return
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    gist = sanitize_tty(lines[-1]) if lines else "no error output"
    print(f"WARNING: image prune failed: {gist}", file=sys.stderr)


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
        # The image already carries the ownership label, but the teardown
        # sweep must find leftover containers even when label inheritance
        # doesn't apply — so the run stamps it explicitly too.
        "--label",
        f"{OWNERSHIP_LABEL}={config.image_prefix}",
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
