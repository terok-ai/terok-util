# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`render_template`][terok_util.templates.render_template]."""

from __future__ import annotations

from pathlib import Path

import pytest

from terok_util.templates import render_template


class TestRenderTemplateValidation:
    """``render_template`` must reject control characters in substitution values."""

    def test_rejects_newline_in_value(self, tmp_path: Path) -> None:
        """Newline in a template variable is rejected."""
        tpl = tmp_path / "test.service"
        tpl.write_text("ExecStart={{BIN}}")
        with pytest.raises(ValueError, match="forbidden control characters"):
            render_template(tpl, {"BIN": "/usr/bin/evil\n[Install]"})

    def test_rejects_carriage_return(self, tmp_path: Path) -> None:
        """Carriage return in a template variable is rejected."""
        tpl = tmp_path / "test.service"
        tpl.write_text("ExecStart={{BIN}}")
        with pytest.raises(ValueError, match="forbidden control characters"):
            render_template(tpl, {"BIN": "path\rwith\rcr"})

    def test_rejects_nul(self, tmp_path: Path) -> None:
        """NUL byte in a template variable is rejected."""
        tpl = tmp_path / "test.service"
        tpl.write_text("ExecStart={{BIN}}")
        with pytest.raises(ValueError, match="forbidden control characters"):
            render_template(tpl, {"BIN": "path\x00evil"})

    def test_accepts_clean_values(self, tmp_path: Path) -> None:
        """Normal values without control characters are accepted."""
        tpl = tmp_path / "test.service"
        tpl.write_text("ExecStart={{BIN}} --port={{PORT}}")
        result = render_template(tpl, {"BIN": "/usr/local/bin/terok-gate", "PORT": "9418"})
        assert result == "ExecStart=/usr/local/bin/terok-gate --port=9418"

    def test_error_names_offending_key(self, tmp_path: Path) -> None:
        """Error message identifies which variable was invalid."""
        tpl = tmp_path / "test.service"
        tpl.write_text("{{SAFE}} {{BAD}}")
        with pytest.raises(ValueError, match="BAD"):
            render_template(tpl, {"SAFE": "ok", "BAD": "not\nok"})

    def test_unmatched_token_left_in_place(self, tmp_path: Path) -> None:
        """A ``{{VAR}}`` token not present in ``variables`` is left as-is."""
        tpl = tmp_path / "test.service"
        tpl.write_text("{{KNOWN}} and {{UNKNOWN}}")
        result = render_template(tpl, {"KNOWN": "ok"})
        assert result == "ok and {{UNKNOWN}}"

    def test_empty_variables_round_trips(self, tmp_path: Path) -> None:
        """A template with no tokens and an empty mapping renders verbatim."""
        tpl = tmp_path / "test.service"
        tpl.write_text("literal content\nwith newlines\n")
        assert render_template(tpl, {}) == "literal content\nwith newlines\n"
