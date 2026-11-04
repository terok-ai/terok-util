# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Terminal output sanitization.

Strings derived from external sources (SSH key comments, file paths,
credential names, JSON config values, container output) may contain ANSI
escape sequences or other C0/C1 control characters.  Printing them raw to
a terminal allows output spoofing, title manipulation, or clipboard
injection via OSC 52 (CWE-150).

[`sanitize_tty`][terok_util.security.sanitize_tty] neutralises them in a
single-line field; [`sanitize_log_text`][terok_util.security.sanitize_log_text]
does the same for multiline task output while keeping its layout and,
on an interactive terminal, its colours.
"""

from __future__ import annotations

import re
import unicodedata

_SGR = re.compile(r"\x1b\[[0-9;:]*m")
"""A complete Select Graphic Rendition sequence: colour and style, the
one family of terminal controls that renders without side effects."""

_SGR_RESET = "\x1b[0m"


def sanitize_tty(s: str) -> str:
    """Replace terminal control characters with safe representations.

    Whitespace controls (newline, carriage return, tab) become spaces.
    All other characters in Unicode category ``C`` (control, format,
    surrogate, private use, unassigned) are rendered as ``\\xNN`` hex
    escapes.  Printable text passes through unchanged.
    """
    return _escape(s, multiline=False)


def sanitize_log_text(text: str, *, color: bool) -> str:
    """Make multiline task output safe to display while keeping its layout.

    Line breaks and tabs survive, and ``\\r\\n`` reads as ``\\n``, so log
    lines keep their shape.  Complete SGR colour sequences are kept when
    *color* is true (an interactive terminal renders them) and removed
    otherwise (a file or a pipe), so a legitimate agent's colours reach
    the operator but never land in a log file as noise.  Kept colours are
    reset at the end of the text, so a task's styling cannot outlive its
    own output.  Every other control character is hex-escaped exactly as
    in [`sanitize_tty`][terok_util.security.sanitize_tty]: cursor
    movement, screen erasure, OSC title or clipboard writes and bidi
    overrides stay visible text.
    """
    text = text.replace("\r\n", "\n")
    escaped = [_escape(piece, multiline=True) for piece in _SGR.split(text)]
    sequences = _SGR.findall(text)
    if not color or not sequences:
        return "".join(escaped)
    return "".join(a + b for a, b in zip(escaped, [*sequences, _SGR_RESET], strict=True))


def _escape(s: str, *, multiline: bool) -> str:
    """Hex-escape every control character; keep newline and tab when *multiline*."""
    out: list[str] = []
    for ch in s:
        if multiline and ch in ("\n", "\t"):
            out.append(ch)
        elif ch in ("\n", "\r", "\t"):
            out.append(" ")
        elif unicodedata.category(ch).startswith("C"):
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


__all__ = ["sanitize_log_text", "sanitize_tty"]
