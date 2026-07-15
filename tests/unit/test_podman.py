# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`podman_userns_args`][terok_util.podman.podman_userns_args]."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from terok_util.podman import _podman_version, podman_userns_args

KEEP_ID_ARGS = ["--userns=keep-id:uid=1000,gid=1000"]

EXPLICIT_MAP_ARGS = [
    *("--uidmap", "0:1:1000", "--uidmap", "1000:0:1", "--uidmap", "1001:1001:64535"),
    *("--gidmap", "0:1:1000", "--gidmap", "1000:0:1", "--gidmap", "1001:1001:64535"),
]


@pytest.fixture(autouse=True)
def _fresh_version_probe():
    """Reset the cached podman version around every test."""
    _podman_version.cache_clear()
    yield
    _podman_version.cache_clear()


def _probe_result(version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["podman"], returncode=0, stdout=f"{version}\n")


class TestPodmanUsernsArgs:
    """``podman_userns_args`` returns mapping args only rootless."""

    @patch("terok_util.podman.subprocess.run", return_value=_probe_result("5.2.1"))
    @patch("terok_util.podman.os.geteuid", return_value=1000)
    def test_rootless_emits_keep_id(self, _euid, _run) -> None:
        """Non-zero euid on a modern podman yields the keep-id flag."""
        assert podman_userns_args() == KEEP_ID_ARGS

    @patch("terok_util.podman.os.geteuid", return_value=0)
    def test_root_emits_nothing(self, _euid) -> None:
        """Running as root yields no extra args."""
        assert podman_userns_args() == []

    @patch("terok_util.podman.subprocess.run", return_value=_probe_result("3.4.4"))
    @patch("terok_util.podman.os.geteuid", return_value=1000)
    def test_pre_43_podman_gets_explicit_maps(self, _euid, _run) -> None:
        """Podman < 4.3 predates keep-id:uid= — explicit map triples instead."""
        assert podman_userns_args() == EXPLICIT_MAP_ARGS

    @patch("terok_util.podman.subprocess.run", return_value=_probe_result("4.3.0"))
    @patch("terok_util.podman.os.geteuid", return_value=1000)
    def test_43_boundary_gets_keep_id(self, _euid, _run) -> None:
        """Podman 4.3 is the first release with keep-id:uid= support."""
        assert podman_userns_args() == KEEP_ID_ARGS

    @patch("terok_util.podman.subprocess.run", side_effect=FileNotFoundError("podman"))
    @patch("terok_util.podman.os.geteuid", return_value=1000)
    def test_probe_failure_assumes_modern(self, _euid, _run) -> None:
        """A missing podman falls back to keep-id — its own error surfaces later."""
        assert podman_userns_args() == KEEP_ID_ARGS

    @patch("terok_util.podman.subprocess.run", return_value=_probe_result("noise"))
    @patch("terok_util.podman.os.geteuid", return_value=1000)
    def test_unparseable_version_assumes_modern(self, _euid, _run) -> None:
        """Garbage version output falls back to keep-id."""
        assert podman_userns_args() == KEEP_ID_ARGS

    @patch("terok_util.podman.subprocess.run", return_value=_probe_result("4.9.3"))
    @patch("terok_util.podman.os.geteuid", return_value=1000)
    def test_probe_runs_once(self, _euid, run) -> None:
        """The version probe result is cached across calls."""
        podman_userns_args()
        podman_userns_args()
        assert run.call_count == 1
