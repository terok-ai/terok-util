# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Minimal template rendering via ``{{VAR}}`` token replacement.

Strict variant: every substitution value is scanned for C0 control
characters (newline, carriage return, NUL) and rejected if any are
present.  Without this check a template variable could inject extra
directives into the rendered systemd unit file, which the parser
interprets as legitimate configuration.
"""

from __future__ import annotations

from pathlib import Path

_FORBIDDEN_CHARS = frozenset("\n\r\0")


def render_template(template_path: Path, variables: dict[str, str]) -> str:
    """Read *template_path* and replace ``{{KEY}}`` tokens with *variables* values.

    Raises [`ValueError`][ValueError] if any value contains control characters
    (newline, carriage-return, NUL) that could inject extra directives
    into the rendered systemd unit.
    """
    for key, val in variables.items():
        if _FORBIDDEN_CHARS & set(val):
            raise ValueError(f"Template variable {key!r} contains forbidden control characters")
    content = template_path.read_text()
    for k, v in variables.items():
        content = content.replace(f"{{{{{k}}}}}", v)
    return content


__all__ = ["render_template"]
