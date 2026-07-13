# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""What the kernel does with fs.py's promises, on a real filesystem.

[`write_sensitive_file`][terok_util.fs.write_sensitive_file] is the
helper every sibling routes a secret through — SSH keys, vault
passphrases, gate tokens.  Its guarantees are stated in terms of things
a mock cannot produce: a file mode that survives a hostile *process*
umask, a parent directory clamped to ``0o700`` whatever it was before,
and an ``O_EXCL | O_NOFOLLOW`` open that refuses to let a planted
symlink redirect the write.  Patching ``os.open`` and asserting on the
flag integer proves the flags were *passed*; only the VFS can say what
they mean — and it says different things on overlayfs, on tmpfs, on
musl, and under a root uid that carries ``CAP_DAC_OVERRIDE``.

The umask cases run in a child process on purpose: umask is per-process
state, and a test that set it in-process would leak it into every test
that came after.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from terok_util.fs import ensure_dir_writable, write_sensitive_file

from .constants import (
    PERMISSIVE_UMASK,
    RESTRICTIVE_UMASK,
    SENSITIVE_FILE_MODE,
    SENSITIVE_PARENT_MODE,
    UNWRITABLE_DIR_MODE,
    WORLD_WRITABLE_DIR_MODE,
)

pytestmark = pytest.mark.needs_host_features

RUNNING_AS_ROOT = os.geteuid() == 0

_SECRET = "hunter2\n"
_LEAKED_SECRET = "this must never be written through the link\n"

# The child sets the umask *before* calling the helper — the sequence a
# systemd unit, a cron job or a podman exec would hand us — then reports
# the modes the kernel actually stamped on the file and its parent.
_WRITE_PROBE = """
import json, os
from pathlib import Path
from terok_util.fs import write_sensitive_file

os.umask({umask:#o})
path = Path({path!r})
created = write_sensitive_file(path, {content!r})
print(json.dumps({{
    "created": created,
    "file_mode": os.stat(path).st_mode & 0o7777,
    "parent_mode": os.stat(path.parent).st_mode & 0o7777,
    "content": path.read_text(),
}}))
"""


def _mode_of(path: Path) -> int:
    """The permission bits of *path*, symlinks not followed."""
    return stat.S_IMODE(path.lstat().st_mode)


# ── Permission bits, against the process umask ─────────────────────────


@pytest.mark.parametrize("umask", [PERMISSIVE_UMASK, RESTRICTIVE_UMASK])
def test_secret_is_0600_whatever_the_umask(child_json, tmp_path, umask) -> None:
    """The mode is the helper's promise, not the umask's leftovers.

    ``os.open(..., 0o600)`` is a *request*: the kernel hands back
    ``mode & ~umask``.  With the permissive umask the request is granted
    as written; the restrictive one is included so a pass cannot be the
    umask accidentally doing the helper's job for it.  A regression that
    dropped the explicit mode would sail through both unit tests and the
    ``0o077`` case, and only leak on the machine whose umask is ``0o000``.
    """
    path = tmp_path / "secrets" / "id_ed25519"
    result = child_json(_WRITE_PROBE.format(umask=umask, path=str(path), content=_SECRET))

    assert result["created"] is True
    assert result["file_mode"] == SENSITIVE_FILE_MODE
    assert result["content"] == _SECRET


def test_parent_directory_is_clamped_to_0700(child_json, tmp_path) -> None:
    """A pre-existing world-writable parent is tightened, not accepted."""
    parent = tmp_path / "secrets"
    parent.mkdir()
    parent.chmod(WORLD_WRITABLE_DIR_MODE)

    result = child_json(
        _WRITE_PROBE.format(umask=PERMISSIVE_UMASK, path=str(parent / "token"), content=_SECRET)
    )

    assert result["parent_mode"] == SENSITIVE_PARENT_MODE
    assert _mode_of(parent) == SENSITIVE_PARENT_MODE


def test_existing_file_is_not_overwritten(tmp_path) -> None:
    """``O_EXCL`` makes the second write a no-op that says so."""
    path = tmp_path / "secrets" / "token"

    assert write_sensitive_file(path, _SECRET) is True
    assert write_sensitive_file(path, "a different secret") is False
    assert path.read_text() == _SECRET


# ── Symlinks: the attack the flags exist for ───────────────────────────


def test_planted_symlink_never_receives_the_secret(tmp_path) -> None:
    """A symlink at the final path is refused; its target stays untouched.

    The classic ``/tmp`` race, reduced to its kernel rule: ``O_CREAT |
    O_EXCL`` on a path that *is* a symlink fails with ``EEXIST`` — the
    link is never followed, whatever it points at.  ``O_NOFOLLOW``
    belt-and-braces the same thing on the platforms that define it.
    Python surfaces the ``EEXIST`` as ``FileExistsError``, which the
    helper reports as "already existed" — the safe answer.  The
    load-bearing assertion is the second one: the attacker's target file
    was never created.
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    victim = tmp_path / "victim"
    link = secrets / "token"
    link.symlink_to(victim)

    assert write_sensitive_file(link, _LEAKED_SECRET) is False
    assert not victim.exists()
    assert link.is_symlink()


def test_symlinked_parent_is_refused(tmp_path) -> None:
    """A symlinked parent is rejected before anything is chmod'ed.

    ``os.chmod`` follows symlinks, so honouring a symlinked parent would
    hand ``0o700`` to whatever directory the link names — an operator's
    ``~/.ssh``, say.  The helper raises instead; the pointed-at directory
    must come out with its original mode.
    """
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(WORLD_WRITABLE_DIR_MODE)
    linked_parent = tmp_path / "secrets"
    linked_parent.symlink_to(real)

    with pytest.raises(RuntimeError, match="symlinked directory"):
        write_sensitive_file(linked_parent / "token", _LEAKED_SECRET)

    assert _mode_of(real) == WORLD_WRITABLE_DIR_MODE


# ── DAC, which is a property of the uid and not of the code ────────────


@pytest.mark.needs_unprivileged
@pytest.mark.skipif(RUNNING_AS_ROOT, reason="root bypasses DAC; see the root-side test")
def test_unwritable_directory_exits_for_an_operator(tmp_path) -> None:
    """A non-root process is told, in words, that it cannot write there.

    ``ensure_dir_writable`` exists to turn a permission error into an
    operator-facing message instead of a traceback three frames deeper.
    Whether the directory *is* writable is a kernel verdict on a real
    uid — the exact question a mocked ``os.access`` cannot be wrong
    about, and therefore cannot be right about either.
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(UNWRITABLE_DIR_MODE)

    with pytest.raises(SystemExit, match="not writable"):
        ensure_dir_writable(locked, "state")


@pytest.mark.needs_root
@pytest.mark.skipif(not RUNNING_AS_ROOT, reason="asserts the root DAC bypass")
def test_unwritable_directory_is_writable_for_root(tmp_path) -> None:
    """Root walks into a 0o500 directory, and the helper must let it.

    ``CAP_DAC_OVERRIDE`` makes ``os.access(W_OK)`` true for uid 0, so the
    guard is a no-op for root — which is correct, and is the reason the
    non-root case above must be asserted separately rather than assumed.
    The root-in-container matrix slots are what exercise this branch.
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(UNWRITABLE_DIR_MODE)

    ensure_dir_writable(locked, "state")  # must not raise

    assert write_sensitive_file(locked / "token", _SECRET) is True
