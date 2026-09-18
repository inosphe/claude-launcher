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
    if not events:
        return
    changed = False
    with database() as db:
        for event in events:
            payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
            cursor = db.execute("INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(session,id) DO UPDATE SET at=excluded.at,payload=excluded.payload WHERE records.payload != excluded.payload",
                                (session, event["id"], event.get("at", ""), payload))
            changed |= bool(cursor.rowcount)
        db.commit()
    if changed:
        for hook in list(change_hooks):
            hook()


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


def import_current():
    """Import surviving pre-upgrade briefing/check snapshots once per source."""
    from . import briefing, status_checks
    briefing._restore_cache()
    existing = {(row["session"], row.get("kind")) for row in rows()}
    for name, (_, result) in list(briefing._cache.items()):
        if (name, "briefing") not in existing:
            capture(name, "briefing", result.get("briefing") or {"raw": result.get("raw")}, result.get("generated_at", ""))
    data = status_checks._read()
    presets = {p["id"]: p for p in data["presets"]}
    for name, reports in data["reports"].items():
        if (name, "checks") not in existing:
            for key, report in reports.items():
                capture(name, "checks", {**presets.get(key, {"id": key}), **report}, report.get("reported_at", ""))
