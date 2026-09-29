# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Publish matrix progress as an atomic JSON snapshot, independent of terminal output."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SlotReport:
    """A slot's current phase and eventual outcome; duration includes its build."""

    state: str = "pending"
    reason: str = ""
    observed: str = "?"
    network_hint: str | None = None
    duration: float = 0
    skips: dict[str, dict[str, int]] | None = None


class MatrixReport:
    """Write snapshots after each transition so readers can follow an unfinished run.

    A missing destination disables file output. Readers see either the old
    snapshot or the new one, never half a JSON document.
    """

    def __init__(self, path: Path | None, slots: list[str]) -> None:
        """Publish the initial pending slots before execution starts."""
        self.path = path
        self.slots = {name: SlotReport() for name in slots}
        self.state = "running"
        self.exit_code: int | None = None
        self.error = ""
        self._started = time.monotonic()
        self._slot_started: dict[str, float] = {}
        self._lock = threading.Lock()
        self._write()

    def update(
        self,
        name: str,
        state: str,
        *,
        reason: str = "",
        observed: str = "?",
        network_hint: str | None = None,
        skips: dict[str, dict[str, int]] | None = None,
    ) -> None:
        """Record a build, test, or verdict from either serial or parallel execution."""
        with self._lock:
            now = time.monotonic()
            started = self._slot_started.setdefault(name, now)
            self.slots[name] = SlotReport(
                state, reason, observed, network_hint, now - started, skips
            )
            self._write()

    def finish(self, exit_code: int, error: str = "") -> None:
        """Close the report, retaining completed verdicts after errors or interruption."""
        with self._lock:
            self.exit_code = exit_code
            self.error = error
            self.state = {0: "passed", 1: "failed", 130: "cancelled"}.get(exit_code, "error")
            for slot in self.slots.values():
                if slot.state in {"pending", "building", "built", "testing"}:
                    if exit_code:
                        slot.state = "cancelled" if exit_code == 130 else "error"
                        slot.reason = error or "matrix ended before this slot completed"
            self._write()

    def _write(self) -> None:
        """Replace the destination beside its temporary sibling, on the same filesystem."""
        if self.path is None:
            return
        data = {
            "schema_version": 1,
            "state": self.state,
            "exit_code": self.exit_code,
            "error": self.error,
            "duration": time.monotonic() - self._started,
            "slots": {name: asdict(slot) for name, slot in self.slots.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
