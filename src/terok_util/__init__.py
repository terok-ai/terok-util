# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""terok-util — shared utility library for the terok-* sibling packages.

`terok-util` sits at the bottom of the terok dependency chain.  Every
sibling package depends on it; it depends on nothing else in the
ecosystem (only stdlib + ``platformdirs`` + ``ruamel.yaml``).
It collects the small set of cross-cutting helpers that would otherwise
be duplicated — or, worse, quietly diverge — across
[terok-shield](https://github.com/terok-ai/terok-shield),
[terok-clearance](https://github.com/terok-ai/terok-clearance),
[terok-sandbox](https://github.com/terok-ai/terok-sandbox),
[terok-executor](https://github.com/terok-ai/terok-executor),
and [terok](https://github.com/terok-ai/terok).

What lives here, by module:

* [`cli_types`][terok_util.cli_types] — argparse-driven CLI registry
  vocabulary: [`CommandDef`][terok_util.cli_types.CommandDef],
  [`ArgDef`][terok_util.cli_types.ArgDef],
  [`CommandTree`][terok_util.cli_types.CommandTree],
  [`KeyRow`][terok_util.cli_types.KeyRow].
* [`fs`][terok_util.fs] — small filesystem helpers
  ([`ensure_dir`][terok_util.fs.ensure_dir],
  [`ensure_dir_writable`][terok_util.fs.ensure_dir_writable],
  [`write_sensitive_file`][terok_util.fs.write_sensitive_file]).
* [`journal`][terok_util.journal] — dependency-free writer for the systemd
  journal's native protocol ([`JournalWriter`][terok_util.journal.JournalWriter],
  [`journald_available`][terok_util.journal.journald_available]).
* [`logging`][terok_util.logging] — soft-fail file logger
  ([`BestEffortLogger`][terok_util.logging.BestEffortLogger]) plus the unified
  [`configure`][terok_util.logging.configure] that routes stdlib logging to
  journald-when-present, else stderr.
* [`output_capture`][terok_util.output_capture] — tee a block's stdout/stderr
  to journald/a file with the live terminal intact
  ([`tee_output`][terok_util.output_capture.tee_output]).
* [`yaml`][terok_util.yaml] — round-trip YAML facade over ``ruamel.yaml``
  ([`load`][terok_util.yaml.load], [`dump`][terok_util.yaml.dump]).
* [`paths`][terok_util.paths] — XDG-aware namespace path resolution
  ([`namespace_state_dir`][terok_util.paths.namespace_state_dir],
  [`namespace_config_dir`][terok_util.paths.namespace_config_dir],
  [`namespace_runtime_dir`][terok_util.paths.namespace_runtime_dir]).
* [`config_stack`][terok_util.config_stack] — layered config merge engine
  ([`ConfigStack`][terok_util.config_stack.ConfigStack],
  [`deep_merge`][terok_util.config_stack.deep_merge]).
* [`security`][terok_util.security] — untrusted-string TTY sanitiser
  ([`sanitize_tty`][terok_util.security.sanitize_tty]).
* [`hardening`][terok_util.hardening] — process self-hardening floor
  ([`harden_self`][terok_util.hardening.harden_self],
  [`HardeningReport`][terok_util.hardening.HardeningReport]).
* [`podman`][terok_util.podman] — version-aware podman argument spelling
  ([`podman_userns_args`][terok_util.podman.podman_userns_args],
  [`podman_pull_always_args`][terok_util.podman.podman_pull_always_args]).
* [`matrix`][terok_util.matrix] — the shared multi-distro test-matrix
  engine behind the ``terok-matrix`` CLI; consuming repos declare their
  matrix in ``tests/containers/matrix.yml``
  ([`load_config`][terok_util.matrix.config.load_config],
  [`MatrixConfig`][terok_util.matrix.config.MatrixConfig]).

The rule for what belongs here: **if two or more terok-`*` packages
need it, it lives in terok-util.**  Single-package helpers stay in
the package that owns them.  The `__all__` declaration below is the
contract — symbols listed are stable across minor releases; anything
underscore-prefixed or absent from `__all__` is internal and may change
without notice.
"""

from __future__ import annotations

# ── CLI registry types ────────────────────────────────────────────
from .cli_types import ArgDef, CommandDef, CommandTree, KeyRow, LazyHandler

# ── Config-stack engine ───────────────────────────────────────────
from .config_stack import ConfigStack, deep_merge

# ── Filesystem helpers ────────────────────────────────────────────
from .fs import ensure_dir, ensure_dir_writable, write_sensitive_file

# ── Process self-hardening ────────────────────────────────────────
from .hardening import HardeningReport, harden_self

# ── Native journald writer (dependency-free) ──────────────────────
from .journal import JournalWriter, journald_available

# ── Best-effort file logger + unified stdlib-logging configuration ─
from .logging import BestEffortLogger, configure

# ── Output-stream capture (tee to journald/file, live terminal intact) ─
from .output_capture import tee_output

# ── XDG-aware namespace paths + layered config readers ────────────
from .paths import (
    config_file_paths,
    host_uid,
    namespace_config_dir,
    namespace_runtime_dir,
    namespace_state_dir,
    read_config_section,
    read_config_top_level,
)

# ── Podman helpers ────────────────────────────────────────────────
from .podman import podman_pull_always_args, podman_userns_args

# ── Untrusted-string sanitisation ─────────────────────────────────
from .security import sanitize_tty

# The round-trip YAML facade is reached as the ``terok_util.yaml`` submodule
# (``from terok_util.yaml import load, dump``) rather than flattened here.
# The matrix engine likewise stays a submodule (``terok_util.matrix``) —
# its consumers are the ``terok-matrix`` CLI and dev tooling, not library
# code.

__all__ = [
    "ArgDef",
    "BestEffortLogger",
    "CommandDef",
    "CommandTree",
    "ConfigStack",
    "HardeningReport",
    "JournalWriter",
    "KeyRow",
    "LazyHandler",
    "configure",
    "deep_merge",
    "journald_available",
    "tee_output",
    "ensure_dir",
    "ensure_dir_writable",
    "harden_self",
    "config_file_paths",
    "host_uid",
    "namespace_config_dir",
    "namespace_runtime_dir",
    "namespace_state_dir",
    "podman_pull_always_args",
    "podman_userns_args",
    "read_config_section",
    "read_config_top_level",
    "sanitize_tty",
    "write_sensitive_file",
]
