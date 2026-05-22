# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`sanitize_tty`][terok_util.security.sanitize_tty] (CWE-150 mitigation)."""

from __future__ import annotations

import pytest

from terok_util.security import sanitize_tty


class TestSanitizeTty:
    """Whitespace controls collapse to spaces; everything else in category C is hex-escaped."""

    def test_empty_string_round_trips(self) -> None:
        assert sanitize_tty("") == ""

    def test_plain_ascii_unchanged(self) -> None:
        assert sanitize_tty("alpine-7-redis") == "alpine-7-redis"

    def test_full_printable_ascii_passes_through(self) -> None:
        """Every byte in [0x20, 0x7E] is preserved, including markup chars."""
        full = "".join(chr(c) for c in range(0x20, 0x7F))
        assert sanitize_tty(full) == full

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("line1\nline2", "line1 line2"),
            ("tab\there", "tab here"),
            ("car\rriage", "car riage"),
        ],
    )
    def test_whitespace_controls_become_spaces(self, raw: str, expected: str) -> None:
        """Newline, carriage return, and tab collapse to a single space each."""
        assert sanitize_tty(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # NUL
            ("null\x00byte", "null\\x00byte"),
            # ESC (start of ANSI CSI)
            ("esc\x1bseq", "esc\\x1bseq"),
            # BEL
            ("ring\x07bell", "ring\\x07bell"),
            # DEL
            ("del\x7fchar", "del\\x7fchar"),
        ],
    )
    def test_non_whitespace_controls_become_hex_escapes(self, raw: str, expected: str) -> None:
        """C0/C1 control bytes are rendered as ``\\xNN`` so the literal sequence is visible."""
        assert sanitize_tty(raw) == expected

    def test_ansi_csi_sequence_is_escaped(self) -> None:
        """An ANSI colour escape doesn't reach the terminal — the ESC is hex-escaped."""
        # ESC [ 31 m  →  the ESC becomes ``\x1b``, the literal ``[31m`` survives
        result = sanitize_tty("\x1b[31mRED\x1b[0m")
        assert result == "\\x1b[31mRED\\x1b[0m"

    def test_unicode_letters_pass_through(self) -> None:
        """Unicode letters (category L) are printable and survive unchanged."""
        assert sanitize_tty("café") == "café"
        assert sanitize_tty("naïve") == "naïve"

    def test_bidi_override_is_escaped(self) -> None:
        """U+202E (RTL override) is category Cf — escaped to keep homoglyph attacks visible."""
        # The U+202E codepoint is spelled as a ``\u202e`` escape (not a raw
        # bidi char in the source) so the file remains visually auditable
        # — a reviewer can see exactly what the test feeds the sanitizer
        # without having to detect invisible right-to-left override marks.
        result = sanitize_tty("evil\u202e.com")
        assert "\\x" in result
        assert "\u202e" not in result

    def test_markup_chars_pass_unchanged(self) -> None:
        """``& < >`` are printable ASCII — sanitiser is wire-neutral, not HTML-aware."""
        assert sanitize_tty("<script>alert(1)</script>") == "<script>alert(1)</script>"
        assert sanitize_tty("a & b") == "a & b"
