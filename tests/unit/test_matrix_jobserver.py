# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The make jobserver client, against real pipes, and the matrix walk that joins it."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from terok_util.matrix import cli, runner
from terok_util.matrix.jobserver import Jobserver
from unit.matrix_fixtures import load_fixture, write_config


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """A named-pipe jobserver as make 4.4 creates it: the path, and the server's own end."""
    fifo = tmp_path / "jobserver"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    yield fifo, fd
    os.close(fd)


def _tokens(fd: int) -> bytes:
    """The tokens waiting in the pipe, taken out without blocking."""
    try:
        return os.read(fd, 64)
    except BlockingIOError:
        return b""


def test_no_jobserver_to_join_without_its_auth_option() -> None:
    """No MAKEFLAGS, or MAKEFLAGS without a jobserver: the run stays on its own."""
    assert Jobserver.from_environ({}) is None
    assert Jobserver.from_environ({"MAKEFLAGS": "-k -j4"}) is None


def test_fifo_jobserver_hands_out_the_implicit_slot_then_its_tokens(
    server: tuple[Path, int],
) -> None:
    """The first slot is free; each further one takes a token and gives the same byte back."""
    fifo, fd = server
    os.write(fd, b"ab")
    client = Jobserver.from_environ({"MAKEFLAGS": f"-j3 --jobserver-auth=fifo:{fifo}"})
    assert client is not None

    with client.slot(), client.slot(), client.slot():
        assert _tokens(fd) == b""

    assert sorted(_tokens(fd)) == sorted(b"ab")


def test_a_waiting_slot_takes_the_implicit_slot_when_it_frees(server: tuple[Path, int]) -> None:
    """All of a run's slots may wait for tokens at once; one of them must take the implicit slot back."""
    fifo, fd = server
    client = Jobserver.from_environ({"MAKEFLAGS": f"--jobserver-auth=fifo:{fifo}"})
    assert client is not None
    entered = threading.Event()

    def waiting_slot() -> None:
        with client.slot():
            entered.set()

    with client.slot():
        waiter = threading.Thread(target=waiting_slot, daemon=True)
        waiter.start()
        assert not entered.wait(0.3), "no token and no free implicit slot: it must wait"
    assert entered.wait(3), "the freed implicit slot went unused"
    waiter.join(3)
    assert _tokens(fd) == b""


def test_pipe_jobserver_uses_the_inherited_descriptors() -> None:
    """make 4.3 passes a plain pipe as R,W; the last auth option counts."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"+")
        makeflags = f"--jobserver-auth=-2,-2 -j2 --jobserver-auth={read_fd},{write_fd}"
        client = Jobserver.from_environ({"MAKEFLAGS": makeflags})
        assert client is not None
        with client.slot(), client.slot():
            pass
        assert os.read(read_fd, 1) == b"+"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_disabled_or_unreachable_jobservers_leave_the_run_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative descriptors mean disabled; closed ones and a missing pipe are said out loud."""
    assert Jobserver.from_environ({"MAKEFLAGS": "--jobserver-auth=-2,-2"}) is None
    assert capsys.readouterr().err == ""

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    assert Jobserver.from_environ({"MAKEFLAGS": f"--jobserver-auth={read_fd},{write_fd}"}) is None
    assert "mark the make recipe with +" in capsys.readouterr().err

    missing = tmp_path / "gone"
    assert Jobserver.from_environ({"MAKEFLAGS": f"--jobserver-auth=fifo:{missing}"}) is None
    assert "cannot be opened" in capsys.readouterr().err


def test_parallel_walk_holds_to_the_jobserver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: tuple[Path, int]
) -> None:
    """Without -j a jobserver sets the pace: its tokens plus the implicit slot, and no more."""
    write_config(tmp_path)
    assert len(load_fixture(tmp_path).slots) > 2, "the cap is only visible above two slots"
    fifo, fd = server
    os.write(fd, b"+")
    monkeypatch.setenv("MAKEFLAGS", f"-j2 --jobserver-auth=fifo:{fifo}")

    lock = threading.Lock()
    running = peak = 0

    def fake_run_slot(config, name, results_dir, scope="all", line_prefix=None):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return runner.SlotResult(passed=True, observed="5.0.0")

    monkeypatch.setattr(cli, "run_slot", fake_run_slot)
    monkeypatch.setattr(cli, "_build_images", lambda *a, **k: set())
    monkeypatch.setattr(cli, "prune_dangling", lambda config: 0)
    monkeypatch.setattr(cli, "sweep_containers", lambda config: 0)
    monkeypatch.setattr(cli, "external_storage_leftovers", lambda: [])
    monkeypatch.setattr(cli, "_skip_reason", lambda config, name: "")

    assert cli.main(["--config", str(tmp_path / "tests" / "containers" / "matrix.yml")]) == 0
    assert peak == 2
    assert _tokens(fd) == b"+"
