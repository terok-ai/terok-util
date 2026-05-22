# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for [`podman_userns_args`][terok_util.podman.podman_userns_args]."""

from __future__ import annotations

from unittest.mock import patch

from terok_util.podman import podman_userns_args


class TestPodmanUsernsArgs:
    """``podman_userns_args`` returns mapping args only rootless."""

    @patch("terok_util.podman.os.geteuid", return_value=1000)
    def test_rootless_emits_keep_id(self, _euid) -> None:
        """Non-zero euid yields the keep-id user namespace flag."""
        assert podman_userns_args() == ["--userns=keep-id:uid=1000,gid=1000"]

    @patch("terok_util.podman.os.geteuid", return_value=0)
    def test_root_emits_nothing(self, _euid) -> None:
        """Running as root yields no extra args."""
        assert podman_userns_args() == []
