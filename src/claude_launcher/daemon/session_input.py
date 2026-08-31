"""Durable audit trail for operator session-line submissions.

The terminal is an ephemeral transport.  A line submitted from the native
input therefore gets a small, append-only record keyed by a client request
id, so a reconnect can distinguish an accepted line from one that was never
sent.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from . import paths
from .. import journal

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def path(name: str):
    return paths.session_dir(name) / "input-journal.jsonl"


def write(name: str, event: str, *, request_id: str, text: str,
          status: str, pid: Optional[int] = None) -> dict:
    data = {"request_id": request_id, "text": text, "status": status}
    if pid is not None:
        data["pid"] = pid
    with _lock:
        return journal.append(path(name), event, data, at=_now())


def read(name: str, *, limit: int = 50) -> List[dict]:
    with _lock:
        entries = journal.read(path(name))
    return entries[-max(1, min(limit, 200)):]


def latest(name: str, request_id: str) -> Optional[dict]:
    for entry in reversed(read(name, limit=200)):
        if entry.get("request_id") == request_id:
            return entry
    return None
