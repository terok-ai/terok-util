# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for path resolution functions in [`terok_util.paths`][terok_util.paths]."""

from __future__ import annotations

import os
import unittest.mock
from pathlib import Path

import pytest

from terok_util.paths import (
    namespace_config_dir,
    namespace_runtime_dir,
    namespace_state_dir,
)

# Test-only mock filesystem prefix.  All synthetic paths live here so we
# never poke at real directories — mirrors the convention in the sibling
# packages' ``tests/constants.py``.
MOCK_BASE = Path("/tmp/terok-util-testing")  # nosec B108 — synthetic test-only path

_NAMESPACE_ROOT = "terok"


class TestNamespaceRoot:
    """``TEROK_ROOT`` env var moves the namespace state root for all subdirs."""

    def test_terok_root_env_overrides_platform_default(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"TEROK_ROOT": str(MOCK_BASE / "custom")}):
            assert namespace_state_dir("sandbox") == MOCK_BASE / "custom" / "sandbox"
            assert namespace_state_dir("agent") == MOCK_BASE / "custom" / "agent"
            assert namespace_state_dir() == MOCK_BASE / "custom"

    def test_package_env_overrides_terok_root(self) -> None:
        """Package-specific env var beats ``TEROK_ROOT``."""
        with unittest.mock.patch.dict(
            os.environ,
            {"TEROK_ROOT": str(MOCK_BASE / "root"), "MY_PKG": str(MOCK_BASE / "pkg")},
        ):
            assert namespace_state_dir("x", env_var="MY_PKG") == MOCK_BASE / "pkg"

    def test_config_yml_paths_root_relocates_state_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``paths.root`` in ``config.yml`` relocates the state tree (Podman model).

        Operator-facing behaviour: setting ``paths.root: /virt/terok``
        in the user config moves every sibling's state subdir without
        having to export ``TEROK_ROOT`` on every shell.
        """
        from terok_util.paths import _reset_config_caches_for_tests

        cfg = tmp_path / "config.yml"
        relocated = tmp_path / "virt-terok"
        cfg.write_text(f"paths:\n  root: {relocated}\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        monkeypatch.delenv("TEROK_ROOT", raising=False)
        _reset_config_caches_for_tests()

        assert namespace_state_dir("sandbox") == relocated / "sandbox"
        assert namespace_state_dir("agent") == relocated / "agent"
        assert namespace_state_dir() == relocated

    def test_terok_root_env_beats_config_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TEROK_ROOT`` env var takes precedence over ``paths.root`` in config."""
        from terok_util.paths import _reset_config_caches_for_tests

        cfg = tmp_path / "config.yml"
        cfg.write_text(f"paths:\n  root: {tmp_path / 'from-config'}\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        monkeypatch.setenv("TEROK_ROOT", str(tmp_path / "from-env"))
        _reset_config_caches_for_tests()

        assert namespace_state_dir("sandbox") == tmp_path / "from-env" / "sandbox"


class TestReadConfigSection:
    """[`read_config_section`][terok_util.paths.read_config_section]."""

    def test_reads_section_from_user_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A nested mapping under a top-level key resolves to a stringified dict."""
        from terok_util.paths import _reset_config_caches_for_tests, read_config_section

        cfg = tmp_path / "config.yml"
        cfg.write_text("services:\n  mode: tcp\n  gate_port: 8123\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        section = read_config_section("services")
        assert section == {"mode": "tcp", "gate_port": "8123"}

    def test_missing_section_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A section that isn't in the config files resolves to ``{}``."""
        from terok_util.paths import _reset_config_caches_for_tests, read_config_section

        cfg = tmp_path / "config.yml"
        cfg.write_text("other:\n  unrelated: value\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        assert read_config_section("services") == {}

    def test_corrupt_config_fails_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed config file resolves to ``{}`` instead of raising."""
        from terok_util.paths import _reset_config_caches_for_tests, read_config_section

        cfg = tmp_path / "config.yml"
        cfg.write_text("services:\n  mode: : : invalid yaml\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        assert read_config_section("services") == {}

    def test_user_section_overlays_system(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User config keys override system config keys at the leaf level.

        Uses the explicit override file to simulate layering without
        having to write into ``/etc/`` (which the test fixtures don't
        allow).  The override slot replaces *both* system and user.
        """
        from terok_util.paths import _reset_config_caches_for_tests, read_config_section

        cfg = tmp_path / "config.yml"
        cfg.write_text("services:\n  mode: tcp\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        assert read_config_section("services") == {"mode": "tcp"}


class TestReadConfigTopLevel:
    """[`read_config_top_level`][terok_util.paths.read_config_top_level]."""

    def test_reads_scalar_at_top_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Top-level scalar (e.g. ``experimental: true``) is returned as-is."""
        from terok_util.paths import _reset_config_caches_for_tests, read_config_top_level

        cfg = tmp_path / "config.yml"
        cfg.write_text("experimental: true\nlog_level: debug\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        assert read_config_top_level("experimental") is True
        assert read_config_top_level("log_level") == "debug"

    def test_missing_key_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key not present in any config file resolves to ``None``."""
        from terok_util.paths import _reset_config_caches_for_tests, read_config_top_level

        cfg = tmp_path / "config.yml"
        cfg.write_text("other: value\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        assert read_config_top_level("experimental") is None

    def test_reads_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Top-level lists round-trip as Python lists."""
        from terok_util.paths import _reset_config_caches_for_tests, read_config_top_level

        cfg = tmp_path / "config.yml"
        cfg.write_text("profiles:\n  - default\n  - dev\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        assert read_config_top_level("profiles") == ["default", "dev"]

    def test_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeat reads come from cache; rewriting the file underneath has no effect.

        Production code expects config to be read once per process.  A
        rewrite-and-reread workflow is a test-side concern (use
        ``_reset_config_caches_for_tests``).
        """
        from terok_util.paths import _reset_config_caches_for_tests, read_config_top_level

        cfg = tmp_path / "config.yml"
        cfg.write_text("flag: first\n")
        monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg))
        _reset_config_caches_for_tests()

        assert read_config_top_level("flag") == "first"
        cfg.write_text("flag: second\n")
        assert read_config_top_level("flag") == "first"  # cached
        _reset_config_caches_for_tests()
        assert read_config_top_level("flag") == "second"


class TestNamespaceStateDir:
    """[`namespace_state_dir`][terok_util.paths.namespace_state_dir]."""

    def test_no_subdir_returns_namespace_root(self) -> None:
        """Empty subdir returns the bare terok/ data root."""
        result = namespace_state_dir()
        assert result.name == _NAMESPACE_ROOT

    def test_subdir_appended(self) -> None:
        """Subdir is appended to the namespace root."""
        result = namespace_state_dir("agent")
        assert result.name == "agent"
        assert result.parent.name == _NAMESPACE_ROOT

    def test_env_var_override(self) -> None:
        """Specific env var takes precedence over platform default."""
        with unittest.mock.patch.dict(os.environ, {"MY_STATE": str(MOCK_BASE / "custom-state")}):
            assert namespace_state_dir("agent", env_var="MY_STATE") == MOCK_BASE / "custom-state"

    def test_env_var_none_ignored(self) -> None:
        """When env_var is None, no env lookup is performed."""
        result = namespace_state_dir("sandbox")
        assert isinstance(result, Path)

    def test_root_user(self) -> None:
        """Root user gets ``/var/lib/terok/<subdir>``."""
        with unittest.mock.patch("terok_util.paths._is_root", return_value=True):
            assert namespace_state_dir("sandbox") == Path("/var/lib/terok/sandbox")
            assert namespace_state_dir() == Path("/var/lib/terok")

    def test_tilde_expansion(self) -> None:
        """Env var values with ``~`` are expanded."""
        with unittest.mock.patch.dict(os.environ, {"MY_STATE": "~/terok-data"}):
            result = namespace_state_dir("x", env_var="MY_STATE")
            assert "~" not in str(result)
            assert result.is_absolute()

    def test_absolute_subdir_rejected(self) -> None:
        """Absolute subdir paths are rejected to prevent namespace escape."""
        with pytest.raises(ValueError, match="relative"):
            namespace_state_dir("/tmp/evil")

    def test_parent_traversal_rejected(self) -> None:
        """Parent traversal in subdir is rejected."""
        with pytest.raises(ValueError, match="relative"):
            namespace_state_dir("../escape")


class TestNamespaceConfigDir:
    """[`namespace_config_dir`][terok_util.paths.namespace_config_dir]."""

    def test_no_subdir_returns_namespace_root(self) -> None:
        """Empty subdir returns the bare terok/ config root."""
        result = namespace_config_dir()
        assert result.name == _NAMESPACE_ROOT

    def test_subdir_appended(self) -> None:
        """Subdir is appended to the config namespace root."""
        result = namespace_config_dir("agent")
        assert result.name == "agent"
        assert result.parent.name == _NAMESPACE_ROOT

    def test_env_var_override(self) -> None:
        """Specific env var takes precedence over platform default."""
        with unittest.mock.patch.dict(os.environ, {"MY_CFG": str(MOCK_BASE / "custom-cfg")}):
            assert namespace_config_dir("agent", env_var="MY_CFG") == MOCK_BASE / "custom-cfg"

    def test_root_user(self) -> None:
        """Root user gets ``/etc/terok/<subdir>``."""
        with unittest.mock.patch("terok_util.paths._is_root", return_value=True):
            assert namespace_config_dir("sandbox") == Path("/etc/terok/sandbox")
            assert namespace_config_dir() == Path("/etc/terok")


class TestNamespaceRuntimeDir:
    """[`namespace_runtime_dir`][terok_util.paths.namespace_runtime_dir]."""

    def test_no_subdir_returns_namespace_root(self) -> None:
        """Empty subdir returns the bare terok/ runtime root."""
        result = namespace_runtime_dir()
        assert result.name == _NAMESPACE_ROOT

    def test_subdir_appended(self) -> None:
        """Subdir is appended to the runtime namespace root."""
        result = namespace_runtime_dir("sandbox")
        assert result.name == "sandbox"
        assert result.parent.name == _NAMESPACE_ROOT

    def test_env_var_override(self) -> None:
        """Specific env var takes precedence."""
        with unittest.mock.patch.dict(os.environ, {"MY_RUN": str(MOCK_BASE / "custom-run")}):
            assert namespace_runtime_dir("x", env_var="MY_RUN") == MOCK_BASE / "custom-run"

    def test_root_user(self) -> None:
        """Root user gets ``/run/terok/<subdir>``."""
        with unittest.mock.patch("terok_util.paths._is_root", return_value=True):
            assert namespace_runtime_dir("sandbox") == Path("/run/terok/sandbox")
            assert namespace_runtime_dir() == Path("/run/terok")

    def test_xdg_runtime_dir_fallback(self) -> None:
        """``XDG_RUNTIME_DIR`` is used when available (non-root)."""
        with (
            unittest.mock.patch.dict(
                os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=False
            ),
            unittest.mock.patch("terok_util.paths._is_root", return_value=False),
        ):
            assert namespace_runtime_dir("sandbox") == Path("/run/user/1000/terok/sandbox")

    def test_xdg_state_home_fallback(self) -> None:
        """``XDG_STATE_HOME`` is used when ``XDG_RUNTIME_DIR`` is absent."""
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env["XDG_STATE_HOME"] = str(MOCK_BASE / "state-home")
        with (
            unittest.mock.patch.dict(os.environ, env, clear=True),
            unittest.mock.patch("terok_util.paths._is_root", return_value=False),
        ):
            assert (
                namespace_runtime_dir("sandbox") == MOCK_BASE / "state-home" / "terok" / "sandbox"
            )
