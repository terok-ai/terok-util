<!--
SPDX-FileCopyrightText: 2026 Jiri Vyskocil
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Host-tool lookup from the current PATH, with a standalone-copyable implementation.
- Package-owned setup receipts, readiness results, and downgrade guards.
- A shared lock serializes setup and uninstall across packages.

Initial extraction from the surrounding terok-`*` packages. First
release scope (Tier 1 + Tier 2 per
[terok#111](https://github.com/terok-ai/terok/issues/111)):

- `cli_types` — `CommandDef`, `ArgDef`, `CommandTree`. Replaces
  parallel copies in `terok-shield`, `terok-clearance`, and the
  canonical definition in `terok-sandbox`.
- `fs` — `ensure_dir`, `ensure_dir_writable`, `write_sensitive_file`.
  Consolidates near-identical copies across `terok-sandbox`,
  `terok-executor`, and `terok`.
- `paths` — `namespace_state_dir`, `namespace_config_dir`,
  `namespace_runtime_dir`. Canonical XDG-aware namespace path
  resolution.
- `config_stack` — `ConfigStack`, `deep_merge`. Layered YAML config
  merge engine.
- `security` — `sanitize_tty`. Untrusted-string sanitisation for
  terminal output.
- `templates` — `render_template` (the strict control-char-rejecting
  variant from `terok-sandbox`; supersedes the permissive variant
  that lived in `terok`).
- `podman` — `podman_userns_args`. Rootless `--userns=keep-id`
  builder.
## v0.4.0 — At Your Service

## What's Changed
* feat: shared setup receipts and host tool lookup by @sliwowitz in https://github.com/terok-ai/terok-util/pull/128
* fix: complete host tool lookup in matrix and version probes by @sliwowitz in https://github.com/terok-ai/terok-util/pull/129
* fix: restore admin tool lookup in matrix login sessions by @sliwowitz in https://github.com/terok-ai/terok-util/pull/131
* ci: bump the external-actions group with 2 updates by @dependabot[bot] in https://github.com/terok-ai/terok-util/pull/130
* fix: install Debian 12 matrix init helper by @sliwowitz in https://github.com/terok-ai/terok-util/pull/132
* test: drop obsolete slirp4netns from Alpine matrix image by @sliwowitz in https://github.com/terok-ai/terok-util/pull/134

**Full Changelog**: https://github.com/terok-ai/terok-util/compare/v0.3.1...v0.4.0

## v0.3.1 — Past Prologue

## What's Changed
* fix(paths): privilege is uid 0 in the initial namespace, not a mapped 0 by @sliwowitz in https://github.com/terok-ai/terok-util/pull/67
* feat(hardening): harden_self process floor + dispatch exit-code fix by @sliwowitz in https://github.com/terok-ai/terok-util/pull/70
* feat(hardening): add no-new-privs to the process floor by @sliwowitz in https://github.com/terok-ai/terok-util/pull/72
* feat(podman): explicit uidmap fallback for podman older than 4.3 by @sliwowitz in https://github.com/terok-ai/terok-util/pull/73
* feat(podman): version-aware force-pull flag for image builds by @sliwowitz in https://github.com/terok-ai/terok-util/pull/75
* feat: unified logging facility (journald writer + configure + output capture) by @sliwowitz in https://github.com/terok-ai/terok-util/pull/85
* feat(hardening): confine_filesystem — Landlock FS floor by @sliwowitz in https://github.com/terok-ai/terok-util/pull/86
* Run matrix slots under krun (libkrun microVM); add a Manjaro slot by @sliwowitz in https://github.com/terok-ai/terok-util/pull/105
* fix(matrix): an init as PID 1 in every slot, and git-http-backend on Alpine by @sliwowitz in https://github.com/terok-ai/terok-util/pull/113
* feat(matrix): a host whose AppArmor confines nested pasta skips the symlinked-pasta slots by @sliwowitz in https://github.com/terok-ai/terok-util/pull/116
* fix(journal): the line a pty ends with CR LF reaches journald by @sliwowitz in https://github.com/terok-ai/terok-util/pull/117
* feat(matrix): the systemd slots boot systemd as PID 1 under krun by @sliwowitz in https://github.com/terok-ai/terok-util/pull/118
* fix(matrix): the booted krun slot without podman exec, and a make jobserver client by @sliwowitz in https://github.com/terok-ai/terok-util/pull/119


**Full Changelog**: https://github.com/terok-ai/terok-util/compare/v0.3.0...v0.3.1

## v0.3.0 — Past Prologue

Port from poetry to uv.
Provide reuseable static analysis workflows.
Multiple integration test matrix fixes.
API lazyfication for faster startup.

**Full Changelog**: https://github.com/terok-ai/terok-util/compare/v0.2.1...v0.3.0

## v0.2.1 — The Celestial Temple

Shared multi-distro test-matrix engine + terok-matrix CLI, https://github.com/terok-ai/terok-util/pull/31

**Full Changelog**: https://github.com/terok-ai/terok-util/compare/v0.2.0...v0.2.1

## v0.2.0 — Emissary, Part II

Extracted host BestEffortLogger and the YAML round-trip facade, https://github.com/terok-ai/terok-util/pull/16

**Full Changelog**: https://github.com/terok-ai/terok-util/compare/v0.1.0...v0.2.0

## v0.1.0 — The Emissary

First public PyPi release. For historical pre-releases, see https://github.com/terok-ai/terok-util/releases.
