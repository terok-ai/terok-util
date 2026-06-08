<!--
SPDX-FileCopyrightText: 2026 Jiri Vyskocil
SPDX-License-Identifier: Apache-2.0
-->

# terok-util

The shared utility library that sits at the bottom of the
[`terok-*`](https://github.com/terok-ai) dependency chain — the foundation of [terok](https://terok-ai.github.io/terok/).

Every terok-`*` package depends on `terok-util`. `terok-util` depends
on nothing else in the ecosystem. The package collects cross-cutting
bits that would otherwise be duplicated (or quietly diverge) across
the siblings: CLI registry types, filesystem invariants,
namespace-aware XDG path resolution, the layered YAML config-merge
engine, untrusted-string sanitisation, and a strict template renderer.

## Where to go next

- [Adoption Guide](adoption.md) — how to pick up `terok-util` in a
  downstream package; what to replace; what to leave alone.
- [API Reference](reference/) — every public symbol, grouped by
  module.
- [Code Metrics](code-metrics.md) — package-level stats (LoC,
  complexity, coverage).
- [CI Workflow Map](ci-map.md) — what runs where, on what triggers.

## Source

[github.com/terok-ai/terok-util](https://github.com/terok-ai/terok-util)
