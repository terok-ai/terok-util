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

# A waiting slot wakes at once when a token arrives or when this process frees
# its implicit slot; this timeout is only the safety net for a missed wake.
_RECHECK_SECONDS = 1.0


class Jobserver:
    """Job slots shared through a GNU make jobserver.

    Attributes:
        inherited_fds: make 4.3's pipe descriptors, which a child that joins
            too must inherit (``subprocess``'s ``pass_fds``); empty for a
            named pipe, which children find by its path in ``MAKEFLAGS``.
    """

    def __init__(self, read_fd: int, write_fd: int, inherited_fds: tuple[int, ...] = ()) -> None:
        """Hold the jobserver's two ends; *read_fd* is this process's own, non-blocking."""
        self._read_fd = read_fd
        self._write_fd = write_fd
        self.inherited_fds = inherited_fds
        self._implicit = threading.Lock()
        self._closed = threading.Event()
        # A self-pipe: freeing the implicit slot, or closing, wakes the waiters.
        self._wake_read, self._wake_write = os.pipe()
        os.set_blocking(self._wake_read, False)
        os.set_blocking(self._wake_write, False)

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
        return cls(own_read_fd, write_fd, inherited_fds=(read_fd, write_fd))

    @contextmanager
    def slot(self, *, implicit: bool = True) -> Iterator[None]:
        """Hold one job slot: the implicit one when it is free, else a token from the server.

        *implicit* false takes tokens only.  It is for a launcher whose own slot
        is spoken for, such as a panel that starts other clients and does no
        work of its own.

        A slot that waits for a token also takes the implicit slot when that
        frees.  All of a run's slots may be waiting at once, and without this
        none of them would come back for the implicit one.

        Raises:
            InterruptedError: The jobserver was closed before a slot was free.
        """
        while True:
            if self._closed.is_set():
                raise InterruptedError("the jobserver closed before a job slot was free")
            if implicit and self._implicit.acquire(blocking=False):
                try:
                    yield
                finally:
                    self._implicit.release()
                    self._wake()
                return
            if token := self._take_token():
                try:
                    yield
                finally:
                    os.write(self._write_fd, token)
                return
            ready, _, _ = select.select([self._read_fd, self._wake_read], [], [], _RECHECK_SECONDS)
            if self._wake_read in ready:
                self._drain_wakes()

    def close(self) -> None:
        """Hand out no more slots: waiting ones give up, held ones still give their tokens back."""
        self._closed.set()
        self._wake()

    def _wake(self) -> None:
        """Wake the slots waiting in ``select``; a wake already pending is enough."""
        try:
            os.write(self._wake_write, b"!")
        except BlockingIOError:
            pass

    def _drain_wakes(self) -> None:
        """Empty the self-pipe, so the next ``select`` waits for a new wake."""
        try:
            while os.read(self._wake_read, 64):
                pass
        except BlockingIOError:
            pass

    def _take_token(self) -> bytes:
        """One token from the server, or nothing when none is waiting."""
        try:
            return os.read(self._read_fd, 1)
        except BlockingIOError:
            return b""


def _unreachable(why: str) -> None:
    """Say that the advertised jobserver is out of reach; the run goes on without it."""
    print(f"WARNING: MAKEFLAGS names a jobserver, but {why}; running without it", file=sys.stderr)
