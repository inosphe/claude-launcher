"""Durable source records shared by Observer and semantic search.

The SQLite archive survives the Observer's display retention limit. Source IDs
make repeated imports idempotent; updated reports retain their original ID.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from . import paths

change_hooks = []


@contextmanager
def database():
    path = paths.daemon_dir() / "search-records.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS records (session TEXT, id TEXT, at TEXT, payload TEXT, PRIMARY KEY(session,id))")
        yield db
    finally:
        db.close()


def remember(session, events):
    remember_many([(session, events)])


def remember_many(batch, *, notify=True):
    """Write several sessions' events in one connection and one transaction.

    ``batch`` is ``[(session, events), ...]``, applied in order, so a later
    entry for the same record wins exactly as a later :func:`remember` call
    would. The unified corpus writes every registered session on each pass --
    three calls per session, each opening the database, creating the table if
    missing and committing -- and on this machine that was hundreds of
    connections per pass (claunch-lol7h).

    ``notify=False`` leaves the change hooks alone. The corpus pass passes it
    for its own writes: it reads the records back later in the same pass, so
    they are already in what it returns, and the hook would only queue a
    second full pass to find them again. It also keeps this function callable
    from a worker thread, where the hook's ``enqueue`` has no running loop.
    Returns whether any row changed.
    """
    batch = [(session, events) for session, events in batch if events]
    if not batch:
        return False
    changed = False
    with database() as db:
        for session, events in batch:
            for event in events:
                payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
                cursor = db.execute("INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(session,id) DO UPDATE SET at=excluded.at,payload=excluded.payload WHERE records.payload != excluded.payload",
                                    (session, event["id"], event.get("at", ""), payload))
                changed |= bool(cursor.rowcount)
        db.commit()
    if changed and notify:
        for hook in list(change_hooks):
            hook()
    return changed


#: The opening task is one record per session, not one per write: the id is
#: fixed so a task edited after creation replaces the old row instead of
#: leaving two records of the same session's job.
OPENING_TASK_ID = "opening-task"


def capture_task(session, task, at=""):
    """Archive a session's opening task so it outlives the registry entry.

    The task is already searchable while the session is registered, because
    the unified corpus reads it off the definition. That is exactly what ends
    when the session is cleared: the definition goes with it, and the opening
    task is the part a reader searches for by memory months later. Stored
    here it is kept beside the Observer records, which already survive that
    removal.

    The text is stored raw rather than as a JSON payload the way
    :func:`capture` stores structured snapshots -- embedding and the result
    excerpt both read this field, and JSON quoting would put escaped newlines
    in front of the reader.
    """
    event = task_event(task, at)
    if event is not None:
        remember(session, [event])
    return event


def task_event(task, at=""):
    """The record :func:`capture_task` stores, without storing it; None for
    an empty task. Split out so a caller batching many sessions' writes into
    one :func:`remember_many` builds the same row."""
    text = str(task or "").strip()
    if not text:
        return None
    return {"id": OPENING_TASK_ID, "kind": "opening-task", "origin": "record",
            "at": at or "", "text": text, "source": "opening-task", "needs_action": False}


def capture(session, kind, payload, at):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    event_id = kind + "-" + hashlib.sha256((session + at + text).encode()).hexdigest()[:24]
    event = {"id": event_id, "kind": kind, "origin": "record", "at": at,
             "text": text, "source": kind, "evidence": payload, "needs_action": False}
    remember(session, [event])
    return event


def rows(session=None, *, kinds=None, limit=None):
    with database() as db:
        clauses, args = [], []
        if session is not None:
            clauses.append("session=?")
            args.append(session)
        if kinds:
            clauses.append("json_extract(payload,'$.kind') IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        sql = "SELECT session,payload FROM records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY at DESC,rowid DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        records = list(reversed(db.execute(sql, args).fetchall()))
    return [{**json.loads(payload), "session": name} for name, payload in records]


def find(session, event_id):
    with database() as db:
        row = db.execute("SELECT payload FROM records WHERE session=? AND id=?", (session, event_id)).fetchone()
    return json.loads(row[0]) if row else None


def kinds_present(pairs):
    """Which ``(session, kind)`` pairs already have a record.

    Asked one pair at a time through the ``(session, id)`` key: a record
    :func:`capture` wrote has the id ``<kind>-<hash>``, so the key narrows
    each lookup to that prefix and the payload is decoded only for the rows
    in it. :func:`import_current` used to answer this by reading every record
    and decoding every payload -- 20610 rows and 22 MB on this machine, 0.6 s
    of event loop on the first Observer snapshot after a daemon start
    (claunch-fh8u1).
    """
    found = set()
    with database() as db:
        for session, kind in dict.fromkeys(pairs):
            if db.execute("SELECT 1 FROM records WHERE session=? AND id>=? AND id<? AND json_extract(payload,'$.kind')=? LIMIT 1",
                          (session, kind + "-", kind + ".", kind)).fetchone():
                found.add((session, kind))
    return found


def import_current():
    """Import surviving pre-upgrade briefing/check snapshots once per source."""
    from . import briefing, status_checks
    briefing._restore_cache()
    briefings = list(briefing._cache.items())
    data = status_checks._read()
    existing = kinds_present([(name, "briefing") for name, _ in briefings]
                             + [(name, "checks") for name in data["reports"]])
    for name, (_, result) in briefings:
        if (name, "briefing") not in existing:
            capture(name, "briefing", result.get("briefing") or {"raw": result.get("raw")}, result.get("generated_at", ""))
    presets = {p["id"]: p for p in data["presets"]}
    for name, reports in data["reports"].items():
        if (name, "checks") not in existing:
            for key, report in reports.items():
                capture(name, "checks", {**presets.get(key, {"id": key}), **report}, report.get("reported_at", ""))
