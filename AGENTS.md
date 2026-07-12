# Agent Guide (terok-util)

`terok-util` is the shared foundation library imported directly by every
`terok-*` sibling (`from terok_util import …`). It sits below the whole
stack, so keep its public API small and stable.

Standard workflow: `make lint` / `make format` before committing;
`make test` and `make check` before pushing.

**During development, ALWAYS iterate with `make test-fast`** — it runs only
the tests affected by your branch diff (tach impact analysis, no coverage).
Rerunning the full suite after every edit is the single biggest time sink in
agent dev loops — don't do it; run the full `make test` exactly once, right
before committing. One exception: impact analysis follows the Python import
graph only, so after changing non-Python inputs (YAML, templates, shell
scripts) run the full `make test` — `make test-fast` would skip tests that
are actually affected.

## Dependency Pinning & `pyproject.toml` Hygiene

**Version pinning policy.** Runtime/production dependencies — those pulled in
by a plain `pip install` / `pipx install` of this package (the
`[project].dependencies` table) — are pinned by the dependency's major
version:

- **Third-party, major 0 (`0.y.z`)** → pin to an **exact patch**
  (`pkg==0.y.z`). Pre-1.0 packages promise no compatibility across either
  minors *or* patches, so a floating range invites silent breakage.
- **Third-party, major ≥ 1** → **compatible-release at the tested
  baseline**: `pkg~=X.Y` where `X.Y` is the locked major.minor (floor =
  what we test against, cap = next major). Use the patch-series form
  `pkg~=X.Y.Z` only where a specific patch floor is required — note the
  PEP 440 truncation rule: the cap is one level above the last written
  component (`~=2.13` → `<3`, `~=8.2.5` → `<8.3`). Prefer `~=` over a
  hand-rolled `>=,<` pair: it states the baseline as one fact with the
  ceiling derived by construction, so the bounds cannot drift apart.
- **Sibling `terok-*` deps** → `~=0.y.z` (or their release-wheel URL pin).
  We guarantee patch-level API stability across the sibling packages, so
  the patch-series form is exactly right — do *not* exact-pin them (it
  would fight the multi-repo release/PR-chain flow).

Dev / test / docs / tooling dependencies (the `[dependency-groups]` tables)
are **exempt** — they are not shipped to installers and exact-pinning them is
an unwarranted maintenance burden the developers can absorb. After changing
any pin, run `uv lock` and commit `pyproject.toml` and `uv.lock`
together.

**Comment discipline in `pyproject.toml`.** The dependency tables stay
comment-free and self-documenting, apart from the standing policy pointer
above them. **Never** comment on why a dependency -- especially a sibling
`terok-*` package -- is pinned a certain way, and never mention dev-cycle
state (temporary git-branch pins, the multi-repo PR chain): cross-repo
merges are performed by a script that does not understand comments, so any
such note is carried straight into a production release. Keep pin
rationale in commit messages, PR descriptions, or this file. Ordinary
explanatory comments in `[tool.*]` sections are fine. `pyproject.toml`
stays ASCII-only.
