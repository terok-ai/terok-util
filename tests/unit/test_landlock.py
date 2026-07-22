# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`restrict_writes`][terok_util.landlock.restrict_writes].

The real restriction is irreversible and process-wide, so its effect is
exercised in a fresh interpreter (``subprocess``); the degradation logic is
checked in-process with the Landlock ABI probe stubbed out.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from terok_util import landlock
from terok_util.landlock import LandlockReport, restrict_writes

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only")


def test_confines_writes_but_leaves_reads_and_exec(tmp_path) -> None:
    """In a fresh process, writes land only under the granted path; reads stay open."""
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    probe = textwrap.dedent(
        f"""
        import ctypes, os
        from pathlib import Path
        from terok_util.landlock import restrict_writes

        ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)  # no_new_privs
        report = restrict_writes([Path({str(allowed)!r})])
        if not report.confined:
            print(f"unsupported:{{report.reason}}")
            raise SystemExit(0)

        Path({str(allowed)!r}, "ok").write_text("x")  # granted → succeeds
        try:
            Path({str(denied)!r}, "no").write_text("x")
            leaked = True
        except PermissionError:
            leaked = False
        os.listdir("/usr")  # reads stay unrestricted → no raise
        print(f"confined:{{int(not leaked)}}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    out = result.stdout.strip()
    if out.startswith("unsupported:"):
        pytest.skip(f"kernel without Landlock: {out}")
    assert out == "confined:1", f"a denied write leaked past the confinement: {out!r}"


def test_unsupported_kernel_is_a_noop(monkeypatch) -> None:
    """A kernel without Landlock reports ``confined=False`` and restricts nothing."""
    monkeypatch.setattr(landlock, "_landlock_abi", lambda _libc: -1)
    report = restrict_writes([])
    assert isinstance(report, LandlockReport)
    assert report.confined is False
    assert "unavailable" in report.reason


def test_missing_writable_path_is_skipped(monkeypatch, tmp_path) -> None:
    """A non-existent grant path is skipped rather than aborting the restriction.

    Stubs ``restrict_self`` so the test process itself stays unconfined while
    still exercising the add-rule loop over a path that isn't there.
    """
    calls: list[int] = []
    real_syscall = landlock.ctypes.CDLL(None, use_errno=True).syscall

    def _syscall(nr, *args):  # noqa: ANN001, ANN202
        if nr == landlock._NR_RESTRICT_SELF:
            calls.append(nr)
            return 0
        return real_syscall(nr, *args)

    fake_libc = type("Libc", (), {"syscall": staticmethod(_syscall)})()
    monkeypatch.setattr(landlock.ctypes, "CDLL", lambda *_a, **_k: fake_libc)

    report = restrict_writes([tmp_path / "does-not-exist"])
    assert report.confined is True  # restrict_self stubbed to succeed
    assert calls == [landlock._NR_RESTRICT_SELF]
