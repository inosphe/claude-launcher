"""Daemon identity on disk: address file, auth token, singleton lock.

``daemon.json`` is written *after* the HTTP server is listening, so its
presence with a live pid doubles as the readiness signal auto-start polls for.
The token is generated once and required on every API call (even loopback —
the CLI reads it from disk automatically, so mandatory auth costs nothing and
removes the "opened to LAN but forgot auth" foot-gun).

It also records **which code this process imported**, under ``code``. That is
not identity in the same sense as the pid, but it is written here for the same
reason: boot is the only moment it can be established. Python loads modules
once, at import, so what a running daemon serves is the content of its source
directory *at boot* -- a fact that stops being readable the instant anyone
edits, commits or checks out anything. Nothing else on disk carries it.

``tools/deploy_check.py`` is the reader, and until this field existed it had
no left-hand side: it compared the daemon's boot *time* with a commit *time*
and called that "the daemon booted after the merge". Two states came out of
that wrong, in opposite directions -- a working tree that matches no commit
passed (``claunch-tig1``), and a commit that changed only ``.beads`` was
reported as unserved code (``claunch-33id``). Both are answered by content
once the content is recorded.

Everything here is best-effort and bounded: a boot must not fail, hang or slow
down because git is busy, missing, or looking at something enormous. Every
field is ``None`` when it could not be read, and ``None`` means "not read",
never "nothing there" -- the reader keeps those two apart and reports the
first as *cannot tell* rather than as a verdict.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .. import __version__
from . import paths

#: Ceiling on any single boot-time git call. A daemon that cannot start
#: because ``git status`` is waiting on somebody's ``index.lock`` would be a
#: worse bug than the one this snapshot exists to fix.
_GIT_TIMEOUT = 5.0

#: How many dirty paths are recorded before the list is cut. The reader's
#: verdict turns on whether the list is *empty*, and on nothing else, so a cut
#: list can only shorten what a message displays -- it cannot change an answer.
#: The remainder is still counted, under ``dirty_more``.
_DIRTY_CAP = 500


def _git(cwd: Path, *args: str) -> Optional[str]:
    """``git -C cwd args...``, or ``None`` if it could not be run at all.

    ``None`` and ``""`` are different answers and stay different: an empty
    string is git's answer for a clean tree, and ``None`` is "there was no
    answer". Collapsing them is the failure this whole snapshot is written to
    avoid one level up.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _dirty_paths(out: str) -> list:
    """Paths from ``git status --porcelain -z``.

    ``-z`` rather than the plain form because the plain form quotes and escapes
    any path that is not printable ASCII, and this repository's own board and
    docs carry Korean filenames' worth of that risk. A rename entry spends two
    NUL-separated fields (new path, then the original); both are recorded --
    over-listing costs a longer message and cannot turn a dirty tree clean.
    """
    fields = [f for f in out.split("\x00") if f]
    paths, i = [], 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            if i < len(fields):
                paths.append(fields[i])
                i += 1
    return paths


def code_snapshot(root: Optional[Path] = None) -> dict:
    """Which code this process imported, as far as git can be made to say.

    ``root`` defaults to the package directory actually in use -- resolved from
    this module rather than from a configured path, so it names the copy Python
    loaded even when several are installed. The rest describes the repository
    that directory sits in, and is ``None`` when there is no repository (an
    installed wheel), when git could not answer, or when it timed out.

    ``dirty`` is the whole checkout's ``git status``, not just the package's.
    Narrowing it to ``src/`` would be a second rule about which paths are code,
    and this repository already has one (``tools/sweep.py`` ``NON_CODE_ENTRIES``,
    which subtracts rather than selects). The reader applies that one.

    The argument exists for tests and nothing else. A test that let the default
    stand would read this repository's own HEAD, which
    ``tests/_repo_history_guard.py`` stops for a reason that lands on this very
    gate: two commits with the same tree share a sweep receipt, so a test that
    consults refs can be handed the other commit's verdict as a green.
    """
    root = Path(__file__).resolve().parents[1] if root is None else Path(root)
    snap = {
        "root": str(root),
        "repo": None,
        "head": None,
        "dirty": None,
        "dirty_more": 0,
    }
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None or not top.strip():
        return snap
    snap["repo"] = str(Path(top.strip()))
    head = _git(root, "rev-parse", "HEAD")
    if head is not None and head.strip():
        snap["head"] = head.strip()
    status = _git(root, "status", "--porcelain", "-z")
    if status is not None:
        found = _dirty_paths(status)
        snap["dirty"] = found[:_DIRTY_CAP]
        snap["dirty_more"] = max(0, len(found) - _DIRTY_CAP)
    return snap


def write_daemon_json(host: str, port: int, code: Optional[dict] = None) -> None:
    """Write ``daemon.json``. ``code`` is :func:`code_snapshot`'s result.

    The snapshot is passed in rather than taken here, and the boot path
    (``daemon/__main__.py``) is the one caller that takes it. Two reasons, and
    the second is the load-bearing one:

    * It is a fact about *this boot*, so the place that is booting should be
      the place that records it. Everything else that writes this file is
      standing in for a daemon rather than being one.
    * Taking it here would make every in-process caller read this repository's
      HEAD, and ``tests/_repo_history_guard.py`` refuses that -- for a reason
      that lands on this very gate: two commits with the same tree share a
      sweep receipt, so a test that consults refs can be handed the other
      commit's verdict as a green. Five test modules write this file while
      exercising something else entirely (restart, instances, delegation), and
      none of them booted a daemon.

    Omitted, the field is ``None``, and the reader reports *cannot tell* rather
    than guessing -- which is also what a future boot path that forgets to pass
    it would get. That direction is the safe one: a missing snapshot can cost a
    restart, and cannot produce a green over a tree nobody identified.
    """
    from datetime import datetime, timezone

    doc = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "instance": paths.instance() or None,
        "version": __version__,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code": code,
    }
    path = paths.daemon_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    _chmod_private(path)


def read_daemon_json() -> Optional[dict]:
    path = paths.daemon_json()
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def remove_daemon_json() -> None:
    try:
        paths.daemon_json().unlink()
    except OSError:
        pass


def load_or_create_token() -> str:
    path = paths.token_file()
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    return rotate_token()


def rotate_token() -> str:
    token = secrets.token_urlsafe(32)
    path = paths.token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    _chmod_private(path)
    return token


def _chmod_private(path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows: the user-profile ACL is the real protection
        pass


def lock_is_free() -> bool:
    """Probe (acquire + release) whether the daemon singleton lock is unheld.

    Used by ``stop()`` to wait for the daemon *process* to actually exit:
    ``daemon.json`` disappears early in shutdown, but the lock is only
    released when the process dies after draining its sessions.
    """
    lock = SingletonLock()
    if not lock.acquire():
        return False
    lock.release()
    return True


class SingletonLock:
    """An OS-level exclusive lock held for the daemon's lifetime.

    Two racing auto-starts both spawn a daemon; the loser fails to acquire and
    exits quietly while both CLIs converge on the winner via health polling.
    """

    def __init__(self) -> None:
        self._path = paths.lock_file()
        self._fh = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
