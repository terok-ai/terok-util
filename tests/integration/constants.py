# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Named constants for the integration suite — no magic literals in tests.

The FHS destinations below are the *root* branch of
[`paths`][terok_util.paths]; the XDG names are the environment the
non-root branch reads.  They are spelled out here once so a test that
asserts "root lands in /etc/terok" cannot drift from a test that asserts
"non-root does not".
"""

from __future__ import annotations

from pathlib import Path

# ── Namespace + FHS destinations (the root branch of terok_util.paths) ──

NAMESPACE = "terok"

FHS_CONFIG_DIR = Path("/etc") / NAMESPACE
FHS_STATE_DIR = Path("/var/lib") / NAMESPACE
FHS_RUNTIME_DIR = Path("/run") / NAMESPACE

# ── Kernel surfaces the deep-reach tests read directly ─────────────────

UID_MAP_PATH = Path("/proc/self/uid_map")

#: The identity map every process in the *initial* user namespace sees.
#: Its presence is how a test tells "no userns in play" from "userns".
IDENTITY_UID_MAP_ROW = (0, 0, 4294967295)

# ── Child-process probe plumbing ───────────────────────────────────────

#: Seconds a probe interpreter gets before the test calls it hung.  A
#: bare `python -c "import terok_util"` is a ~50 ms affair even on the
#: slowest matrix slot; this is a hang detector, not a budget.
CHILD_TIMEOUT_S = 60

#: PATH handed to probe children.  Probes are run with a *scrubbed*
#: environment (the whole point is controlling XDG_*/HOME), so PATH has
#: to be reinstated explicitly.
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"

#: Exit code `pytest.exit` uses when the matrix capability contract is
#: broken — mirrors the sibling repos so the runner's log reads the same.
CONTRACT_EXIT_CODE = 3

# ── Import-laziness contract ───────────────────────────────────────────

#: Importing the `terok_util` barrel must pull none of these.  Each is a
#: real cost the barrel promises not to charge a caller who only wants,
#: say, `sanitize_tty`: the YAML round-tripper, the template engine
#: behind the matrix runner, and the matrix engine itself.
LAZY_FORBIDDEN_PREFIXES = (
    "ruamel",
    "jinja2",
    "terok_util.matrix",
    "terok_util.yaml",
)

#: Stdlib module nothing else in the import graph touches — a clean
#: canary for "LazyHandler has not resolved its target yet".
LAZY_CANARY_MODULE = "colorsys"
LAZY_CANARY_TARGET = f"{LAZY_CANARY_MODULE}:rgb_to_hls"

# ── Permission bits fs.write_sensitive_file promises ───────────────────

SENSITIVE_FILE_MODE = 0o600
SENSITIVE_PARENT_MODE = 0o700

#: Deliberately hostile umasks for the child that writes a secret: one
#: that would leave the file world-readable if the mode were left to the
#: umask, and one that already matches the promise (so a pass under the
#: first cannot be a lucky accident of the second).
PERMISSIVE_UMASK = 0o000
RESTRICTIVE_UMASK = 0o077

#: A directory the owner may enter and read but not write.  Non-root
#: hits EACCES on it; root sails straight through (DAC bypass) — the
#: uid-sensitivity that makes this worth an integration test.
UNWRITABLE_DIR_MODE = 0o500
WORLD_WRITABLE_DIR_MODE = 0o777
