# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Platform-aware path resolution for the terok ecosystem.

Provides generic **namespace resolvers** that any sibling package can
call to place its state/config/runtime under the shared ``terok/``
namespace.

Resolution priority for each resolver:

1. Package-specific override (``env_var`` argument)
2. ``TEROK_ROOT`` env var (namespace-wide override; state only)
3. Platform default (FHS root paths or XDG dirs via
   ``platformdirs``)

Layered config-file reading (``/etc/terok/config.yml`` →
``~/.config/terok/config.yml``) is a sibling-package concern — packages
that want it compose [`ConfigStack`][terok_util.config_stack.ConfigStack]
against these resolvers themselves.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable
from pathlib import Path

try:
    from platformdirs import user_config_dir, user_data_dir

    _user_config_dir: Callable[..., str] | None = user_config_dir
    _user_data_dir: Callable[..., str] | None = user_data_dir
except ImportError:  # optional dependency
    _user_config_dir = None
    _user_data_dir = None


_NAMESPACE = "terok"

_TEROK_ROOT_ENV = "TEROK_ROOT"
"""Env var overriding the namespace state root for all ecosystem packages."""

_TEROK_CONFIG_FILE_ENV = "TEROK_CONFIG_FILE"
"""Env var pointing at a single ``config.yml`` override (no layering)."""

#: Process-lifetime cache for resolved config sections.  Path resolvers
#: get called many times during a single CLI invocation; reading and
#: parsing two YAML files for every call would dominate latency.
_config_section_cache: dict[str, dict[str, str]] = {}

#: Process-lifetime cache for resolved top-level scalar/list reads (e.g.
#: ``experimental: true``).  Same motivation as the section cache.
_config_top_level_cache: dict[str, object | None] = {}


#: The identity map's length — 2**32 - 1, the whole uid_t range.  Only the
#: initial user namespace carries it; every nested namespace maps a slice.
_UID_T_MAX = 4294967295


def host_uid() -> int:
    """Return this process's UID as its *parent* user namespace sees it.

    Inside an unprivileged user namespace (rootless ``podman`` / ``crun``
    hook, sandboxed CI runner, ``unshare -U``), ``os.geteuid()`` returns
    the *inner*-userns UID — typically ``0`` even when the operator
    really ran the program as UID 1000.  Network peers (D-Bus
    ``SO_PEERCRED``, ``AUTH EXTERNAL``) and kernel-level checks see the
    *outer* (host) UID via the userns ``uid_map`` translation, so a
    process that advertises its inner UID over the wire is rejected for
    a credential mismatch.  This helper hands callers the outer UID those
    peers expect.

    The mapping comes from ``/proc/self/uid_map``.  When it is
    unavailable (macOS, BSD, exotic chroot) or no row covers the
    effective UID, the bare ``geteuid()`` answer is returned — correct on
    systems without Linux user namespaces.

    One hop, not all of them.  ``uid_map``'s second column is a UID in the
    **parent** user namespace (``user_namespaces(7)``), which equals the
    initial namespace only at a single level of nesting.  Rootless podman's
    ``--userns=keep-id`` — how terok runs agent containers — nests twice, so
    an unprivileged process there translates to ``0``: the parent's view,
    not the host's.

    That makes this the wrong question to ask about privilege: a ``0`` here
    says "some ancestor namespace calls me root", not "I can write
    ``/etc``".  Privilege is decided by ``_is_root()`` instead, which asks
    whether uid 0 is held in the *initial* namespace.  What this *is* right
    for is what it was written for: telling a peer outside our namespace
    which UID it will see us as (D-Bus ``SO_PEERCRED`` / ``AUTH EXTERNAL``).
    """
    try:
        euid = os.geteuid()
    except AttributeError:
        return 0 if getpass.getuser() == "root" else -1
    try:
        with open("/proc/self/uid_map", encoding="ascii") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) != 3:
                    continue
                try:
                    inner_start, outer_start, length = (int(p) for p in parts)
                except ValueError:
                    continue
                if inner_start <= euid < inner_start + length:
                    return outer_start + (euid - inner_start)
    except OSError:
        pass
    return euid


def _in_initial_userns() -> bool:
    """Whether this process runs in the initial user namespace.

    That namespace is the one with the full identity map; every namespace
    created since maps a slice of it.  A missing ``uid_map`` means the
    kernel has no user namespaces at all (or is not Linux), which is the
    initial namespace by definition.
    """
    try:
        with open("/proc/self/uid_map", encoding="ascii") as fh:
            rows = [line.split() for line in fh if line.split()]
    except OSError:
        return True
    return rows == [["0", "0", str(_UID_T_MAX)]]


def _is_root() -> bool:
    """Return True only for root that can actually write the FHS tree.

    Two ways to hold uid 0 without that power, and terok meets both:

    * an **OCI hook** under rootless podman runs with the operator's UID
      mapped to inner 0, but in the *host's* mount namespace — so
      ``/var/lib`` there is the host's, and the operator (really uid 1000)
      cannot write it;
    * a process in an **agent container** runs unprivileged (uid 1000),
      yet ``--userns=keep-id``'s nested map translates it to 0.

    So neither ``geteuid()`` nor [`host_uid`][terok_util.paths.host_uid]
    settles it alone: real root is uid 0 *in the initial namespace*.  This
    used to delegate to ``host_uid()``, which answers a different question
    entirely — what a peer outside our namespace sees — and thereby handed
    the second case a root verdict: every resolver took the root branch,
    ignored ``XDG_*``, and aimed state at an unwritable ``/var/lib/terok``.
    That is the mechanism behind the ten terok-shield unit tests that
    failed inside terok's own containers.
    """
    try:
        euid = os.geteuid()
    except AttributeError:  # pragma: no cover — non-POSIX
        return getpass.getuser() == "root"
    return euid == 0 and _in_initial_userns()


def config_file_paths() -> list[tuple[str, Path]]:
    """Ordered config.yml locations with scope labels (lowest → highest priority).

    ``TEROK_CONFIG_FILE`` → single override (no layering).  Otherwise:
    ``/etc/terok/config.yml`` (system) → ``~/.config/terok/config.yml``
    (user).  Root processes see only the system path.

    Public so consumers can render an "edit one of these to override X"
    hint to the operator (which file gets the highest priority, where
    on disk the operator would put the override, etc.).
    """
    env = os.getenv(_TEROK_CONFIG_FILE_ENV)
    if env:
        return [("override", Path(env).expanduser())]
    result: list[tuple[str, Path]] = [
        ("system", Path("/etc") / _NAMESPACE / "config.yml"),
    ]
    if not _is_root():
        if _user_config_dir is not None:
            user_base = Path(_user_config_dir(_NAMESPACE))
        else:
            user_base = Path.home() / ".config" / _NAMESPACE
        result.append(("user", user_base / "config.yml"))
    return result


def read_config_section(section: str) -> dict[str, str]:
    """Read a top-level section from layered terok configs (cached, fail-silent).

    Merges system and user ``config.yml`` files via
    [`ConfigStack`][terok_util.config_stack.ConfigStack] — user values
    override system defaults at the leaf level.  Lazy-imports
    ``config_stack`` so importing ``paths`` doesn't drag the YAML
    parser into a process that only needs the platform defaults.
    """
    if section in _config_section_cache:
        return _config_section_cache[section]

    result: dict[str, str] = {}
    try:
        from .config_stack import ConfigStack, load_yaml_scope

        stack = ConfigStack()
        for label, path in config_file_paths():
            stack.push(load_yaml_scope(label, path))
        merged = stack.resolve_section(section)
        result = {k: str(v) for k, v in merged.items() if v is not None}
    except Exception:  # noqa: BLE001 — fail-silent; bad config should not crash path resolution  # nosec B110 — best-effort probe; failure is non-fatal
        pass
    _config_section_cache[section] = result
    return result


def read_config_top_level(key: str) -> object | None:
    """Read a top-level scalar / list / mapping from layered terok configs.

    Counterpart to
    [`read_config_section`][terok_util.paths.read_config_section] for
    keys whose value isn't a dict — e.g. the ecosystem-wide
    ``experimental: true`` opt-in or a bare ``log_level: debug`` knob.
    Returns the merged value (user wins over system) or ``None`` when
    the key is absent or the config files can't be loaded.  Cached for
    the lifetime of the process; reaches for the ``_config_top_level_cache``
    private to flush in tests.
    """
    if key in _config_top_level_cache:
        return _config_top_level_cache[key]

    result: object | None = None
    try:
        from .config_stack import ConfigStack, load_yaml_scope

        stack = ConfigStack()
        for label, path in config_file_paths():
            stack.push(load_yaml_scope(label, path))
        result = stack.resolve().get(key)
    except Exception:  # noqa: BLE001 — fail-silent; bad config should not crash field resolution  # nosec B110 — best-effort probe; failure is non-fatal
        pass
    _config_top_level_cache[key] = result
    return result


def _reset_config_caches_for_tests() -> None:
    """Drop both config caches.  Test-only helper.

    Production code has no reason to invalidate the cache; config files
    are read once per process and never rewritten under a running CLI.
    Tests that swap ``TEROK_CONFIG_FILE`` between cases must call this
    so a later test isn't served the prior case's parsed config.
    """
    _config_section_cache.clear()
    _config_top_level_cache.clear()


def _namespace_root() -> Path | None:
    """Return the configured namespace state root, or ``None`` for platform default.

    Resolution: ``TEROK_ROOT`` env var → ``config.yml`` ``paths.root``
    → ``None`` (caller uses platform default).  Mirrors Podman's
    behaviour so an operator who sets ``paths.root: /virt/terok``
    in ``config.yml`` relocates the whole state tree without having
    to export an env var.
    """
    env = os.getenv(_TEROK_ROOT_ENV)
    if env:
        return Path(env).expanduser()
    val = read_config_section("paths").get("root")
    return Path(val).expanduser().resolve() if val else None


# ---------------------------------------------------------------------------
# Generic namespace resolvers
# ---------------------------------------------------------------------------


def _safe_subdir(base: Path, subdir: str) -> Path:
    """Join *subdir* to *base*, rejecting absolute or parent-traversal paths."""
    if not subdir:
        return base
    if Path(subdir).is_absolute() or ".." in Path(subdir).parts:
        raise ValueError(f"subdir must be relative without '..', got {subdir!r}")
    return base / subdir


def _platform_state_base() -> Path:
    """Return the platform-default state base (no config override)."""
    if _is_root():
        return Path("/var/lib") / _NAMESPACE
    if _user_data_dir is not None:
        return Path(_user_data_dir(_NAMESPACE))
    xdg = os.getenv("XDG_DATA_HOME")
    return Path(xdg) / _NAMESPACE if xdg else Path.home() / ".local" / "share" / _NAMESPACE


def namespace_state_dir(subdir: str = "", *, env_var: str | None = None) -> Path:
    """Resolve a state directory under the ``terok/`` namespace.

    Priority:

    1. *env_var* (package-specific override, e.g. ``TEROK_SANDBOX_STATE_DIR``)
    2. ``TEROK_ROOT`` env var (namespace override)
    3. ``config.yml`` → ``paths.root`` (Podman model — all packages honour it)
    4. Platform default (``/var/lib/terok/<subdir>`` for root, XDG data
       dir otherwise)

    *env_var* is keyword-only so a positional second argument can never
    accidentally be reinterpreted as an override name.
    """
    if env_var:
        val = os.getenv(env_var)
        if val:
            return Path(val).expanduser()
    root = _namespace_root()
    base = root if root else _platform_state_base()
    return _safe_subdir(base, subdir)


def namespace_config_dir(subdir: str = "", *, env_var: str | None = None) -> Path:
    """Resolve a config directory under the ``terok/`` namespace.

    Priority: *env_var* → ``/etc/terok/<subdir>`` (root) → platformdirs
    → ``~/.config/terok/<subdir>``.  *env_var* is keyword-only.
    """
    if env_var:
        val = os.getenv(env_var)
        if val:
            return Path(val).expanduser()
    base: Path
    if _is_root():
        base = Path("/etc") / _NAMESPACE
    elif _user_config_dir is not None:
        base = Path(_user_config_dir(_NAMESPACE))
    else:
        base = Path.home() / ".config" / _NAMESPACE
    return _safe_subdir(base, subdir)


def namespace_runtime_dir(subdir: str = "", *, env_var: str | None = None) -> Path:
    """Resolve a runtime directory under the ``terok/`` namespace.

    Priority: *env_var* → ``/run/terok/<subdir>`` (root)
    → ``$XDG_RUNTIME_DIR/terok/<subdir>`` → ``$XDG_STATE_HOME/terok/<subdir>``
    → ``~/.local/state/terok/<subdir>``.  *env_var* is keyword-only.
    """
    if env_var:
        val = os.getenv(env_var)
        if val:
            return Path(val).expanduser()
    base: Path
    if _is_root():
        base = Path("/run") / _NAMESPACE
    else:
        xdg_runtime = os.getenv("XDG_RUNTIME_DIR")
        if xdg_runtime:
            base = Path(xdg_runtime) / _NAMESPACE
        else:
            xdg_state = os.getenv("XDG_STATE_HOME")
            base = (
                Path(xdg_state) / _NAMESPACE
                if xdg_state
                else Path.home() / ".local" / "state" / _NAMESPACE
            )
    return _safe_subdir(base, subdir)


__all__ = [
    "config_file_paths",
    "host_uid",
    "namespace_config_dir",
    "namespace_runtime_dir",
    "namespace_state_dir",
    "read_config_section",
    "read_config_top_level",
]
