# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Multi-distro test-matrix engine shared by the terok-* siblings.

Historically each sibling carried its own ~500-line ``run-matrix.sh``
plus a set of Containerfiles, ~90% identical across repos and already
drifting.  This package is the single engine; a consuming repo keeps only
a declarative ``tests/containers/matrix.yml`` and (optionally) per-slot
Containerfile fragments.

Collaborators, in reading order:

* [`catalog`][terok_util.matrix.catalog] — facts about the shared slot
  images (expected podman versions, non-systemd slots, slot kinds);
* [`config`][terok_util.matrix.config] — the ``matrix.yml`` schema and
  loader ([`MatrixConfig`][terok_util.matrix.config.MatrixConfig]);
* [`contract`][terok_util.matrix.contract] — the consumer conftest's half
  of the ``TEROK_EXPECT`` capability contract
  ([`check_capability_contract`][terok_util.matrix.contract.check_capability_contract]);
* [`inner`][terok_util.matrix.inner] — generates the scripts that run
  inside a slot's test container;
* [`runner`][terok_util.matrix.runner] — host-side build/run/prune;
* [`cli`][terok_util.matrix.cli] — the ``terok-matrix`` entry point
  (``uv run terok-matrix`` from a consuming repo's root).

External tooling (the superbuild TUI, CI matrix generation) should not
shell-parse anything: load the same ``matrix.yml`` via
[`load_config`][terok_util.matrix.config.load_config] or ask the CLI
(``terok-matrix --slots-json`` / ``--image-prefix``).
"""

from __future__ import annotations

from .catalog import SLOTS, SlotKind, SlotSpec
from .config import MatrixConfig, MatrixConfigError, load_config
from .contract import binary_on_path, check_capability_contract, tcp_reachable

__all__ = [
    "SLOTS",
    "MatrixConfig",
    "MatrixConfigError",
    "SlotKind",
    "SlotSpec",
    "binary_on_path",
    "check_capability_contract",
    "load_config",
    "tcp_reachable",
]
