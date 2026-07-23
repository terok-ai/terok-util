# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tee a block's stdout/stderr to a durable sink, live terminal untouched.

[`tee_output`][terok_util.output_capture.tee_output] wraps an operation
(an image build, a task launch — anything that spawns subprocesses) and
copies every byte those subprocesses write to stdout/stderr into a durable
sink, without disturbing the live terminal:

* a pseudo-terminal fronts the wrapped block when stdout is a TTY, so a
  child like ``podman`` still sees ``isatty(1) == True`` and keeps its
  colour + progress output — the forwarded bytes are byte-for-byte what the
  operator would have seen;
* a plain pipe is used when stdout is not a TTY (redirected / CI), matching
  ordinary non-interactive behaviour while still capturing the stream.

The sink follows the same journald-else-file rule as the rest of terok
logging: captured output goes to journald (line-buffered into structured
entries via [`JournalWriter`][terok_util.journal.JournalWriter]) when its
socket is present, else to a caller-provided file resolved lazily.  The
module stays generic — callers pass the journald *fields* and a
*file_path_fn*; nothing here knows a project or a state-dir layout.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol

from .journal import JournalWriter, journald_available

_READ_CHUNK = 65536
"""Bytes pulled per pump-thread read from the capture fd."""

_LOG_FILE_MODE = 0o600
"""Owner-only permissions for a bespoke capture-log file."""

_SIGWINCH = getattr(signal, "SIGWINCH", None)
"""Terminal-resize signal, or ``None`` on platforms without it."""


class _StreamSink(Protocol):
    """A durable destination for a captured byte stream."""

    def write(self, data: bytes) -> None:
        """Absorb a chunk of captured bytes (never raises to the pump)."""

    def close(self) -> None:
        """Flush any buffered tail and release resources."""

    def hint(self) -> str:
        """One-line, operator-facing pointer to where the output landed."""


class _FileStreamSink:
    """Append a captured byte stream verbatim to an owner-only log file."""

    def __init__(self, path: Path) -> None:
        """Create (truncating) the log file at *path*."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _LOG_FILE_MODE)

    def write(self, data: bytes) -> None:
        """Write *data* to the log file, looping over short writes."""
        _write_all(self._fd, data)

    def close(self) -> None:
        """Close the underlying file descriptor."""
        os.close(self._fd)

    def hint(self) -> str:
        """Point the operator at the saved log file."""
        return f"output saved to {self._path}"


class _JournalStreamSink:
    """Line-buffer a captured byte stream into structured journald entries.

    Splits on ``\\n`` and emits one entry per line; carriage-return
    progress redraws collapse to their final rendered segment so an
    in-place progress bar becomes a single tidy line rather than a wall of
    overwrites.
    """

    def __init__(self, identifier: str, fields: dict[str, str]) -> None:
        """Open a journal writer stamped with *identifier* and static *fields*."""
        self._writer = JournalWriter(identifier, static_fields=fields)
        self._identifier = identifier
        self._fields = fields
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        """Buffer *data* and flush every complete line to the journal."""
        self._buf += data
        while (nl := self._buf.find(b"\n")) != -1:
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            self._emit(line)

    def close(self) -> None:
        """Emit any buffered tail (no trailing newline) and close the writer."""
        if self._buf:
            self._emit(bytes(self._buf))
            self._buf.clear()
        self._writer.close()

    def hint(self) -> str:
        """Point the operator at the matching ``journalctl`` query."""
        query = f"journalctl -t {self._identifier}"
        for key in ("TEROK_KIND", "TEROK_TASK"):
            if key in self._fields:
                query += f" {key}={self._fields[key]}"
        return f"output logged to journald — {query}"

    def _emit(self, line: bytes) -> None:
        """Send one line as a journal entry (drops CR-progress redraws)."""
        if b"\r" in line:
            line = line.rsplit(b"\r", 1)[-1]
        # Encode via the writer's MESSAGE path by decoding leniently: captured
        # bytes may not be valid UTF-8, so replace undecodable bytes rather
        # than dropping the line.
        self._writer.send(line.decode("utf-8", "replace"))


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, looping over short writes."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _copy_winsize(src_fd: int, dst_fd: int) -> None:  # pragma: no cover — real-tty only
    """Copy the terminal window size from *src_fd* onto *dst_fd* (best-effort)."""
    import fcntl
    import termios

    with contextlib.suppress(OSError):
        packed = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, packed)


@contextlib.contextmanager
def _capture(sink: _StreamSink) -> Iterator[None]:
    """Redirect fd 1/2 through a pty (or pipe) and pump every byte to *sink*.

    A reader thread copies bytes to both the real terminal (so the live
    stream is unchanged) and *sink*.  A pty is used when the real stdout is
    a TTY so downstream ``isatty`` checks still pass; a plain pipe otherwise.
    """
    import threading

    sys.stdout.flush()
    sys.stderr.flush()
    saved_out, saved_err = os.dup(1), os.dup(2)
    is_tty = os.isatty(saved_out)
    if is_tty:  # pragma: no cover — real-tty only
        master, slave = os.openpty()
        _copy_winsize(saved_out, slave)
    else:
        master, slave = os.pipe()

    def _pump() -> None:
        """Forward capture-fd bytes to the terminal and the sink until EOF."""
        while True:
            try:
                data = os.read(master, _READ_CHUNK)
            except OSError:  # pragma: no cover — pty EIO on slave close
                break  # EIO once the slave side is fully closed
            if not data:
                break
            _write_all(saved_out, data)
            with contextlib.suppress(Exception):
                sink.write(data)  # a failing sink must never break live output

    reader = threading.Thread(target=_pump, name="terok-output-tee", daemon=True)
    prev_winch = None
    try:
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        reader.start()
        if is_tty and _SIGWINCH is not None:  # pragma: no cover — real-tty only
            with contextlib.suppress(ValueError):  # not in the main thread
                prev_winch = signal.getsignal(_SIGWINCH)
                signal.signal(_SIGWINCH, lambda *_: _copy_winsize(saved_out, slave))
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        if prev_winch is not None and _SIGWINCH is not None:  # pragma: no cover — real-tty only
            signal.signal(_SIGWINCH, prev_winch)
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(slave)  # drop the last writer so the pump reads EOF
        reader.join()
        for fd in (master, saved_out, saved_err):
            os.close(fd)


@contextlib.contextmanager
def tee_output(
    identifier: str,
    *,
    fields: dict[str, str] | None = None,
    file_path_fn: Callable[[], Path] | None = None,
) -> Iterator[None]:
    """Capture the wrapped operation's output to journald or a log file.

    Picks a journald sink when its socket is present, else a file at
    ``file_path_fn()`` (resolved lazily, so no directory is created on the
    journald path).  With neither available the block still runs, un-teed —
    durability is a bonus that never blocks the operation.

    Args:
        identifier: ``SYSLOG_IDENTIFIER`` for journald entries.
        fields: Static journald fields (e.g. ``{"TEROK_KIND": "build"}``);
            also drive the ``journalctl`` hint.
        file_path_fn: Zero-arg callable returning the fallback log path.
    """
    fields = fields or {}
    sink: _StreamSink
    if journald_available():
        sink = _JournalStreamSink(identifier, fields)
    elif file_path_fn is not None:
        try:
            sink = _FileStreamSink(file_path_fn())
        except OSError:
            yield
            return
    else:
        yield
        return
    try:
        with _capture(sink):
            yield
    finally:
        with contextlib.suppress(Exception):
            sink.close()
        print(f"↳ {sink.hint()}", file=sys.stderr)


__all__ = ["tee_output"]
