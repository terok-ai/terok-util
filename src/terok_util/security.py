# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Terminal output sanitization.

Strings derived from external sources (SSH key comments, file paths,
credential names, JSON config values) may contain ANSI escape sequences
or other C0/C1 control characters.  Printing them raw to a terminal
allows output spoofing, title manipulation, or clipboard injection via
OSC 52 (CWE-150).

[`sanitize_tty`][terok_util.security.sanitize_tty] strips these before display.
"""

from __future__ import annotations

import unicodedata


def sanitize_tty(s: str) -> str:
    """Replace terminal control characters with safe representations.

    Whitespace controls (newline, carriage return, tab) become spaces.
    All other characters in Unicode category ``C`` (control, format,
    surrogate, private use, unassigned) are rendered as ``\\xNN`` hex
    escapes.  Printable text passes through unchanged.
    """
    out: list[str] = []
    for ch in s:
        if ch in ("\n", "\r", "\t"):
            out.append(" ")
        elif unicodedata.category(ch).startswith("C"):
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


__all__ = ["sanitize_tty"]
