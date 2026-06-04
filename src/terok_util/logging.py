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

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .security import sanitize_tty


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


__all__ = ["BestEffortLogger"]
