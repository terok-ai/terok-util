# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the [`CommandTree`][terok_util.cli_types.CommandTree] composition helpers."""

from __future__ import annotations

import argparse
import os.path
import sys

import pytest

from terok_util.cli_types import (
    ArgDef,
    CommandDef,
    CommandTree,
    KeyRow,
    LazyHandler,
    _resolve_handler,
)


def _leaf(name: str, handler=None) -> CommandDef:
    """A leaf node with a no-op handler unless overridden."""
    return CommandDef(name=name, help=f"{name} verb", handler=handler or (lambda: None))


def _group(name: str, children: tuple[CommandDef, ...]) -> CommandDef:
    """A group node with no handler and the given children."""
    return CommandDef(name=name, help=f"{name} group", children=children)


class TestCommandDef:
    """The dataclass + helpers stay pure-data; no side effects on construction."""

    def test_leaf_is_not_group(self) -> None:
        assert _leaf("x").is_group is False

    def test_group_is_group(self) -> None:
        assert _group("g", (_leaf("x"),)).is_group is True

    def test_with_handler_returns_new_instance(self) -> None:
        original = _leaf("x")

        def new_handler() -> None:
            pass

        rewrapped = original.with_handler(new_handler)
        assert rewrapped.handler is new_handler
        assert original.handler is not new_handler  # unchanged

    def test_with_children_returns_new_instance(self) -> None:
        original = _group("g", (_leaf("a"),))
        new = original.with_children((_leaf("b"),))
        assert [c.name for c in new.children] == ["b"]
        assert [c.name for c in original.children] == ["a"]


class TestFindAt:
    """[`CommandTree.find_at`][terok_util.cli_types.CommandTree.find_at] walks paths."""

    def _build(self) -> CommandTree:
        return CommandTree(
            [
                _group("vault", (_leaf("start"), _group("passphrase", (_leaf("seal"),)))),
                _leaf("doctor"),
            ]
        )

    def test_finds_top_level_leaf(self) -> None:
        assert self._build().find_at(("doctor",)).name == "doctor"

    def test_finds_nested_leaf(self) -> None:
        assert self._build().find_at(("vault", "passphrase", "seal")).name == "seal"

    def test_finds_group(self) -> None:
        node = self._build().find_at(("vault", "passphrase"))
        assert node.is_group

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(KeyError, match="empty path"):
            self._build().find_at(())

    def test_unknown_top_level(self) -> None:
        with pytest.raises(KeyError, match="no top-level verb 'bogus'"):
            self._build().find_at(("bogus",))

    def test_unknown_descendant(self) -> None:
        with pytest.raises(KeyError, match="'nope' not found under 'vault'"):
            self._build().find_at(("vault", "nope"))


class TestOverlay:
    """Handler overlay preserves identity for untouched subtrees — the load-bearing property."""

    def test_replaces_handler_at_path(self) -> None:
        tree = CommandTree([_group("g", (_leaf("a"), _leaf("b")))])

        def new_a() -> None:
            pass

        overlaid = tree.overlay({("g", "a"): new_a})
        assert overlaid.find_at(("g", "a")).handler is new_a

    def test_unrelated_sibling_preserves_identity(self) -> None:
        """The other child in the same group is the SAME CommandDef instance."""
        original_b = _leaf("b")
        tree = CommandTree([_group("g", (_leaf("a"), original_b))])

        def new_a() -> None:
            pass

        overlaid = tree.overlay({("g", "a"): new_a})
        assert overlaid.find_at(("g", "b")) is original_b

    def test_untouched_top_root_preserves_identity(self) -> None:
        """An unrelated top-level root shares identity with the input."""
        untouched = _leaf("untouched")
        tree = CommandTree([_group("g", (_leaf("a"),)), untouched])

        def new_a() -> None:
            pass

        overlaid = tree.overlay({("g", "a"): new_a})
        assert overlaid.roots[1] is untouched

    def test_no_match_is_pure_identity_passthrough(self) -> None:
        """An overlay with no matching paths preserves every node's identity."""
        tree = CommandTree([_group("g", (_leaf("a"),))])
        overlaid = tree.overlay({("nowhere",): lambda: None})
        # Every node identical to the input — the propagation rule
        # the consumer's shortcut-by-identity story depends on.
        assert overlaid.roots[0] is tree.roots[0]
        assert overlaid.roots[0].children is tree.roots[0].children


class TestExtendAt:
    """[`CommandTree.extend_at`][terok_util.cli_types.CommandTree.extend_at] appends children."""

    def test_appends_to_top_level(self) -> None:
        tree = CommandTree([_leaf("a")])
        extended = tree.extend_at((), [_leaf("b")])
        assert [c.name for c in extended] == ["a", "b"]

    def test_appends_to_named_group(self) -> None:
        tree = CommandTree([_group("g", (_leaf("a"),))])
        extended = tree.extend_at(("g",), [_leaf("b")])
        assert [c.name for c in extended.find_at(("g",)).children] == ["a", "b"]

    def test_appends_to_deep_path(self) -> None:
        """Multi-level paths descend through the tree to the target group."""
        tree = CommandTree(
            [_group("outer", (_group("inner", (_leaf("a"),)),))],
        )
        extended = tree.extend_at(("outer", "inner"), [_leaf("b")])
        names = [c.name for c in extended.find_at(("outer", "inner")).children]
        assert names == ["a", "b"]

    def test_unknown_path_raises(self) -> None:
        tree = CommandTree([_leaf("a")])
        with pytest.raises(KeyError, match="bogus"):
            tree.extend_at(("bogus",), [_leaf("x")])

    def test_refuses_to_extend_a_leaf(self) -> None:
        """Extending a leaf would produce a handler+children hybrid argparse can't model."""
        tree = CommandTree([_leaf("a")])
        with pytest.raises(TypeError, match="cannot extend leaf 'a'"):
            tree.extend_at(("a",), [_leaf("b")])


class TestForestMechanics:
    """[`CommandTree`][terok_util.cli_types.CommandTree] is iterable, sized, addable."""

    def test_len(self) -> None:
        assert len(CommandTree([_leaf("a"), _leaf("b"), _leaf("c")])) == 3

    def test_add_concatenates_trees(self) -> None:
        a = CommandTree([_leaf("a")])
        b = CommandTree([_leaf("b")])
        combined = a + b
        assert [c.name for c in combined] == ["a", "b"]

    def test_add_accepts_iterable(self) -> None:
        a = CommandTree([_leaf("a")])
        combined = a + [_leaf("b")]
        assert [c.name for c in combined] == ["a", "b"]


class TestWalk:
    """Depth-first traversal yields every node with its full path."""

    def test_walks_depth_first(self) -> None:
        tree = CommandTree([_group("g", (_leaf("a"), _leaf("b"))), _leaf("c")])
        paths = [path for path, _ in tree.walk()]
        assert paths == [("g",), ("g", "a"), ("g", "b"), ("c",)]


class TestWireAndDispatch:
    """End-to-end: wire a tree, parse args, dispatch — async handlers run too."""

    def test_dispatch_invokes_sync_handler(self) -> None:
        called: list[int] = []

        def handler() -> None:
            called.append(1)

        tree = CommandTree([_leaf("v", handler=handler)])
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        args = parser.parse_args(["v"])
        CommandTree.dispatch(args)
        assert called == [1]

    def test_dispatch_invokes_async_handler(self) -> None:
        """Async handlers detected via ``inspect.iscoroutine`` and run via ``asyncio.run``."""
        called: list[int] = []

        async def handler() -> None:
            called.append(1)

        tree = CommandTree([_leaf("v", handler=handler)])
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        args = parser.parse_args(["v"])
        CommandTree.dispatch(args)
        assert called == [1]

    def test_slash_separated_arg_names_expand_to_short_plus_long(self) -> None:
        """``ArgDef(name="-t/--timeout", ...)`` registers both flags under one dest."""
        captured: dict[str, int] = {}

        def handler(*, timeout: int = -1) -> None:
            captured["timeout"] = timeout

        cmd = CommandDef(
            name="v",
            help="v",
            handler=handler,
            args=(ArgDef(name="-t/--timeout", dest="timeout", type=int, default=-1),),
        )
        tree = CommandTree([cmd])
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        # Both the short and long form parse to the same ``dest``.
        for argv in (["v", "-t", "42"], ["v", "--timeout", "42"]):
            args = parser.parse_args(argv)
            CommandTree.dispatch(args)
            assert captured == {"timeout": 42}

    def test_slash_separated_names_derive_dest_from_longest_form(self) -> None:
        """Without explicit ``dest=``, slash-names pick the long form — matches argparse's convention."""
        captured: dict[str, int] = {}

        def handler(*, timeout: int = -1) -> None:
            captured["timeout"] = timeout

        cmd = CommandDef(
            name="v",
            help="v",
            handler=handler,
            args=(ArgDef(name="-t/--timeout", type=int, default=-1),),
        )
        tree = CommandTree([cmd])
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        args = parser.parse_args(["v", "--timeout", "42"])
        CommandTree.dispatch(args)
        assert captured == {"timeout": 42}

    def test_dispatch_passes_arg_kwargs(self) -> None:
        captured: dict[str, str] = {}

        def handler(*, name: str = "") -> None:
            captured["name"] = name

        cmd = CommandDef(
            name="v",
            help="v",
            handler=handler,
            args=(ArgDef(name="--name", default="default"),),
        )
        tree = CommandTree([cmd])
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        args = parser.parse_args(["v", "--name", "set"])
        CommandTree.dispatch(args)
        assert captured == {"name": "set"}

    def test_nested_subgroup_routes_via_argparse(self) -> None:
        called: list[str] = []

        def seal() -> None:
            called.append("seal")

        tree = CommandTree(
            [
                _group(
                    "vault",
                    (_group("passphrase", (_leaf("seal", handler=seal),)),),
                ),
            ]
        )
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        args = parser.parse_args(["vault", "passphrase", "seal"])
        CommandTree.dispatch(args)
        assert called == ["seal"]

    def test_handlerless_command_raises_clearly(self) -> None:
        """An accidental handler-less leaf surfaces as SystemExit, not AttributeError."""
        cmd = CommandDef(name="v", help="v", handler=None)
        # Bypass the structural is_group check by stamping a fake namespace.
        ns = argparse.Namespace(_cmd=cmd)
        with pytest.raises(SystemExit, match="has no handler"):
            CommandTree.dispatch(ns)

    def test_dispatch_forwards_trailing_podman_args(self) -> None:
        """``podman_args`` on the namespace flows through as a handler kwarg."""
        captured: dict[str, object] = {}

        def handler(*, podman_args: list[str] | None = None) -> None:
            captured["podman_args"] = podman_args

        cmd = CommandDef(name="run", help="run", handler=handler)
        ns = argparse.Namespace(_cmd=cmd, podman_args=["--rm", "img"])
        CommandTree.dispatch(ns)
        assert captured == {"podman_args": ["--rm", "img"]}


class TestSharedIdentityAcrossShortcuts:
    """The reason this all matters: a node referenced at two paths shares wraps."""

    def test_same_subtree_referenced_twice_shares_overlay(self) -> None:
        """Splicing a subtree into two positions of a parent tree, then overlaying
        the original, propagates the wrap to both positions."""
        seal = _leaf("seal")
        passphrase = _group("passphrase", (seal,))
        # Two paths point at the same passphrase group:
        deep = _group("sandbox", (_group("vault", (passphrase,)),))
        shortcut = _group("vault", (passphrase,))
        tree = CommandTree([deep, shortcut])

        def new_seal() -> None:
            pass

        # Overlay the deep path; the shortcut should NOT pick it up — they
        # diverge because overlay creates new CommandDefs for the touched
        # path's ancestors.  Consumers preserve identity by overlaying the
        # subtree FIRST, then splicing the (same) modified subtree at
        # multiple positions.
        overlaid = tree.overlay({("sandbox", "vault", "passphrase", "seal"): new_seal})
        assert overlaid.find_at(("sandbox", "vault", "passphrase", "seal")).handler is new_seal
        assert overlaid.find_at(("vault", "passphrase", "seal")).handler is not new_seal

    def test_pre_overlay_then_splice_preserves_shared_wrap(self) -> None:
        """Correct pattern: overlay the leaf subtree first, then splice the
        modified node into both positions of the parent tree."""

        def new_seal() -> None:
            pass

        seal = _leaf("seal")
        passphrase_orig = _group("passphrase", (seal,))
        # Apply the wrap once, get back a modified subtree
        modified = (
            CommandTree([passphrase_orig]).overlay({("passphrase", "seal"): new_seal}).roots[0]
        )
        # Splice the modified subtree at both deep and shortcut positions
        deep = _group("sandbox", (_group("vault", (modified,)),))
        shortcut = _group("vault", (modified,))
        tree = CommandTree([deep, shortcut])
        # Both paths now resolve to the same wrapped handler — that's
        # the load-bearing property for "terok shield install" and
        # "terok executor sandbox shield install" sharing wraps.
        assert tree.find_at(("sandbox", "vault", "passphrase", "seal")).handler is new_seal
        assert tree.find_at(("vault", "passphrase", "seal")).handler is new_seal


class TestDuckTypedCommandDef:
    """Wire layer accepts foreign CommandDef shapes (terok-shield / clearance)."""

    def test_wires_object_without_epilog_attribute(self) -> None:
        """A registry CommandDef lacking ``epilog`` still wires — getattr defaults to ''."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class SlimDef:
            name: str
            help: str = ""
            handler: object = None
            args: tuple = ()
            children: tuple = ()

        called: list[int] = []

        def handler() -> None:
            called.append(1)

        slim = SlimDef(name="v", help="v", handler=handler)
        tree = CommandTree([slim])  # type: ignore[list-item]
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        args = parser.parse_args(["v"])
        CommandTree.dispatch(args)
        assert called == [1]


class TestKeyRow:
    """Backward-compat smoke for the SSH key row."""

    def test_constructs_and_unpacks_positionally(self) -> None:
        row = KeyRow("s", "c", "ed25519", "fp", "priv", "pub")
        scope, comment, _, _, _, _ = row
        assert (scope, comment) == ("s", "c")
        assert row.scope == "s"


class TestLazyHandler:
    """A [`LazyHandler`][terok_util.cli_types.LazyHandler] imports its target
    only when called, never at tree-build time — and stays transparent to
    dispatch (sync and async alike)."""

    def test_construction_does_not_import(self) -> None:
        """Constructing over a missing module must not raise — only calling does."""
        handler = LazyHandler("terok_util._no_such_module_xyz:fn")
        with pytest.raises(ModuleNotFoundError):
            handler()

    def test_resolves_and_invokes_positionally(self) -> None:
        assert LazyHandler("os.path:join")("a", "b") == os.path.join("a", "b")

    def test_dotted_qualname_walks_attributes(self) -> None:
        """A dotted qualname resolves nested attributes on the imported module."""
        import collections

        assert _resolve_handler("collections:abc.Mapping") is collections.abc.Mapping

    def test_missing_separator_raises(self) -> None:
        with pytest.raises(ValueError, match="module:qualname"):
            LazyHandler("no_colon_here")()

    def test_resolution_is_cached(self) -> None:
        assert _resolve_handler("os.path:join") is _resolve_handler("os.path:join")

    def test_resolve_returns_the_underlying_callable(self) -> None:
        """`resolve()` hands back the real target for introspection (e.g. signatures)."""
        assert LazyHandler("os.path:join").resolve() is os.path.join

    def test_wiring_a_tree_does_not_import_the_target(self, tmp_path, monkeypatch) -> None:
        """Building + wiring the tree must not import the handler module."""
        (tmp_path / "lazy_wire_target.py").write_text(
            "calls = []\n\ndef run(*, n=0):\n    calls.append(n)\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        assert "lazy_wire_target" not in sys.modules

        cmd = CommandDef(
            name="v",
            handler=LazyHandler("lazy_wire_target:run"),
            args=(ArgDef(name="--n", type=int, default=0),),
        )
        parser = argparse.ArgumentParser()
        CommandTree([cmd]).wire(parser)
        assert "lazy_wire_target" not in sys.modules  # still cold after wiring

        CommandTree.dispatch(parser.parse_args(["v", "--n", "3"]))
        import lazy_wire_target

        assert lazy_wire_target.calls == [3]  # imported + invoked at dispatch

    def test_dispatch_runs_lazy_async_handler(self, tmp_path, monkeypatch) -> None:
        """The coroutine returned by a lazily-resolved async handler still runs."""
        (tmp_path / "lazy_async_target.py").write_text(
            "calls = []\n\nasync def run():\n    calls.append(1)\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        tree = CommandTree([CommandDef(name="v", handler=LazyHandler("lazy_async_target:run"))])
        parser = argparse.ArgumentParser()
        tree.wire(parser)
        CommandTree.dispatch(parser.parse_args(["v"]))
        import lazy_async_target

        assert lazy_async_target.calls == [1]


class TestLazyDispatch:
    """A lazy root ([`CommandDef.source`][terok_util.cli_types.CommandDef])
    resolves to its real definition only when the verb is invoked, so a
    multi-subsystem CLI imports one verb's module per run."""

    @staticmethod
    def _write_verb(tmp_path, module: str, verb: str, body: str = "") -> None:
        """Write a throwaway module exposing ``VERB`` — a CommandDef named *verb*."""
        (tmp_path / f"{module}.py").write_text(
            "from terok_util.cli_types import ArgDef, CommandDef\n"
            f"{body}"
            f"VERB = CommandDef(name={verb!r}, help={verb + ' help'!r}, handler=lambda: None)\n"
        )

    def test_lazy_flag_and_resolve(self, tmp_path, monkeypatch) -> None:
        self._write_verb(tmp_path, "lz_resolve", "v")
        monkeypatch.syspath_prepend(str(tmp_path))
        lazy = CommandDef(name="v", help="v help", source="lz_resolve:VERB")
        assert lazy.is_lazy is True
        assert lazy.resolve().name == "v" and lazy.resolve().source is None

    def test_wire_loads_only_the_invoked_verb(self, tmp_path, monkeypatch) -> None:
        self._write_verb(tmp_path, "lz_a", "a")
        self._write_verb(tmp_path, "lz_b", "b")
        monkeypatch.syspath_prepend(str(tmp_path))
        tree = CommandTree(
            [
                CommandDef(name="a", help="ah", source="lz_a:VERB"),
                CommandDef(name="b", help="bh", source="lz_b:VERB"),
            ]
        )
        assert "lz_a" not in sys.modules and "lz_b" not in sys.modules
        tree.wire(argparse.ArgumentParser(), argv=["a", "--flag"])
        assert "lz_a" in sys.modules  # the invoked verb resolved
        assert "lz_b" not in sys.modules  # its sibling stayed a placeholder

    def test_help_lists_placeholders_without_loading(self, tmp_path, monkeypatch) -> None:
        self._write_verb(tmp_path, "lz_help", "h1")
        monkeypatch.syspath_prepend(str(tmp_path))
        tree = CommandTree([CommandDef(name="h1", help="h1 help", source="lz_help:VERB")])
        parser = argparse.ArgumentParser()
        tree.wire(parser, argv=["--help"])
        assert "lz_help" not in sys.modules  # nothing resolved for a bare --help
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])  # argparse still lists the placeholder

    def test_completion_env_forces_full_tree(self, tmp_path, monkeypatch) -> None:
        self._write_verb(tmp_path, "lz_comp", "c1")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        tree = CommandTree([CommandDef(name="c1", help="ch", source="lz_comp:VERB")])
        tree.wire(argparse.ArgumentParser(), argv=["c1"])
        assert "lz_comp" in sys.modules  # completion needs the whole surface

    def test_argv_none_wires_everything(self, tmp_path, monkeypatch) -> None:
        self._write_verb(tmp_path, "lz_eager", "e1")
        monkeypatch.syspath_prepend(str(tmp_path))
        tree = CommandTree([CommandDef(name="e1", help="eh", source="lz_eager:VERB")])
        tree.wire(argparse.ArgumentParser())  # no argv → back-compat full wire
        assert "lz_eager" in sys.modules

    def test_dispatch_through_a_lazy_root(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "lz_disp.py").write_text(
            "from terok_util.cli_types import ArgDef, CommandDef\n"
            "calls = []\n"
            "def run(*, n=0):\n    calls.append(n)\n"
            "VERB = CommandDef(name='d1', help='dh', "
            "args=(ArgDef(name='--n', type=int, default=0),), handler=run)\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        tree = CommandTree([CommandDef(name="d1", help="dh", source="lz_disp:VERB")])
        parser = argparse.ArgumentParser()
        tree.wire(parser, argv=["d1", "--n", "5"])
        CommandTree.dispatch(parser.parse_args(["d1", "--n", "5"]))
        import lz_disp

        assert lz_disp.calls == [5]


class TestLazyComposition:
    """Lazy nodes stay transparent to the tree-composition operations
    (``find_at`` / ``extend_at`` / ``overlay`` / ``walk``), so a consumer
    can splice and compose a registry it received as lazy roots."""

    @staticmethod
    def _write_group(tmp_path, module: str, group: str, child: str) -> None:
        (tmp_path / f"{module}.py").write_text(
            "from terok_util.cli_types import CommandDef\n"
            f"GROUP = CommandDef(name={group!r}, help='g', "
            f"children=(CommandDef(name={child!r}, help='c', handler=lambda: None),))\n"
        )

    def test_extend_at_resolves_a_lazy_target(self, tmp_path, monkeypatch) -> None:
        """Appending under a lazy root keeps its real children (the executor case)."""
        self._write_group(tmp_path, "lz_ext", "vault", "status")
        monkeypatch.syspath_prepend(str(tmp_path))
        tree = CommandTree([CommandDef(name="vault", help="vh", source="lz_ext:GROUP")])
        extended = tree.extend_at(
            ("vault",), [CommandDef(name="serve", help="sv", handler=lambda: None)]
        )
        names = [c.name for c in extended.find_at(("vault",)).children]
        assert names == ["status", "serve"]  # real child preserved, addition appended

    def test_find_at_descends_through_a_lazy_node(self, tmp_path, monkeypatch) -> None:
        self._write_group(tmp_path, "lz_find", "vault", "status")
        monkeypatch.syspath_prepend(str(tmp_path))
        tree = CommandTree([CommandDef(name="vault", help="vh", source="lz_find:GROUP")])
        assert tree.find_at(("vault", "status")).name == "status"

    def test_nested_lazy_child_wires_in_full(self, tmp_path, monkeypatch) -> None:
        """A lazy root spliced as a child materialises when its branch is wired."""
        (tmp_path / "lz_child.py").write_text(
            "from terok_util.cli_types import ArgDef, CommandDef\n"
            "VERB = CommandDef(name='notify', help='n', "
            "args=(ArgDef(name='message'),), handler=lambda **k: None)\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        lazy_child = CommandDef(name="notify", help="n", source="lz_child:VERB")
        group = CommandDef(name="dbus", help="d", children=(lazy_child,))
        parser = argparse.ArgumentParser()
        CommandTree([group]).wire(parser)  # eager wire of the whole tree
        # The nested lazy child's positional arg parses — proof it materialised.
        args = parser.parse_args(["dbus", "notify", "hello"])
        assert args.message == "hello"
