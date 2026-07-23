# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the native-protocol journald writer."""

from __future__ import annotations

import socket
import struct
from pathlib import Path

import pytest

from terok_util import journal


def test_encode_field_compact_form() -> None:
    assert journal.encode_field("MESSAGE", b"hello") == b"MESSAGE=hello\n"


def test_encode_field_binary_form_for_multiline() -> None:
    value = b"line1\nline2"
    assert journal.encode_field("MESSAGE", value) == (
        b"MESSAGE\n" + struct.pack("<Q", len(value)) + value + b"\n"
    )


def test_encode_fields_concatenates() -> None:
    out = journal.encode_fields({"A": "1", "B": "2"})
    assert out == b"A=1\nB=2\n"


def test_journald_available_true_for_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock_path = tmp_path / "journal.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(sock_path))
    monkeypatch.setattr(journal, "JOURNALD_SOCKET", sock_path)
    try:
        assert journal.journald_available() is True
    finally:
        receiver.close()


def test_journald_available_false_for_plain_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "not-a-socket"
    plain.write_text("")
    monkeypatch.setattr(journal, "JOURNALD_SOCKET", plain)
    assert journal.journald_available() is False


def _bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> socket.socket:
    sock_path = tmp_path / "journal.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(sock_path))
    receiver.settimeout(2.0)
    monkeypatch.setattr(journal, "JOURNALD_SOCKET", sock_path)
    return receiver


def test_writer_sends_identifier_priority_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = _bind(tmp_path, monkeypatch)
    try:
        writer = journal.JournalWriter("terok-shield", static_fields={"TEROK_KIND": "run"})
        writer.send("hello", priority=journal.PRIORITY_WARNING, CODE_LINE="42")
        datagram = receiver.recv(65536)
        writer.close()
    finally:
        receiver.close()
    assert b"SYSLOG_IDENTIFIER=terok-shield\n" in datagram
    assert b"TEROK_KIND=run\n" in datagram
    assert b"PRIORITY=4\n" in datagram
    assert b"CODE_LINE=42\n" in datagram
    assert b"MESSAGE=hello\n" in datagram


def test_writer_multiline_message_uses_binary_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = _bind(tmp_path, monkeypatch)
    try:
        writer = journal.JournalWriter("terok")
        writer.send("traceback\n  line 2")
        datagram = receiver.recv(65536)
        writer.close()
    finally:
        receiver.close()
    # multi-line MESSAGE switches to the length-prefixed binary form
    assert b"MESSAGE\n" + struct.pack("<Q", len(b"traceback\n  line 2")) in datagram


def test_writer_send_is_best_effort_when_socket_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = _bind(tmp_path, monkeypatch)
    writer = journal.JournalWriter("terok")
    receiver.close()  # peer vanishes
    # send must swallow the resulting OSError rather than raise into the caller
    writer.send("into the void")
    writer.close()


def test_journald_available_false_when_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``is_socket`` that raises ``OSError`` reads as unavailable, not a crash."""

    class _Exploding:
        def is_socket(self) -> bool:
            raise OSError("permission denied")

    monkeypatch.setattr(journal, "JOURNALD_SOCKET", _Exploding())
    assert journal.journald_available() is False
