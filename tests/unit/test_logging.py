# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`BestEffortLogger`][terok_util.logging.BestEffortLogger]."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from terok_util.logging import BestEffortLogger


class TestWrites:
    """Lines land in the file with the right shape."""

    def test_log_writes_timestamped_line(self, tmp_path: Path) -> None:
        log = tmp_path / "state" / "terok.log"
        BestEffortLogger(lambda: log).log("hello world")
        content = log.read_text(encoding="utf-8")
        assert "DEBUG: hello world" in content
        assert content.endswith("\n")

    def test_level_is_recorded(self, tmp_path: Path) -> None:
        log = tmp_path / "terok.log"
        BestEffortLogger(lambda: log).log("boom", level="ERROR")
        assert "ERROR: boom" in log.read_text(encoding="utf-8")

    def test_debug_helper(self, tmp_path: Path) -> None:
        log = tmp_path / "terok.log"
        BestEffortLogger(lambda: log).debug("dbg")
        assert "DEBUG: dbg" in log.read_text(encoding="utf-8")

    def test_warning_helper(self, tmp_path: Path) -> None:
        log = tmp_path / "terok.log"
        BestEffortLogger(lambda: log).warning("warn")
        assert "WARNING: warn" in log.read_text(encoding="utf-8")

    def test_appends_rather_than_truncates(self, tmp_path: Path) -> None:
        log = tmp_path / "terok.log"
        logger = BestEffortLogger(lambda: log)
        logger.log("first")
        logger.log("second")
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]

    def test_creates_missing_parent_dirs(self, tmp_path: Path) -> None:
        log = tmp_path / "deep" / "nested" / "terok.log"
        BestEffortLogger(lambda: log).log("x")
        assert log.exists()


class TestOwnerOnlyPermissions:
    """New log files are created mode 0o600 by construction."""

    def test_new_file_is_owner_only(self, tmp_path: Path) -> None:
        log = tmp_path / "terok.log"
        BestEffortLogger(lambda: log).log("secret-ish")
        mode = stat.S_IMODE(log.stat().st_mode)
        assert mode == 0o600


class TestWarnUser:
    """``warn_user`` sanitises the terminal, keeps the file faithful."""

    def test_stderr_is_sanitised(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """ANSI/control bytes in the stderr line are hex-escaped (CWE-150)."""
        log = tmp_path / "terok.log"
        BestEffortLogger(lambda: log).warn_user("comp\x1b[31m", "msg\x07")
        err = capsys.readouterr().err
        assert "\x1b" not in err
        assert "\x07" not in err
        assert "\\x1b" in err
        assert "\\x07" in err

    def test_file_keeps_original_bytes(self, tmp_path: Path) -> None:
        """The file side is unsanitised so forensic review sees the raw bytes."""
        log = tmp_path / "terok.log"
        BestEffortLogger(lambda: log).warn_user("comp", "raw\x1bseq")
        content = log.read_text(encoding="utf-8")
        assert "\x1bseq" in content
        assert "WARNING: [comp] raw" in content


class TestSoftFail:
    """A logging error never propagates to the caller."""

    def test_unwritable_destination_swallowed(self, tmp_path: Path) -> None:
        # A path whose parent is a regular file can't be mkdir'd — the write
        # must fail silently rather than raise into the caller's hot path.
        clash = tmp_path / "afile"
        clash.write_text("x", encoding="utf-8")
        logger = BestEffortLogger(lambda: clash / "nested" / "terok.log")
        logger.log("should not raise")  # no exception == pass

    def test_warn_user_absorbs_broken_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``warn_user`` absorbs an ``OSError`` from ``print`` to stderr.

        A broken pipe / EPIPE / closed FD on stderr must not propagate out of
        the helper, and the file-side ``WARNING`` line must still land so the
        forensic trail survives the stderr failure.
        """
        import io
        import sys

        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("broken pipe")

        broken = io.StringIO()
        broken.write = explode  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stderr", broken)

        log = tmp_path / "terok.log"
        BestEffortLogger(lambda: log).warn_user("vault", "stderr is dead")  # must not raise

        assert "WARNING: [vault] stderr is dead" in log.read_text(encoding="utf-8")


class TestLazyPathResolution:
    """The path callable is invoked on every write, not bound at construction."""

    def test_path_resolved_per_write(self, tmp_path: Path) -> None:
        first, second = tmp_path / "first.log", tmp_path / "second.log"
        destination = {"path": first}
        logger = BestEffortLogger(lambda: destination["path"])

        logger.log("one")
        # Redirect after construction — the next write must follow the new path,
        # proving the callable is re-evaluated rather than cached.
        destination["path"] = second
        logger.log("two")

        assert "one" in first.read_text(encoding="utf-8")
        assert "two" in second.read_text(encoding="utf-8")
        assert "two" not in first.read_text(encoding="utf-8")
