"""Mega thread store: clusters of ``(session, thread)`` pairs and the routing
log of what was sent to them (claunch-v5nhp).

A mega thread is one conversation the user writes into once. Behind it stand
several session threads, each with its own history, and one question per
message: which of them is the user talking to? The daemon asks Jev
(``daemon/jev.py``) for each member's probability of ``mine`` against
``not_mine``, turns those into log-odds, and either routes the message to the
single member that cleared the threshold or hands the user a short list to
choose from (``daemon/megathread_route.py``).

Rows:
  mega     one cluster: title, open/closed, timestamps
  members  one ``(session, thread)`` pair per row; ``(mega, thread)`` is the
           key, so one session may stand in a cluster with more than one of
           its threads. A member joins with a thread that already exists; the
           route layer opens one when the session has none
  routes   one row per user message: the verdict, every member's
           probabilities and log-odds, which members the message reached, and
           the input tokens the Jev calls cost. The token column is what the
           budget (``megathread_jev_budget_tokens``) is measured on, so the
           log is the accounting, not a copy of it

The store does not know sessions or threads: it checks that ids are non-empty
and that ``(mega, thread)`` is unique, and leaves ownership to
``daemon/megathread_api.py``, which can see both registries.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA_VERSION = 2

MAX_TITLE = 200
MAX_BODY = 20_000
#: Members one mega thread may hold. Each one costs a Jev call per message,
#: so the cap is a spend cap as much as a UI one.
MAX_MEMBERS = 32
PAGE = 200
#: How far back the budget window looks.
BUDGET_HOURS = 24.0

STATUSES = ("open", "closed")
#: ``auto`` one member took it, ``choose`` the user picked from the offers,
#: ``manual`` the user typed the picks themselves, ``none`` nobody cleared
#: the threshold, ``error`` every Jev call failed.
VERDICTS = ("auto", "choose", "manual", "none", "error")

#: What wrote a ``routes`` row. ``route`` is a message that went through a
#: cluster; ``draft`` is a topic scored before any cluster exists (the create
#: form's Jev preview), whose row carries no cluster (``mega`` is empty). The
#: two are kept in one table because the column this table already has --
#: ``tokens`` -- is the accounting for both, and a second ledger would let the
#: same spend be counted twice or not at all.
KINDS = ("route", "draft")


class MegaError(Exception):
    status = 400


class NotFound(MegaError):
    status = 404


class Conflict(MegaError):
    status = 409


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mega (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(title) > 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    mega TEXT NOT NULL REFERENCES mega(id) ON DELETE CASCADE,
    session TEXT NOT NULL CHECK (length(session) > 0),
    thread TEXT NOT NULL CHECK (length(thread) > 0),
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (mega, thread)
);
CREATE INDEX IF NOT EXISTS members_session ON members(session);
CREATE TABLE IF NOT EXISTS routes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mega TEXT NOT NULL,
    body TEXT NOT NULL,
    at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    threshold REAL NOT NULL,
    candidates TEXT,
    chosen TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    error TEXT,
    kind TEXT NOT NULL DEFAULT 'route'
);
CREATE INDEX IF NOT EXISTS routes_mega ON routes(mega, seq);
"""

#: Applied on top of ``_SCHEMA`` to a file written by an older build. A
#: ``CREATE TABLE IF NOT EXISTS`` cannot add a column to an existing table, so
#: schema 2 (draft rows, ``kind``) needs its own statement. Each entry runs
#: only when ``PRAGMA user_version`` is below the version it belongs to.
_MIGRATIONS = {
    2: ("ALTER TABLE routes ADD COLUMN kind TEXT NOT NULL DEFAULT 'route'",),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id() -> str:
    return "mt-" + secrets.token_hex(3)


def _text(value, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


class MegaThreadStore:
    """One SQLite file for the daemon (``daemon_dir()/megathread.sqlite3``)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise MegaError(f"{self.path} has schema version {version}; this claunch knows {SCHEMA_VERSION}")
            db.executescript(_SCHEMA)
            for target in sorted(_MIGRATIONS):
                if version >= target:
                    continue
                for statement in _MIGRATIONS[target]:
                    try:
                        db.execute(statement)
                    except sqlite3.OperationalError as exc:
                        # A file written by the version this column belongs to
                        # runs the statement first (``CREATE TABLE`` already had
                        # it), and SQLite refuses the duplicate column. The
                        # column being there is the outcome we want either way.
                        if "duplicate column name" not in str(exc).lower():
                            raise
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _row(db, mid: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM mega WHERE id = ?", (str(mid or ""),)).fetchone()
        if row is None:
            raise NotFound(f"no mega thread {mid!r}")
        return row

    @staticmethod
    def _members(db, mid: str) -> list[dict]:
        rows = db.execute(
            "SELECT * FROM members WHERE mega = ? ORDER BY added_at, thread", (mid,)
        ).fetchall()
        return [
            {"session": r["session"], "thread": r["thread"],
             "added_by": r["added_by"], "added_at": r["added_at"]}
            for r in rows
        ]

    @staticmethod
    def _mega(db, row: sqlite3.Row) -> dict:
        members = MegaThreadStore._members(db, row["id"])
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "members": members,
            "sessions": len({m["session"] for m in members}),
        }

    @staticmethod
    def _route(row: sqlite3.Row) -> dict:
        def load(value):
            try:
                return json.loads(value) if value else None
            except ValueError:
                return None

        return {
            "seq": row["seq"],
            "mega": row["mega"],
            "body": row["body"],
            "at": row["at"],
            "verdict": row["verdict"],
            "threshold": row["threshold"],
            "candidates": load(row["candidates"]) or [],
            "chosen": load(row["chosen"]) or [],
            "delivered": bool(row["delivered"]),
            "tokens": int(row["tokens"] or 0),
            "model": row["model"],
            "error": row["error"],
            "kind": row["kind"] if "kind" in row.keys() else "route",
        }

    # -- clusters ----------------------------------------------------------
    def create(self, title: str, *, actor: str = "user") -> dict:
        name = _text(title, MAX_TITLE)
        if not name:
            raise MegaError("a mega thread needs a title")
        mid = _id()
        now = _now()
        with self._write() as db:
            while db.execute("SELECT 1 FROM mega WHERE id = ?", (mid,)).fetchone():
                mid = _id()
            db.execute(
                "INSERT INTO mega (id, title, status, created_at, updated_at) VALUES (?, ?, 'open', ?, ?)",
                (mid, name, now, now),
            )
            return self._mega(db, self._row(db, mid))

    def get(self, mid: str) -> dict:
        with self._connect() as db:
            return self._mega(db, self._row(db, mid))

    def list(self, *, closed: bool = True) -> dict:
        with self._connect() as db:
            sql = "SELECT * FROM mega"
            if not closed:
                sql += " WHERE status = 'open'"
            rows = db.execute(sql + " ORDER BY updated_at DESC, id").fetchall()
            return {"megathreads": [self._mega(db, r) for r in rows], "total": len(rows)}

    def set_status(self, mid: str, status: str) -> dict:
        if status not in STATUSES:
            raise MegaError("status is open or closed")
        with self._write() as db:
            self._row(db, mid)
            db.execute("UPDATE mega SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), mid))
            return self._mega(db, self._row(db, mid))

    def rename(self, mid: str, title: str) -> dict:
        name = _text(title, MAX_TITLE)
        if not name:
            raise MegaError("a mega thread needs a title")
        with self._write() as db:
            self._row(db, mid)
            db.execute("UPDATE mega SET title = ?, updated_at = ? WHERE id = ?", (name, _now(), mid))
            return self._mega(db, self._row(db, mid))

    def delete(self, mid: str) -> dict:
        with self._write() as db:
            self._row(db, mid)
            db.execute("DELETE FROM members WHERE mega = ?", (mid,))
            db.execute("DELETE FROM routes WHERE mega = ?", (mid,))
            db.execute("DELETE FROM mega WHERE id = ?", (mid,))
        return {"deleted": mid}

    # -- members -----------------------------------------------------------
    def add_member(self, mid: str, session: str, thread: str, *, actor: str = "user") -> dict:
        """Join ``(session, thread)``. Idempotent: joining twice is the same
        as joining once, and ``added`` says which happened."""
        session = _text(session, 200)
        thread = _text(thread, 200)
        if not session or not thread:
            raise MegaError("a member is a (session, thread) pair")
        with self._write() as db:
            self._row(db, mid)
            count = int(db.execute("SELECT COUNT(*) FROM members WHERE mega = ?", (mid,)).fetchone()[0])
            have = db.execute(
                "SELECT 1 FROM members WHERE mega = ? AND thread = ?", (mid, thread)
            ).fetchone()
            if have is None and count >= MAX_MEMBERS:
                raise Conflict(f"a mega thread holds at most {MAX_MEMBERS} members")
            if have is None:
                db.execute(
                    "INSERT INTO members (mega, session, thread, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                    (mid, session, thread, _text(actor, 200) or "user", _now()),
                )
                db.execute("UPDATE mega SET updated_at = ? WHERE id = ?", (_now(), mid))
            return {"mega": self._mega(db, self._row(db, mid)), "added": have is None}

    def remove_member(self, mid: str, thread: str) -> dict:
        with self._write() as db:
            self._row(db, mid)
            removed = db.execute(
                "DELETE FROM members WHERE mega = ? AND thread = ?", (mid, str(thread or ""))
            ).rowcount
            if removed:
                db.execute("UPDATE mega SET updated_at = ? WHERE id = ?", (_now(), mid))
            return {"mega": self._mega(db, self._row(db, mid)), "removed": bool(removed)}

    def members(self, mid: str) -> list[dict]:
        with self._connect() as db:
            self._row(db, mid)
            return self._members(db, mid)

    def clusters_of(self, session: str) -> list[dict]:
        """The open mega threads a session stands in (any of its threads)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT mega FROM members WHERE session = ?", (session,)
            ).fetchall()
            out = []
            for row in rows:
                mega = self._mega(db, self._row(db, row["mega"]))
                if mega["status"] == "open":
                    out.append(mega)
            return out

    def drop_session(self, session: str) -> int:
        """Remove every seat a session holds.

        Not an exit hook: the api keeps a seat when its session ends, because
        a resumed session with the same name and thread should find its place
        again. This is the explicit cleanup for when the seat itself is no
        longer wanted.
        """
        with self._write() as db:
            return db.execute("DELETE FROM members WHERE session = ?", (session,)).rowcount

    # -- routing log -------------------------------------------------------
    def record_route(
        self, mid: str, body: str, *, verdict: str, threshold: float,
        candidates: Optional[list] = None, chosen: Optional[list] = None,
        tokens: int = 0, model: str = "", delivered: bool = False,
        error: str = "", kind: str = "route",
    ) -> dict:
        """Write one row of the ledger.

        ``kind='draft'`` is a scoring of a topic before any cluster exists:
        nothing is delivered and there is no cluster to stamp, so ``mid`` is
        empty and the row is only accounting. A routing (``kind='route'``)
        still requires its cluster, and touching it keeps the cluster's
        ``updated_at`` moving.
        """
        if verdict not in VERDICTS:
            raise MegaError(f"verdict is one of {', '.join(VERDICTS)}")
        if kind not in KINDS:
            raise MegaError(f"kind is one of {', '.join(KINDS)}")
        text = _text(body, MAX_BODY)
        if not text:
            raise MegaError("a routed message has text")
        with self._write() as db:
            if kind == "route":
                self._row(db, mid)
            cur = db.execute(
                "INSERT INTO routes (mega, body, at, verdict, threshold, candidates, chosen,"
                " delivered, tokens, model, error, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid if kind == "route" else "", text, _now(), verdict, float(threshold),
                 json.dumps(candidates or [], ensure_ascii=False),
                 json.dumps([str(t) for t in (chosen or [])], ensure_ascii=False),
                 1 if delivered else 0, int(tokens or 0), model or "", error or None, kind),
            )
            if kind == "route":
                db.execute("UPDATE mega SET updated_at = ? WHERE id = ?", (_now(), mid))
            row = db.execute("SELECT * FROM routes WHERE seq = ?", (cur.lastrowid,)).fetchone()
            return self._route(row)

    def routes(self, mid: str, *, limit: int = 50) -> dict:
        try:
            limit = max(1, min(PAGE, int(limit or 50)))
        except (TypeError, ValueError):
            raise MegaError("limit is an integer") from None
        with self._connect() as db:
            self._row(db, mid)
            rows = db.execute(
                "SELECT * FROM routes WHERE mega = ? ORDER BY seq DESC LIMIT ?", (mid, limit)
            ).fetchall()
        return {"routes": [self._route(r) for r in rows], "total": len(rows)}

    def usage_tokens(self, *, hours: float = BUDGET_HOURS, mega: Optional[str] = None,
                     kind: Optional[str] = None) -> int:
        """Input tokens spent in the last ``hours``.

        Decimals are compared as ISO-8601 UTC strings, which sort in time
        order for the fixed format :func:`_now` writes.

        ``kind`` splits the two budgets: a routing is charged to
        ``megathread_jev_budget_tokens`` and a draft to
        ``megathread_draft_budget_tokens``, and neither may count the other's
        spend. Absent, the sum is the whole ledger.
        """
        try:
            hours = max(0.0, float(hours))
        except (TypeError, ValueError):
            raise MegaError("hours is a number") from None
        if kind is not None and kind not in KINDS:
            raise MegaError(f"kind is one of {', '.join(KINDS)}")
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        sql = "SELECT COALESCE(SUM(tokens), 0) FROM routes WHERE at >= ?"
        args: list = [since]
        if mega:
            sql += " AND mega = ?"
            args.append(str(mega))
        if kind:
            sql += " AND kind = ?"
            args.append(str(kind))
        with self._connect() as db:
            return int(db.execute(sql, args).fetchone()[0])
