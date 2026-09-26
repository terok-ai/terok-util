# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Check and certify package-owned setup without knowing the dependency graph."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from packaging.version import InvalidVersion, Version


class SetupStatus(StrEnum):
    """Whether installed setup can be used by the running package."""

    READY = "ready"
    MISSING = "missing"
    STALE = "stale"
    DOWNGRADE = "downgrade"
    INVALID = "invalid"


@dataclass(frozen=True)
class SetupCheck:
    """One owner's setup result, composable with checks from its dependencies."""

    owner: str
    component: str
    status: SetupStatus
    diagnostic: str = ""


@dataclass(frozen=True)
class SetupReceipt:
    """Certify one owner's version and setup inputs in an atomic JSON receipt.

    Owners preflight their full dependency closure before changing artifacts,
    clear their receipt immediately before their own writes, and write it only
    after verification succeeds. A receipt never certifies child packages.
    """

    path: Path
    owner: str
    version: str
    inputs: Mapping[str, str]

    def check(self) -> SetupCheck:
        """Compare the receipt with the current owner, version, and setup inputs."""
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._result(SetupStatus.MISSING, "Setup receipt is missing")
        except (OSError, ValueError):
            return self._result(SetupStatus.INVALID, "Setup receipt cannot be read")
        if not _valid_receipt(stored) or stored["owner"] != self.owner:
            return self._result(SetupStatus.INVALID, "Setup receipt has an invalid owner or format")
        try:
            installed, current = Version(stored["version"]), Version(self.version)
        except InvalidVersion:
            return self._result(SetupStatus.INVALID, "Setup receipt version is invalid")
        if installed > current:
            return self._result(
                SetupStatus.DOWNGRADE,
                f"Setup belongs to newer {self.owner} {installed}; running {current}",
            )
        if installed != current or stored["inputs"] != self.inputs:
            return self._result(SetupStatus.STALE, "Setup version or inputs changed")
        return self._result(SetupStatus.READY)

    def clear(self) -> None:
        """Invalidate this owner's receipt, refusing an unsupported downgrade."""
        require_no_downgrade([self.check()])
        self.path.unlink(missing_ok=True)

    def write(self) -> None:
        """Atomically certify successful setup, refusing an unsupported downgrade."""
        require_no_downgrade([self.check()])
        receipt = {"owner": self.owner, "version": self.version, "inputs": dict(self.inputs)}
        if not _valid_receipt(receipt):
            raise ValueError("Setup receipt requires a string owner, version, and inputs")
        Version(self.version)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(receipt, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def _result(self, status: SetupStatus, diagnostic: str = "") -> SetupCheck:
        """Label a receipt check with its owner."""
        return SetupCheck(self.owner, "receipt", status, diagnostic)


def setup_status(checks: Iterable[SetupCheck]) -> SetupStatus:
    """Summarize checks, prioritizing unsupported downgrades over repairable setup."""
    statuses = {check.status for check in checks}
    return next(
        (
            status
            for status in (
                SetupStatus.DOWNGRADE,
                SetupStatus.INVALID,
                SetupStatus.MISSING,
                SetupStatus.STALE,
            )
            if status in statuses
        ),
        SetupStatus.READY,
    )


def require_no_downgrade(checks: Iterable[SetupCheck]) -> None:
    """Reject newer setup anywhere in the supplied dependency closure."""
    downgrades = [check for check in checks if check.status == SetupStatus.DOWNGRADE]
    if downgrades:
        raise SetupDowngradeError(_diagnostics(downgrades) + ". Downgrades are not supported.")


def require_setup(checks: Iterable[SetupCheck]) -> None:
    """Require all checks to be ready, using typed errors instead of process exits."""
    pending = [check for check in checks if check.status != SetupStatus.READY]
    require_no_downgrade(pending)
    if pending:
        raise SetupRequiredError(_diagnostics(pending) + ". Run setup again.")


def python_identity() -> str:
    """Identify the running bootstrap Python and detect replacement or disappearance."""
    executable = Path(sys.executable)
    try:
        stat = executable.stat()
        identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    except OSError:
        identity = None
    return json.dumps([str(executable), sys.version, identity])


class SetupRequiredError(RuntimeError):
    """Setup must complete before an operation may change running tasks."""


class SetupDowngradeError(SetupRequiredError):
    """Newer installed setup cannot be replaced by an older package."""


def _valid_receipt(receipt: object) -> bool:
    """Accept only the owner-local receipt shape, not legacy aggregate stamps."""
    return (
        isinstance(receipt, dict)
        and set(receipt) == {"owner", "version", "inputs"}
        and isinstance(receipt["owner"], str)
        and bool(receipt["owner"])
        and isinstance(receipt["version"], str)
        and isinstance(receipt["inputs"], dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in receipt["inputs"].items())
    )


def _diagnostics(checks: Iterable[SetupCheck]) -> str:
    """Join owner-labelled checks without knowing how owners expose their setup CLI."""
    return "; ".join(
        f"{check.owner}/{check.component}: {check.diagnostic or check.status}" for check in checks
    )


__all__ = [
    "SetupStatus",
    "SetupCheck",
    "SetupReceipt",
    "SetupRequiredError",
    "SetupDowngradeError",
    "setup_status",
    "require_no_downgrade",
    "require_setup",
    "python_identity",
]
