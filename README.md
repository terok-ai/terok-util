<!--
SPDX-FileCopyrightText: 2026 Jiri Vyskocil
SPDX-License-Identifier: Apache-2.0
-->

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://terok-ai.github.io/terok/terok-logo-w.svg">
    <img src="https://terok-ai.github.io/terok/terok-logo-b.svg" alt="terok-util" width="120">
  </picture>
</p>

# terok-util

[![PyPI](https://img.shields.io/pypi/v/terok-util)](https://pypi.org/project/terok-util/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![REUSE status](https://api.reuse.software/badge/github.com/terok-ai/terok-util)](https://api.reuse.software/info/github.com/terok-ai/terok-util)

Shared utility library for the [`terok-*`](https://github.com/terok-ai)
sibling packages.

`terok-util` sits at the bottom of the terok dependency chain: every
sibling package depends on it, and it depends on nothing else in the
ecosystem. It collects the small set of cross-cutting bits that would
otherwise be duplicated (or quietly diverge) across
[`terok-shield`](https://github.com/terok-ai/terok-shield),
[`terok-clearance`](https://github.com/terok-ai/terok-clearance),
[`terok-sandbox`](https://github.com/terok-ai/terok-sandbox),
[`terok-executor`](https://github.com/terok-ai/terok-executor), and
[`terok`](https://github.com/terok-ai/terok).

## What's in the box

| Module | Use case |
|---|---|
| `cli_types` | `CommandDef` / `ArgDef` / `CommandTree` — argparse-driven CLI registry types used by every sibling that exposes a CLI tree. |
| `fs` | `ensure_dir`, `ensure_dir_writable`, `write_sensitive_file` (atomic `O_CREAT \| O_EXCL` 0o600 writer). |
| `untrusted_fs` | Bounded `read_untrusted_text` / `read_untrusted_bytes`, fresh-inode `write_untrusted_text` / `write_untrusted_bytes`, `remove_untrusted_file`, `open_untrusted_directory`, and `untrusted_file_lock`. Refuse links and special files in task-writable paths. |
| `paths` | `namespace_state_dir`, `namespace_config_dir`, `namespace_runtime_dir` — XDG-aware path resolution for a per-namespace deployment. |
| `config_stack` | `ConfigStack` + `deep_merge` — layered round-trip YAML config merge engine. |
| `security` | `sanitize_tty` — strips C0 / C1 / ANSI sequences from untrusted strings before rendering to the operator's terminal (CWE-150 mitigation); `sanitize_log_text` — the same for multiline task output, keeping layout and (on a terminal) colour. |
| `podman` | `podman_userns_args` — rootless `--userns=keep-id:uid=1000,gid=1000` builder; `podman_hosts_file_args` — base of the container's `/etc/hosts`. |

## Installation

```bash
pip install terok-util
```

The package is published as a Python wheel; siblings pin to a specific
GitHub-release wheel URL via `pyproject.toml`.

## Convention

The rule for what belongs here: **if two or more terok-`*` packages
need it, it lives in `terok-util`.** Single-package helpers stay in
the package that owns them. The `__all__` declaration in
`src/terok_util/__init__.py` is the contract — symbols listed there
are stable across minor releases.

## Setup downgrade authorization

`setup_lock(allow_downgrade=True)` lets an explicit operator-approved setup
replace newer package receipts. Nested setup calls inherit permission until
the scope exits. The permission does not survive a fork or authorize another
thread. Receipt checks still report the actual version mismatch, and
`require_setup()` still rejects unrepaired setup.

The option permits installers to run; it does not establish that older code
can read newer state. Callers must obtain explicit operator consent and warn
about compatibility before entering the scope. Ordinary setup and runtime
checks keep their downgrade guards.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Matrix reporting

`terok-matrix --report report.json [slots...]` writes an atomic JSON snapshot
alongside the normal terminal output. It updates after build and test transitions,
and retains completed verdicts if the matrix is interrupted. The destination is
independent of the engine's temporary workspace.

The snapshot has `schema_version: 1`, matrix `state`, `exit_code`, `error`, elapsed
`duration` in seconds, and a `slots` object keyed by slot name. Each slot records
its `state`, `reason`, `observed` version, optional suspected `network_hint`,
`duration`, and within-slot `skips`. Slot states are `pending`, `building`, `built`,
`testing`, `passed`, `failed`, `build_failed`, `skipped`, `cancelled`, or `error`.
A build-only run leaves successful slots `built`, not `passed`.

Consumers can poll this file without parsing terminal output. SIGTERM and Ctrl-C
finalize the report; SIGKILL cannot, so the supervising process must report that
interruption. Configuration errors before execution may leave no report.

### Workload file boundaries

Generated scripts and systemd units are mounted read-only. Each slot gets its
own writable `/results` directory; no slot receives another slot's results or
the host scratch directory. Result reads reject symlinks (including parent
directories), special files, invalid UTF-8, and oversized files. Output is
streamed in bounded chunks even when a workload never prints a newline.
Host aliases through the checkout root and temporary workspace are resolved
before reading; links inside either tree remain forbidden.

Matrix declarations require their declared scalar types, not string coercion.
They are limited to 64 KiB, 4096 YAML events and 16 collection levels; aliases
are rejected. Declarations and optional Containerfile fragments must be regular
files without symlinks in their repository paths. Fragments are limited to
1 MiB. Image prefixes are simple lowercase names; extra packages are individual
package tokens, not package-manager flags.

These are file-handling protections, **not a hostile-code sandbox**. Repository
phases and Containerfile fragments still execute code. `--krun` selects the test
runtime, not the image-build runtime or the systemd capability probe. Nested
Podman slots still use privileged containers. Deployment policy must control
credentials, network access, resource limits and interruption recovery.

Image tags, ownership labels and caching are unchanged. A supervisor sharing
one Podman store must serialize matrices with the same image prefix; different
repositories with distinct prefixes and their slots may run in parallel using
the existing jobserver. Shared image caches belong to one accepted trust domain.
