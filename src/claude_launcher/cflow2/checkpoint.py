"""Transactional recovery copy for the JSON run store.

SQLite's rollback journal and synchronous=EXTRA cover process/OS crashes.
JSON files are readable projections. Once a checkpoint exists it is authoritative;
never fall back to an older JSON state automatically, which can repeat an effect.

Hot readers (the daemon's clock and web API load the same runs many times a
second) go through :func:`read_state` and :func:`read_chart`. Each call still
opens the database and reads the committed revision inside a read transaction;
the verified and parsed value is reused only while that revision and the file's
stat (device, inode, size, mtime) are unchanged. Every write -- this process,
the CLI, another daemon -- bumps the revision and rewrites the file, so a write
anywhere makes the next read verify the checksums and parse again. A state read
verifies only the state; the chart is verified when the chart is read.

A parsed chart is kept apart from that, by the checksum of its text
(:data:`CHART_CACHE_MAX` texts): a commit that moves only the state
re-verifies the chart text but does not parse it again. It does not cover a
clock pass over more live runs than that: on 2026-10-26 the directories the
daemon's clock scans held 46 live runs with 45 distinct chart texts, so each
pass reads every one of them again. What keeps that read cheap is
:class:`..ir.LazyNodes` (a node is built when it is looked up).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Tuple

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


#: Paths whose verified state/chart is kept (least recently used dropped).
CACHE_MAX = 32
_cache_lock = threading.Lock()
_cache: "OrderedDict[Tuple[str, str], Tuple[Any, Any]]" = OrderedDict()


def _identity(conn, path: Path):
    """The committed generation a read transaction sees: the revision (every
    commit and receipt bumps it), SQLite's file change counter (header bytes
    24-27, bumped by any write transaction, ours or not) and the file's stat,
    taken while the shared lock keeps writers out."""
    _version(conn)
    row = conn.execute("SELECT revision FROM snapshot WHERE id=1").fetchone()
    if row is None:
        raise CheckpointError("checkpoint has no committed state")
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        st = os.fstat(fd)
        header = os.read(fd, 28)
    finally:
        os.close(fd)
    return (row[0], header[24:28], st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _cached(path: Path, part: str, identity):
    key = (str(path), part)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None or hit[0] != identity:
            return None
        _cache.move_to_end(key)
        return hit


def _remember(path: Path, part: str, identity, value) -> None:
    with _cache_lock:
        _cache[(str(path), part)] = (identity, value)
        _cache.move_to_end((str(path), part))
        while len(_cache) > CACHE_MAX * 2:
            _cache.popitem(last=False)


#: Parsed charts by the checksum of their text (least recently used dropped).
#: Runs of one workflow version share the entry.
CHART_CACHE_MAX = 32
_charts: "OrderedDict[str, Any]" = OrderedDict()


def _parsed_chart(sha: str):
    with _cache_lock:
        value = _charts.get(sha)
        if value is not None:
            _charts.move_to_end(sha)
        return value


def _keep_chart(sha: str, value) -> None:
    with _cache_lock:
        _charts[sha] = value
        _charts.move_to_end(sha)
        while len(_charts) > CHART_CACHE_MAX:
            _charts.popitem(last=False)


def _state(path: Path) -> Tuple[str, Any]:
    """The committed state text, and its parsed object on a fresh verify
    (None when the text came from the cache)."""
    with connect(path) as conn:
        conn.execute("BEGIN")  # identity and body from one committed generation
        identity = _identity(conn, path)
        hit = _cached(path, "state", identity)
        if hit is not None:
            return hit[1], None
        state, state_sha = conn.execute(
            "SELECT state,state_sha FROM snapshot WHERE id=1").fetchone()
        if not isinstance(state, str) or digest(state) != state_sha:
            raise CheckpointError("checkpoint checksum mismatch")
        parsed = json.loads(state)
        if not isinstance(parsed, dict):
            raise CheckpointError("checkpoint state is not an object")
        _remember(path, "state", identity, state)
        return state, parsed


def read_state(path: Path) -> dict:
    """The committed state as a new dict (callers may change it)."""
    text, parsed = _state(path)
    return parsed if parsed is not None else json.loads(text)


def read_state_text(path: Path) -> str:
    return _state(path)[0]


def read_chart(path: Path, parse: Callable[[str], Any]):
    """``parse(chart text)`` of the committed chart. The parsed value is
    shared between callers of the same chart text: treat it as read-only."""
    with connect(path) as conn:
        conn.execute("BEGIN")
        identity = _identity(conn, path)
        hit = _cached(path, "chart", identity)
        if hit is not None:
            value = _parsed_chart(hit[1])
            if value is not None:
                return value
        row = conn.execute("SELECT body,sha FROM definition WHERE id=1").fetchone()
        if row is None:
            raise CheckpointError("checkpoint has no committed state")
        chart, chart_sha = row
        if not isinstance(chart, str) or digest(chart) != chart_sha:
            raise CheckpointError("checkpoint checksum mismatch")
    value = _parsed_chart(chart_sha)
    if value is None:
        value = parse(chart)
        _keep_chart(chart_sha, value)
    _remember(path, "chart", identity, chart_sha)
    return value


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


def read_tail(path: Path, limit: int) -> list[dict]:
    """The last ``limit`` events, oldest first, each checksum-verified. A
    view's bounded read: unlike ``read(full=True)`` it neither scans the
    whole journal nor runs quick_check, whose cost grows with the run."""
    with connect(path) as conn:
        conn.execute("BEGIN")
        _version(conn)
        rows = conn.execute("SELECT body,sha FROM events ORDER BY id DESC LIMIT ?",
                            (max(int(limit), 0),)).fetchall()
    result = []
    for body, sha in reversed(rows):
        if not isinstance(body, str) or digest(body) != sha:
            raise CheckpointError("checkpoint event checksum mismatch")
        event = json.loads(body)
        if not isinstance(event, dict):
            raise CheckpointError("checkpoint event is not an object")
        result.append(event)
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

    The caller holds the run lock. ``chart`` may be None for an existing
    checkpoint (its committed definition is kept). The first import commits in a private DB
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
            if chart is not None and previous["chart_sha"] != digest(chart):
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
