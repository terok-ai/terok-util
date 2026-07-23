# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free writer for the systemd journal's native datagram protocol.

Every terok-* package that wants its logs to reach journald routes through
[`JournalWriter`][terok_util.journal.JournalWriter].  The wire format is
implemented directly against ``/run/systemd/journal/socket`` — **no**
``systemd-python`` / ``libsystemd`` binding — because the fleet must run
unchanged on non-systemd inits (OpenRC and friends), where
[`journald_available`][terok_util.journal.journald_available] simply reports
``False`` and callers fall back to a file.

The socket's presence is the systemd-is-here probe: it is precise where a
"which init am I under" guess is not — a container on a systemd host often
has no journal socket, and that case must degrade to a file too.

Should a hard ``systemd-python`` dependency ever become acceptable on a
subset of hosts, a binding-backed writer can be slotted in behind this same
class surface without touching a single caller — the point of keeping the
protocol here, behind one seam.
"""

from __future__ import annotations

import socket
import struct
from pathlib import Path

JOURNALD_SOCKET = Path("/run/systemd/journal/socket")
"""Local journald datagram socket; its presence is the systemd-is-here probe."""

PRIORITY_INFO = 6
"""syslog ``info`` priority — the default for a plain entry."""

PRIORITY_WARNING = 4
"""syslog ``warning`` priority."""

PRIORITY_ERR = 3
"""syslog ``err`` priority."""


def journald_available() -> bool:
    """Return True when a local journald datagram socket is accepting.

    A plain ``is_socket`` probe: on non-systemd hosts (or containers with no
    forwarded journal) the path is absent and callers route to a file
    instead.  Never raises — an unreadable path is reported as unavailable.
    """
    try:
        return JOURNALD_SOCKET.is_socket()
    except OSError:
        return False


def encode_field(name: str, value: bytes) -> bytes:
    """Encode one journal field in the native export format.

    Newline-free values take the compact ``NAME=value\\n`` form; a value
    containing a newline switches to the ``NAME\\n<64-bit LE length><value>\\n``
    binary form journald mandates for multi-line data.
    """
    if b"\n" in value:
        return name.encode() + b"\n" + struct.pack("<Q", len(value)) + value + b"\n"
    return name.encode() + b"=" + value + b"\n"


def encode_fields(fields: dict[str, str]) -> bytes:
    """Concatenate encoded journal fields into one datagram body."""
    return b"".join(encode_field(k, v.encode()) for k, v in fields.items())


class JournalWriter:
    """Send structured entries to journald over the native datagram socket.

    The identifier (``SYSLOG_IDENTIFIER``) and any *static_fields* are
    encoded once at construction and prefixed onto every entry; per-entry
    ``MESSAGE`` / ``PRIORITY`` / extra fields are appended on
    [`send`][terok_util.journal.JournalWriter.send].  Every send is
    best-effort: a full datagram buffer or a vanished socket is swallowed so
    logging never propagates a failure into the caller.

    Args:
        identifier: ``SYSLOG_IDENTIFIER`` stamped on every entry
            (``journalctl -t <identifier>``).
        static_fields: Extra fields repeated on every entry (e.g.
            ``TEROK_PROJECT``); values are plain strings.
    """

    def __init__(self, identifier: str, *, static_fields: dict[str, str] | None = None) -> None:
        """Connect the datagram socket and precompute the static field block."""
        self._prefix = encode_fields({"SYSLOG_IDENTIFIER": identifier, **(static_fields or {})})
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.connect(str(JOURNALD_SOCKET))

    def send(self, message: str, *, priority: int = PRIORITY_INFO, **fields: str) -> None:
        """Emit one journal entry (best-effort; never raises).

        Args:
            message: The ``MESSAGE`` field text.
            priority: syslog priority (0-7); defaults to ``info``.
            **fields: Extra journal fields for this entry (journald upper-cases
                field names by convention, e.g. ``CODE_FILE``, ``TEROK_TASK``).
        """
        datagram = (
            self._prefix
            + encode_field("PRIORITY", str(priority).encode())
            + encode_fields(fields)
            + encode_field("MESSAGE", message.encode())
        )
        try:
            self._sock.send(datagram)
        except OSError:
            pass  # nosec B110 — logging is best-effort, never fatal to the caller

    def close(self) -> None:
        """Close the underlying datagram socket."""
        self._sock.close()


__all__ = [
    "JOURNALD_SOCKET",
    "PRIORITY_ERR",
    "PRIORITY_INFO",
    "PRIORITY_WARNING",
    "JournalWriter",
    "encode_field",
    "encode_fields",
    "journald_available",
]
