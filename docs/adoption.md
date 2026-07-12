<!--
SPDX-FileCopyrightText: 2026 Jiri Vyskocil
SPDX-License-Identifier: Apache-2.0
-->

# Usage guide

`terok-util` consolidates symbols that previously lived in
near-duplicate form across the terok-`*` siblings. That consolidation
is complete — every sibling imports from `terok_util` today. This page
covers how to depend on the package and what to know when calling it.

## Add the dependency

Siblings pin the release wheel in `pyproject.toml` (replace the
version with the release you target):

```toml
[project]
dependencies = [
    "terok-util @ https://github.com/terok-ai/terok-util/releases/download/vX.Y.Z/terok_util-X.Y.Z-py3-none-any.whl",
]
```

During a cross-repo PR chain, the pin temporarily points at the PR
branch tip on the contributor's fork instead:

```toml
"terok-util @ git+https://github.com/<fork-owner>/terok-util.git@<pr-branch>",
```

Refresh `uv.lock` after editing. The `no-git-deps` release guard
rejects builds while a git pin is in place, so repin to the release
wheel before cutting a release.

## Import surface

Everything public is importable from the package root:

```python
from terok_util import CommandDef, ensure_dir, namespace_state_dir, ...
```

The one exception is the round-trip YAML facade, reached as a
submodule: `from terok_util.yaml import load, dump`.

## Behaviour to know about

- **`namespace_*_dir(subdir, env_var=…)`** — the `env_var` parameter
  is **keyword-only**. Calls with a positional second argument fail
  loudly instead of silently reinterpreting a path string as an env
  var name.
- **Layered config readers** — `read_config_section(section)` and
  `read_config_top_level(key)` walk `/etc/terok/config.yml` →
  `~/.config/terok/config.yml`. Both honour the `TEROK_CONFIG_FILE`
  env var as a single-file override with no layering (useful for
  tests). Both are process-cached;
  `_reset_config_caches_for_tests()` invalidates the caches between
  test cases that swap config files.
- **`CommandDef`** — one nominal type across the ecosystem. Don't
  define a structurally-compatible local copy; import it.

## Versioning

`terok-util` follows semver. Symbols listed in
[`src/terok_util/__init__.py`'s `__all__`](https://github.com/terok-ai/terok-util/blob/master/src/terok_util/__init__.py)
are stable across minor releases. Anything underscore-prefixed or
absent from `__all__` is internal and may change without notice.
