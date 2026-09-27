# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Runtime-aware skip plugin: which marker skips in which runtime, and the report."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from terok_util import matrix_skip_plugin as plugin
from terok_util.matrix.catalog import KERNEL_ISOLATED_ENV, MATRIX_ENV, SLOT_ENV

#: The plugin imports RESULTS_MOUNT lazily from the catalog inside its hook, so
#: tests redirect the write by patching the catalog source, not the plugin.
_RESULTS_MOUNT_ATTR = "terok_util.matrix.catalog.RESULTS_MOUNT"

pytest_plugins = ["pytester"]


@pytest.mark.parametrize(
    ("markers", "krun", "expected"),
    [
        (["needs_krun"], False, "needs_krun"),  # needs own kernel, running under crun
        (["needs_krun"], True, None),  # runs under krun
        (["needs_podman"], True, None),  # podman MUST run under krun — never skipped
        (["needs_podman"], False, None),  # and under crun
        (["needs_loopback"], True, "needs_loopback"),  # TSI breaks loopback under krun
        (["needs_loopback"], False, None),
        (["needs_vm"], False, "needs_vm"),  # matrix container is never a full VM
        (["needs_vm"], True, "needs_vm"),
        (["slow"], True, None),  # unrelated marker never skips
        ([], True, None),
    ],
)
def test_skip_reason(
    monkeypatch: pytest.MonkeyPatch, markers: list[str], krun: bool, expected: str | None
) -> None:
    """Each runtime skips exactly the markers it cannot run, named by the marker."""
    monkeypatch.setattr(plugin, "_under_krun", lambda: krun)
    monkeypatch.setattr(plugin, "_in_matrix", lambda: True)
    assert plugin._skip_reason(set(markers)) == expected


def test_under_krun_detects_env_flag_and_pid1(monkeypatch: pytest.MonkeyPatch) -> None:
    """krun is seen from the matrix env flag, or PID 1's comm off-matrix."""
    monkeypatch.setattr(plugin, "_pid1_comm", lambda: "systemd")
    monkeypatch.setenv(KERNEL_ISOLATED_ENV, "1")
    assert plugin._under_krun()
    monkeypatch.delenv(KERNEL_ISOLATED_ENV, raising=False)
    assert not plugin._under_krun()
    monkeypatch.setattr(plugin, "_pid1_comm", lambda: plugin._KRUN_INIT_COMM)
    assert plugin._under_krun()


def test_needs_x86_skips_off_x86(monkeypatch: pytest.MonkeyPatch) -> None:
    """A needs_x86 test skips when the host is not x86_64, runs when it is."""
    monkeypatch.setattr(plugin.platform, "machine", lambda: "aarch64")
    assert plugin._skip_reason({"needs_x86"}) == "needs_x86"
    monkeypatch.setattr(plugin.platform, "machine", lambda: "x86_64")
    assert plugin._skip_reason({"needs_x86"}) is None


def _fake_item(*marker_names: str) -> object:
    """A collected-item stand-in exposing markers and recording skip marks."""
    added: list[object] = []
    return SimpleNamespace(
        iter_markers=lambda: [SimpleNamespace(name=n) for n in marker_names],
        add_marker=added.append,
        added=added,
        stash=pytest.Stash(),
    )


def _fake_config() -> pytest.Config:
    """A config stand-in carrying only the stash the plugin uses."""
    config = SimpleNamespace(stash=pytest.Stash())
    config.stash[plugin._SKIPS_KEY] = {}
    return config  # type: ignore[return-value]


def test_collection_marks_without_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collection marks unrunnable items without counting tests not yet executed."""
    monkeypatch.setenv(KERNEL_ISOLATED_ENV, "1")
    monkeypatch.setenv(MATRIX_ENV, "1")
    config = _fake_config()
    # Under krun: two loopback items skip; needs_podman and needs_krun both run.
    items = [
        _fake_item("needs_loopback"),
        _fake_item("needs_loopback"),
        _fake_item("needs_podman"),
        _fake_item("needs_krun"),
    ]
    plugin.pytest_collection_modifyitems(items)  # type: ignore[arg-type]

    added = [len(i.added) for i in items]  # type: ignore[attr-defined]
    assert added == [1, 1, 0, 0]  # only the two loopback items were skipped
    assert config.stash[plugin._SKIPS_KEY] == {}
    assert items[0].stash[plugin._MATRIX_REASON_KEY] == "needs_loopback"


def test_actual_pytest_skips_are_separated_from_matrix_skips(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real reports cover markers, fixtures, collection, xfail and deselection."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv(SLOT_ENV, "manjaro")
    pytester.makeconftest(
        f"""
        from terok_util import matrix_skip_plugin
        from terok_util.matrix import catalog
        catalog.RESULTS_MOUNT = {str(pytester.path)!r}
        matrix_skip_plugin._under_krun = lambda: False
        """
    )
    pytester.makepyfile(
        test_outcomes="""
        import pytest

        @pytest.mark.needs_krun
        def test_matrix(): pass

        @pytest.mark.needs_krun
        def test_deselected(): pass

        @pytest.mark.needs_krun
        @pytest.mark.skip(reason="explicit")
        def test_explicit(): pass

        @pytest.mark.skipif(True, reason="conditional")
        def test_conditional(): pass

        def test_dynamic(): pytest.skip("dynamic")

        @pytest.fixture
        def unavailable(): pytest.skip("fixture")

        def test_fixture(unavailable): pass

        @pytest.fixture
        def teardown_skip():
            yield
            pytest.skip("teardown")

        def test_multiple_phases(teardown_skip): pytest.skip("call")

        @pytest.mark.xfail
        def test_xfail(): assert False

        @pytest.mark.xfail(run=False)
        def test_not_run(): pass

        def test_pass(): pass
        """,
        test_collection="""
        import pytest
        pytest.skip("collection", allow_module_level=True)
        """,
    )

    result = pytester.runpytest_subprocess(
        "-p", "terok_util.matrix_skip_plugin", "-k", "not deselected", "-q"
    )

    assert result.ret == 0
    assert json.loads((pytester.path / "manjaro.skips.json").read_text()) == {
        "matrix": {"needs_krun": 1},
        "pytest": {
            "explicit": 1,
            "conditional": 1,
            "dynamic": 1,
            "fixture": 1,
            "call": 1,
            "collection": 1,
        },
    }


def test_sessionfinish_merges_counts_across_phases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each pytest invocation merges its counts into the slot's report file."""
    monkeypatch.setattr(_RESULTS_MOUNT_ATTR, str(tmp_path))
    monkeypatch.setenv(SLOT_ENV, "manjaro")

    def _run(skips: dict[str, tuple[str, str]]) -> None:
        config = _fake_config()
        config.stash[plugin._SKIPS_KEY] = skips
        plugin.pytest_sessionfinish(SimpleNamespace(config=config))  # type: ignore[arg-type]

    _run({"test_unit": ("matrix", "needs_krun")})
    _run(
        {"test_integration": ("matrix", "needs_krun"), "test_optional": ("pytest", "missing tool")}
    )

    report = json.loads((tmp_path / "manjaro.skips.json").read_text())
    assert report == {"matrix": {"needs_krun": 2}, "pytest": {"missing tool": 1}}


def test_sessionfinish_noop_without_slot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Off-matrix (no TEROK_SLOT) the rules still skip, but no file is written."""
    monkeypatch.setattr(_RESULTS_MOUNT_ATTR, str(tmp_path))
    monkeypatch.delenv(SLOT_ENV, raising=False)
    config = _fake_config()
    config.stash[plugin._SKIPS_KEY] = {"test_runtime": ("matrix", "needs_krun")}
    plugin.pytest_sessionfinish(SimpleNamespace(config=config))  # type: ignore[arg-type]
    assert not list(tmp_path.iterdir())


def test_sessionfinish_ignores_an_unknown_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A SLOT_ENV that is not a known catalog slot writes nothing (path allowlist)."""
    monkeypatch.setattr(_RESULTS_MOUNT_ATTR, str(tmp_path))
    monkeypatch.setenv(SLOT_ENV, "../../etc/not-a-slot")
    config = _fake_config()
    config.stash[plugin._SKIPS_KEY] = {"test_runtime": ("matrix", "needs_krun")}
    plugin.pytest_sessionfinish(SimpleNamespace(config=config))  # type: ignore[arg-type]
    assert not list(tmp_path.iterdir())
