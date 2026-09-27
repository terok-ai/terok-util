# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Setup keeps its exclusion boundary even while owner receipts are absent."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from textwrap import dedent

import pytest

from terok_util import SetupReceipt, SetupRequiredError, paths, setup, setup_lock


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
    with pytest.raises(SetupRequiredError, match="Another setup"):
        with setup_lock():
            pytest.fail("forked child entered the parent's setup transaction")


@pytest.mark.filterwarnings("ignore:This process.*multi-threaded:DeprecationWarning")
def test_fork_does_not_inherit_or_release_ownership(lock_dir: Path, tmp_path: Path) -> None:
    """Closing the child's inherited descriptor must leave the parent's lock held."""
    with setup_lock():
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
