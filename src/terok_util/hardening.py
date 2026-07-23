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

[`confine_filesystem`][terok_util.hardening.confine_filesystem] is the
companion FS floor: it pins the process to the lane it actually needs
(read+execute the shared runtime, read+write its own data, touch nothing
else) via Landlock, so a bug in a binary it shells out to cannot read a
secret it does not own nor drop a payload outside its lane.  It requires
the ``no_new_privs`` that ``harden_self`` sets, so it runs second.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import resource
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

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


#: Landlock syscall numbers.  They were allocated together on the
#: architecture-generic table, so x86-64 and arm64 — terok's targets — share
#: them; any arch where they differ simply fails the ABI probe and degrades to
#: a no-op.
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446

#: ``landlock_create_ruleset`` flag asking for the supported ABI version
#: instead of building a ruleset.
_CREATE_RULESET_VERSION = 1 << 0
#: ``enum landlock_rule_type`` — a rule over a path and everything beneath it.
_RULE_PATH_BENEATH = 1

#: Read-side filesystem access rights (Landlock ABI 1): open a file for
#: reading, list a directory, execute a file.  Granted on the read-exec lane.
_READ_ACCESS = (1 << 0) | (1 << 2) | (1 << 3)  # EXECUTE | READ_FILE | READ_DIR

#: Write-side filesystem access rights (Landlock ABI 1): every way of creating,
#: changing, or removing a file.  Granted only on the read-write lane, on top
#: of the read rights.
_WRITE_ACCESS = (
    (1 << 1)  # WRITE_FILE
    | (1 << 4)  # REMOVE_DIR
    | (1 << 5)  # REMOVE_FILE
    | (1 << 6)  # MAKE_CHAR
    | (1 << 7)  # MAKE_DIR
    | (1 << 8)  # MAKE_REG
    | (1 << 9)  # MAKE_SOCK
    | (1 << 10)  # MAKE_FIFO
    | (1 << 11)  # MAKE_BLOCK
    | (1 << 12)  # MAKE_SYM
)

#: ``struct`` formats for the two Landlock attribute structs, native byte order
#: and packed (``=`` = standard sizes, no alignment padding), matching the
#: kernel's ``__attribute__((packed))`` uapi layout:
#: ``landlock_ruleset_attr { __u64 handled_access_fs; }`` and
#: ``landlock_path_beneath_attr { __u64 allowed_access; __s32 parent_fd; }``.
_RULESET_ATTR = "=Q"
_PATH_BENEATH_ATTR = "=Qi"


@dataclass(frozen=True)
class LandlockReport:
    """Whether [`confine_filesystem`][terok_util.hardening.confine_filesystem] took hold.

    ``confined`` is ``True`` only when the kernel is now enforcing the
    restriction on this process.  ``reason`` explains a ``False`` — a kernel
    without Landlock (< 5.13) or a build lacking the syscalls degrades to a
    no-op, which the caller may log but must not treat as an error.
    """

    #: ``True`` when access outside the granted lanes is now denied by the kernel.
    confined: bool
    #: One-line explanation, ready for a diagnostic log line.
    reason: str


def confine_filesystem(read_exec: Iterable[Path], read_write: Iterable[Path]) -> LandlockReport:
    """Pin this process and its descendants to the given filesystem lane.

    After this, the process may read and execute only under *read_exec*, and
    additionally create/modify/remove only under *read_write* (each grant
    covers a directory and everything beneath it).  Every other path is denied
    even for reading.  Requires ``no_new_privs`` already set — the kernel gates
    unprivileged Landlock on it — so call
    [`harden_self`][terok_util.hardening.harden_self] first.

    Best-effort and irreversible: never raises; a kernel or build without
    Landlock returns ``confined=False`` and changes nothing.  A path that does
    not exist is skipped (there is nothing to reach until it is created, and a
    parent grant covers that creation).  Connecting to a unix socket is not a
    filesystem access Landlock gates, so IPC to sockets outside the lane keeps
    working.
    """
    libc = _libc()
    if libc is None or _landlock_abi(libc) < 1:
        return LandlockReport(False, "landlock unavailable (kernel < 5.13 or no syscall)")
    ruleset = _create_ruleset(libc, _READ_ACCESS | _WRITE_ACCESS)
    if ruleset < 0:
        return LandlockReport(False, f"create_ruleset failed (errno {ctypes.get_errno()})")
    try:
        for path in read_exec:
            _grant_beneath(libc, ruleset, path, _READ_ACCESS)
        for path in read_write:
            _grant_beneath(libc, ruleset, path, _READ_ACCESS | _WRITE_ACCESS)
        if libc.syscall(_NR_RESTRICT_SELF, ruleset, 0) != 0:
            return LandlockReport(False, f"restrict_self failed (errno {ctypes.get_errno()})")
    finally:
        os.close(ruleset)
    return LandlockReport(True, "filesystem confined")


def _landlock_abi(libc: ctypes.CDLL) -> int:
    """Return the kernel's Landlock ABI version, or a negative value when unsupported."""
    return libc.syscall(_NR_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION)


def _create_ruleset(libc: ctypes.CDLL, handled_access: int) -> int:
    """Create a ruleset governing *handled_access*; return its fd or a negative errno."""
    attr = struct.pack(_RULESET_ATTR, handled_access)
    return libc.syscall(_NR_CREATE_RULESET, attr, len(attr), 0)


def _grant_beneath(libc: ctypes.CDLL, ruleset: int, path: Path, access: int) -> None:
    """Grant *access* on *path* and everything beneath it (best-effort)."""
    try:
        parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return
    try:
        attr = struct.pack(_PATH_BENEATH_ATTR, access, parent_fd)
        libc.syscall(_NR_ADD_RULE, ruleset, _RULE_PATH_BENEATH, attr, 0)
    finally:
        os.close(parent_fd)


__all__ = ["HardeningReport", "LandlockReport", "confine_filesystem", "harden_self"]
