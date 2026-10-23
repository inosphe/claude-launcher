"""Durable audit trail for operator session-line submissions.

The terminal is an ephemeral transport.  A line submitted from the native
input therefore gets a small, append-only record keyed by a client request
id, so a reconnect can distinguish an accepted line from one that was never
sent.

A line submitted while the session has exited has no terminal to go to, so
it is recorded as ``queued``. A held session is automatically restored;
other exited sessions wait for an explicit relaunch. The journal is the
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

from . import keys as keys_mod, paths
from .. import journal

log = logging.getLogger(__name__)

_lock = threading.Lock()
_flush_locks: Dict[str, asyncio.Lock] = {}
_active_requests: set = set()

QUEUED = "queued"
#: Statuses after which nothing more happens to a request.
FINAL = ("sent", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def path(name: str):
    return paths.session_dir(name) / "input-journal.jsonl"


def _write(name: str, event: str, *, request_id: str, text: str,
           status: str, pid: Optional[int] = None, origin: str = "",
           images: Optional[List[dict]] = None) -> dict:
    data = {"request_id": request_id, "text": text, "status": status}
    if pid is not None:
        data["pid"] = pid
    if origin:
        # Who typed it, when it was not this daemon's own operator: a
        # linked peer's session line (daemon/shadow.py) is ``peer:<machine>``.
        data["origin"] = origin
    if images:
        # The images the line carried (claunch-wxmcw): name, media type and
        # size of each file in the session's ``pastes/`` folder, in the order
        # they were attached. The detail panel shows them beside the text, and
        # a queued line's flush hands them over before the text.
        data["images"] = images
    return journal.append(path(name), event, data, at=_now())


def write(name: str, event: str, *, request_id: str, text: str,
          status: str, pid: Optional[int] = None, origin: str = "",
          images: Optional[List[dict]] = None) -> dict:
    with _lock:
        return _write(name, event, request_id=request_id, text=text,
                      status=status, pid=pid, origin=origin, images=images)


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
                               "submitted_at": entry.get("at"),
                               "images": []}
        row.update({"text": entry.get("text", ""),
                    "status": entry.get("status"),
                    "at": entry.get("at")})
        # The images ride the first event that names them and every later one
        # that does; an event without them (a cancel, a failure raised before
        # the line was built) leaves what the line carried in place.
        if entry.get("images"):
            row["images"] = entry["images"]
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


def queue(name: str, *, request_id: str, text: str,
          images: Optional[List[dict]] = None) -> dict:
    """Record a line for a session that cannot take it now."""
    return write(name, "input_queued", request_id=request_id, text=text,
                 status=QUEUED, images=images)


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
                              text=row["text"], status=status,
                              images=row.get("images"))
    return None


def cancel(name: str, request_id: str) -> Optional[dict]:
    """Withdraw a queued line. None if it is unknown or no longer queued."""
    return _transition(name, request_id, "input_cancelled", "cancelled")


async def type_line(session, text: str, *, keys: bool = True,
                    force: bool = True) -> bytes:
    """Type one operator line into a live session, the way the web session
    line does: one paste when it carries a newline, otherwise text and Enter
    as one keys call (see ``h_session_keys``).

    The remote session line (``daemon/shadow.py``) types through here too,
    with both switches off. ``keys=False``: a line that is only key names
    (``Escape``, ``C-c``, ``Up``) is pasted as the words it spells, never
    pressed -- a peer may type text into the composer and nothing else.
    ``force=False``: an unsent draft on the terminal is never submitted to
    make room; ``send_keys`` holds and then refuses with
    :class:`~claude_launcher.daemon.session.KeyboardHeld`.
    """
    single = "\n" not in text
    if not keys:
        single = single and "\r" not in text and keys_mod.has_text([text])
    if single:
        return await session.send_keys([text, "Enter"], force=force)
    return await session.paste(text, enter=True)


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
            _active_requests.add((name, rid))
            try:
                await session.prepare_operator_input()
                if row.get("images"):
                    # The clipboard and its lock live in the api module, which
                    # imports this one; imported here, at call time, so the
                    # two are not a cycle at load.
                    from . import api as api_mod
                    await api_mod._deliver_queued_images(session, row["images"])
                await type_line(session, row["text"])
            except Exception as exc:  # noqa: BLE001 — SessionGone, PTY write
                log.info("queued input %s for %r failed: %s", rid, name, exc)
                write(name, "input_failed", request_id=rid, text=row["text"],
                      status="failed", pid=session.pid,
                      images=row.get("images"))
                break
            finally:
                _active_requests.discard((name, rid))
            write(name, "input_sent", request_id=rid, text=row["text"],
                  status="sent", pid=session.pid, images=row.get("images"))
            sent += 1
    return sent


def delivering(name: str, request_id: str) -> bool:
    """Whether this daemon is currently flushing this request (not an old receipt)."""
    return (name, request_id) in _active_requests


def flushing(name: str) -> bool:
    """New operator lines join the queue until the current flush releases it."""
    lock = _flush_locks.get(name)
    return lock is not None and lock.locked()
