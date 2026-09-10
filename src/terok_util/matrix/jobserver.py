# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""A GNU make jobserver client: one ``-j`` shared with whoever started this run.

``make -j12`` hands its children a jobserver in ``MAKEFLAGS``: a pipe
pre-loaded with one single-byte token per job slot beyond the ones its
clients already hold.  Every client owns one implicit slot; each further
concurrent job reads a token first and writes the same byte back when it
ends.  GNU make, ninja and cargo speak this protocol, and so does the
superbuild's matrix TUI, so the matrix runs of several repos can share one
worker cap instead of each taking ``--jobs`` for itself.

Both of make's auth forms are understood: ``fifo:PATH`` (make 4.4 and later)
and ``R,W`` file descriptors (make 4.3, which passes them only to recipes
marked ``+``).  A token lost to a killed run only shrinks the pool; the
implicit slot keeps every run able to finish.
"""

from __future__ import annotations

import os
import re
import select
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

# The jobserver make advertises in MAKEFLAGS.
_AUTH = re.compile(r"--jobserver-auth=(?:fifo:(?P<fifo>\S+)|(?P<read>-?\d+),(?P<write>-?\d+))")

# How often a slot waiting for a token looks at the implicit slot again; an
# arriving token wakes it at once.
_RECHECK_SECONDS = 1.0


class Jobserver:
    """Job slots shared through a GNU make jobserver."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        """Hold the jobserver's two ends; *read_fd* is this process's own, non-blocking."""
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._implicit = threading.Lock()

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] = os.environ) -> Jobserver | None:
        """Join the jobserver ``MAKEFLAGS`` names, or ``None`` when there is none to join.

        make may repeat the option; the last one counts.  The read end is
        opened afresh and non-blocking, so a wait for a token can be broken
        off; make's own descriptors keep their blocking mode, as make asks.
        A jobserver this process cannot reach is reported on stderr, and the
        run goes on without it.
        """
        matches = list(_AUTH.finditer(environ.get("MAKEFLAGS", "")))
        if not matches:
            return None
        auth = matches[-1]
        if fifo := auth["fifo"]:
            try:
                fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            except OSError as error:
                _unreachable(f"{fifo} cannot be opened ({error.strerror})")
                return None
            return cls(fd, fd)
        read_fd, write_fd = int(auth["read"]), int(auth["write"])
        if read_fd < 0 or write_fd < 0:
            return None  # make disabled the jobserver for this process
        try:
            os.fstat(read_fd)
            os.fstat(write_fd)
        except OSError:
            _unreachable("its descriptors are closed (mark the make recipe with +)")
            return None
        try:
            own_read_fd = os.open(f"/proc/self/fd/{read_fd}", os.O_RDONLY | os.O_NONBLOCK)
        except OSError as error:
            _unreachable(f"its read end cannot be opened again ({error.strerror})")
            return None
        return cls(own_read_fd, write_fd)

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one job slot: the implicit one when it is free, else a token from the server.

        A slot that waits for a token also takes the implicit slot when that
        frees.  All of a run's slots may be waiting at once, and without this
        none of them would come back for the implicit one.
        """
        while True:
            if self._implicit.acquire(blocking=False):
                try:
                    yield
                finally:
                    self._implicit.release()
                return
            if token := self._take_token():
                try:
                    yield
                finally:
                    os.write(self._write_fd, token)
                return
            select.select([self._read_fd], [], [], _RECHECK_SECONDS)

    def _take_token(self) -> bytes:
        """One token from the server, or nothing when none is waiting."""
        try:
            return os.read(self._read_fd, 1)
        except BlockingIOError:
            return b""


def _unreachable(why: str) -> None:
    """Say that the advertised jobserver is out of reach; the run goes on without it."""
    print(f"WARNING: MAKEFLAGS names a jobserver, but {why}; running without it", file=sys.stderr)
