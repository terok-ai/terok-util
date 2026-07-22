# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Filesystem write-confinement via Landlock — deny a process any write outside its lane.

A process that shells out to third-party binaries (the supervisor's gate
child runs ``git``, its vault child runs ``systemd-creds``) wants one cheap
kernel guarantee: whatever it runs can create, modify, or delete files only
under a handful of directories it legitimately owns — nowhere else.  Then a
bug in one of those binaries turned into code execution still cannot drop a
payload in ``/usr``, tamper with a sibling service's data, or write a
persistence hook; the blast radius is the process's own lane.

[`restrict_writes`][terok_util.landlock.restrict_writes] applies that to the
current process and returns a
[`LandlockReport`][terok_util.landlock.LandlockReport] of whether the kernel
took it.  Reads and executes are deliberately left unrestricted — the
process keeps its whole runtime and can still run what it needs; only the
write side is confined.  It is the second self-applied floor (after
[`harden_self`][terok_util.hardening.harden_self]) every isolated child
process installs at start-up, and it needs ``no_new_privs`` from that floor
already in force.
"""

from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

#: Landlock syscall numbers.  They were allocated together on the
#: architecture-generic table, so x86-64 and arm64 — terok's targets —
#: share them; any arch where they differ simply fails the ABI probe and
#: degrades to a no-op.
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446

#: ``landlock_create_ruleset`` flag that asks for the supported ABI version
#: instead of building a ruleset.
_CREATE_RULESET_VERSION = 1 << 0
#: ``enum landlock_rule_type`` — a rule over a path and everything beneath it.
_RULE_PATH_BENEATH = 1

#: The write-family filesystem access rights (Landlock ABI 1).  Read
#: (``READ_FILE``/``READ_DIR``) and ``EXECUTE`` are intentionally excluded:
#: a right the ruleset does not *handle* is one Landlock never restricts, so
#: leaving them out keeps read and exec fully open while every way of
#: creating, changing, or removing a file is confined to the granted paths.
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


#: ``struct`` formats for the two Landlock attribute structs, native byte
#: order and packed (``=`` = standard sizes, no alignment padding), matching
#: the kernel's ``__attribute__((packed))`` uapi layout:
#: ``landlock_ruleset_attr { __u64 handled_access_fs; }`` and
#: ``landlock_path_beneath_attr { __u64 allowed_access; __s32 parent_fd; }``.
_RULESET_ATTR = "=Q"
_PATH_BENEATH_ATTR = "=Qi"


@dataclass(frozen=True)
class LandlockReport:
    """Whether [`restrict_writes`][terok_util.landlock.restrict_writes] took hold.

    ``confined`` is ``True`` only when the kernel is now enforcing the write
    restriction on this process.  ``reason`` explains a ``False`` — a kernel
    without Landlock (< 5.13) or a build lacking the syscalls degrades to a
    no-op, which the caller may log but must not treat as an error.
    """

    #: ``True`` when writes outside the granted paths are now denied by the kernel.
    confined: bool
    #: One-line explanation, ready for a diagnostic log line.
    reason: str


def restrict_writes(writable: Iterable[Path]) -> LandlockReport:
    """Deny this process and its descendants any filesystem write outside *writable*.

    Reads and executes are left untouched; only writing, creating, and
    removing are confined to the *writable* hierarchies (each grant covers a
    directory and everything beneath it).  Requires ``no_new_privs`` already
    set — the kernel gates unprivileged Landlock on it — so call
    [`harden_self`][terok_util.hardening.harden_self] first.

    Best-effort and irreversible: never raises; a kernel or build without
    Landlock returns ``confined=False`` and changes nothing.  A *writable*
    path that does not exist is skipped — there is nothing to write into
    until it is created, and its parent grant covers that creation.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if _landlock_abi(libc) < 1:
        return LandlockReport(False, "landlock unavailable (kernel < 5.13 or no syscall)")
    ruleset = _create_ruleset(libc, _WRITE_ACCESS)
    if ruleset < 0:
        return LandlockReport(False, f"create_ruleset failed (errno {ctypes.get_errno()})")
    try:
        for path in writable:
            _grant_writes_beneath(libc, ruleset, path)
        if libc.syscall(_NR_RESTRICT_SELF, ruleset, 0) != 0:
            return LandlockReport(False, f"restrict_self failed (errno {ctypes.get_errno()})")
    finally:
        os.close(ruleset)
    return LandlockReport(True, "writes confined")


def _landlock_abi(libc: ctypes.CDLL) -> int:
    """Return the kernel's Landlock ABI version, or a negative value when unsupported."""
    return libc.syscall(_NR_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION)


def _create_ruleset(libc: ctypes.CDLL, handled_access: int) -> int:
    """Create a ruleset governing *handled_access*; return its fd or a negative errno."""
    attr = struct.pack(_RULESET_ATTR, handled_access)
    return libc.syscall(_NR_CREATE_RULESET, attr, len(attr), 0)


def _grant_writes_beneath(libc: ctypes.CDLL, ruleset: int, path: Path) -> None:
    """Grant the write-family rights on *path* and everything beneath it (best-effort)."""
    try:
        parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return
    try:
        attr = struct.pack(_PATH_BENEATH_ATTR, _WRITE_ACCESS, parent_fd)
        libc.syscall(_NR_ADD_RULE, ruleset, _RULE_PATH_BENEATH, attr, 0)
    finally:
        os.close(parent_fd)


__all__ = ["LandlockReport", "restrict_writes"]
