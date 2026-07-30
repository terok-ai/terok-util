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

import errno
import os
import struct
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock

import pytest

from terok_util import hardening
from terok_util.hardening import (
    HardeningReport,
    LandlockReport,
    confine_filesystem,
    harden_self,
)

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


class _FakeLandlockLibc:
    """A Landlock syscall recorder with real disposable fds for rulesets."""

    def __init__(
        self,
        *,
        abi: int = 7,
        create_error: int | None = None,
        add_error: int | None = None,
        restrict_error: int | None = None,
    ) -> None:
        self.abi = abi
        self.create_error = create_error
        self.add_error = add_error
        self.restrict_error = restrict_error
        self.handled_access: list[int] = []
        self.allowed_access: list[int] = []
        self.restrict_flags: list[int] = []

    def syscall(self, number: int, *args) -> int:  # noqa: ANN002
        """Emulate the three Landlock syscalls and record their policy arguments."""
        if number == hardening._NR_CREATE_RULESET:
            if args == (None, 0, hardening._CREATE_RULESET_VERSION):
                return self.abi
            if self.create_error is not None:
                hardening.ctypes.set_errno(self.create_error)
                return -1
            self.handled_access.append(struct.unpack(hardening._RULESET_ATTR, args[0])[0])
            return os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
        if number == hardening._NR_ADD_RULE:
            self.allowed_access.append(struct.unpack(hardening._PATH_BENEATH_ATTR, args[2])[0])
            if self.add_error is not None:
                hardening.ctypes.set_errno(self.add_error)
                return -1
            return 0
        if number == hardening._NR_RESTRICT_SELF:
            self.restrict_flags.append(args[1])
            if self.restrict_error is not None:
                hardening.ctypes.set_errno(self.restrict_error)
                return -1
            return 0
        raise AssertionError(f"unexpected syscall {number}")


def _stub_landlock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    abi: int = 7,
    thread_count: int | None = 1,
    create_error: int | None = None,
    add_error: int | None = None,
    restrict_error: int | None = None,
) -> _FakeLandlockLibc:
    """Install a deterministic Landlock syscall/thread-count facade."""
    fake = _FakeLandlockLibc(
        abi=abi,
        create_error=create_error,
        add_error=add_error,
        restrict_error=restrict_error,
    )
    monkeypatch.setattr(hardening, "_libc", lambda: fake)
    monkeypatch.setattr(hardening, "_process_thread_count", lambda: thread_count)
    return fake


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


class TestConfineFilesystem:
    """The Landlock FS floor — irreversible restriction runs in a subprocess.

    The real ``restrict_self`` is permanent, so its effect is exercised in a
    fresh interpreter; degradation and syscall orchestration are checked
    in-process with stubbed Landlock calls.
    """

    def test_confines_reads_and_writes_to_the_lane(self, tmp_path) -> None:
        """A full policy denies reads, writes, and truncation outside the writable lane."""
        ro = tmp_path / "ro"
        rw = tmp_path / "rw"
        outside = tmp_path / "outside"
        for directory in (ro, rw, outside):
            directory.mkdir()
        ro_file = ro / "readonly"
        rw_file = rw / "writable"
        outside_file = outside / "secret"
        ro_file.write_text("read only")
        rw_file.write_text("writable")
        outside_file.write_text("classified")
        rename_source = rw / "source"
        rename_destination = rw / "destination"
        rename_source.mkdir()
        rename_destination.mkdir()
        (rename_source / "object").write_text("move me")

        probe = textwrap.dedent(
            f"""
            import ctypes, os
            from pathlib import Path
            from terok_util.hardening import confine_filesystem

            ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)  # no_new_privs
            report = confine_filesystem(
                [Path({str(ro)!r})],
                [Path({str(rw)!r}), Path(os.devnull)],
            )
            if not report.confined:
                if (
                    report.partially_confined
                    or "unavailable" in report.reason
                    or report.reason.startswith("Landlock ABI 1")
                ):
                    print(f"unsupported:{{report.reason}}")
                    raise SystemExit(0)
                raise RuntimeError(report.reason)

            out = []
            Path({str(rw_file)!r}).write_text("updated")
            os.truncate({str(rw_file)!r}, 1)
            Path({str(rename_source / "object")!r}).rename(
                Path({str(rename_destination / "object")!r})
            )
            list(Path({str(ro)!r}).iterdir())
            descriptor = os.open(os.devnull, os.O_RDWR)
            os.close(descriptor)
            out.append("exact-file-ok")
            try:
                list(Path(os.devnull).parent.iterdir())
                out.append("device-parent-read-LEAK")
            except PermissionError:
                out.append("device-parent-read-denied")
            try:
                Path({str(ro)!r}, "no").write_text("x")
                out.append("ro-write-LEAK")
            except PermissionError:
                out.append("ro-write-denied")
            try:
                Path({str(outside_file)!r}).read_text()
                out.append("sibling-read-LEAK")
            except (PermissionError, OSError):
                out.append("sibling-read-denied")
            try:
                os.truncate({str(ro_file)!r}, 0)
                out.append("ro-truncate-LEAK")
            except PermissionError:
                out.append("ro-truncate-denied")
            try:
                os.truncate({str(outside_file)!r}, 0)
                out.append("sibling-truncate-LEAK")
            except PermissionError:
                out.append("sibling-truncate-denied")
            print(";".join(out))
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        line = result.stdout.strip()
        if line.startswith("unsupported:"):
            pytest.skip(f"kernel without Landlock: {line}")
        assert line == (
            "exact-file-ok;device-parent-read-denied;ro-write-denied;"
            "sibling-read-denied;ro-truncate-denied;sibling-truncate-denied"
        ), f"confinement leaked: {line!r}"

    def test_unsupported_kernel_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kernel without Landlock reports ``confined=False`` and restricts nothing."""
        monkeypatch.setattr(hardening, "_landlock_abi", lambda _libc: -1)
        report = confine_filesystem([], [])
        assert isinstance(report, LandlockReport)
        assert report.confined is False
        assert "unavailable" in report.reason

    def test_absent_libc_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With libc unreachable (musl edge case) confinement degrades, never raises."""
        monkeypatch.setattr(hardening, "_libc", lambda: None)
        report = confine_filesystem([], [])
        assert report.confined is False
        assert report.partially_confined is False

    def test_missing_lane_path_is_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """A non-existent grant path is skipped rather than aborting the restriction.

        Stubs ``restrict_self`` so the runner itself stays unconfined while
        still exercising the add-rule loop over a path that isn't there.
        """
        fake_libc = _stub_landlock(monkeypatch)

        report = confine_filesystem([tmp_path / "nope-r"], [tmp_path / "nope-w"])
        assert report.confined is True  # restrict_self stubbed to succeed
        assert fake_libc.allowed_access == []
        assert fake_libc.restrict_flags == [0]

    @pytest.mark.parametrize(
        ("abi", "has_refer", "has_truncate"),
        [(1, False, False), (2, True, False), (3, True, True)],
    )
    def test_access_masks_follow_the_probed_abi(
        self, abi: int, has_refer: bool, has_truncate: bool
    ) -> None:
        """REFER starts at ABI 2 and TRUNCATE at ABI 3, never earlier."""
        read_access, write_access = hardening._access_masks(abi)
        refer = hardening._FilesystemAccess.REFER
        truncate = hardening._FilesystemAccess.TRUNCATE

        assert bool(write_access & refer) is has_refer
        assert bool(write_access & truncate) is has_truncate
        assert not read_access & (refer | truncate)

    def test_rules_use_directory_or_exact_file_masks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Opened object type selects recursive directory or exact-file rights."""
        read_directory = tmp_path / "read-directory"
        write_directory = tmp_path / "write-directory"
        read_file = tmp_path / "read-file"
        write_file = tmp_path / "write-file"
        read_directory.mkdir()
        write_directory.mkdir()
        read_file.touch()
        write_file.touch()
        fake_libc = _stub_landlock(monkeypatch, abi=3)

        report = confine_filesystem(
            [read_directory, read_file],
            [write_directory, write_file],
        )

        read_access, write_access = hardening._access_masks(3)
        read_file_access = read_access & hardening._FILE_OBJECT_ACCESS
        write_file_access = write_access & hardening._FILE_OBJECT_ACCESS
        assert report.confined
        assert fake_libc.handled_access == [write_access]
        assert fake_libc.allowed_access == [
            read_access,
            read_file_access,
            write_access,
            write_file_access,
        ]

    def test_failed_add_rule_abandons_ruleset_before_restriction(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A requested existing grant must not be silently lost from an installed policy."""
        lane = tmp_path / "lane"
        lane.mkdir()
        fake_libc = _stub_landlock(monkeypatch, add_error=errno.ENOMEM)

        report = confine_filesystem([lane], [])

        assert not report.confined
        assert not report.partially_confined
        assert "add_rule" in report.reason
        assert fake_libc.restrict_flags == []

    def test_nonmissing_open_error_abandons_ruleset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Only an absent grant is skippable; another open error aborts transactionally."""
        symlink_loop = tmp_path / "loop"
        symlink_loop.symlink_to(symlink_loop.name)
        fake_libc = _stub_landlock(monkeypatch)

        report = confine_filesystem([symlink_loop], [])

        assert not report.confined
        assert "open grant path" in report.reason
        assert fake_libc.restrict_flags == []

    def test_inspection_error_abandons_ruleset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A failed object-type inspection cannot degrade into an over-broad rule."""
        lane = tmp_path / "lane"
        lane.touch()
        fake_libc = _stub_landlock(monkeypatch)
        monkeypatch.setattr(hardening.os, "fstat", MagicMock(side_effect=OSError(errno.EIO, "I/O")))

        report = confine_filesystem([lane], [])

        assert not report.confined
        assert "inspect grant path" in report.reason
        assert fake_libc.restrict_flags == []

    def test_process_thread_count_is_best_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The procfs snapshot reports live tasks and degrades when procfs is inaccessible."""
        assert (hardening._process_thread_count() or 0) >= 1
        monkeypatch.setattr(hardening.os, "scandir", MagicMock(side_effect=OSError))
        assert hardening._process_thread_count() is None

    def test_abi_two_is_reported_as_partial_best_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An old kernel receives supported restrictions without a false full-policy claim."""
        fake_libc = _stub_landlock(monkeypatch, abi=2)

        report = confine_filesystem([], [])

        assert not report.confined
        assert report.partially_confined
        assert "truncation" in report.reason
        assert fake_libc.restrict_flags == [0]

    def test_abi_one_is_a_noop_to_preserve_write_lane_semantics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ABI 1 cannot allow the cross-directory renames needed by writable lanes."""
        fake_libc = _stub_landlock(monkeypatch, abi=1)

        report = confine_filesystem([], [])

        assert not report.confined
        assert not report.partially_confined
        assert "rename/link" in report.reason
        assert fake_libc.handled_access == []

    @pytest.mark.parametrize(
        ("thread_count", "detail"),
        [(2, "2 threads"), (None, "thread count unavailable")],
    )
    def test_old_abi_requires_a_verifiably_single_threaded_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
        thread_count: int | None,
        detail: str,
    ) -> None:
        """Without TSYNC, claiming process-wide coverage would leave sibling threads free."""
        fake_libc = _stub_landlock(monkeypatch, thread_count=thread_count)

        report = confine_filesystem([], [])

        assert not report.confined
        assert not report.partially_confined
        assert detail in report.reason
        assert fake_libc.handled_access == []

    def test_abi_eight_uses_tsync_without_single_thread_precondition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TSYNC atomically applies the policy to every existing process thread."""
        fake_libc = _stub_landlock(monkeypatch, abi=8)
        monkeypatch.setattr(
            hardening,
            "_process_thread_count",
            MagicMock(side_effect=AssertionError("TSYNC must not preflight thread count")),
        )

        report = confine_filesystem([], [])

        assert report.confined
        assert fake_libc.restrict_flags == [hardening._RESTRICT_SELF_TSYNC]

    def test_create_ruleset_failure_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ruleset creation error is reported before any restriction attempt."""
        fake_libc = _stub_landlock(monkeypatch, create_error=errno.EMFILE)

        report = confine_filesystem([], [])

        assert not report.confined
        assert "create_ruleset" in report.reason
        assert fake_libc.restrict_flags == []

    def test_restrict_self_failure_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kernel rejection leaves the process outside the prepared ruleset."""
        fake_libc = _stub_landlock(monkeypatch, restrict_error=errno.EPERM)

        report = confine_filesystem([], [])

        assert not report.confined
        assert "restrict_self" in report.reason
        assert fake_libc.restrict_flags == [0]
