# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Setup keeps its exclusion boundary even while owner receipts are absent."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from terok_util import (
    SetupCheck,
    SetupDowngradeError,
    SetupReceipt,
    SetupRequiredError,
    SetupStatus,
    paths,
    require_no_downgrade,
    require_setup,
    setup,
    setup_lock,
)

_NEWER_SETUP = (SetupCheck("owner", "receipt", SetupStatus.DOWNGRADE, "newer installed"),)


@pytest.fixture
def lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every lock, including subprocess probes, below this test's directory."""
    directory = tmp_path / "locks"
    monkeypatch.setattr(setup, "_platform_state_base", lambda: directory)
    return directory


def _older_setup(lock_dir: Path, receipt: Path) -> str:
    """Attempt a real competing setup with an older version in a fresh interpreter."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            dedent("""\
                import sys
                from pathlib import Path
                from terok_util import SetupReceipt, SetupRequiredError, setup, setup_lock

                setup._platform_state_base = lambda: Path(sys.argv[1])
                receipt = SetupReceipt(Path(sys.argv[2]), "owner", "0.3.0", {})
                try:
                    with setup_lock():
                        receipt.clear()
                        receipt.write()
                except SetupRequiredError as exc:
                    print(type(exc).__name__ + ": " + str(exc))
                else:
                    print("written")
                """),
            str(lock_dir),
            str(receipt),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return result.stdout.strip()


def test_lock_prevents_downgrade_while_receipt_is_cleared(lock_dir: Path, tmp_path: Path) -> None:
    """An older process cannot sneak past downgrade checks during a setup transaction."""
    receipt = SetupReceipt(tmp_path / "setup.json", "owner", "0.4.0", {})
    receipt.write()
    with setup_lock():
        receipt.clear()
        assert _older_setup(lock_dir, receipt.path).startswith("SetupRequiredError: Another setup")
        assert not receipt.path.exists()
        receipt.write()
    before = receipt.path.read_bytes()
    assert _older_setup(lock_dir, receipt.path).startswith("SetupDowngradeError:")
    assert receipt.path.read_bytes() == before


def test_nested_context_and_decorator_retain_outer_lock(lock_dir: Path, tmp_path: Path) -> None:
    """Downward calls share ownership without releasing the caller's transaction."""

    @setup_lock()
    def prepare_child() -> None:
        """A dependency may guard its own entry point."""
        with setup_lock():
            pass

    with setup_lock():
        inode = (lock_dir / "setup.lock").stat().st_ino
        prepare_child()
        prepare_child()
        assert _older_setup(lock_dir, tmp_path / "setup.json").startswith("SetupRequiredError:")
    with setup_lock():
        assert (lock_dir / "setup.lock").stat().st_ino == inode


def test_exception_releases_lock(lock_dir: Path, tmp_path: Path) -> None:
    """A failed setup lets a fresh process repair it without deleting the lock file."""
    with pytest.raises(ValueError, match="setup failed"), setup_lock(), setup_lock():
        raise ValueError("setup failed")
    assert (lock_dir / "setup.lock").exists()
    assert _older_setup(lock_dir, tmp_path / "setup.json") == "written"


@pytest.mark.parametrize("operation", ["clear", "write"])
@pytest.mark.parametrize("versions", [("0.4.0", "0.3.9"), ("0.4.0.dev11+g111", "0.4.0.dev1+g222")])
def test_explicit_setup_can_replace_newer_receipt(
    lock_dir: Path, tmp_path: Path, operation: str, versions: tuple[str, str]
) -> None:
    """Authorization permits owner writes without hiding unrepaired setup from runtime."""
    installed = SetupReceipt(tmp_path / "setup.json", "owner", versions[0], {})
    installed.write()
    older = replace(installed, version=versions[1])
    with setup_lock(allow_downgrade=True):
        assert older.check().status is SetupStatus.DOWNGRADE
        require_no_downgrade((older.check(),))
        with pytest.raises(SetupDowngradeError):
            require_setup((older.check(),))
        getattr(older, operation)()
    expected = SetupStatus.MISSING if operation == "clear" else SetupStatus.READY
    assert older.check().status is expected
    with pytest.raises(SetupDowngradeError):
        require_no_downgrade(_NEWER_SETUP)


def test_nested_setup_inherits_and_restores_downgrade_permission(lock_dir: Path) -> None:
    """Dependency decorators inherit permission without granting it to later setup."""

    @setup_lock()
    def prepare_child() -> None:
        """A lower installer uses its unchanged default setup guard."""
        require_no_downgrade(_NEWER_SETUP)

    with setup_lock():
        with setup_lock(allow_downgrade=True):
            prepare_child()
            prepare_child()
        with pytest.raises(SetupDowngradeError):
            prepare_child()


@pytest.mark.parametrize("fail", [False, True])
def test_scope_exit_revokes_copied_downgrade_permission(lock_dir: Path, fail: bool) -> None:
    """Copied contexts cannot retain authorization beyond the owning setup scope."""
    outcome = pytest.raises(ValueError, match="setup failed") if fail else nullcontext()
    with outcome, setup_lock(allow_downgrade=True):
        copied = copy_context()
        if fail:
            raise ValueError("setup failed")
    for check in (
        lambda: require_no_downgrade(_NEWER_SETUP),
        lambda: copied.run(require_no_downgrade, _NEWER_SETUP),
    ):
        with pytest.raises(SetupDowngradeError):
            check()
        with setup_lock(), pytest.raises(SetupDowngradeError):
            check()


def test_copied_context_cannot_authorize_another_thread(lock_dir: Path) -> None:
    """A live permit belongs to the lock holder, even if a thread inherits its context."""
    with setup_lock(allow_downgrade=True), ThreadPoolExecutor(max_workers=1) as pool:
        copied = copy_context()
        with pytest.raises(SetupDowngradeError):
            pool.submit(copied.run, require_no_downgrade, _NEWER_SETUP).result(timeout=5)


def test_failed_acquisition_does_not_authorize_downgrade(lock_dir: Path) -> None:
    """An explicit request grants nothing if another thread owns setup."""

    def competing_setup() -> None:
        """Check the denied caller without relying on the thread's later disposal."""
        with pytest.raises(SetupRequiredError, match="Another setup"):
            with setup_lock(allow_downgrade=True):
                pytest.fail("competing setup acquired the lock")
        with pytest.raises(SetupDowngradeError):
            require_no_downgrade(_NEWER_SETUP)

    with setup_lock(), ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(competing_setup).result(timeout=5)


def test_other_thread_cannot_inherit_ownership(lock_dir: Path) -> None:
    """Reentrancy belongs to the acquiring thread, not every thread in the process."""

    def competing_setup() -> None:
        """Attempt acquisition while the calling thread retains its context."""
        with setup_lock():
            pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        with setup_lock():
            with pytest.raises(SetupRequiredError, match="Another setup"):
                pool.submit(competing_setup).result(timeout=5)
        pool.submit(competing_setup).result(timeout=5)


def _forked_setup() -> None:
    """A forked child must fail instead of treating inherited state as reentrancy."""
    with pytest.raises(SetupDowngradeError):
        require_no_downgrade(_NEWER_SETUP)
    with pytest.raises(SetupRequiredError, match="Another setup"):
        with setup_lock():
            pytest.fail("forked child entered the parent's setup transaction")


def test_after_fork_resets_authorization(lock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the child reset directly because coverage does not collect forked execution."""
    with setup_lock(allow_downgrade=True):
        inherited_mutex, inherited_fd = setup._setup_mutex, setup._setup_fd
        monkeypatch.setattr(setup, "_setup_mutex", inherited_mutex)
        with patch.object(setup.os, "close") as close:
            setup._reset_setup_lock_after_fork()
        close.assert_called_once_with(inherited_fd)
        assert setup._setup_fd is None
        assert setup._setup_mutex is not inherited_mutex
        with pytest.raises(SetupDowngradeError):
            require_no_downgrade(_NEWER_SETUP)


@pytest.mark.filterwarnings("ignore:This process.*multi-threaded:DeprecationWarning")
def test_fork_does_not_inherit_or_release_ownership(lock_dir: Path, tmp_path: Path) -> None:
    """Closing the child's inherited descriptor must leave the parent's lock held."""
    with setup_lock(allow_downgrade=True):
        child = multiprocessing.get_context("fork").Process(target=_forked_setup)
        child.start()
        try:
            child.join(timeout=5)
            assert child.exitcode == 0
        finally:
            if child.is_alive():
                child.kill()
                child.join()
        assert _older_setup(lock_dir, tmp_path / "setup.json").startswith("SetupRequiredError:")


def test_package_path_overrides_do_not_split_global_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global-hook setup shares the default platform data namespace across package roots."""
    monkeypatch.setattr(paths, "_is_root", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for name in ("TEROK_ROOT", "TEROK_RUNTIME_DIR", "TEROK_SANDBOX_RUNTIME_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    with setup_lock():
        assert (tmp_path / "data" / "terok" / "setup.lock").is_file()
    assert not any((tmp_path / name).exists() for name in ("TEROK_ROOT", "TEROK_RUNTIME_DIR"))


@pytest.mark.parametrize("has_runtime", [False, True])
def test_runtime_environment_does_not_split_global_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_runtime: bool
) -> None:
    """Login sessions and launches without XDG_RUNTIME_DIR contend on the same lock."""
    monkeypatch.setattr(paths, "_is_root", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    runtime = str(tmp_path / "runtime")
    if has_runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    child_env = dict(os.environ)
    if has_runtime:
        child_env.pop("XDG_RUNTIME_DIR")
    else:
        child_env["XDG_RUNTIME_DIR"] = runtime
    with setup_lock():
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                dedent("""\
                    from terok_util import paths, setup_lock

                    paths._is_root = lambda: False
                    with setup_lock():
                        pass
                    """),
            ],
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    assert result.returncode != 0
    assert "SetupRequiredError: Another setup operation is already running" in result.stderr
