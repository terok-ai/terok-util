# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Workload files and output cannot redirect or exhaust the host-side reader."""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from terok_util.matrix import _files, cli, config, runner
from terok_util.matrix._files import read_regular, slot_output_dir
from terok_util.matrix.catalog import INPUTS_MOUNT, RESULTS_MOUNT, SYSTEMD_CONTROL_DIR
from unit.matrix_fixtures import load_fixture, minimal_yml, write_config


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("link_checkout", [False, True])
def test_declaration_accepts_linked_checkout_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool, link_checkout: bool
) -> None:
    """Host path aliases resolve before the repository's no-follow boundary starts."""
    root = tmp_path / "real" / "repo"
    path = write_config(root, minimal_yml())
    alias = tmp_path / "alias"
    target = root if link_checkout else root.parent
    alias.symlink_to(target, target_is_directory=True)
    requested = alias / path.relative_to(target)
    if relative:
        monkeypatch.chdir(tmp_path)
        requested = requested.relative_to(tmp_path)
    declaration = config.load_config(requested)
    assert declaration.repo_root == root
    assert declaration.containers_dir == path.parent
    fragments = path.parent / "fragments"
    fragments.mkdir()
    fragment = "RUN echo regular-fragment\n"
    (fragments / "Containerfile.debian13").write_text(fragment)
    assert runner.render_containerfile(declaration, "debian13").endswith(fragment)


@pytest.mark.parametrize(
    "filename", ["matrix.yml", "config/matrix.yml", "tests/containers/matrix.yml"]
)
def test_relative_declaration_keeps_its_inferred_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """Canonicalizing host prefixes preserves shallow and conventional config layouts."""
    monkeypatch.chdir(tmp_path)
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(minimal_yml())
    declaration = config.load_config(path)
    assert declaration.repo_root == tmp_path
    assert declaration.containers_dir == tmp_path / path.parent


@pytest.mark.parametrize("component", ["tests", "containers"])
def test_declaration_rejects_linked_directories_inside_checkout(
    tmp_path: Path, component: str
) -> None:
    """Canonicalizing the host root must not follow repository-controlled parent links."""
    root = tmp_path / "repo"
    path = write_config(root, minimal_yml())
    directory = root / "tests" if component == "tests" else path.parent
    outside = tmp_path / "outside"
    directory.rename(outside)
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(config.MatrixConfigError, match="cannot read"):
        config.load_config(path)


def test_cli_reads_results_under_linked_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_podman: Path
) -> None:
    """A host TMPDIR alias does not erase version/skips or fail a completed booted slot."""
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    alias = tmp_path / "tmpdir"
    alias.symlink_to(temporary, target_is_directory=True)
    monkeypatch.setattr(cli.tempfile, "tempdir", str(alias))
    monkeypatch.setattr(cli, "_host_confines_pasta", lambda: False)
    monkeypatch.setattr(cli, "_warn_kernel_keyring", lambda: None)
    monkeypatch.setattr(runner, "_stream", lambda *a, **k: (0, None))

    def run_matrix(declaration, targets, args, workspace):
        output = slot_output_dir(workspace, "debian13")
        output.mkdir(parents=True)
        skips = {"matrix": {"unavailable": 2}, "pytest": {}}
        (output / "debian13.skips.json").write_text(json.dumps(skips))
        (output / "debian13.podman-version").write_text("5.4.2")
        (output / "debian13.exit").write_text("0")
        assert runner._observed_version("debian13", workspace) == "5.4.2"
        assert cli._read_slot_skips(workspace, "debian13") == skips
        assert runner._run_in_booted_slot(declaration, "debian13", workspace, "")[0] == 0
        assert workspace.parent == temporary
        return 0

    run = Mock(side_effect=run_matrix)
    monkeypatch.setattr(cli, "_run_matrix", run)
    path = write_config(tmp_path / "repo", minimal_yml())
    assert cli.main(["--config", str(path)]) == 0
    run.assert_called_once()
    assert not list(temporary.iterdir())


@pytest.mark.parametrize("kind", ["symlink", "parent-link", "fifo", "directory", "large", "binary"])
def test_result_readers_reject_unsafe_files(tmp_path: Path, kind: str) -> None:
    """Every result consumer treats unsafe workload files as missing, without blocking."""
    output = slot_output_dir(tmp_path, "debian13")
    output.mkdir(parents=True)
    secret = tmp_path / "private"
    secret.write_text("0")
    if kind == "parent-link":
        other = tmp_path / "redirected"
        other.mkdir()
        output.rmdir()
        output.symlink_to(other, target_is_directory=True)
    for filename in ("debian13.podman-version", "debian13.skips.json", "debian13.exit"):
        path = output / filename
        match kind:
            case "symlink":
                path.symlink_to(secret)
            case "fifo":
                os.mkfifo(path)
            case "directory":
                path.mkdir()
            case "large":
                path.write_text("x" * (_files.MAX_RESULT_BYTES + 1))
            case "binary":
                path.write_bytes(b"\xff")
            case _:
                path.write_text("0")
        with pytest.raises((OSError, UnicodeError)):
            read_regular(path)
    assert runner._observed_version("debian13", tmp_path) == "?"
    assert cli._read_slot_skips(tmp_path, "debian13") == {}
    assert secret.read_text() == "0"


def test_result_growth_after_stat_is_still_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Growing an already-open file cannot evade its byte limit."""
    path = tmp_path / "result"
    path.write_text("ok")
    original_stat = os.fstat

    def grow_after_stat(descriptor: int):
        metadata = original_stat(descriptor)
        path.write_text("this no longer fits")
        return metadata

    monkeypatch.setattr(_files.os, "fstat", grow_after_stat)
    with pytest.raises(OSError, match="exceeds"):
        read_regular(path, limit=4)


def test_generated_inputs_and_slot_outputs_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel slots share only the source mount, never writable scripts or outputs."""
    declaration = load_fixture(tmp_path / "repo")
    workspace = tmp_path / "scratch"
    workspace.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "_stream", lambda *a: (0, None))
    for name in ("debian13", "alpine"):
        assert runner.run_slot(declaration, name, workspace).passed
        output = slot_output_dir(workspace, name)
        assert output.stat().st_mode & 0o777 == 0o777
        # A workload can forge input-looking files only in its output directory.
        (output / f"inner-{name}.sh").symlink_to(tmp_path / "must-not-be-written")
        argv = runner._run_argv(declaration, name, workspace, boots_systemd=True)
        assert f"{workspace}/inputs/{name}:{INPUTS_MOUNT}:ro,z" in argv
        assert f"{output}:{RESULTS_MOUNT}:rw,z" in argv
        assert f"{workspace}/inputs/{name}/systemd:{SYSTEMD_CONTROL_DIR}:ro,z" in argv
        assert f"{workspace}:{RESULTS_MOUNT}:rw,z" not in argv
        assert "uv sync --locked" in (workspace / "inputs" / name / f"inner-{name}.sh").read_text()
    assert not (tmp_path / "must-not-be-written").exists()
    assert workspace.stat().st_mode & 0o777 == 0o700


def test_repository_commands_remain_inside_generated_scripts(tmp_path: Path) -> None:
    """A declaration's shell commands are not executed when preparing its scripts."""
    marker = tmp_path / "must-not-exist"
    command = f"touch {marker}"
    declaration = load_fixture(
        tmp_path / "repo", minimal_yml().replace("pytest: tests/ -v", f"run: ['{command}']")
    )
    runner._write_scripts(declaration, "debian13", tmp_path / "scratch", "all", False)
    assert command in (tmp_path / "scratch/inputs/debian13/inner-debian13.sh").read_text()
    assert not marker.exists()


@pytest.mark.parametrize("status", ["symlink", "9" * 300, "²", "\udcff"])
def test_boot_status_rejects_unsafe_or_non_ascii_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A forged boot-status artifact cannot escape its output directory or crash int()."""
    declaration = load_fixture(tmp_path / "repo")
    output = slot_output_dir(tmp_path, "debian13")
    output.mkdir(parents=True)
    path = output / "debian13.exit"
    if status == "symlink":
        secret = tmp_path / "secret"
        secret.write_text("0")
        path.symlink_to(secret)
    else:
        path.write_text(status, errors="surrogateescape")
    monkeypatch.setattr(runner, "_stream", lambda *a, **k: (0, None))
    assert runner._run_in_booted_slot(declaration, "debian13", tmp_path, "")[0] == 1


def test_deeply_nested_skip_json_is_ignored(tmp_path: Path) -> None:
    """The bounded file reader also tolerates JSON nesting beyond the parser limit."""
    output = slot_output_dir(tmp_path, "debian13")
    output.mkdir(parents=True)
    (output / "debian13.skips.json").write_text("[" * 2000 + "0" + "]" * 2000)
    assert cli._read_slot_skips(tmp_path, "debian13") == {}


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("image-prefix: t", "image-prefix: {nested: [not, a, string]}"),
        ("image-prefix: t", "image-prefix: true"),
        ("image-prefix: t", "image-prefix: ../escape"),
        ("image-prefix: t", "image-prefix: --option"),
        ("flavor: podman", "flavor: 1"),
        ("  debian13:", "  debian13: false"),
        ("  debian13:", "  debian13:\n    groups: [false]"),
        ("  debian13:", "  debian13:\n    skip: []"),
        ("  debian13:", "  debian13:\n    phases: false"),
        ("  debian13:", "  debian13:\n    extra-packages: ['--config=/other']"),
        ("  debian13:", "  true:"),
        ("name: all", "name: [not, a, string]"),
        ("pytest: tests/ -v", "run: [{not: a-string}]"),
        ("image-prefix: t", "true: t"),
    ],
)
def test_declaration_fields_are_typed_not_coerced(
    tmp_path: Path, original: str, replacement: str
) -> None:
    """Malformed nested values are rejected rather than recursively stringified."""
    with pytest.raises(config.MatrixConfigError):
        load_fixture(tmp_path, minimal_yml().replace(original, replacement))


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("image-prefix: &cycle [*cycle]", "aliases"),
        ("a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]", "aliases"),
        ("a: " + "[" * 20 + "x" + "]" * 20, "nesting"),
        ("a: [" + ",".join("x" for _ in range(5000)) + "]", "event"),
        ("#" * (config._MAX_DECLARATION_BYTES + 1), "bounded regular"),
        ("a: [", "cannot read"),
    ],
)
def test_declaration_limits_precede_object_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, reason: str
) -> None:
    """Aliases, excessive structure and oversized files never reach YAML construction."""
    construct = Mock(side_effect=AssertionError("unsafe construction"))
    monkeypatch.setattr(config, "yaml_load", construct)
    with pytest.raises(config.MatrixConfigError, match=reason):
        config.load_config(write_config(tmp_path, text))
    construct.assert_not_called()


def test_null_optional_declarations_keep_their_defaults(tmp_path: Path) -> None:
    """Optional empty mappings and phase lists still accept YAML null."""
    declaration = load_fixture(
        tmp_path, "image-prefix: t\nflavor: podman\nslots: null\nphases: null\n"
    )
    assert declaration.slots == {}
    assert declaration.phases == ()


@pytest.mark.parametrize("kind", ["config", "fragment", "fragment-directory"])
def test_source_declarations_cannot_follow_host_links(tmp_path: Path, kind: str) -> None:
    """Neither a declaration nor an optional fragment can read another host path."""
    declaration = load_fixture(tmp_path / "repo")
    secret = tmp_path / "secret"
    secret.write_text("RUN touch /marker\n")
    fragments = declaration.containers_dir / "fragments"
    if kind == "config":
        path = declaration.containers_dir / "matrix.yml"
        path.unlink()
        path.symlink_to(secret)
        with pytest.raises(config.MatrixConfigError):
            config.load_config(path)
    else:
        if kind == "fragment-directory":
            fragments.symlink_to(tmp_path)
        else:
            fragments.mkdir()
        (fragments / "Containerfile.debian13").symlink_to(secret)
        with pytest.raises(OSError):
            runner.render_containerfile(declaration, "debian13")


def test_workload_stream_without_newlines_uses_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch, host_podman: Path
) -> None:
    """An unterminated line cannot accumulate unbounded runner memory."""
    content = "x" * (runner._STREAM_CHUNK_CHARS * 3 + 7)
    stream = StringIO(content)
    read = Mock(side_effect=stream.readline)
    process = MagicMock()
    process.__enter__.return_value = process
    process.stdout = SimpleNamespace(readline=read)
    process.wait.return_value = 0
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    output = Mock()
    prefix = "[slot] "
    with monkeypatch.context() as context:
        context.setattr(runner.sys, "stdout", output)
        assert runner._stream([str(host_podman), "run"], prefix) == (0, None)
    chunks = [call.args[0] for call in output.write.call_args_list]
    assert "".join(chunks) == prefix + content
    assert max(map(len, chunks)) <= runner._STREAM_CHUNK_CHARS + len(prefix)
    assert len(chunks) == 4
    assert all(call.args == (runner._STREAM_CHUNK_CHARS,) for call in read.call_args_list)
