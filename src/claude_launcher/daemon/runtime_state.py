"""Daemon identity on disk: address file, auth token, singleton lock.

``daemon.json`` is written *after* the HTTP server is listening, so its
presence with a live pid doubles as the readiness signal auto-start polls for.
The token is generated once and required on every API call (even loopback —
the CLI reads it from disk automatically, so mandatory auth costs nothing and
removes the "opened to LAN but forgot auth" foot-gun).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Optional

from .. import __version__
from . import paths


def write_daemon_json(host: str, port: int) -> None:
    from datetime import datetime, timezone

    doc = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "version": __version__,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
