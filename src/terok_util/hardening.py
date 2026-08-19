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
companion filesystem floor.  It uses Landlock to pin the process to its
lane: read and execute the shared runtime, read and write its own data,
touch nothing else.  A bug in a spawned binary then cannot read a secret
outside the lane and cannot write a payload outside the lane.  The kernel
gates unprivileged Landlock on the ``no_new_privs`` that ``harden_self``
sets, so ``confine_filesystem`` runs second.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import resource
import stat
import struct
from contextlib import suppress
from dataclasses import dataclass
from enum import IntFlag
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


#: Landlock syscall numbers.  Linux allocated them on the
#: architecture-generic table, so terok's targets x86-64 and arm64 share
#: them.  An architecture with different numbers fails the ABI probe, and
#: the confinement degrades to a no-op.
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446

#: ``landlock_create_ruleset`` flag asking for the supported ABI version
#: instead of building a ruleset.
_CREATE_RULESET_VERSION = 1 << 0
#: ``enum landlock_rule_type`` — a rule over a path and everything beneath it.
_RULE_PATH_BENEATH = 1
#: Apply a ruleset atomically to every thread in the process (Landlock ABI 8).
_RESTRICT_SELF_TSYNC = 1 << 3

_LANDLOCK_ABI_REFER = 2
_LANDLOCK_ABI_TRUNCATE = 3
_LANDLOCK_ABI_TSYNC = 8

#: Linux exposes one directory entry per live thread here.  Landlock ABI < 8
#: has no atomic process-wide restriction, so this snapshot guards its
#: single-threaded startup contract.
_PROCESS_TASK_DIRECTORY = "/proc/self/task"


class _FilesystemAccess(IntFlag):
    """Filesystem rights from ``include/uapi/linux/landlock.h``."""

    EXECUTE = 1 << 0
    WRITE_FILE = 1 << 1
    READ_FILE = 1 << 2
    READ_DIR = 1 << 3
    REMOVE_DIR = 1 << 4
    REMOVE_FILE = 1 << 5
    MAKE_CHAR = 1 << 6
    MAKE_DIR = 1 << 7
    MAKE_REG = 1 << 8
    MAKE_SOCK = 1 << 9
    MAKE_FIFO = 1 << 10
    MAKE_BLOCK = 1 << 11
    MAKE_SYM = 1 << 12
    REFER = 1 << 13
    TRUNCATE = 1 << 14


_READ_FILE_ACCESS = _FilesystemAccess.EXECUTE | _FilesystemAccess.READ_FILE
_READ_DIRECTORY_ACCESS = _READ_FILE_ACCESS | _FilesystemAccess.READ_DIR
_FILE_OBJECT_ACCESS = (
    _FilesystemAccess.EXECUTE
    | _FilesystemAccess.WRITE_FILE
    | _FilesystemAccess.READ_FILE
    | _FilesystemAccess.TRUNCATE
)
_WRITE_ACCESS_V1 = (
    _FilesystemAccess.WRITE_FILE
    | _FilesystemAccess.REMOVE_DIR
    | _FilesystemAccess.REMOVE_FILE
    | _FilesystemAccess.MAKE_CHAR
    | _FilesystemAccess.MAKE_DIR
    | _FilesystemAccess.MAKE_REG
    | _FilesystemAccess.MAKE_SOCK
    | _FilesystemAccess.MAKE_FIFO
    | _FilesystemAccess.MAKE_BLOCK
    | _FilesystemAccess.MAKE_SYM
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

    ``confined`` is ``True`` only when the complete requested policy covers
    every thread in the process.  An ABI 2 kernel enforces every right it
    supports but cannot deny truncation; it reports ``partially_confined=True``.
    When both fields are ``False``, the process is unchanged.  ``reason``
    always fits a diagnostic log line.
    """

    #: ``True`` when the complete policy covers the whole process.
    confined: bool
    #: One-line explanation, ready for a diagnostic log line.
    reason: str
    #: ``True`` when an ABI 2 kernel enforced every right it supports.
    partially_confined: bool = False


def confine_filesystem(read_exec: Iterable[Path], read_write: Iterable[Path]) -> LandlockReport:
    """Pin the whole process and its descendants to the given filesystem lane.

    After this call the process reads and executes only under *read_exec*.
    It creates, modifies, and removes only under *read_write*.  A directory
    grant covers the directory's whole hierarchy.  A non-directory grant
    covers that exact object — this permits a writable ``/dev/null`` without
    a writable ``/dev``.  Landlock denies every other path, even for reading.
    The kernel gates unprivileged Landlock on ``no_new_privs``, so call
    [`harden_self`][terok_util.hardening.harden_self] first.

    Call this before starting threads on Landlock ABI 1–7.  Those kernels
    restrict only the calling thread, so the call leaves an
    already-multithreaded process unchanged and reports it as unconfined.
    ABI 8 applies the ruleset to all threads atomically.

    Best-effort and irreversible: the call never raises.  A kernel or build
    without Landlock changes nothing and returns ``confined=False``.  ABI 1
    cannot allow cross-directory rename, so it also changes nothing rather
    than break read-write lane semantics.  ABI 2 receives its supported
    subset and reports ``partially_confined=True`` because it cannot deny
    truncation.  The call skips a path that does not exist: a parent grant
    covers its later creation.  When it cannot grant an existing path, it
    installs no ruleset.  Pathname unix sockets are intentionally outside
    this filesystem policy.
    """
    libc = _libc()
    if libc is None:
        return LandlockReport(False, "landlock unavailable (kernel < 5.13 or no syscall)")

    abi = _landlock_abi(libc)
    if abi < 1:
        return LandlockReport(False, "landlock unavailable (kernel < 5.13 or no syscall)")
    if abi < _LANDLOCK_ABI_REFER:
        return LandlockReport(
            False,
            "Landlock ABI 1 cannot allow cross-directory rename/link; filesystem unconfined",
        )

    if scope_failure := _thread_scope_failure(abi):
        return LandlockReport(False, scope_failure)

    read_access, write_access = _access_masks(abi)
    ruleset = _create_ruleset(libc, write_access)
    if ruleset < 0:
        return LandlockReport(False, f"create_ruleset failed (errno {ctypes.get_errno()})")

    try:
        for paths, access in ((read_exec, read_access), (read_write, write_access)):
            for path in paths:
                if failure := _grant_beneath(libc, ruleset, path, access):
                    return LandlockReport(False, failure)

        flags = _RESTRICT_SELF_TSYNC if abi >= _LANDLOCK_ABI_TSYNC else 0
        if libc.syscall(_NR_RESTRICT_SELF, ruleset, flags) != 0:
            return LandlockReport(False, f"restrict_self failed (errno {ctypes.get_errno()})")
    finally:
        with suppress(OSError):
            os.close(ruleset)

    if abi < _LANDLOCK_ABI_TRUNCATE:
        return LandlockReport(
            False,
            f"filesystem partially confined (Landlock ABI {abi} cannot deny truncation)",
            partially_confined=True,
        )
    return LandlockReport(True, f"filesystem confined (Landlock ABI {abi})")


def _landlock_abi(libc: ctypes.CDLL) -> int:
    """Return the kernel's Landlock ABI version, or a negative value when unsupported."""
    return libc.syscall(_NR_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION)


def _create_ruleset(libc: ctypes.CDLL, handled_access: int) -> int:
    """Create a ruleset governing *handled_access*; return its fd or a negative errno."""
    attr = struct.pack(_RULESET_ATTR, handled_access)
    return libc.syscall(_NR_CREATE_RULESET, attr, len(attr), 0)


def _access_masks(abi: int) -> tuple[_FilesystemAccess, _FilesystemAccess]:
    """Build ABI-compatible read-exec and read-write access masks."""
    truncate = _FilesystemAccess.TRUNCATE if abi >= _LANDLOCK_ABI_TRUNCATE else _FilesystemAccess(0)
    refer = _FilesystemAccess.REFER if abi >= _LANDLOCK_ABI_REFER else _FilesystemAccess(0)
    return (
        _READ_DIRECTORY_ACCESS,
        _READ_DIRECTORY_ACCESS | _WRITE_ACCESS_V1 | refer | truncate,
    )


def _process_thread_count() -> int | None:
    """Return a snapshot of the process's thread count, or ``None`` if unknowable."""
    try:
        with os.scandir(_PROCESS_TASK_DIRECTORY) as tasks:
            return sum(1 for _task in tasks)
    except OSError:
        return None


def _thread_scope_failure(abi: int) -> str | None:
    """Explain why *abi* cannot safely restrict this process, if applicable."""
    if abi >= _LANDLOCK_ABI_TSYNC:
        return None
    thread_count = _process_thread_count()
    if thread_count == 1:
        return None
    detail = (
        "thread count unavailable"
        if thread_count is None
        else f"process already has {thread_count} threads"
    )
    return f"Landlock ABI {abi} cannot confine the whole process ({detail})"


def _grant_beneath(
    libc: ctypes.CDLL,
    ruleset: int,
    path: Path,
    access: _FilesystemAccess,
) -> str | None:
    """Add one path rule, returning a diagnostic on failure.

    Deliberately skips a missing path.  Returns every other failure, so the
    caller can abandon the not-yet-enforced ruleset.
    """
    try:
        parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"open grant path {os.fspath(path)!r} failed (errno {error.errno})"

    try:
        allowed = (
            access if stat.S_ISDIR(os.fstat(parent_fd).st_mode) else access & _FILE_OBJECT_ACCESS
        )
        attr = struct.pack(_PATH_BENEATH_ATTR, allowed, parent_fd)
        if libc.syscall(_NR_ADD_RULE, ruleset, _RULE_PATH_BENEATH, attr, 0) != 0:
            return (
                f"add_rule for grant path {os.fspath(path)!r} failed (errno {ctypes.get_errno()})"
            )
    except OSError as error:
        return f"inspect grant path {os.fspath(path)!r} failed (errno {error.errno})"
    finally:
        with suppress(OSError):
            os.close(parent_fd)
    return None


__all__ = ["HardeningReport", "LandlockReport", "confine_filesystem", "harden_self"]
