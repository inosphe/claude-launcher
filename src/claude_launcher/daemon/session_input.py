"""Durable audit trail for operator session-line submissions.

The terminal is an ephemeral transport.  A line submitted from the native
input therefore gets a small, append-only record keyed by a client request
id, so a reconnect can distinguish an accepted line from one that was never
sent.

A line submitted while the session has exited has no terminal to go to, so
it is recorded as ``queued`` and nothing else happens.  The journal is the
queue: when the session is launched again (respawn, resume, or a daemon
restore) :func:`flush` types every request whose latest status is still
``queued``, oldest first, and records ``sent`` or ``failed`` for each.  Until
then the operator may withdraw a line (:func:`cancel`).  Because the queue is
the file, it survives a daemon restart with nothing else to persist.

Statuses a request moves through::

    accepted -> sent | failed                 (live session)
    queued -> accepted -> sent | failed       (typed after a relaunch)
    queued -> cancelled                       (withdrawn before it was typed)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import paths
from .. import journal

log = logging.getLogger(__name__)

_lock = threading.Lock()
_flush_locks: Dict[str, asyncio.Lock] = {}

QUEUED = "queued"
#: Statuses after which nothing more happens to a request.
FINAL = ("sent", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def path(name: str):
    return paths.session_dir(name) / "input-journal.jsonl"


def _write(name: str, event: str, *, request_id: str, text: str,
           status: str, pid: Optional[int] = None) -> dict:
    data = {"request_id": request_id, "text": text, "status": status}
    if pid is not None:
        data["pid"] = pid
    return journal.append(path(name), event, data, at=_now())


def write(name: str, event: str, *, request_id: str, text: str,
          status: str, pid: Optional[int] = None) -> dict:
    with _lock:
        return _write(name, event, request_id=request_id, text=text,
                      status=status, pid=pid)


def read(name: str, *, limit: int = 50) -> List[dict]:
    with _lock:
        entries = journal.read(path(name))
    return entries[-max(1, min(limit, 200)):]


def latest(name: str, request_id: str) -> Optional[dict]:
    for entry in reversed(read(name, limit=200)):
        if entry.get("request_id") == request_id:
            return entry
    return None


def _requests(entries: List[dict]) -> List[dict]:
    """One row per request id, in the order the requests were first made.

    ``status`` is the latest one; ``submitted_at`` is when the line was first
    recorded and ``at`` when its status last changed.
    """
    rows: Dict[str, dict] = {}
    for entry in entries:
        rid = entry.get("request_id")
        if not isinstance(rid, str):
            continue
        row = rows.get(rid)
        if row is None:
            row = rows[rid] = {"request_id": rid,
                               "submitted_at": entry.get("at")}
        row.update({"text": entry.get("text", ""),
                    "status": entry.get("status"),
                    "at": entry.get("at")})
    return list(rows.values())


def requests(name: str, *, limit: int = 50) -> List[dict]:
    """The latest state of each request, oldest first (see :func:`_requests`).

    Read over the whole file rather than the last ``limit`` events, so a
    line that has been waiting a long time is not cut off by newer events.
    """
    with _lock:
        entries = journal.read(path(name))
    return _requests(entries)[-max(1, min(limit, 200)):]


def pending(name: str) -> List[dict]:
    """Requests still waiting to be typed, oldest first."""
    with _lock:
        entries = journal.read(path(name))
    return [r for r in _requests(entries) if r["status"] == QUEUED]


def queue(name: str, *, request_id: str, text: str) -> dict:
    """Record a line for a session that cannot take it now."""
    return write(name, "input_queued", request_id=request_id, text=text,
                 status=QUEUED)


def _transition(name: str, request_id: str, event: str,
                status: str) -> Optional[dict]:
    """Move a request out of ``queued``, or return None if it is not there.

    The check and the write happen under one lock, so a cancel and a flush
    racing for the same line cannot both win.
    """
    with _lock:
        entries = journal.read(path(name))
        for row in _requests(entries):
            if row["request_id"] == request_id:
                if row["status"] != QUEUED:
                    return None
                return _write(name, event, request_id=request_id,
                              text=row["text"], status=status)
    return None


def cancel(name: str, request_id: str) -> Optional[dict]:
    """Withdraw a queued line. None if it is unknown or no longer queued."""
    return _transition(name, request_id, "input_cancelled", "cancelled")


async def type_line(session, text: str) -> None:
    """Type one operator line into a live session, the way the web session
    line does: one paste when it carries a newline, otherwise text and Enter
    as one keys call (see ``h_session_keys``)."""
    if "\n" in text:
        await session.paste(text, enter=True)
    else:
        await session.send_keys([text, "Enter"], force=True)


async def flush(session) -> int:
    """Type every queued line into ``session``, oldest first.

    Waits until the program can take input first (the same readiness wait
    automated deliveries use), then submits any line a human left in the
    composer rather than typing into it. Stops at the first failure or when
    the session exits again; whatever is left stays queued for the next
    launch. Returns the number of lines sent.
    """
    name = session.sdef.name
    lock = _flush_locks.setdefault(name, asyncio.Lock())
    sent = 0
    async with lock:
        if not pending(name):
            return 0
        await session.await_input_ready()
        for row in pending(name):
            if session.exited:
                break
            rid = row["request_id"]
            if _transition(name, rid, "input_accepted", "accepted") is None:
                continue  # cancelled while we waited
            try:
                await session.prepare_operator_input()
                await type_line(session, row["text"])
            except Exception as exc:  # noqa: BLE001 — SessionGone, PTY write
                log.info("queued input %s for %r failed: %s", rid, name, exc)
                write(name, "input_failed", request_id=rid, text=row["text"],
                      status="failed", pid=session.pid)
                break
            write(name, "input_sent", request_id=rid, text=row["text"],
                  status="sent", pid=session.pid)
            sent += 1
    return sent
