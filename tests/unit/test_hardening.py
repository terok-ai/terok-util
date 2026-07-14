# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`harden_self`][terok_util.hardening.harden_self].

The real-syscall behaviour is exercised in a fresh interpreter
(``subprocess``) so the floor's side effects — a cleared dumpable flag,
a zeroed core limit, and possibly ``mlockall`` — never bleed into the
pytest runner (a privileged runner that locked memory could otherwise
hit ``RLIMIT_MEMLOCK`` in later tests).  The branch/report logic is
checked in-process with the syscalls stubbed out.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from unittest.mock import MagicMock

import pytest

from terok_util import hardening
from terok_util.hardening import HardeningReport, harden_self

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="hardening floor is Linux-only")


class _FakeLibc:
    """A libc stand-in whose prctl/mlockall return preset codes (no real syscall)."""

    def __init__(self, *, prctl_rc: int = 0, mlockall_rc: int = 0) -> None:
        self._prctl_rc = prctl_rc
        self._mlockall_rc = mlockall_rc

    def prctl(self, *_args: int) -> int:
        return self._prctl_rc

    def mlockall(self, *_args: int) -> int:
        return self._mlockall_rc


def test_real_syscalls_take_effect() -> None:
    """In a fresh process the dumpable flag, core limit, and no-new-privs actually take."""
    probe = textwrap.dedent(
        """
        import ctypes, ctypes.util, resource
        from terok_util.hardening import harden_self

        report = harden_self()
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        _PR_GET_DUMPABLE = 3
        _PR_GET_NO_NEW_PRIVS = 39
        dumpable = libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0)
        nnp = libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        flags = f"{int(report.no_dump)}{int(report.no_core)}{int(report.no_new_privs)}"
        print(f"{flags}:{dumpable}:{nnp}:{soft}:{hard}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    flags, dumpable, nnp, soft, hard = result.stdout.strip().split(":")
    assert flags == "111", "no_dump, no_core, no_new_privs must all succeed in a normal process"
    assert dumpable == "0", "PR_GET_DUMPABLE must read back 0 after harden_self"
    assert nnp == "1", "PR_GET_NO_NEW_PRIVS must read back 1 after harden_self"
    assert (soft, hard) == ("0", "0"), "RLIMIT_CORE must be pinned to zero"


def test_allow_debugger_keeps_process_dumpable() -> None:
    """Debug mode leaves the process dumpable, but no-new-privs still takes."""
    probe = textwrap.dedent(
        """
        import ctypes, ctypes.util
        from terok_util.hardening import harden_self

        report = harden_self(allow_debugger=True)
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        _PR_GET_DUMPABLE = 3
        _PR_GET_NO_NEW_PRIVS = 39
        dumpable = libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0)
        nnp = libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)
        print(f"{int(report.no_dump)}{int(report.no_new_privs)}:{dumpable}:{nnp}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    flags, dumpable, nnp = result.stdout.strip().split(":")
    assert flags == "01", "no_dump False but no_new_privs True — the latter holds in debug mode"
    assert dumpable == "1", "the process must stay ptrace-able (dumpable left at 1)"
    assert nnp == "1", "no-new-privs never impedes a debugger attaching, so it still applies"


def test_core_limit_is_independent_of_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    """With libc unreachable, the pure-``resource`` core-limit clear still takes."""
    monkeypatch.setattr(hardening, "_libc", lambda: None)
    report = harden_self()
    assert report.no_dump is False
    assert report.memory_locked is False
    assert report.no_new_privs is False
    assert report.no_core is True


class TestHardeningReport:
    """The ``fully_hardened`` roll-up is a plain four-way AND."""

    def test_all_true_is_fully_hardened(self) -> None:
        report = HardeningReport(no_dump=True, no_core=True, memory_locked=True, no_new_privs=True)
        assert report.fully_hardened

    @pytest.mark.parametrize(
        ("no_dump", "no_core", "memory_locked", "no_new_privs"),
        [
            (False, True, True, True),
            (True, False, True, True),
            (True, True, False, True),
            (True, True, True, False),
        ],
    )
    def test_any_gap_is_not_fully_hardened(
        self, no_dump: bool, no_core: bool, memory_locked: bool, no_new_privs: bool
    ) -> None:
        report = HardeningReport(
            no_dump=no_dump, no_core=no_core, memory_locked=memory_locked, no_new_privs=no_new_privs
        )
        assert not report.fully_hardened


class TestHelpersInProcess:
    """Cover the syscall helpers in-process (the real-syscall path runs in a subprocess).

    The subprocess test proves the syscalls actually take; these mock libc
    so the branch/return logic is measured without touching the runner's
    real dumpable flag or locking its memory.
    """

    def test_libc_returns_a_usable_handle(self) -> None:
        handle = hardening._libc()
        assert handle is None or hasattr(handle, "prctl")

    def test_libc_is_none_when_cdll_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hardening.ctypes, "CDLL", MagicMock(side_effect=OSError))
        assert hardening._libc() is None

    def test_clear_dumpable(self) -> None:
        assert hardening._clear_dumpable(None) is False
        assert hardening._clear_dumpable(_FakeLibc(prctl_rc=0)) is True
        assert hardening._clear_dumpable(_FakeLibc(prctl_rc=-1)) is False

    def test_set_no_new_privs(self) -> None:
        assert hardening._set_no_new_privs(None) is False
        assert hardening._set_no_new_privs(_FakeLibc(prctl_rc=0)) is True
        assert hardening._set_no_new_privs(_FakeLibc(prctl_rc=-1)) is False

    def test_lock_memory(self) -> None:
        assert hardening._lock_memory(None) is False
        assert hardening._lock_memory(_FakeLibc(mlockall_rc=0)) is True
        assert hardening._lock_memory(_FakeLibc(mlockall_rc=-1)) is False

    def test_zero_core_limit_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hardening.resource, "setrlimit", MagicMock(side_effect=ValueError))
        assert hardening._zero_core_limit() is False

    def test_harden_self_full_with_capable_libc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With a libc whose calls succeed, all four guarantees report taken."""
        monkeypatch.setattr(hardening, "_libc", lambda: _FakeLibc(prctl_rc=0, mlockall_rc=0))
        report = harden_self()
        assert report == HardeningReport(
            no_dump=True, no_core=True, memory_locked=True, no_new_privs=True
        )
        assert report.fully_hardened

    def test_allow_debugger_skips_only_the_dumpable_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Debug mode drops the no-ptrace guarantee but keeps core, swap, and no-new-privs."""
        monkeypatch.setattr(hardening, "_libc", lambda: _FakeLibc(prctl_rc=0, mlockall_rc=0))
        report = harden_self(allow_debugger=True)
        # prctl would have returned 0 (success) had it been called — no_dump False
        # therefore proves the dumpable clear was skipped, not that it failed.
        assert report == HardeningReport(
            no_dump=False, no_core=True, memory_locked=True, no_new_privs=True
        )
        assert not report.fully_hardened
