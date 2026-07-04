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
## v0.2.1 — The Celestial Temple

Shared multi-distro test-matrix engine + terok-matrix CLI, https://github.com/terok-ai/terok-util/pull/31

**Full Changelog**: https://github.com/terok-ai/terok-util/compare/v0.2.0...v0.2.1

## v0.2.0 — Emissary, Part II

Extracted host BestEffortLogger and the YAML round-trip facade, https://github.com/terok-ai/terok-util/pull/16

**Full Changelog**: https://github.com/terok-ai/terok-util/compare/v0.1.0...v0.2.0

## v0.1.0 — The Emissary

First public PyPi release. For historical pre-releases, see https://github.com/terok-ai/terok-util/releases.

