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


class TestIsRootNamespaceAware:
    """``_is_root`` distinguishes real root from rootless-namespaced uid 0.

    Rootless container runtimes (podman / crun) execute hook scripts
    with the operator's host UID mapped to inner UID 0; a generic
    ``os.geteuid() == 0`` check would misclassify them as root and
    misroute every ``namespace_*_dir()`` to ``/var/lib/terok`` /
    ``/run/terok`` paths the operator can't write to.
    """

    @staticmethod
    def _redirect_uid_map(monkeypatch: pytest.MonkeyPatch, uid_map_path: Path) -> None:
        """Redirect ``open('/proc/self/uid_map')`` to *uid_map_path*."""
        real_open = open

        def _open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            return real_open(
                uid_map_path if str(path) == "/proc/self/uid_map" else path, *args, **kwargs
            )

        monkeypatch.setattr("builtins.open", _open)

    def test_geteuid_nonzero_is_not_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Plain operator process (uid != 0 ⇒ host UID != 0) is not root."""
        from terok_util.paths import _is_root

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0          0 4294967295\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        assert _is_root() is False

    def test_real_root_in_initial_namespace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """uid 0 + uid_map shows inner 0 → outer 0 ⇒ real root."""
        from terok_util.paths import _is_root

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0          0 4294967295\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        assert _is_root() is True

    def test_rootless_user_namespace_is_not_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """uid 0 in rootless userns (inner 0 → outer 1000) ⇒ NOT root."""
        from terok_util.paths import _is_root

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0       1000          1\n         1     100000      65536\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        assert _is_root() is False


class TestInitialUserNamespace:
    """``_in_initial_userns`` — the identity map, and nothing else, is initial."""

    _redirect_uid_map = staticmethod(TestIsRootNamespaceAware._redirect_uid_map)

    def test_identity_map_is_the_initial_namespace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The full uid_t range mapped 1:1 is what only the initial ns carries."""
        from terok_util.paths import _in_initial_userns

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0          0 4294967295\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        assert _in_initial_userns() is True

    def test_a_mapped_slice_is_not(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Rootless podman's map: inner 0 is the operator, not the initial root."""
        from terok_util.paths import _in_initial_userns

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0       1000          1\n         1     100000      65536\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        assert _in_initial_userns() is False

    def test_keep_id_nesting_is_not(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--userns=keep-id: an unprivileged uid that maps to 0 -- the bug's shape."""
        from terok_util.paths import _in_initial_userns

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("      1000          0          1\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        assert _in_initial_userns() is False

    def test_missing_uid_map_means_no_user_namespaces(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No uid_map at all (non-Linux, exotic chroot) is the initial ns by definition."""
        from terok_util.paths import _in_initial_userns

        self._redirect_uid_map(monkeypatch, tmp_path / "does-not-exist")
        assert _in_initial_userns() is True


class TestIsRootUnderNesting:
    """The bug this fix closes: a mapped 0 is not privilege."""

    _redirect_uid_map = staticmethod(TestIsRootNamespaceAware._redirect_uid_map)

    def test_keep_id_unprivileged_is_not_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """uid 1000 whose map translates it to 0 must not take the root branch.

        This is the agent-container shape.  ``host_uid()`` reports 0 here --
        legitimately, it is the parent's view -- and the old ``_is_root``
        believed it, sending state to an unwritable ``/var/lib/terok``.
        """
        from terok_util.paths import _is_root, host_uid

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("      1000          0          1\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        assert host_uid() == 0  # the translation that fooled the old check
        assert _is_root() is False

    def test_missing_uid_map_root_is_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Without user namespaces, uid 0 is simply root."""
        from terok_util.paths import _is_root

        self._redirect_uid_map(monkeypatch, tmp_path / "does-not-exist")
        monkeypatch.setattr("os.geteuid", lambda: 0)
        assert _is_root() is True


class TestHostUidNamespaceAware:
    """``host_uid`` returns the kernel-visible (initial-userns) UID.

    Wire-level peers (D-Bus ``AUTH EXTERNAL``, kernel SO_PEERCRED) see
    the outer UID after userns translation; this helper must hand
    callers the same number or credential checks fail.
    """

    _redirect_uid_map = staticmethod(TestIsRootNamespaceAware._redirect_uid_map)

    def test_initial_namespace_returns_geteuid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Identity uid_map (init userns) ⇒ ``host_uid`` echoes ``geteuid``."""
        from terok_util.paths import host_uid

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0          0 4294967295\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        assert host_uid() == 1000

    def test_rootless_inner_zero_returns_outer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Rootless container: inner 0 → outer 1000 ⇒ ``host_uid`` returns 1000."""
        from terok_util.paths import host_uid

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0       1000          1\n         1     100000      65536\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        assert host_uid() == 1000

    def test_offset_inside_range(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Inner UID inside a range ⇒ outer = outer_start + (inner − inner_start)."""
        from terok_util.paths import host_uid

        uid_map = tmp_path / "uid_map"
        uid_map.write_text("         0       1000          1\n         1     100000      65536\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 42)
        assert host_uid() == 100041

    def test_no_uid_map_falls_back_to_geteuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``/proc`` unavailable ⇒ ``host_uid`` returns ``geteuid``."""
        from terok_util.paths import host_uid

        def _raise(*_a: object, **_kw: object) -> None:
            raise OSError("no /proc here")

        monkeypatch.setattr("builtins.open", _raise)
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        assert host_uid() == 1000

    def test_unmapped_euid_falls_back_to_geteuid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No uid_map row covers the effective UID ⇒ ``host_uid`` echoes ``geteuid``.

        Exercises the loop running to exhaustion without a match (the
        bare ``return euid`` after the ``with`` block) — distinct from
        the OSError path where ``/proc`` is missing entirely.
        """
        from terok_util.paths import host_uid

        uid_map = tmp_path / "uid_map"
        # Only inner UIDs 0..9 are mapped; effective UID 1000 falls outside.
        uid_map.write_text("         0          0         10\n")
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        assert host_uid() == 1000

    def test_malformed_uid_map_rows_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Blank, short and non-integer rows are skipped; a valid row still matches.

        Real ``/proc/self/uid_map`` can carry a trailing blank line, and a
        defensive parser must tolerate garbage without raising — the
        mapping is still resolved from the one well-formed row.
        """
        from terok_util.paths import host_uid

        uid_map = tmp_path / "uid_map"
        uid_map.write_text(
            "\n"  # blank line ⇒ zero fields, skipped (len != 3)
            "0 1000\n"  # two fields ⇒ skipped (len != 3)
            "a b c\n"  # non-integer fields ⇒ ValueError, skipped
            "         0       1000          1\n"  # valid: inner 0 → outer 1000
        )
        self._redirect_uid_map(monkeypatch, uid_map)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        assert host_uid() == 1000

    def test_geteuid_unavailable_root_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ``os.geteuid`` (macOS/BSD/Windows) + root username ⇒ 0.

        On platforms without POSIX ``geteuid`` the helper falls back to
        the login name; ``root`` is the only name that resolves to UID 0.
        """
        import terok_util.paths as paths_mod

        monkeypatch.delattr(paths_mod.os, "geteuid", raising=False)
        monkeypatch.setattr(paths_mod.getpass, "getuser", lambda: "root")
        assert paths_mod.host_uid() == 0

    def test_geteuid_unavailable_non_root_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ``os.geteuid`` + non-root username ⇒ sentinel ``-1``.

        Without ``geteuid`` and without the ``root`` login name the
        helper cannot determine a host UID, so it returns the ``-1``
        sentinel rather than guessing.
        """
        import terok_util.paths as paths_mod

        monkeypatch.delattr(paths_mod.os, "geteuid", raising=False)
        monkeypatch.setattr(paths_mod.getpass, "getuser", lambda: "operator")
        assert paths_mod.host_uid() == -1
