# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`BestEffortLogger`][terok_util.logging.BestEffortLogger]."""

from __future__ import annotations

import stat
from collections.abc import Iterator
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


class TestConfigure:
    """`configure` installs the unified root handler (journald or stderr)."""

    @pytest.fixture(autouse=True)
    def _restore_root(self) -> Iterator[None]:
        """Snapshot and restore the root logger so tests don't leak handlers."""
        import logging

        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        yield
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    def test_stderr_fallback_when_no_journald(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import logging

        from terok_util import logging as tlog

        monkeypatch.setattr(tlog, "journald_available", lambda: False)
        tlog.configure("terok-x", level=logging.INFO)
        logging.getLogger("terok_x.sub").info("hello-fallback")
        assert "hello-fallback" in capsys.readouterr().err

    def test_journald_handler_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import logging

        from terok_util import logging as tlog

        class _FakeWriter:
            def __init__(self, identifier: str, *, static_fields: object = None) -> None:
                self.identifier = identifier
                self.sent: list[tuple[str, int, dict]] = []

            def send(self, message: str, *, priority: int = 6, **fields: str) -> None:
                self.sent.append((message, priority, fields))

            def close(self) -> None:
                pass

        monkeypatch.setattr(tlog, "journald_available", lambda: True)
        monkeypatch.setattr(tlog, "JournalWriter", _FakeWriter)
        (handler,) = tlog.configure("terok-shield")
        logging.getLogger("terok_shield.dns").warning("dns-warn")

        sent = handler._writer.sent  # type: ignore[attr-defined]
        assert sent and sent[0][0] == "dns-warn"
        assert sent[0][1] == tlog.PRIORITY_WARNING
        assert sent[0][2]["LOGGER"] == "terok_shield.dns"

    def test_stderr_flag_adds_stderr_alongside_journald(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import logging

        from terok_util import logging as tlog

        class _FakeWriter:
            def __init__(self, identifier: str, *, static_fields: object = None) -> None:
                self.sent: list[tuple[str, int, dict]] = []

            def send(self, message: str, *, priority: int = 6, **fields: str) -> None:
                self.sent.append((message, priority, fields))

            def close(self) -> None:
                pass

        monkeypatch.setattr(tlog, "journald_available", lambda: True)
        monkeypatch.setattr(tlog, "JournalWriter", _FakeWriter)
        handlers = tlog.configure("terok-clearance-hub", stderr=True)
        logging.getLogger("terok_clearance.hub").info("dual-sink")

        assert len(handlers) == 2  # journald + stderr
        assert handlers[0]._writer.sent[0][0] == "dual-sink"  # type: ignore[attr-defined]
        assert "dual-sink" in capsys.readouterr().err  # parent's pipe still fed

    def test_reconfigure_does_not_stack_handlers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import logging

        from terok_util import logging as tlog

        monkeypatch.setattr(tlog, "journald_available", lambda: False)
        tlog.configure("terok-a")
        tlog.configure("terok-b")
        root = logging.getLogger()
        tagged = [h for h in root.handlers if getattr(h, tlog._HANDLER_TAG, False)]
        assert len(tagged) == 1

    def test_emit_survives_a_failing_writer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A writer that raises is routed through ``handleError``, never the caller."""
        import logging

        from terok_util import logging as tlog

        class _BrokenWriter:
            def __init__(self, identifier: str, *, static_fields: object = None) -> None:
                pass

            def send(self, message: str, *, priority: int = 6, **fields: str) -> None:
                raise OSError("journal gone")

            def close(self) -> None:
                pass

        monkeypatch.setattr(tlog, "journald_available", lambda: True)
        monkeypatch.setattr(tlog, "JournalWriter", _BrokenWriter)
        (handler,) = tlog.configure("terok-x")
        handled: list[logging.LogRecord] = []
        monkeypatch.setattr(handler, "handleError", handled.append)

        logging.getLogger("terok_x.sub").info("boom")  # must not raise
        assert len(handled) == 1

    def test_close_closes_the_journal_writer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Closing the handler releases the underlying journal socket."""
        from terok_util import logging as tlog

        class _FakeWriter:
            def __init__(self, identifier: str, *, static_fields: object = None) -> None:
                self.closed = False

            def send(self, message: str, *, priority: int = 6, **fields: str) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        monkeypatch.setattr(tlog, "journald_available", lambda: True)
        monkeypatch.setattr(tlog, "JournalWriter", _FakeWriter)
        (handler,) = tlog.configure("terok-x")
        writer = handler._writer  # type: ignore[attr-defined]
        handler.close()
        assert writer.closed is True
