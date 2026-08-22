# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The consumer half of the capability contract - protocol and probes."""

from __future__ import annotations

import socket

import pytest

from terok_util.matrix import binary_on_path, check_capability_contract, tcp_reachable


@pytest.fixture
def probes() -> dict[str, object]:
    """A two-capability probe map: one present, one missing."""
    return {"present": lambda: True, "absent": lambda: False}


def test_no_declared_contract_passes(
    monkeypatch: pytest.MonkeyPatch, probes: dict[str, object]
) -> None:
    """Dev machines declare nothing - the contract never fires."""
    monkeypatch.delenv("TEROK_EXPECT", raising=False)

    assert check_capability_contract(probes) is None


def test_satisfied_contract_passes(
    monkeypatch: pytest.MonkeyPatch, probes: dict[str, object]
) -> None:
    """Every declared capability probing True means no failure message."""
    monkeypatch.setenv("TEROK_EXPECT", "present")

    assert check_capability_contract(probes) is None


def test_missing_capability_is_named(
    monkeypatch: pytest.MonkeyPatch, probes: dict[str, object]
) -> None:
    """A declared-but-missing capability yields the broken-contract message."""
    monkeypatch.setenv("TEROK_EXPECT", "present,absent")

    message = check_capability_contract(probes)

    assert message is not None
    assert "absent" in message and "broken" in message


def test_unknown_capability_is_rejected(
    monkeypatch: pytest.MonkeyPatch, probes: dict[str, object]
) -> None:
    """A capability the conftest has no probe for is a contract typo."""
    monkeypatch.setenv("TEROK_EXPECT", "warpdrive")

    message = check_capability_contract(probes)

    assert message is not None
    assert "unknown" in message and "warpdrive" in message


def test_binary_on_path_searches_sbin_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH without sbin still finds sbin-installed daemons; junk does not resolve."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert binary_on_path("sh")
    assert not binary_on_path("no-such-binary-anywhere")


@pytest.mark.needs_loopback  # binds a real loopback listener; krun's TSI refuses it
def test_tcp_reachable_against_a_live_and_dead_port() -> None:
    """The internet probe distinguishes a listening socket from a refusing one."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        assert tcp_reachable("127.0.0.1", listener.getsockname()[1], timeout=2.0)

    with socket.socket() as bound_only:
        # Bound but never listen()ed: connections are refused, and the port
        # cannot be grabbed by a concurrent test in the meantime.
        bound_only.bind(("127.0.0.1", 0))

        assert not tcp_reachable("127.0.0.1", bound_only.getsockname()[1], timeout=0.5)
