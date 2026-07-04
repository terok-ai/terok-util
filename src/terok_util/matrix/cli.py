# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""``terok-matrix`` — run a repo's multi-distro test matrix.

The operator-facing entry point: load the repo's ``matrix.yml``, then
walk the selected slots — build every image first (a failed build is recorded, not
fatal, so one run surfaces every distro's problems), run each slot's test
container, and close with the classic PASS/SKIP/FAIL summary and the
dangling-layer prune.

``--slots-json`` exists for CI: a workflow derives its ``strategy.matrix``
from the same ``matrix.yml`` the local runs use, so the slot list has a
single home.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

from .catalog import SLOTS, SlotKind
from .config import MatrixConfig, MatrixConfigError, load_config
from .runner import build_image, prune_dangling, run_slot

DEFAULT_CONFIG = Path("tests/containers/matrix.yml")

# ── Terminal colors (disabled when stdout is not a tty) ────────────

_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
CYAN = "\033[1;36m" if _TTY else ""
YELLOW = "\033[1;33m" if _TTY else ""
GREEN = "\033[1;32m" if _TTY else ""
RED = "\033[1;31m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def main(argv: list[str] | None = None) -> int:
    """Run the matrix; the exit code is 1 when any slot failed."""
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, MatrixConfigError) as error:
        print(f"{RED}Error: {error}{RESET}", file=sys.stderr)
        return 2

    targets = list(args.slots or config.slots)
    unknown = [name for name in targets if name not in config.slots]
    if unknown:
        print(
            f"{RED}Error: unknown slot(s) {unknown}. Available: {list(config.slots)}{RESET}",
            file=sys.stderr,
        )
        return 2

    if args.list:
        for name in sorted(targets):
            expectation = _version_expectation(config, name)
            print(f"{name} ({expectation})" if expectation else name)
        return 0
    if args.slots_json:
        print(json.dumps(targets))
        return 0
    if args.image_prefix:
        print(config.image_prefix)
        return 0

    _warn_keyring()
    with tempfile.TemporaryDirectory(prefix=f"{config.image_prefix}-matrix-") as scratch:
        results_dir = Path(scratch)
        # The container's uid-1000 user (an unknown host subuid) must write
        # its observed-version files here, so the dir is world-writable -
        # with the sticky bit, so other host accounts cannot replace the
        # generated scripts podman is about to execute.
        results_dir.chmod(0o1777)
        try:
            return _run_matrix(config, targets, args, results_dir)
        except OSError as error:
            print(f"{RED}Error: {error}{RESET}", file=sys.stderr)
            return 2


# ── The matrix walk ────────────────────────────────────────────────


def _run_matrix(
    config: MatrixConfig, targets: list[str], args: argparse.Namespace, results_dir: Path
) -> int:
    """Build all images, run all runnable slots, summarise, prune."""
    build_failed = _build_images(config, targets, results_dir, no_cache=args.no_cache)
    if args.build_only:
        print(f"{GREEN}Images built.{RESET} Run without --build-only to run tests.")
        return 1 if build_failed else 0

    passed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    observed: dict[str, str] = {}
    for name in targets:
        if reason := _skip_reason(config, name):
            print(
                f"{YELLOW}==> Skipping {BOLD}{name}{YELLOW} on {platform.machine()}: {reason}{RESET}"
            )
            skipped.append(name)
            continue
        if name in build_failed:
            print(f"{RED}==> {name}: FAIL (image build failed){RESET}", file=sys.stderr)
            failed.append(name)
            continue
        _print_slot_heading(config, name, args.scope)
        result = run_slot(config, name, results_dir, scope=args.scope)
        observed[name] = result.observed
        verdict = f"{GREEN}==> {name}: PASS" if result.passed else f"{RED}==> {name}: FAIL"
        print(f"{verdict}{RESET} {_version_summary(config, name, result.observed)}")
        (passed if result.passed else failed).append(name)

    _print_summary(config, passed, skipped, failed, observed)
    if not args.keep_dangling:
        print(f"\n{DIM}Pruning this harness's dangling image generations (idle io)...{RESET}")
        print(f"{DIM}pruned {prune_dangling(config)} image record(s){RESET}")
    return 1 if failed else 0


def _build_images(
    config: MatrixConfig, targets: list[str], results_dir: Path, no_cache: bool
) -> set[str]:
    """Build every runnable target; return the ones whose build failed."""
    build_failed = set()
    for name in targets:
        if _skip_reason(config, name):
            continue
        print(f"{CYAN}==> Building {BOLD}{config.image_prefix}:{name}{RESET}")
        if not build_image(config, name, results_dir, no_cache=no_cache):
            print(
                f"{RED}==> Build FAILED for {BOLD}{name}{RED} - recording and continuing{RESET}",
                file=sys.stderr,
            )
            build_failed.add(name)
    return build_failed


def _skip_reason(config: MatrixConfig, name: str) -> str:
    """Why ``name`` cannot run on this host, or an empty string."""
    slot = config.slots[name]
    if platform.machine() in slot.skip_arches:
        return slot.skip_reason or "not supported on this architecture"
    return ""


def _print_slot_heading(config: MatrixConfig, name: str, scope: str) -> None:
    """The ``==> Testing ...`` banner before a slot run."""
    expectation = _version_expectation(config, name)
    detail = f" ({expectation})" if expectation else ""
    print(f"\n{CYAN}==> Testing {BOLD}{name}{CYAN}{detail}{RESET}")
    print(f"    {DIM}scope: {scope}, user: {SLOTS[name].user}{RESET}\n")


def _print_summary(
    config: MatrixConfig,
    passed: list[str],
    skipped: list[str],
    failed: list[str],
    observed: dict[str, str],
) -> None:
    """The classic closing PASS/SKIP/FAIL table."""
    print(f"\n{BOLD}===== Matrix Summary ====={RESET}")
    for name in passed:
        print(f"  {GREEN}PASS{RESET}: {name} {_version_summary(config, name, observed[name])}")
    for name in skipped:
        print(f"  {YELLOW}SKIP{RESET}: {name} ({_skip_reason(config, name)})")
    for name in failed:
        print(
            f"  {RED}FAIL{RESET}: {name} {_version_summary(config, name, observed.get(name, '?'))}"
        )


# ── Version reporting ──────────────────────────────────────────────


def _version_expectation(config: MatrixConfig, name: str) -> str:
    """Human phrasing of what the slot is pinned to, for headings."""
    spec = SLOTS[name]
    if spec.kind is SlotKind.NIX:
        return "nix-wrapped Python"
    if config.flavor != "podman":
        return ""
    if spec.expected_podman == "latest":
        return "podman latest, version pinned by upstream"
    return f"expected podman {spec.expected_podman}"


def _version_summary(config: MatrixConfig, name: str, observed: str) -> str:
    """Parenthesised observed-version note after a run.

    A mismatch never fails the run — distro point releases are routine;
    the yellow warning is a nudge to refresh the catalog pin.
    """
    spec = SLOTS[name]
    if spec.kind is SlotKind.NIX:
        return f"{DIM}(nix-wrapped Python {observed}){RESET}"
    if config.flavor != "podman":
        return ""
    if spec.expected_podman in ("latest", observed):
        return f"{DIM}(podman {observed}){RESET}"
    return (
        f"{YELLOW}(WARNING: expected podman {spec.expected_podman}, got podman {observed}){RESET}"
    )


# ── Host preflight ─────────────────────────────────────────────────


def _warn_keyring() -> None:
    """Nudge the operator to disable kernel keyrings in containers.conf.

    Matrix runs cycle many containers and can exhaust the per-user 200-key
    quota, causing misleading "Disk quota exceeded" (EDQUOT) from crun.
    """
    candidates = [Path(conf) for conf in [os.environ.get("CONTAINERS_CONF", "")] if conf] or [
        Path.home() / ".config/containers/containers.conf",
        Path("/etc/containers/containers.conf"),
    ]
    for conf in candidates:
        try:
            lines = conf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if any("".join(line.split("#")[0].split()) == "keyring=false" for line in lines):
            return
        break
    print(
        f"{YELLOW}WARNING: kernel keyring is not disabled in containers.conf\n"
        "\n"
        "  Matrix tests create many containers and may exhaust the per-user\n"
        "  keyring quota (200 keys), causing spurious EDQUOT errors.\n"
        "\n"
        f"  Add to {BOLD}~/.config/containers/containers.conf{YELLOW}:\n"
        "\n"
        f"    {BOLD}[containers]{YELLOW}\n"
        f"    {BOLD}keyring = false{YELLOW}\n"
        "\n"
        f"  See: https://terok-ai.github.io/terok/kernel-keyring/{RESET}\n"
    )


# ── Argument parsing ───────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The flag surface every historical ``run-matrix.sh`` accepted."""
    parser = argparse.ArgumentParser(
        prog="terok-matrix",
        description="Run this repo's multi-distro test matrix (declared in matrix.yml).",
    )
    parser.add_argument("slots", nargs="*", help="slots to run (default: all declared)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to matrix.yml")
    parser.add_argument("--list", action="store_true", help="list available slots")
    parser.add_argument(
        "--slots-json", action="store_true", help="print the slot list as JSON (for CI)"
    )
    parser.add_argument(
        "--image-prefix",
        action="store_true",
        help="print the image/container name prefix (for external tooling)",
    )
    parser.add_argument(
        "--build-only", action="store_true", help="build images without running tests"
    )
    parser.add_argument("--no-cache", action="store_true", help="rebuild images from scratch")
    parser.add_argument(
        "--keep-dangling", action="store_true", help="skip the teardown prune of dangling layers"
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--unit-only",
        dest="scope",
        action="store_const",
        const="unit",
        help="run only unit-scoped phases",
    )
    scope.add_argument(
        "--integ-only",
        dest="scope",
        action="store_const",
        const="integ",
        help="run only integ-scoped phases",
    )
    parser.set_defaults(scope="all")
    return parser.parse_args(argv)
