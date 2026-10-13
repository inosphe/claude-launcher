"""Transactional recovery copy for the JSON run store.

SQLite's rollback journal and synchronous=EXTRA cover process/OS crashes.
JSON files are readable projections. Once a checkpoint exists it is authoritative;
never fall back to an older JSON state automatically, which can repeat an effect.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .. import atomic

FILE = "checkpoint.sqlite3"
VERSION = 1


class CheckpointError(Exception):
    pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, default=str)


@contextlib.contextmanager
def connect(path: Path, *, create: bool = False):
    # mode=rw prevents a read/diagnosis from creating an empty database.
    # rw also permits SQLite to roll back a hot journal after a crash.
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                               uri=True, timeout=10)
        conn.execute("PRAGMA synchronous=EXTRA")
        conn.execute("PRAGMA fullfsync=ON")
        if create:
            conn.execute("PRAGMA journal_mode=DELETE")
        with conn:
            yield conn
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise CheckpointError(f"checkpoint {path}: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def _schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), "
                 "revision INTEGER NOT NULL, state TEXT NOT NULL, state_sha TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS definition (id INTEGER PRIMARY KEY CHECK(id=1), "
                 "body TEXT NOT NULL, sha TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, "
                 "body TEXT NOT NULL, sha TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS receipts (name TEXT PRIMARY KEY, "
                 "body TEXT NOT NULL, sha TEXT NOT NULL)")
    conn.execute(f"PRAGMA user_version={VERSION}")


def _version(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] != VERSION:
        raise CheckpointError("unsupported or incomplete checkpoint; preserve the run files")


def _snapshot(conn):
    _version(conn)
    row = conn.execute("SELECT revision,state,state_sha,body,sha FROM snapshot "
                       "JOIN definition USING(id) WHERE id=1").fetchone()
    if row is None:
        raise CheckpointError("checkpoint has no committed state")
    rev, state, state_sha, chart, chart_sha = row
    if (not isinstance(state, str) or not isinstance(chart, str)
            or digest(state) != state_sha or digest(chart) != chart_sha):
        raise CheckpointError("checkpoint checksum mismatch")
    if not isinstance(json.loads(state), dict):
        raise CheckpointError("checkpoint state is not an object")
    return {"revision": rev, "state": state, "state_sha": state_sha,
            "chart": chart, "chart_sha": chart_sha}


def read(path: Path, *, full: bool = False):
    with connect(path) as conn:
        conn.execute("BEGIN")  # all reads see one committed generation
        if full and conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CheckpointError("checkpoint failed SQLite quick_check")
        result = _snapshot(conn)
        if full:
            events = []
            for body, sha in conn.execute("SELECT body,sha FROM events ORDER BY id"):
                if (not isinstance(body, str) or digest(body) != sha
                        or not isinstance(json.loads(body), dict)):
                    raise CheckpointError("checkpoint event checksum/type mismatch")
                events.append(body)
            result["journal"] = "".join(body + "\n" for body in events)
            result["receipts"] = {}
            for name, body, sha in conn.execute("SELECT name,body,sha FROM receipts"):
                if (not isinstance(name, str) or Path(name).name != name
                        or not isinstance(body, str) or digest(body) != sha):
                    raise CheckpointError("checkpoint receipt checksum/name mismatch")
                result["receipts"][name] = body
        return result


def read_events(path: Path, names: list[str], limit: int) -> list[dict]:
    """Read a bounded selection without loading receipts or the whole journal."""
    with connect(path) as conn:
        conn.execute("BEGIN")
        _version(conn)
        marks = ",".join("?" for _ in names)
        rows = conn.execute(
            "SELECT body,sha FROM (SELECT id,body,sha FROM events ORDER BY id LIMIT 2048) "
            f"WHERE json_extract(body,'$.event') IN ({marks}) ORDER BY id LIMIT ?",
            (*names, limit))
        result = []
        for body, sha in rows:
            if not isinstance(body, str) or digest(body) != sha:
                raise CheckpointError("checkpoint event checksum mismatch")
            event = json.loads(body)
            if not isinstance(event, dict):
                raise CheckpointError("checkpoint event is not an object")
            result.append(event)
        return result


def metrics_snapshot(path: Path, *, limit: int, byte_limit: int) -> dict:
    """One committed generation, with a bounded, checksum-verified event prefix."""
    with connect(path) as conn:
        conn.execute("BEGIN")
        result = _snapshot(conn)
        events, size, complete = [], 0, True
        for length, body, sha in conn.execute(
                "SELECT length(body),substr(body,1,65537),sha FROM events ORDER BY id LIMIT ?",
                (limit + 1,)):
            if len(events) == limit or length > 65536 or size + len(body.encode()) > byte_limit:
                complete = False
                break
            if digest(body) != sha:
                raise CheckpointError("checkpoint event checksum mismatch")
            event = json.loads(body)
            if not isinstance(event, dict):
                raise CheckpointError("checkpoint event is not an object")
            events.append(event)
            size += len(body.encode())
        result.update(events=events, complete=complete, bytes=size)
        return result


def commit(path: Path, state: str, chart: str, events: list, *, legacy: list,
           receipts: dict):
    """Commit the complete state and its events together, before publishing JSON.

    The caller holds the run lock. The first import commits in a private DB
    before publishing it. Until publication the legacy files remain authoritative.
    Once published, a broken checkpoint must never fall back to those files.
    """
    creating = not path.exists()
    if not creating:
        return _commit(path, state, chart, events, legacy=legacy,
                       receipts=receipts, creating=False)
    with atomic.scratch(path) as tmp:
        try:
            revision = _commit(tmp, state, chart, events, legacy=legacy,
                               receipts=receipts, creating=True)
            # SQLite is closed and its transaction committed before publication.
            # A crash earlier may leave a private tmp DB/journal, never a
            # checkpoint.sqlite3 that would hide the unchanged legacy state.
            _publish(tmp, path)
            return revision
        finally:
            with contextlib.suppress(OSError):
                Path(str(tmp) + "-journal").unlink(missing_ok=True)


def _publish(tmp: Path, path: Path) -> None:
    with tmp.open("r+b") as fh:
        os.fsync(fh.fileno())
    atomic.replace(tmp, path)
    if os.name != "nt":
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _commit(path: Path, state: str, chart: str, events: list, *, legacy: list,
            receipts: dict, creating: bool):
    with connect(path, create=creating) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if creating:
            _schema(conn)
            conn.execute("INSERT INTO definition VALUES (1,?,?)", (chart, digest(chart)))
            revision = 1
        else:
            previous = _snapshot(conn)
            if previous["chart_sha"] != digest(chart):
                raise CheckpointError("chart changed within a run; preserve and recover the slot")
            revision = previous["revision"] + 1
        for event in (legacy if creating else []) + events:
            body = json.dumps(event, ensure_ascii=False, default=str)
            conn.execute("INSERT INTO events(body,sha) VALUES (?,?)", (body, digest(body)))
        if creating:
            for name, body in receipts.items():
                conn.execute("INSERT INTO receipts VALUES (?,?,?)", (name, body, digest(body)))
        conn.execute("INSERT OR REPLACE INTO snapshot VALUES (1,?,?,?)",
                     (revision, state, digest(state)))
    return revision


def receipt(path: Path, name: str, body: str):
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _snapshot(conn)
        conn.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?)", (name, body, digest(body)))
        conn.execute("UPDATE snapshot SET revision=revision+1 WHERE id=1")
