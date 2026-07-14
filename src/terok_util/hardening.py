# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Process self-hardening — shrink what a leaked address space can reveal.

A process that holds secret material (a vault DB session key, an SSH
private key) wants four cheap kernel-level guarantees the moment it
starts, before it opens anything sensitive:

* **No ptrace / no debugger attach** — ``prctl(PR_SET_DUMPABLE, 0)``
  clears the dumpable flag, so another process in the same user (a
  compromised sibling) cannot ``ptrace`` the address space and cannot
  read ``/proc/<pid>/mem``.  It also stops the kernel writing a core
  dump for this process.
* **No core dumps** — ``setrlimit(RLIMIT_CORE, 0)`` belt-and-braces the
  dumpable clear: even a SIGSEGV can't spill the heap (keys included)
  to a file on disk.
* **No swap-out** — ``mlockall(MCL_CURRENT | MCL_FUTURE)`` pins the
  pages into RAM so secret bytes never land in the swap file where they
  outlive the process.  This one is best-effort: it needs
  ``CAP_IPC_LOCK`` or a generous ``RLIMIT_MEMLOCK`` and legitimately
  fails in a locked-down rootless container — a failure is reported,
  never raised.
* **No privilege gain on exec** — ``prctl(PR_SET_NO_NEW_PRIVS, 1)`` bars
  this process *and every descendant* from ever gaining privilege through
  a setuid/setgid bit or file capabilities on ``exec``.  A child that is
  compromised while shelling out (the gate service runs ``git``) cannot
  re-``exec`` a setuid helper to climb out.  It also unlocks installing an
  unprivileged ``seccomp`` filter, which the kernel permits only once
  no-new-privs is set.  Unlike the dumpable clear, it never impedes a
  debugger *attaching*, so it holds even in debug mode.

[`harden_self`][terok_util.hardening.harden_self] applies all four to
the current process and returns a
[`HardeningReport`][terok_util.hardening.HardeningReport] of what took —
the floor every isolated child process (terok-sandbox's split supervisor
children) applies at start-up.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import resource
from dataclasses import dataclass

#: ``prctl`` option number for the dumpable flag (``linux/prctl.h``).
_PR_SET_DUMPABLE = 4

#: ``prctl`` option number for the no-new-privileges flag (``linux/prctl.h``);
#: once set to 1 it can never be cleared, and it is inherited across
#: ``fork``/``exec``.
_PR_SET_NO_NEW_PRIVS = 38

#: ``mlockall`` flags (``bits/mman-linux.h``): lock resident pages now
#: and every page mapped hereafter.
_MCL_CURRENT = 1
_MCL_FUTURE = 2


@dataclass(frozen=True)
class HardeningReport:
    """What [`harden_self`][terok_util.hardening.harden_self] managed to apply.

    Each field is ``True`` only when the corresponding guarantee is in
    force for the current process.  ``no_dump``, ``no_core``, and
    ``no_new_privs`` are expected to succeed; ``memory_locked`` routinely
    comes back ``False`` in a rootless container without ``CAP_IPC_LOCK``
    and that is not an error — the caller decides whether to log it.
    """

    #: ``prctl(PR_SET_DUMPABLE, 0)`` succeeded — no ptrace, no core dump.
    no_dump: bool
    #: ``RLIMIT_CORE`` is pinned to zero.
    no_core: bool
    #: ``mlockall`` succeeded — no page of this process can swap out.
    memory_locked: bool
    #: ``prctl(PR_SET_NO_NEW_PRIVS, 1)`` succeeded — no setuid/file-cap
    #: privilege gain on ``exec``, for this process and its descendants.
    no_new_privs: bool

    @property
    def fully_hardened(self) -> bool:
        """``True`` when all four guarantees are in force."""
        return self.no_dump and self.no_core and self.memory_locked and self.no_new_privs


def harden_self(*, allow_debugger: bool = False) -> HardeningReport:
    """Apply the process-hardening floor to the current process.

    Idempotent and side-effecting: clears the dumpable flag, zeroes the
    core-dump limit, and locks memory — each independently, so a failure
    of one (typically ``mlockall`` for lack of privilege) still lets the
    others take.  Never raises; the returned
    [`HardeningReport`][terok_util.hardening.HardeningReport] says what
    held.

    Call this as early as possible in a process that will hold secret
    material — before opening the credential store or binding a socket —
    so the sensitive bytes are only ever mapped under the guarantees.

    *allow_debugger* leaves the dumpable flag set so a debugger, ``py-spy``,
    or ``strace`` can attach — the escape hatch for running a task in debug
    mode.  It trades away only the no-ptrace guarantee (``no_dump`` reports
    ``False``); the core-dump, swap-out, and no-new-privileges guarantees
    still apply (the last never impedes a debugger *attaching*).
    """
    libc = _libc()
    return HardeningReport(
        no_dump=False if allow_debugger else _clear_dumpable(libc),
        no_core=_zero_core_limit(),
        memory_locked=_lock_memory(libc),
        no_new_privs=_set_no_new_privs(libc),
    )


def _libc() -> ctypes.CDLL | None:
    """Return a handle on libc for the raw syscalls, or ``None`` if absent.

    ``find_library("c")`` frequently comes up empty on musl (Alpine),
    where the glibc-specific ``libc.so.6`` name also won't load — so fall
    through to ``ctypes.CDLL(None)``, the process's own already-linked
    libc, which exposes ``prctl`` / ``mlockall`` on glibc and musl alike
    without guessing an architecture-specific SONAME.  Returns ``None``
    only if every candidate fails to load.
    """
    for name in (ctypes.util.find_library("c"), None):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    return None


def _clear_dumpable(libc: ctypes.CDLL | None) -> bool:
    """``prctl(PR_SET_DUMPABLE, 0)`` — return ``True`` on success."""
    if libc is None:
        return False
    return libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0


def _set_no_new_privs(libc: ctypes.CDLL | None) -> bool:
    """``prctl(PR_SET_NO_NEW_PRIVS, 1)`` — return ``True`` on success.

    Fails with ``EINVAL`` (reported as ``False``, never raised) on a
    kernel older than 3.5 that predates the flag.  Once it takes it is
    irreversible and inherited by every descendant.
    """
    if libc is None:
        return False
    return libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0


def _zero_core_limit() -> bool:
    """Pin ``RLIMIT_CORE`` to ``(0, 0)`` — return ``True`` on success."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        return False
    return True


def _lock_memory(libc: ctypes.CDLL | None) -> bool:
    """``mlockall(MCL_CURRENT | MCL_FUTURE)`` — return ``True`` on success.

    Best-effort: returns ``False`` (never raises) when the process lacks
    the privilege to lock memory, which is the common rootless-container
    case.
    """
    if libc is None:
        return False
    return libc.mlockall(_MCL_CURRENT | _MCL_FUTURE) == 0


__all__ = ["HardeningReport", "harden_self"]
