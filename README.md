<!--
SPDX-FileCopyrightText: 2026 Jiri Vyskocil
SPDX-License-Identifier: Apache-2.0
-->

# terok-util

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
| `paths` | `namespace_state_dir`, `namespace_config_dir`, `namespace_runtime_dir` — XDG-aware path resolution for a per-namespace deployment. |
| `config_stack` | `ConfigStack` + `deep_merge` — layered round-trip YAML config merge engine. |
| `security` | `sanitize_tty` — strips C0 / C1 / ANSI sequences from untrusted strings before rendering to the operator's terminal (CWE-150 mitigation). |
| `podman` | `podman_userns_args` — rootless `--userns=keep-id:uid=1000,gid=1000` builder. |

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

## License

Apache-2.0. See [LICENSE](LICENSE).
