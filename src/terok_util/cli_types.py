# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Shared vocabulary for the command registry — argument and command definitions.

Every per-subsystem command module in a sibling terok package imports
from here.  Out-of-tree consumers (terok, terok-executor, terok-shield,
terok-clearance) build their CLI frontends against these types without
depending on any handler implementation.

A [`CommandDef`][terok_util.cli_types.CommandDef] is either a **leaf**
(``handler`` set, ``children`` empty) or a **group** (``handler`` is
``None``, ``children`` holds subverbs).  Groups can nest arbitrarily —
``vault passphrase seal`` is a leaf inside the ``passphrase`` group
inside the ``vault`` group.  A package's whole CLI is a forest of
these, wrapped in a [`CommandTree`][terok_util.cli_types.CommandTree]
for composition + argparse wiring.
"""

from __future__ import annotations

import argparse
import inspect
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple


@dataclass(frozen=True)
class ArgDef:
    """Definition of a single CLI argument."""

    name: str
    help: str = ""
    type: Callable[[str], Any] | None = None
    default: Any = None
    action: str | None = None
    dest: str | None = None
    nargs: int | str | None = None
    required: bool = False


@dataclass(frozen=True)
class CommandDef:
    """One node in a command tree — a leaf verb or a group of verbs.

    Attributes:
        name: Verb name as it appears on the CLI.
        help: One-line help string.
        handler: Callable implementing the verb.  ``None`` for groups.
        args: Argument definitions parsed by argparse.
        children: Sub-verbs.  Non-empty makes this node a group.
        group: Free-form tag used by per-subsystem grouping (unrelated
            to the ``children`` structural nesting).
        epilog: Optional long-form text rendered after the argparse
            argument list in ``--help`` output.
        extras: Bag of package-specific metadata downstream consumers
            ignore (shield's ``needs_container`` / ``standalone_only``
            would live here on a unified shape).

    A frozen-dataclass + structural sharing is the load-bearing part
    of the wrap-once-share-everywhere story: when a consumer overlays
    a handler at one path, the modified
    [`CommandDef`][terok_util.cli_types.CommandDef] is referenced from
    every shortcut that also points at that path.  Identity is what
    makes the overlay propagate.
    """

    name: str
    help: str = ""
    handler: Callable[..., Any] | None = None
    args: tuple[ArgDef, ...] = ()
    children: tuple[CommandDef, ...] = ()
    group: str = ""
    epilog: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        """Whether this node carries children (i.e. is a verb group)."""
        return bool(self.children)

    def with_handler(self, handler: Callable[..., Any]) -> CommandDef:
        """Return a copy with ``handler`` replaced — pure leaf-rewrap."""
        return replace(self, handler=handler)

    def with_children(self, children: tuple[CommandDef, ...]) -> CommandDef:
        """Return a copy with ``children`` replaced."""
        return replace(self, children=children)


class KeyRow(NamedTuple):
    """One registered SSH key, fully resolved for display and matching."""

    scope: str
    comment: str
    key_type: str
    fingerprint: str
    private_key: str
    public_key: str


class CommandTree:
    """A forest of [`CommandDef`][terok_util.cli_types.CommandDef] nodes.

    The unit of composition for CLI registries: each package exposes its
    own ``CommandTree``; consumers walk it structurally, overlay
    handlers where they wrap a concept, extend with their own verbs,
    and wire the result into argparse.

    Composition is identity-preserving — nodes the consumer doesn't
    touch share object identity with their pre-overlay counterparts, so
    a shortcut that splices the same subtree at the consumer's top
    level reaches the same modified handler.  ``terok shield install``
    and ``terok executor sandbox shield install`` resolving to the
    same wrap is a direct consequence.
    """

    def __init__(self, roots: Iterable[CommandDef]) -> None:
        """Build a tree from an iterable of top-level verbs/groups."""
        self._roots: tuple[CommandDef, ...] = tuple(roots)

    @property
    def roots(self) -> tuple[CommandDef, ...]:
        """The top-level verbs in this tree, in declaration order."""
        return self._roots

    def __iter__(self) -> Iterator[CommandDef]:
        """Yield each root verb."""
        return iter(self._roots)

    def __len__(self) -> int:
        """Number of root verbs."""
        return len(self._roots)

    def __add__(self, other: CommandTree | Iterable[CommandDef]) -> CommandTree:
        """Concatenate forests — *other*'s roots appended to this one's."""
        other_roots = other.roots if isinstance(other, CommandTree) else tuple(other)
        return CommandTree(self._roots + other_roots)

    def find_at(self, path: Sequence[str]) -> CommandDef:
        """Return the [`CommandDef`][terok_util.cli_types.CommandDef] at *path*.

        *path* is a sequence of verb names from the root.  An empty
        path is rejected (no synthetic root).  ``KeyError`` if any
        segment doesn't match a child name.
        """
        if not path:
            raise KeyError("empty path; specify at least one verb name")
        first, *rest = path
        for root in self._roots:
            if root.name == first:
                return _descend(root, tuple(rest))
        raise KeyError(f"no top-level verb {first!r}")

    def overlay(self, overrides: Mapping[tuple[str, ...], Callable[..., Any]]) -> CommandTree:
        """Return a new tree with handlers replaced at the named paths.

        *overrides* maps verb-name tuples (e.g. ``("vault", "status")``)
        to replacement handlers.  Each match produces one new
        [`CommandDef`][terok_util.cli_types.CommandDef] via ``replace``;
        ancestors are likewise replaced because their ``children``
        tuples now hold a new node, but unrelated siblings share
        identity with the input tree.

        Sandbox-vocab paths use the operator-facing verb names — same
        names you'd type on the CLI — so the override map reads like a
        routing table.
        """
        return CommandTree(_overlay_forest(self._roots, dict(overrides), ()))

    def extend_at(self, path: Sequence[str], additions: Iterable[CommandDef]) -> CommandTree:
        """Return a new tree with *additions* appended at the path's children.

        Empty path extends the top-level forest.  Otherwise the
        [`CommandDef`][terok_util.cli_types.CommandDef] at *path*
        must be a **group** — a leaf (one with ``handler`` set and no
        ``children``) cannot be extended; trying to do so would produce
        a hybrid node argparse can't represent (handler + subparsers
        on the same parser).  Raises ``TypeError`` rather than
        silently inventing such a node.
        """
        addition_tuple = tuple(additions)
        if not path:
            return CommandTree(self._roots + addition_tuple)
        target = self.find_at(path)
        if target.handler is not None and not target.children:
            raise TypeError(
                f"cannot extend leaf {'.'.join(path)!r}: extend_at requires a group "
                f"(handler=None with children), but the target has a handler set"
            )
        return CommandTree(_extend_forest(self._roots, tuple(path), addition_tuple, ()))

    def walk(self) -> Iterator[tuple[tuple[str, ...], CommandDef]]:
        """Yield ``(path, command)`` for every node in the tree, depth-first."""
        for root in self._roots:
            yield from _walk_node(root, ())

    def wire(self, target: argparse.ArgumentParser | argparse._SubParsersAction) -> None:
        """Wire this tree's verbs as subparsers under *target*, recursively.

        *target* may be either an
        [`ArgumentParser`][argparse.ArgumentParser] (a fresh
        ``add_subparsers()`` action is created) or an existing
        ``argparse._SubParsersAction`` (the tree mounts straight under
        it; the private name has no public docs target).  The second
        form lets a consumer mix legacy register-style subparsers
        with structural
        [`CommandTree`][terok_util.cli_types.CommandTree] ones under
        the same root parser without colliding on argparse's
        one-subparsers-per-parser rule.

        The same [`CommandDef`][terok_util.cli_types.CommandDef]
        wired at multiple positions (deep nesting + shortcuts) yields
        independent argparse subparser instances, but each subparser's
        dispatch reads back the same handler object — so concept
        translations applied via ``overlay`` apply uniformly across
        every entry point that references the modified node.
        """
        if isinstance(target, argparse._SubParsersAction):
            sub = target
        else:
            sub = target.add_subparsers()
        for cmd in self._roots:
            _wire_command(sub, cmd)

    @staticmethod
    def dispatch(args: argparse.Namespace) -> None:
        """Invoke the handler stored on *args* by [`CommandTree.wire`][terok_util.cli_types.CommandTree.wire].

        Bridges argparse's parsed-args namespace to the handler kwargs
        the [`CommandDef`][terok_util.cli_types.CommandDef] declared.
        Async handlers are detected and run via ``asyncio.run`` so
        consumers don't need separate dispatch paths per handler
        flavour.
        """
        cmd: CommandDef | None = getattr(args, "_cmd", None)
        if cmd is None:
            # Argparse stopped before a leaf command (bare ``terok`` /
            # ``terok shield`` etc.).  Argparse already printed help; exit
            # with the same "no command given" status it would.
            raise SystemExit(2)
        if cmd.handler is None:
            raise SystemExit(f"Command {cmd.name!r} has no handler")
        kwargs = {_arg_dest(arg): getattr(args, _arg_dest(arg), arg.default) for arg in cmd.args}
        # Trailing args after ``--`` (currently only ``run`` consumes them).
        if hasattr(args, "podman_args"):
            kwargs["podman_args"] = args.podman_args
        result = cmd.handler(**kwargs)
        if inspect.iscoroutine(result):
            import asyncio  # noqa: PLC0415

            asyncio.run(result)


# ── Module-private helpers ─────────────────────────────────────────


def _arg_dest(arg: ArgDef) -> str:
    """Resolve the argparse ``dest`` for an [`ArgDef`][terok_util.cli_types.ArgDef].

    Slash-separated names (e.g. ``"-t/--timeout"``) pick the longest
    form before stripping leading dashes — argparse derives its own
    ``dest`` from the first long option in that case, so picking the
    longer name matches the convention.  ``arg.dest`` (when set)
    overrides everything.
    """
    if arg.dest:
        return arg.dest
    names = arg.name.split("/")
    canonical = max(names, key=len)
    return canonical.lstrip("-").replace("-", "_")


def _descend(node: CommandDef, rest: tuple[str, ...]) -> CommandDef:
    """Walk further into *node* by the remaining path segments."""
    if not rest:
        return node
    head, *tail = rest
    for child in node.children:
        if child.name == head:
            return _descend(child, tuple(tail))
    raise KeyError(f"{head!r} not found under {node.name!r}")


def _overlay_forest(
    roots: tuple[CommandDef, ...],
    overrides: dict[tuple[str, ...], Callable[..., Any]],
    here: tuple[str, ...],
) -> tuple[CommandDef, ...]:
    """Recursively apply *overrides* to *roots*; share identity where untouched.

    Returns the input tuple unchanged (object identity preserved) if no
    node in or under it was touched — so a consumer that splices the
    same subtree at multiple positions only diverges where the overlay
    actually fired.
    """
    new_roots: list[CommandDef] = []
    any_changed = False
    for root in roots:
        new_root = _overlay_node(root, overrides, here + (root.name,))
        if new_root is not root:
            any_changed = True
        new_roots.append(new_root)
    return tuple(new_roots) if any_changed else roots


def _overlay_node(
    node: CommandDef,
    overrides: dict[tuple[str, ...], Callable[..., Any]],
    path: tuple[str, ...],
) -> CommandDef:
    """Apply override at *path* if present, recurse into children."""
    new_children = _overlay_forest(node.children, overrides, path)
    new_handler = overrides.get(path, node.handler)
    if new_handler is node.handler and new_children is node.children:
        # Nothing changed at or below this node — share identity.
        return node
    return replace(node, handler=new_handler, children=new_children)


def _extend_forest(
    roots: tuple[CommandDef, ...],
    path: tuple[str, ...],
    additions: tuple[CommandDef, ...],
    here: tuple[str, ...],
) -> tuple[CommandDef, ...]:
    """Find *path* in *roots* and append *additions* to its children."""
    head, *tail = path
    out: list[CommandDef] = []
    found = False
    for root in roots:
        if root.name != head:
            out.append(root)
            continue
        found = True
        if tail:
            new_children = _extend_forest(root.children, tuple(tail), additions, here + (head,))
            out.append(root.with_children(new_children))
        else:
            out.append(root.with_children(root.children + additions))
    if not found:
        full_path = ".".join(here + (head,))
        raise KeyError(f"no verb at path {full_path!r} to extend")
    return tuple(out)


def _walk_node(
    node: CommandDef, here: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], CommandDef]]:
    """Depth-first yield of ``(path, node)`` for *node* and its descendants."""
    here = here + (node.name,)
    yield here, node
    for child in node.children:
        yield from _walk_node(child, here)


def _wire_command(sub: argparse._SubParsersAction, cmd: CommandDef) -> None:
    """Add *cmd* to *sub*; recurse for groups, set leaf dispatch defaults.

    Reads attributes via ``getattr`` with defaults so command sources
    with a slimmer CommandDef shape (terok-shield, terok-clearance —
    both define their own dataclasses without ``epilog``) wire through
    the same path as the first-class CommandDefs in
    [`terok_util.cli_types`][terok_util.cli_types].  The Protocol-
    style duck typing keeps the unification cheap; the leaves don't
    need to depend on terok-util to share the wire layer.
    """
    parser_kwargs: dict[str, Any] = {"help": cmd.help}
    epilog = getattr(cmd, "epilog", "")
    if epilog:
        parser_kwargs["epilog"] = epilog
        parser_kwargs["formatter_class"] = argparse.RawDescriptionHelpFormatter
    parser = sub.add_parser(cmd.name, **parser_kwargs)
    for arg in cmd.args:
        kwargs: dict[str, Any] = {}
        if arg.help:
            kwargs["help"] = arg.help
        if arg.type is not None:
            kwargs["type"] = arg.type
        if arg.default is not None:
            kwargs["default"] = arg.default
        if arg.action is not None:
            kwargs["action"] = arg.action
        if arg.dest is not None:
            kwargs["dest"] = arg.dest
        if arg.nargs is not None:
            kwargs["nargs"] = arg.nargs
        # ``required`` is first-class on terok-util's ArgDef but
        # clearance / shield ArgDefs don't declare it; getattr defaults
        # keep the wire layer tolerant of slimmer foreign shapes.
        if getattr(arg, "required", False) and arg.name.startswith("-"):
            kwargs["required"] = True
        # Slash-separated names (e.g. ``-t/--timeout``) expand to argparse's
        # multi-name form so a downstream registry (clearance) can declare
        # short + long flags in one ArgDef without splitting at every consumer.
        names = arg.name.split("/")
        parser.add_argument(*names, **kwargs)
    if cmd.children:
        child_sub = parser.add_subparsers()
        for child in cmd.children:
            _wire_command(child_sub, child)
        parser.set_defaults(_group_help=parser)
    if cmd.handler is not None:
        parser.set_defaults(_cmd=cmd)


__all__ = ["ArgDef", "CommandDef", "CommandTree", "KeyRow"]
