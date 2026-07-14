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
    """In a fresh process the dumpable flag and core limit are actually cleared."""
    probe = textwrap.dedent(
        """
        import ctypes, ctypes.util, resource
        from terok_util.hardening import harden_self

        report = harden_self()
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        _PR_GET_DUMPABLE = 3
        dumpable = libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0)
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        print(f"{int(report.no_dump)}{int(report.no_core)}:{dumpable}:{soft}:{hard}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    flags, dumpable, soft, hard = result.stdout.strip().split(":")
    assert flags == "11", "no_dump and no_core must both succeed in a normal process"
    assert dumpable == "0", "PR_GET_DUMPABLE must read back 0 after harden_self"
    assert (soft, hard) == ("0", "0"), "RLIMIT_CORE must be pinned to zero"


def test_allow_debugger_keeps_process_dumpable() -> None:
    """``allow_debugger`` leaves the dumpable flag set so a debugger can attach."""
    probe = textwrap.dedent(
        """
        import ctypes, ctypes.util
        from terok_util.hardening import harden_self

        report = harden_self(allow_debugger=True)
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        _PR_GET_DUMPABLE = 3
        print(f"{int(report.no_dump)}:{libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0)}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    no_dump, dumpable = result.stdout.strip().split(":")
    assert no_dump == "0", "no_dump must report False in debug mode"
    assert dumpable == "1", "the process must stay ptrace-able (dumpable left at 1)"


def test_core_limit_is_independent_of_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    """With libc unreachable, the pure-``resource`` core-limit clear still takes."""
    monkeypatch.setattr(hardening, "_libc", lambda: None)
    report = harden_self()
    assert report.no_dump is False
    assert report.memory_locked is False
    assert report.no_core is True


class TestHardeningReport:
    """The ``fully_hardened`` roll-up is a plain three-way AND."""

    def test_all_true_is_fully_hardened(self) -> None:
        assert HardeningReport(no_dump=True, no_core=True, memory_locked=True).fully_hardened

    @pytest.mark.parametrize(
        ("no_dump", "no_core", "memory_locked"),
        [(False, True, True), (True, False, True), (True, True, False)],
    )
    def test_any_gap_is_not_fully_hardened(
        self, no_dump: bool, no_core: bool, memory_locked: bool
    ) -> None:
        report = HardeningReport(no_dump=no_dump, no_core=no_core, memory_locked=memory_locked)
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

    def test_lock_memory(self) -> None:
        assert hardening._lock_memory(None) is False
        assert hardening._lock_memory(_FakeLibc(mlockall_rc=0)) is True
        assert hardening._lock_memory(_FakeLibc(mlockall_rc=-1)) is False

    def test_zero_core_limit_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hardening.resource, "setrlimit", MagicMock(side_effect=ValueError))
        assert hardening._zero_core_limit() is False

    def test_harden_self_full_with_capable_libc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With a libc whose calls succeed, all three guarantees report taken."""
        monkeypatch.setattr(hardening, "_libc", lambda: _FakeLibc(prctl_rc=0, mlockall_rc=0))
        report = harden_self()
        assert report == HardeningReport(no_dump=True, no_core=True, memory_locked=True)
        assert report.fully_hardened

    def test_allow_debugger_skips_only_the_dumpable_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Debug mode drops the no-ptrace guarantee but keeps core + swap locked."""
        monkeypatch.setattr(hardening, "_libc", lambda: _FakeLibc(prctl_rc=0, mlockall_rc=0))
        report = harden_self(allow_debugger=True)
        # prctl would have returned 0 (success) had it been called — no_dump False
        # therefore proves the dumpable clear was skipped, not that it failed.
        assert report == HardeningReport(no_dump=False, no_core=True, memory_locked=True)
        assert not report.fully_hardened
