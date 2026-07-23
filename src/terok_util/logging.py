# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""File-backed best-effort logger — never raises, never disrupts callers.

[`BestEffortLogger`][terok_util.logging.BestEffortLogger] binds a
destination path on construction so any subsystem in any terok-*
package can spin up its own log file with one shared, audited idiom.

Writes soft-fail: a logging error must never take down the caller, so
every write is wrapped and swallowed.  Operator-facing stderr output
is run through [`sanitize_tty`][terok_util.security.sanitize_tty] so
attacker-influenced strings can't smuggle terminal escapes (CWE-150);
the file-side write keeps the original bytes for forensic review.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .journal import (
    PRIORITY_ERR,
    PRIORITY_INFO,
    PRIORITY_WARNING,
    JournalWriter,
    journald_available,
)
from .security import sanitize_tty

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""Stderr-fallback line format when journald isn't the sink."""

_HANDLER_TAG = "_terok_unified_handler"
"""Marker attribute so :func:`configure` can replace its own handler idempotently."""

_PRIORITY_BY_LEVEL: dict[int, int] = {
    logging.CRITICAL: 2,
    logging.ERROR: PRIORITY_ERR,
    logging.WARNING: PRIORITY_WARNING,
    logging.INFO: PRIORITY_INFO,
    logging.DEBUG: 7,
}
"""stdlib log level → syslog priority for journald entries."""


class BestEffortLogger:
    """Append timestamped lines to a state-file log; soft-fail on any error.

    The destination is supplied as a *callable* rather than an eager
    ``Path`` so XDG / env-var overrides applied between construction
    and write time still take effect.

    Args:
        log_path_fn: Zero-arg callable returning the destination path.
            Called on every write so tests overriding ``HOME`` /
            ``XDG_STATE_HOME`` see their override applied even when the
            logger was constructed under the previous environment.
    """

    def __init__(self, log_path_fn: Callable[[], Path]) -> None:
        """Bind the destination resolver."""
        self._log_path_fn = log_path_fn

    def log(self, message: str, *, level: str = "DEBUG") -> None:
        """Append one ``[timestamp] LEVEL: message`` line.  Never raises.

        File creation goes through ``os.open`` with mode ``0o600`` so the
        log lands owner-only by construction — atomically, without
        relying on the process umask.  The mode bits are honoured by
        the kernel only on creation; existing files keep whatever perms
        they were created with.
        """
        try:
            log_path = self._log_path_fn()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            fd = os.open(
                log_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {level}: {message}\n")
        except Exception:  # nosec B110 — intentionally silent
            pass

    def debug(self, message: str) -> None:
        """Append a DEBUG-level line."""
        self.log(message, level="DEBUG")

    def warning(self, message: str) -> None:
        """Append a WARNING-level line."""
        self.log(message, level="WARNING")

    def warn_user(self, component: str, message: str) -> None:
        """Print a structured warning to stderr and append it to the log file.

        Stderr output is run through
        [`sanitize_tty`][terok_util.security.sanitize_tty] so attacker
        bytes in *component* / *message* (e.g. originating from foreign
        config files) can't smuggle terminal escapes into the operator's
        terminal.  The file-side write is unsanitised so the log keeps
        the original bytes for forensic review.
        """
        try:
            print(
                f"Warning [{sanitize_tty(component)}]: {sanitize_tty(message)}",
                file=sys.stderr,
            )
        except Exception:  # nosec B110 — intentionally silent
            pass
        self.warning(f"[{component}] {message}")


class _JournalHandler(logging.Handler):
    """Route stdlib logging records to journald over the native protocol.

    The record's message (with any exception traceback the default
    formatter appends) becomes the ``MESSAGE`` field; the level maps to a
    syslog ``PRIORITY``; and the logger name / source location ride along
    as ``LOGGER`` / ``CODE_*`` fields so ``journalctl`` can filter and
    show provenance.  Emission is best-effort — a journald hiccup routes
    through [`handleError`][logging.Handler.handleError], never up to the
    caller.
    """

    def __init__(self, identifier: str) -> None:
        """Open a [`JournalWriter`][terok_util.journal.JournalWriter] for *identifier*."""
        super().__init__()
        self._writer = JournalWriter(identifier)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        """Send one record to journald as a structured entry."""
        try:
            self._writer.send(
                self.format(record),
                priority=_PRIORITY_BY_LEVEL.get(record.levelno, PRIORITY_INFO),
                LOGGER=record.name,
                CODE_FILE=record.pathname,
                CODE_LINE=str(record.lineno),
                CODE_FUNC=record.funcName or "",
            )
        except Exception:  # noqa: BLE001 — logging must never raise into the caller
            self.handleError(record)

    def close(self) -> None:
        """Close the journal socket, then the handler."""
        self._writer.close()
        super().close()


def _stderr_handler(level: int, fmt: str, stream: TextIO | None) -> logging.Handler:
    """Build a tagged stderr ``StreamHandler`` at *level* with *fmt*."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))
    handler.setLevel(level)
    setattr(handler, _HANDLER_TAG, True)
    return handler


def configure(
    identifier: str,
    *,
    level: int = logging.INFO,
    fmt: str = _DEFAULT_FORMAT,
    stream: TextIO | None = None,
    stderr: bool = False,
) -> list[logging.Handler]:
    """Install the unified log handler(s) on the root logger — the one call a package makes.

    When journald is present the root logger's records go to it (tagged
    ``SYSLOG_IDENTIFIER=identifier``); otherwise they fall back to a
    stderr [`StreamHandler`][logging.StreamHandler] formatted with *fmt*.
    Containers have no journal socket, so an in-container daemon always
    takes the stderr branch — preserving the pattern where a wrapper
    redirects that stderr to a file.

    Set *stderr* when the process's stderr is deliberately consumed by a
    parent (a launched daemon whose logs the launcher reads): a stderr
    handler is then installed **in addition** to journald, so the parent's
    pipe keeps receiving records even on a journald host.

    Idempotent by construction: handlers previously installed by
    ``configure`` are removed first, so re-invoking it never stacks
    duplicates.  Every module that already does
    ``logging.getLogger(__name__)`` is captured with no call-site change,
    because all such loggers propagate to the root.

    Args:
        identifier: ``SYSLOG_IDENTIFIER`` for journald entries and the
            audit name for the process (e.g. ``"terok-shield"``).
        level: Root log level.
        fmt: ``logging.Formatter`` string for the stderr handler.
        stream: Stderr stream to use (defaults to :data:`sys.stderr`).
        stderr: Also emit to stderr even when journald is the primary sink
            — for daemons whose stderr a parent process consumes.

    Returns:
        The installed handler(s), primary first (for tests / re-wiring).
    """
    root = logging.getLogger()
    root.setLevel(level)
    for stale in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(stale)
        stale.close()

    handlers: list[logging.Handler] = []
    if journald_available():
        primary = _JournalHandler(identifier)
        primary.setLevel(level)
        setattr(primary, _HANDLER_TAG, True)
        handlers.append(primary)
        if stderr:
            handlers.append(_stderr_handler(level, fmt, stream))
    else:
        handlers.append(_stderr_handler(level, fmt, stream))

    for handler in handlers:
        root.addHandler(handler)
    return handlers


__all__ = ["BestEffortLogger", "configure"]
