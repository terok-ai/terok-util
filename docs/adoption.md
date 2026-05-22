<!--
SPDX-FileCopyrightText: 2026 Jiri Vyskocil
SPDX-License-Identifier: Apache-2.0
-->

# Adoption guide

`terok-util` consolidates symbols that previously lived in
near-duplicate form across the terok-`*` siblings. This page is the
crib sheet for picking up `terok-util` in a downstream package and
removing the duplicated original.

## Add the dependency

In `pyproject.toml`:

```toml
[project]
dependencies = [
    "terok-util @ https://github.com/terok-ai/terok-util/releases/download/v0.1.0/terok_util-0.1.0-py3-none-any.whl",
    # ...other deps
]
```

During pre-release review, you can pin to a branch tip instead:

```toml
"terok-util @ git+https://github.com/terok-ai/terok-util.git@chore/some-feature",
```

Refresh `poetry.lock` after editing.

## Switch the imports

The migration is mostly mechanical — find the local copy of the
extracted symbol and route the import through `terok_util`.

| Old import | New import |
|---|---|
| `from terok_sandbox.commands._types import CommandDef, ArgDef, CommandTree` | `from terok_util import CommandDef, ArgDef, CommandTree` |
| `from terok_shield.commands import CommandDef` (local copy) | `from terok_util import CommandDef` |
| `from terok_sandbox._util._fs import ensure_dir, write_sensitive_file` | `from terok_util import ensure_dir, write_sensitive_file` |
| `from terok_sandbox.paths import namespace_state_dir, namespace_config_dir, namespace_runtime_dir` | `from terok_util import namespace_state_dir, namespace_config_dir, namespace_runtime_dir` |
| `from terok_sandbox.paths import read_config_section, read_config_top_level` | `from terok_util import read_config_section, read_config_top_level` |
| `from terok_sandbox.config_stack import ConfigStack, deep_merge` | `from terok_util import ConfigStack, deep_merge` |
| `from terok_sandbox._util._sanitize import sanitize_tty` | `from terok_util import sanitize_tty` |
| `from terok_sandbox._util._templates import render_template` | `from terok_util import render_template` |
| `from terok_executor._util._podman import podman_userns_args` | `from terok_util import podman_userns_args` |

## Delete the local copy

After the imports are switched, delete the now-unused local module
and its tests. The unit tests for the extracted symbols live in
`terok-util`'s own test suite; downstream packages typically retain
only their integration-level tests that exercise the symbols *in
their own context*.

## Behaviour changes to know about

- **`namespace_*_dir(subdir, env_var=…)`** — the `env_var` parameter
  is **keyword-only**. Calls with a positional second argument fail
  loudly instead of silently reinterpreting a path string as an env
  var name. Update call sites accordingly.
- **`render_template`** — the strict variant (control-character
  rejection) is canonical. If your downstream previously used a
  permissive variant, any callers that pushed control characters
  through the template will now raise. Production callers should
  not be affected; test fixtures occasionally need updating.
- **`CommandDef`** — there is now one nominal type across the
  ecosystem. Packages that previously defined their own
  structurally-compatible-but-distinct `CommandDef` (e.g.
  `terok-shield`, `terok-clearance`) drop their local copy. The
  duck-typing concessions previously needed at the wire layer
  (`# type: ignore`, `getattr(..., None)`) can come out as well.
- **Layered config readers** — `read_config_section(section)` and
  `read_config_top_level(key)` consolidate the per-sibling pattern of
  walking `/etc/terok/config.yml` → `~/.config/terok/config.yml`.
  Both honour the `TEROK_CONFIG_FILE` env var as a single-file
  override (useful for tests). Both are process-cached;
  `_reset_config_caches_for_tests()` invalidates the caches between
  test cases that swap config files.

## Versioning

`terok-util` follows semver. Symbols listed in
[`src/terok_util/__init__.py`'s `__all__`](https://github.com/terok-ai/terok-util/blob/master/src/terok_util/__init__.py)
are stable across minor releases. Anything underscore-prefixed or
absent from `__all__` is internal and may change without notice.
