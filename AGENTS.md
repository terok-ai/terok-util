# Agent Guide (terok-util)

`terok-util` is the shared foundation library imported directly by every
`terok-*` sibling (`from terok_util import …`). It sits below the whole
stack, so keep its public API small and stable.

Standard workflow: `make lint` / `make format` before committing;
`make test` and `make check` before pushing.

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

Dev / test / docs / tooling dependencies (the `[tool.poetry.group.*]` groups)
are **exempt** — they are not shipped to installers and exact-pinning them is
an unwarranted maintenance burden the developers can absorb. After changing
any pin, run `poetry lock` and commit `pyproject.toml` and `poetry.lock`
together.

**No comments in `pyproject.toml`.** Do **not** add comments to
`pyproject.toml`, with the single exception of the standing dependency-pinning
policy note above the `dependencies` table. In particular **never** add a
comment about a dependency that is temporarily pinned to a git branch during a
multi-repo PR chain, and never mention the PR-chain workflow in
`pyproject.toml` at all. Cross-repo merges are performed by a script that does
not understand comments, so any stray dev-cycle comment is carried straight
into a production release. Keep such rationale in commit messages, PR
descriptions, or this file.
