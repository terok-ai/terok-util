# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The barrel's laziness, measured on the interpreter the slot ships.

``import terok_util`` is on the startup path of every sibling CLI, so
what it drags in is a cost every ``terok`` invocation pays.  The barrel
therefore flattens only the cheap, stdlib-shaped modules and leaves the
expensive ones behind a submodule import: the YAML round-tripper
(``ruamel``), the template engine the matrix runner needs (``jinja2``),
and the matrix engine itself.  [`LazyHandler`][terok_util.cli_types.LazyHandler]
extends the same bargain to CLI verbs — building a command tree must not
import a single handler module.

Unit tests cannot police this.  By the time a unit test runs, pytest has
imported the world; ``sys.modules`` in that process says nothing about
what a *fresh* one would load.  Only a child interpreter can answer, and
which interpreter matters: the matrix slots run this on Python 3.12,
3.13 and 3.14, on distro builds and on uv-managed ones, and a lazily-
imported module is exactly the kind of thing a new Python's import
machinery can quietly start eager-loading.
"""

from __future__ import annotations

import sys

import pytest

from .constants import LAZY_CANARY_MODULE, LAZY_CANARY_TARGET, LAZY_FORBIDDEN_PREFIXES

pytestmark = pytest.mark.needs_host_features


# Each probe prints the module names it cares about, so a failing matrix
# log shows *what* leaked in rather than just that something did.
_BARREL_PROBE = """
import json, sys
import terok_util
print(json.dumps({
    "python": list(sys.version_info[:2]),
    "loaded": sorted(sys.modules),
}))
"""

_MATRIX_PROBE = """
import json, sys
import terok_util.matrix
print(json.dumps({"loaded": sorted(sys.modules)}))
"""

_HANDLER_PROBE = """
import json, sys
from terok_util import LazyHandler

handler = LazyHandler({target!r})
before = {canary!r} in sys.modules
result = handler(0.0, 0.0, 0.0)
after = {canary!r} in sys.modules
print(json.dumps({{
    "imported_before_call": before,
    "imported_after_call": after,
    "called": result is not None,
}}))
"""


def _leaked(loaded: list[str]) -> list[str]:
    """The forbidden modules present in *loaded*, if any."""
    return [
        name
        for name in loaded
        if any(name == p or name.startswith(f"{p}.") for p in LAZY_FORBIDDEN_PREFIXES)
    ]


def test_importing_the_barrel_pulls_no_heavy_submodule(child_json) -> None:
    """`import terok_util` loads neither ruamel, nor jinja2, nor the matrix.

    The whole contract in one assertion, asked of a process that has
    imported nothing else.  A stray top-level ``from .yaml import load``
    added to ``__init__`` — the natural thing to reach for when a helper
    "obviously belongs in the barrel" — is caught here and nowhere else.
    """
    result = child_json(_BARREL_PROBE, None)

    assert not _leaked(result["loaded"]), (
        f"terok_util pulled {_leaked(result['loaded'])} on Python "
        f"{result['python'][0]}.{result['python'][1]}"
    )


def test_the_matrix_submodule_really_is_the_heavy_one(child_json) -> None:
    """Positive control: importing terok_util.matrix *does* pull ruamel.

    Without this, the test above would keep passing if someone renamed
    ``ruamel.yaml`` out from under the forbidden list, or if the matrix
    engine stopped existing — a green light that means nothing.  Here the
    cost is real and expected, which is precisely why the barrel refuses
    to pay it for everyone.
    """
    result = child_json(_MATRIX_PROBE, None)

    assert any(name.startswith("ruamel") for name in result["loaded"])


def test_lazy_handler_defers_its_import_until_called(child_json) -> None:
    """A LazyHandler is inert until dispatch actually reaches it.

    ``colorsys`` stands in for a CLI subsystem: nothing else in the
    import graph touches it, so its presence in ``sys.modules`` can only
    mean the handler resolved.  Constructing the handler must not; calling
    it must.
    """
    result = child_json(
        _HANDLER_PROBE.format(target=LAZY_CANARY_TARGET, canary=LAZY_CANARY_MODULE), None
    )

    assert result["imported_before_call"] is False
    assert result["imported_after_call"] is True
    assert result["called"] is True


def test_the_probe_interpreter_is_a_supported_python(child_json) -> None:
    """The laziness claims above are about *this slot's* Python.

    Cheap sanity so a slot that silently fell back to some other
    interpreter cannot report a green laziness result for a Python we
    never meant to test.
    """
    result = child_json(_BARREL_PROBE, None)

    assert tuple(result["python"]) == sys.version_info[:2]
    assert tuple(result["python"]) >= (3, 12)
