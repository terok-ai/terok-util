# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Owner-local receipts compose without a central package or setup registry."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from terok_util import (
    SetupCheck,
    SetupDowngradeError,
    SetupReceipt,
    SetupRequiredError,
    SetupStatus,
    python_identity,
    require_no_downgrade,
    require_setup,
    setup_status,
)


@pytest.fixture
def receipt(tmp_path: Path) -> SetupReceipt:
    """A package-owned receipt with one relevant installation input."""
    return SetupReceipt(tmp_path / "state" / "setup.json", "owner", "0.4.0", {"input": "value"})


def test_receipt_lifecycle(receipt: SetupReceipt) -> None:
    """A receipt certifies only its own owner/version/inputs until cleared."""
    assert receipt.check() == SetupCheck(
        "owner", "receipt", SetupStatus.MISSING, "Setup receipt is missing"
    )
    receipt.write()
    assert receipt.check().status is SetupStatus.READY
    assert json.loads(receipt.path.read_text()) == {
        "owner": receipt.owner,
        "version": receipt.version,
        "inputs": receipt.inputs,
    }
    assert receipt.path.stat().st_mode & 0o777 == 0o600
    receipt.clear()
    receipt.clear()
    assert receipt.check().status is SetupStatus.MISSING


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"version": "0.4.1"}, SetupStatus.STALE),
        ({"version": "0.4.0.0"}, SetupStatus.READY),
        ({"inputs": {"input": "changed"}}, SetupStatus.STALE),
        ({"version": "0.3.9", "inputs": {}}, SetupStatus.DOWNGRADE),
        ({"owner": "another"}, SetupStatus.INVALID),
    ],
)
def test_receipt_compares_current_owner_version_and_inputs(
    receipt: SetupReceipt, changes: dict, status: SetupStatus
) -> None:
    """Downgrade detection takes precedence over changed installation inputs."""
    receipt.write()
    assert replace(receipt, **changes).check().status is status


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "null",
        "[]",
        '{"owner": "owner"}',
        '{"owner": 42, "version": "0.4.0", "inputs": {}}',
        '{"owner": "owner", "version": 4, "inputs": {}}',
        '{"owner": "owner", "version": "invalid", "inputs": {}}',
        '{"owner": "owner", "version": "0.4.0", "inputs": []}',
        '{"owner": "owner", "version": "0.4.0", "inputs": {"input": 1}}',
        '{"owner": "owner", "version": "0.4.0", "inputs": {}, "other-owner": "1.0"}',
        '{"versions": {"owner": "0.4.0", "another": "1.0"}}',
    ],
)
def test_corrupt_or_aggregate_receipts_are_not_ready(receipt: SetupReceipt, content: str) -> None:
    """Malformed receipts and legacy aggregate stamps cannot certify new setup."""
    receipt.path.parent.mkdir()
    receipt.path.write_text(content, encoding="utf-8")
    assert receipt.check().status is SetupStatus.INVALID


def test_unreadable_receipt_is_invalid(receipt: SetupReceipt) -> None:
    """Read errors are setup diagnostics rather than unhandled startup exceptions."""
    with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
        assert receipt.check().status is SetupStatus.INVALID


@pytest.mark.parametrize("operation", ["clear", "write"])
def test_downgrade_cannot_mutate_receipt(receipt: SetupReceipt, operation: str) -> None:
    """Neither invalidation nor certification may replace newer setup."""
    receipt.write()
    before = receipt.path.read_bytes()
    with pytest.raises(SetupDowngradeError, match="Downgrades are not supported"):
        getattr(replace(receipt, version="0.3.9"), operation)()
    assert receipt.path.read_bytes() == before


@pytest.mark.parametrize(
    "changes", [{"owner": ""}, {"version": "invalid"}, {"inputs": {"invalid": 1}}]
)
def test_invalid_current_receipt_is_not_written(receipt: SetupReceipt, changes: dict) -> None:
    """A programmer error cannot leave a receipt which passes startup checks."""
    with pytest.raises(ValueError):
        replace(receipt, **changes).write()
    assert not receipt.path.exists()


@pytest.mark.parametrize("failure", ["json.dump", "os.replace"])
def test_failed_atomic_write_preserves_old_receipt_and_removes_temporary(
    receipt: SetupReceipt, failure: str
) -> None:
    """A failed write neither corrupts the previous receipt nor leaves debris."""
    receipt.write()
    previous = receipt.path.read_bytes()
    with patch(f"terok_util.setup.{failure}", side_effect=OSError("failed")):
        with pytest.raises(OSError, match="failed"):
            replace(receipt, version="0.4.1").write()
    assert receipt.path.read_bytes() == previous
    assert list(receipt.path.parent.iterdir()) == [receipt.path]


def test_setup_results_compose_downward_without_owner_knowledge() -> None:
    """Preflight rejects a child's downgrade before a parent's repairable problem."""
    missing = SetupCheck("parent", "receipt", SetupStatus.MISSING)
    newer = SetupCheck("child", "receipt", SetupStatus.DOWNGRADE, "newer installed")
    with pytest.raises(SetupDowngradeError, match="child/receipt: newer installed"):
        require_setup(iter([missing, newer]))
    with pytest.raises(SetupDowngradeError):
        require_no_downgrade([missing, newer])
    require_no_downgrade([missing])
    with pytest.raises(SetupRequiredError, match="Run setup again"):
        require_setup([missing])


def test_ready_and_empty_check_collections_need_no_setup() -> None:
    """Owners without persistent setup do not need fabricated receipts."""
    ready = SetupCheck("owner", "receipt", SetupStatus.READY)
    for checks in ([], [ready]):
        assert setup_status(checks) is SetupStatus.READY
        require_setup(checks)
        require_no_downgrade(checks)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([SetupStatus.READY, SetupStatus.STALE], SetupStatus.STALE),
        ([SetupStatus.STALE, SetupStatus.MISSING], SetupStatus.MISSING),
        ([SetupStatus.MISSING, SetupStatus.INVALID], SetupStatus.INVALID),
        ([SetupStatus.INVALID, SetupStatus.DOWNGRADE], SetupStatus.DOWNGRADE),
    ],
)
def test_setup_summary_prioritizes_blockers(
    statuses: list[SetupStatus], expected: SetupStatus
) -> None:
    """Aggregation is order-independent and prioritizes nonrepairable downgrades."""
    checks = [SetupCheck("owner", "component", status) for status in statuses]
    assert setup_status(checks) is expected
    assert setup_status(reversed(checks)) is expected


def test_python_identity_tracks_binding_version_replacement_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap identity changes when its path, runtime, or on-disk interpreter changes."""
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"first interpreter")
    monkeypatch.setattr(sys, "executable", str(interpreter))
    original = python_identity()
    assert python_identity() == original
    alias = tmp_path / "python-alias"
    alias.symlink_to(interpreter)
    monkeypatch.setattr(sys, "executable", str(alias))
    assert python_identity() != original
    monkeypatch.setattr(sys, "executable", str(interpreter))
    interpreter.write_bytes(b"replacement interpreter")
    assert python_identity() != original
    replaced = python_identity()
    monkeypatch.setattr(sys, "version", "different Python")
    assert python_identity() != replaced
    interpreter.unlink()
    assert json.loads(python_identity())[-1] is None
