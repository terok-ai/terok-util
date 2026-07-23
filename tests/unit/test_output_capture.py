# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the output-capture tee (journald/file stream sinks, pty/pipe)."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from terok_util import journal, output_capture as oc


def test_file_stream_sink_writes_bytes_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "run.log"
    sink = oc._FileStreamSink(path)
    sink.write(b"first\n")
    sink.write(b"second\n")
    sink.close()
    assert path.read_bytes() == b"first\nsecond\n"
    assert (path.stat().st_mode & 0o777) == 0o600


def _bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> socket.socket:
    sock_path = tmp_path / "journal.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(sock_path))
    receiver.settimeout(2.0)
    monkeypatch.setattr(journal, "JOURNALD_SOCKET", sock_path)
    return receiver


def test_journal_stream_sink_one_entry_per_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = _bind(tmp_path, monkeypatch)
    try:
        sink = oc._JournalStreamSink("terok", {"TEROK_KIND": "run"})
        sink.write(b"alpha\nbeta\n")
        first, second = receiver.recv(65536), receiver.recv(65536)
        sink.close()
    finally:
        receiver.close()
    assert b"MESSAGE=alpha\n" in first
    assert b"TEROK_KIND=run\n" in first
    assert b"MESSAGE=beta\n" in second


def test_journal_stream_sink_collapses_carriage_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = _bind(tmp_path, monkeypatch)
    try:
        sink = oc._JournalStreamSink("terok", {})
        sink.write(b"10%\r55%\r100% done\n")
        datagram = receiver.recv(65536)
        sink.close()
    finally:
        receiver.close()
    assert b"MESSAGE=100% done\n" in datagram
    assert b"10%" not in datagram


def test_journal_stream_sink_flushes_tail_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = _bind(tmp_path, monkeypatch)
    try:
        sink = oc._JournalStreamSink("terok", {})
        sink.write(b"no-newline-tail")
        sink.close()
        datagram = receiver.recv(65536)
    finally:
        receiver.close()
    assert b"MESSAGE=no-newline-tail\n" in datagram


def test_journal_stream_sink_hint_names_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = _bind(tmp_path, monkeypatch)
    try:
        sink = oc._JournalStreamSink("terok", {"TEROK_KIND": "run", "TEROK_TASK": "t1"})
        hint = sink.hint()
        sink.close()
    finally:
        receiver.close()
    assert "journalctl -t terok" in hint
    assert "TEROK_KIND=run" in hint
    assert "TEROK_TASK=t1" in hint


def test_tee_output_forwards_live_and_persists_to_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    log_path = tmp_path / "run.log"
    monkeypatch.setattr(oc, "journald_available", lambda: False)

    with oc.tee_output("terok", fields={"TEROK_KIND": "run"}, file_path_fn=lambda: log_path):
        os.write(1, b"direct-fd-output\n")
        subprocess.run(["printf", "subprocess-output\\n"], check=True)

    live = capfd.readouterr()
    logged = log_path.read_text()
    assert "direct-fd-output" in live.out
    assert "subprocess-output" in live.out
    assert "direct-fd-output" in logged
    assert "subprocess-output" in logged
    assert str(log_path) in live.err  # discoverability hint


def test_tee_output_runs_untee_when_no_sink_available(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(oc, "journald_available", lambda: False)
    ran = []
    with oc.tee_output("terok", fields={}, file_path_fn=None):
        ran.append(True)
    assert ran == [True]  # block still executed with no durable sink


def test_tee_output_uses_journald_sink_when_socket_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """With a journald socket present, output is teed to the journal, not a file."""
    sock_path = tmp_path / "journal.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(sock_path))
    receiver.settimeout(2.0)
    monkeypatch.setattr(journal, "JOURNALD_SOCKET", sock_path)
    monkeypatch.setattr(oc, "journald_available", lambda: True)
    try:
        with oc.tee_output("terok", fields={"TEROK_KIND": "run"}):
            os.write(1, b"journal-bound-line\n")
        datagram = receiver.recv(65536)
    finally:
        receiver.close()

    assert b"journal-bound-line" in datagram
    assert b"SYSLOG_IDENTIFIER=terok\n" in datagram
    assert "journalctl" in capfd.readouterr().err  # discoverability hint


def test_tee_output_runs_untee_when_file_path_unopenable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file sink that fails to open drops to running the block un-teed."""
    monkeypatch.setattr(oc, "journald_available", lambda: False)

    def _boom() -> Path:
        raise OSError("no such directory")

    ran = []
    with oc.tee_output("terok", fields={}, file_path_fn=_boom):
        ran.append(True)
    assert ran == [True]
