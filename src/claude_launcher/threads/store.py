"""Session threads store: sub-topic conversations between the user and one
session (claunch-doj98).

A session can hold several lines of work at once, but its terminal is one
scrollback: a side question asked there interleaves with the main task, and
the person has to keep every open topic in their head. A thread is a named
conversation the daemon keeps beside the terminal -- the user writes in the
session's threads pane, the daemon types the message into the session with
the thread's id, and the agent answers into that thread with the thread_*
MCP tools. Each topic keeps its own history and can be read back whole.

Rows:
  threads   one per topic: owning session, title, open/closed, who opened
            it, the last message seq, how far the agent has read, and the
            QA question it was opened about (``qa``, claunch-78dpm)
  messages  the conversation, globally sequenced (``seq``); ``author`` is
            ``user`` or ``agent``

Who may write. The user (the dashboard) may open, post, close and reopen in
any session's threads; a session may do the same only in its own. Posting to
a closed thread reopens it: a reply is a reason to talk again.

``agent_read`` is the seq of the last message the owning session has seen
(it read the thread, or replied in it); user messages past it are what the
agent's thread_list reports as unread. The browser keeps its own read
position (threads-pane.js), so nothing here tracks the user's.

Link to QA (claunch-78dpm). A thread opened to discuss one of the session's
QA questions carries that question's id in ``qa``. It is set once, when the
thread is opened, and never changes: the conversation that follows is about
that question, whatever happens to it. This store checks only the id's
shape; that the question exists and belongs to the same session is the
route's check (daemon/threads_api.py), which can see the QA store. The other
direction -- a question filed out of a thread -- lives on the question
(qa/store.py ``thread``).
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

USER = "user"
AGENT = "agent"
SCHEMA_VERSION = 2

MAX_TITLE = 200
MAX_BODY = 20_000
MAX_OPEN_PER_SESSION = 50
PAGE = 500
HISTORY_PAGE = 20
HISTORY_MAX_PAGE = 100
HISTORY_MESSAGES = 200


class ThreadError(Exception):
    status = 400


class NotFound(ThreadError):
    status = 404


class Forbidden(ThreadError):
    status = 403


class Conflict(ThreadError):
    status = 409


_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL CHECK (length(session) > 0),
    title TEXT NOT NULL CHECK (length(title) > 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_by TEXT NOT NULL CHECK (opened_by IN ('user', 'agent')),
    last_seq INTEGER NOT NULL DEFAULT 0,
    agent_read INTEGER NOT NULL DEFAULT 0,
    qa TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_session ON threads (session, status);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    thread TEXT NOT NULL REFERENCES threads (id),
    session TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('user', 'agent')),
    body TEXT NOT NULL CHECK (length(body) > 0),
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread, seq);
CREATE INDEX IF NOT EXISTS messages_session ON messages (session, author, seq);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, name: str, cap: int, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ThreadError(f"{name} must be a string")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if required and not value:
        raise ThreadError(f"{name} is required")
    if len(value) > cap:
        raise ThreadError(f"{name} is longer than {cap} characters")
    return value


def _qa_id(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"qa-[0-9a-f]{1,32}", value.strip()):
        raise ThreadError("qa must be a QA question id (qa-xxxxxxxx)")
    return value.strip()


def _author(actor: str) -> str:
    return USER if actor == USER else AGENT


def _thread(r: sqlite3.Row, unread: int = 0, last: Optional[dict] = None) -> dict:
    return {
        "id": r["id"],
        "session": r["session"],
        "title": r["title"],
        "status": r["status"],
        "opened_by": r["opened_by"],
        "last_seq": r["last_seq"],
        "agent_read": r["agent_read"],
        "agent_unread": unread,
        "qa": r["qa"],
        "last": last,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _message(r: sqlite3.Row) -> dict:
    return {"seq": r["seq"], "thread": r["thread"], "author": r["author"], "body": r["body"], "at": r["at"]}


class ThreadStore:
    """One SQLite file for the daemon (``daemon_dir()/threads.sqlite3``)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ThreadError(f"{self.path} has schema version {version}; this claunch knows {SCHEMA_VERSION}")
            cols = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
            if cols and "qa" not in cols:
                # v2 (claunch-78dpm): the QA question a thread is about.
                db.execute("ALTER TABLE threads ADD COLUMN qa TEXT")
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=30000")
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
    def _row(db, tid: str) -> sqlite3.Row:
        r = db.execute("SELECT * FROM threads WHERE id = ?", (str(tid or ""),)).fetchone()
        if r is None:
            raise NotFound(f"no thread {tid!r}")
        return r

    @staticmethod
    def _may(row, actor: str) -> None:
        if actor != USER and row["session"] != actor:
            raise Forbidden(f"thread {row['id']} belongs to session {row['session']}; only it (or the user) may write there")

    @staticmethod
    def _unread(db, row) -> int:
        return int(db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread = ? AND author = 'user' AND seq > ?",
            (row["id"], row["agent_read"]),
        ).fetchone()[0])

    @staticmethod
    def _last(db, tid: str) -> Optional[dict]:
        r = db.execute("SELECT * FROM messages WHERE thread = ? ORDER BY seq DESC LIMIT 1", (tid,)).fetchone()
        return _message(r) if r else None

    def _append(self, db, row, actor: str, body: str) -> dict:
        now = _now()
        author = _author(actor)
        cur = db.execute(
            "INSERT INTO messages (thread, session, author, body, at) VALUES (?,?,?,?,?)",
            (row["id"], row["session"], author, body, now),
        )
        seq = int(cur.lastrowid)
        # The agent writing in a thread has seen everything in it so far.
        db.execute(
            "UPDATE threads SET last_seq = ?, updated_at = ?, status = 'open'"
            + (", agent_read = ?" if author == AGENT else "") + " WHERE id = ?",
            (seq, now, seq, row["id"]) if author == AGENT else (seq, now, row["id"]),
        )
        return {"seq": seq, "thread": row["id"], "author": author, "body": body, "at": now}

    # -- writes ------------------------------------------------------------

    def open(self, actor: str, session: str, title, text=None, *, qa=None) -> dict:
        """A new thread in ``session``, optionally with its first message
        and the QA question it is about (``qa``)."""
        session = str(session or "").strip()
        if not session:
            raise ThreadError("session is required: the session the thread belongs to")
        if actor != USER and actor != session:
            raise Forbidden(f"session {actor} may open threads only in its own conversation, not in {session}")
        title = _text(title, "title", MAX_TITLE)
        body = _text(text, "text", MAX_BODY, required=False)
        qa = _qa_id(qa)
        with self._write() as db:
            live = db.execute(
                "SELECT COUNT(*) FROM threads WHERE session = ? AND status = 'open'", (session,)
            ).fetchone()[0]
            if live >= MAX_OPEN_PER_SESSION:
                raise Conflict(f"session {session} already has {live} open threads; close some first")
            tid = "th-" + secrets.token_hex(3)
            while db.execute("SELECT 1 FROM threads WHERE id = ?", (tid,)).fetchone():
                tid = "th-" + secrets.token_hex(3)
            now = _now()
            db.execute(
                "INSERT INTO threads (id, session, title, opened_by, qa, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (tid, session, title, _author(actor), qa, now, now),
            )
            message = self._append(db, self._row(db, tid), actor, body) if body else None
            row = self._row(db, tid)
            return {"thread": _thread(row, self._unread(db, row), message), "message": message}

    def post(self, actor: str, tid: str, text) -> dict:
        body = _text(text, "text", MAX_BODY)
        with self._write() as db:
            row = self._row(db, tid)
            self._may(row, actor)
            reopened = row["status"] == "closed"
            message = self._append(db, row, actor, body)
            row = self._row(db, tid)
            return {"thread": _thread(row, self._unread(db, row), message), "message": message,
                    "reopened": reopened}

    def set_status(self, actor: str, tid: str, status: str) -> dict:
        if status not in ("open", "closed"):
            raise ThreadError("status is open or closed")
        with self._write() as db:
            row = self._row(db, tid)
            self._may(row, actor)
            if row["status"] != status:
                db.execute("UPDATE threads SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), tid))
                row = self._row(db, tid)
            return {"thread": _thread(row, self._unread(db, row), self._last(db, tid))}

    def rename(self, actor: str, tid: str, title) -> dict:
        title = _text(title, "title", MAX_TITLE)
        with self._write() as db:
            row = self._row(db, tid)
            self._may(row, actor)
            db.execute("UPDATE threads SET title = ?, updated_at = ? WHERE id = ?", (title, _now(), tid))
            row = self._row(db, tid)
            return {"thread": _thread(row, self._unread(db, row), self._last(db, tid))}

    # -- reads -------------------------------------------------------------

    def read(self, tid: str, *, after: int = 0, limit: int = PAGE, reader: str = USER) -> dict:
        """A thread and its messages after ``after``. When ``reader`` is the
        owning session, its read position moves to the last message."""
        try:
            after = max(0, int(after or 0))
            limit = max(1, min(PAGE, int(limit or PAGE)))
        except (TypeError, ValueError):
            raise ThreadError("after and limit are integers") from None
        with (self._write() if reader != USER else self._connect()) as db:
            row = self._row(db, tid)
            rows = db.execute(
                "SELECT * FROM messages WHERE thread = ? AND seq > ? ORDER BY seq LIMIT ?", (tid, after, limit)
            ).fetchall()
            messages = [_message(r) for r in rows]
            if reader == row["session"] and messages and messages[-1]["seq"] > row["agent_read"]:
                db.execute("UPDATE threads SET agent_read = ? WHERE id = ?", (messages[-1]["seq"], tid))
                row = self._row(db, tid)
            more = bool(rows) and len(rows) == limit and rows[-1]["seq"] < row["last_seq"]
            return {"thread": _thread(row, self._unread(db, row), self._last(db, tid)),
                    "messages": messages, "more": more}

    def list(self, session: str, *, closed: bool = True) -> dict:
        with self._connect() as db:
            sql = "SELECT * FROM threads WHERE session = ?"
            if not closed:
                sql += " AND status = 'open'"
            rows = db.execute(sql + " ORDER BY status = 'closed', updated_at DESC, id", (session,)).fetchall()
            threads = [_thread(r, self._unread(db, r), self._last(db, r["id"])) for r in rows]
            agent_seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session = ? AND author = 'agent'", (session,)
            ).fetchone()[0]
        return {
            "session": session,
            "threads": threads,
            "open": sum(1 for t in threads if t["status"] == "open"),
            "agent_seq": int(agent_seq),
        }

    def get(self, tid: str) -> dict:
        """One thread's row (no messages, read position untouched)."""
        with self._connect() as db:
            row = self._row(db, tid)
            return _thread(row, self._unread(db, row), self._last(db, tid))

    def link_rows(self, session: str) -> list[dict]:
        """Every thread of ``session``, reduced to what a QA card needs to
        draw a link to it: id, title, status and the question it is about."""
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id, title, status, qa FROM threads WHERE session = ? ORDER BY created_at, id", (session,))]

    def history(self, *, page=1, per_page=HISTORY_PAGE, session=None, status=None) -> dict:
        """Every session's threads with their conversations, most recently
        active first, one page at a time (claunch-8k02k). Each thread carries
        its last ``HISTORY_MESSAGES`` messages in order; ``omitted`` counts
        the older ones left out. ``sessions`` lists every session that has
        threads (with how many), for the page's filter."""
        try:
            page = max(1, int(page or 1))
            per_page = max(1, min(HISTORY_MAX_PAGE, int(per_page or HISTORY_PAGE)))
        except (TypeError, ValueError):
            raise ThreadError("page and per_page are integers") from None
        if status not in (None, "", "open", "closed"):
            raise ThreadError("status is open or closed")
        where, args = [], []
        if session:
            where.append("session = ?")
            args.append(str(session))
        if status:
            where.append("status = ?")
            args.append(status)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as db:
            total = int(db.execute("SELECT COUNT(*) FROM threads" + cond, args).fetchone()[0])
            rows = db.execute(
                "SELECT * FROM threads" + cond + " ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (*args, per_page, (page - 1) * per_page),
            ).fetchall()
            threads = []
            for r in rows:
                t = _thread(r, self._unread(db, r), self._last(db, r["id"]))
                recent = db.execute(
                    "SELECT * FROM messages WHERE thread = ? ORDER BY seq DESC LIMIT ?",
                    (r["id"], HISTORY_MESSAGES),
                ).fetchall()
                count = int(db.execute("SELECT COUNT(*) FROM messages WHERE thread = ?", (r["id"],)).fetchone()[0])
                t["messages"] = [_message(m) for m in reversed(recent)]
                t["omitted"] = count - len(recent)
                threads.append(t)
            sessions = [{"session": r["session"], "threads": int(r["n"])} for r in db.execute(
                "SELECT session, COUNT(*) AS n FROM threads GROUP BY session ORDER BY MAX(updated_at) DESC, session")]
        return {
            "threads": threads,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, -(-total // per_page)),
            "sessions": sessions,
        }

    def rail(self) -> dict:
        """Per session with threads: how many are open, and the seq of the
        last agent message (the page lights its button when that passes
        what this browser has seen)."""
        out: dict[str, dict] = {}
        with self._connect() as db:
            for r in db.execute(
                "SELECT session, SUM(status = 'open') AS open FROM threads GROUP BY session"
            ):
                out[r["session"]] = {"open": int(r["open"] or 0), "agent_seq": 0}
            for r in db.execute(
                "SELECT session, MAX(seq) AS seq FROM messages WHERE author = 'agent' GROUP BY session"
            ):
                out.setdefault(r["session"], {"open": 0, "agent_seq": 0})["agent_seq"] = int(r["seq"] or 0)
        return out
