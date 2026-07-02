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
- **Third-party, major ≥ 1** → pin by **range** (e.g. `pkg>=2.6`), trusting
  the package to honour semver. If a specific `>=1` dependency is known to
  break semver, tighten it deliberately.
- **Sibling `terok-*` deps** → **exempt**: keep ranges (or their
  release-wheel URL pin). We guarantee patch-level API stability across the
  sibling packages, so a `0.y` range there will not silently break — do
  *not* exact-pin them (it would fight the multi-repo release/PR-chain flow).

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
