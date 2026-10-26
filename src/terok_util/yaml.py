# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Centralised YAML I/O — round-trip mode everywhere.

**Facade** over ``ruamel.yaml``'s ceremony-heavy ``YAML()`` class: callers get
a minimal ``load`` / ``dump`` / ``literal_block`` / ``YAMLError`` surface
instead of instance creation, typ selection, stream management, and config
attributes.

**Adapter** from ruamel.yaml's stream-oriented API to the string-based
convention used throughout the ecosystem (``path.read_text()`` → ``load(text)``
→ modify → ``dump(data)`` → ``path.write_text(text)``).

``CommentedMap`` is a ``dict`` subclass — ``isinstance(x, dict)``, ``.get()``,
``x["key"]``, ``.setdefault()`` all work transparently.  Pydantic v2
``model_validate()`` accepts dict subclasses, so read-side validation is
unchanged.
"""

from __future__ import annotations

import re
from io import StringIO
from typing import Any

from ruamel.yaml import YAML, YAMLError  # noqa: F401 — re-exported
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

__all__ = ["load", "dump", "literal_block", "YAMLError"]

#: Characters a literal block cannot carry.  A block has no escapes, and
#: the YAML reader rejects non-printable characters and breaks lines at
#: CR, NEL, LS and PS.
_BLOCK_UNSAFE = re.compile(
    r"[^\t\n\x20-\x7e\xa0-\u2027\u202a-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def _rt() -> YAML:
    """Build a fresh round-trip ``YAML`` instance, quotes preserved.

    A new instance per call is deliberate: ``YAML`` carries composer/
    parser/scanner state on the object, so a shared module-level instance
    races under concurrent ``load`` / ``dump`` from threads (e.g. Textual
    worker threads) — the symptom is ``'MappingEndEvent' object has no
    attribute 'anchor'``.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


def load(text: str) -> Any:
    """Round-trip load from a YAML string, preserving comments and order."""
    return _rt().load(text)


def dump(data: Any, *, default_flow_style: bool = False) -> str:
    """Round-trip dump to a YAML string, preserving comments and order.

    Key order is preserved (insertion order for new dicts, original order for
    round-tripped data).  ``sort_keys`` is always ``False`` — the caller never
    needs to pass it.
    """
    emitter = _rt()
    if default_flow_style:
        emitter.default_flow_style = True
    buf = StringIO()
    emitter.dump(data, buf)
    return buf.getvalue()


def literal_block(text: str) -> str:
    """Mark *text* to dump as a literal block scalar (``|``).

    Multi-line text, such as a Dockerfile snippet, then stays one
    readable block in the file instead of a quoted line full of ``\\n``
    escapes.  Text a block cannot carry — a control character, or a
    line break other than ``\\n`` — dumps double-quoted with escapes
    instead, so it always loads back unchanged.  The value is still a
    ``str`` and compares equal to *text*.
    """
    if _BLOCK_UNSAFE.search(text):
        return DoubleQuotedScalarString(text)
    return LiteralScalarString(text)
