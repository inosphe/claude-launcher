"""Durable queue of thread notifications for a session that has exited.

A user message in a thread is typed into the owning session as a
``[claunch thread <id>]`` block (daemon/threads_api.py). A held session is
woken for it and a live one is queued into its terminal. An ``exited``
session has neither: only a person's respawn or the daemon's restore brings
it back, and until then there is nothing to type into. Those notices are
recorded here and typed in when the session is launched again.

The journal is the queue, exactly as in :mod:`claude_launcher.daemon.session_input`:
each notice is one append-only record, so the queue survives a daemon
restart with nothing else to persist, and an entry's latest status decides
whether :func:`flush` still owes it to the terminal.

Statuses a notice moves through::

    queued -> sent | failed
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List

from . import paths
from .. import journal

log = logging.getLogger(__name__)

_lock = threading.Lock()
_flush_locks: Dict[str, asyncio.Lock] = {}

QUEUED = "queued"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def path(name: str):
    return paths.session_dir(name) / "thread-notices.jsonl"


def _record(name: str, event: str, *, notice: str, text: str, status: str,
            thread: str = "") -> dict:
    data = {"notice": notice, "text": text, "status": status}
    if thread:
        data["thread"] = thread
    return journal.append(path(name), event, data, at=_now())


def _requests(entries: List[dict]) -> List[dict]:
    """One row per notice id, in the order the notices were first made.

    ``status`` is the latest one and ``queued_at`` when the notice was
    recorded.
    """
    rows: Dict[str, dict] = {}
    for entry in entries:
        nid = entry.get("notice")
        if not isinstance(nid, str):
            continue
        row = rows.get(nid)
        if row is None:
            row = rows[nid] = {"notice": nid, "queued_at": entry.get("at")}
        row.update({"text": entry.get("text", ""), "status": entry.get("status"),
                    "thread": entry.get("thread", "")})
    return list(rows.values())


def requests(name: str) -> List[dict]:
    """The latest state of each notice, oldest first."""
    with _lock:
        entries = journal.read(path(name))
    return _requests(entries)


def pending(name: str) -> List[dict]:
    """Notices still waiting to be typed, oldest first."""
    return [r for r in requests(name) if r["status"] == QUEUED]


def queue(name: str, notice: str, text: str, *, thread: str = "") -> bool:
    """Record one notice for a session that cannot take it now.

    ``notice`` identifies the thing being announced (a thread message's
    ``<thread id>:m<seq>``), so a post that reaches this twice -- a retry,
    two callers -- adds one record, not two. Returns whether the notice is
    recorded; it is not a delivery receipt.
    """
    if not notice or not text:
        return False
    with _lock:
        if any(r["notice"] == notice for r in _requests(journal.read(path(name)))):
            return True
        _record(name, "notice_queued", notice=notice, text=text, status=QUEUED, thread=thread)
    return True


def _settle(name: str, notice: str, event: str, status: str, text: str) -> None:
    with _lock:
        _record(name, event, notice=notice, text=text, status=status)


async def flush(session) -> int:
    """Type every notice queued while ``session`` was ended, oldest first.

    Waits for the program to take input (``Session.deliver`` does that), and
    a notice the composer keeps waiting on is typed in when that line is
    sent or cleared, in order. Stops when the session exits again, leaving
    the rest queued for the next launch. Returns how many were typed.

    The status is written after the typing, so a daemon that dies between
    the two types that notice once more on the next launch; writing it first
    would lose the notice instead, which is the worse failure.
    """
    name = session.sdef.name
    lock = _flush_locks.setdefault(name, asyncio.Lock())
    sent = 0
    async with lock:
        for row in pending(name):
            if getattr(session, "exited", False):
                break
            nid = row["notice"]
            try:
                ok = await session.deliver(row["text"], wait_for_draft=True)
            except Exception as exc:  # noqa: BLE001 -- SessionGone, PTY write
                log.info("queued thread notice %s for %r failed: %s", nid, name, exc)
                _settle(name, nid, "notice_failed", "failed", row["text"])
                break
            if not ok:
                if getattr(session, "exited", False):
                    # Ended again while it waited: the next launch retries it.
                    break
                _settle(name, nid, "notice_failed", "failed", row["text"])
                continue
            _settle(name, nid, "notice_sent", "sent", row["text"])
            sent += 1
    return sent
