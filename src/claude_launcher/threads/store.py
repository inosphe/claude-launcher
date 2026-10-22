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
            ``user`` or ``agent``; ``images`` lists the pictures attached to
            it -- by the user (claunch-shl2r) or the agent (claunch-euaz6),
            NULL when there are none; ``files`` lists the other files the
            user attached (claunch-5gpcf, daemon/uploads.py), NULL when there
            are none; ``kind``, ``ref`` and ``data`` carry an operator
            conversation's structure (below)

Who may write. The user (the dashboard) may open, post, close and reopen in
any session's threads; a session may do the same only in its own. Posting to
a closed thread reopens it: a reply is a reason to talk again. A guest's post
is the exception (claunch-mgh8x): a guest may not reopen a thread any more
than close it, so its message is kept and the thread stays closed.

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

Guests (claunch-p9wes). The user or the owning session may invite other
sessions into one thread as guests (table ``guests``, at most
``MAX_GUESTS`` per thread). A guest reviews and advises: it reads the
thread and writes in it (``author`` ``guest``, the guest's name in
``guest``), and that is all -- it may not close, rename or invite, and the
work stays the owner's. A guest may leave (uninvite itself). ``read_seq``
is how far the guest has read, the way ``agent_read`` is the owner's.
:meth:`ThreadStore.list` reports the threads a session is a guest in apart
from its own (``guest_threads``), each saying how many messages of the
others it has not read (``unread``) and how many of the owner's came after
its read position (``owner_unread``).

When a guest's session ends, :meth:`ThreadStore.drop_guest` takes it out of
every thread it was in and writes a ``system`` notice in each, so the
thread itself says the guest is gone rather than its messages simply
stopping (claunch-p9wes.1).

Operator conversations (claunch-iw5i8.12.1). An operator v2 keeps its
conversation with the user here too, in threads whose owner is
``operator:<id>`` (:func:`operator_owner`) rather than a session: the operator
outlives the sessions it runs in. Its messages say more than who wrote them:
``kind`` is ``message`` (the default, every session thread's kind), ``ask``
(a question with answer buttons), ``dispatch`` (an instruction relayed to a
session) or ``system`` (a daemon notice, ``author`` ``system``); ``ref`` is
the message's id in the operator's own vocabulary (unique per owner, so an
import can be repeated); ``data`` is the JSON of the rest (level, refs, the
ask's choices and answer, the dispatch's target). daemon/operators_v2.py
decides who may write them and keeps them out of the session-thread surfaces
(rail, history, index), which list sessions.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

USER = "user"
AGENT = "agent"
GUEST = "guest"
SCHEMA_VERSION = 6

MAX_TITLE = 200
MAX_BODY = 20_000
#: Pictures one message may carry; the same bound an opening task has
#: (daemon/opening_images.MAX_IMAGES).
MAX_IMAGES = 10
#: Other files one message may carry (daemon/uploads.MAX_FILES).
MAX_FILES = 10
MAX_OPEN_PER_SESSION = 50
#: Sessions one thread may have as guests (claunch-p9wes).
MAX_GUESTS = 5
PAGE = 500
HISTORY_PAGE = 20
HISTORY_MAX_PAGE = 100
HISTORY_MESSAGES = 200
INDEX_MAX = 2000
RAIL_REPLIES = 5
RAIL_EXCERPT = 160
SYSTEM = "system"
KINDS = ("message", "ask", "dispatch", "system")
#: The agent's simple ask in a session thread (claunch-etw0s): the user
#: answers it in the threads pane, mirroring the operator conversation's
#: ask (daemon/operators_v2.py ASK_TYPES).
ASK_TYPES = ("approve", "choice", "text")
#: A session may have at most this many unanswered asks across its threads
#: (the operator conversation's bound, daemon/operators_v2.py).
MAX_OPEN_ASKS = 50
#: The owner key of an operator's threads (claunch-iw5i8.12.1).
OPERATOR_PREFIX = "operator:"


def operator_owner(op_id: str) -> str:
    return OPERATOR_PREFIX + str(op_id)


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
    author TEXT NOT NULL CHECK (author IN ('user', 'agent', 'system', 'guest')),
    body TEXT NOT NULL,
    at TEXT NOT NULL,
    images TEXT,
    kind TEXT NOT NULL DEFAULT 'message' CHECK (kind IN ('message', 'ask', 'dispatch', 'system')),
    ref TEXT,
    data TEXT,
    files TEXT,
    guest TEXT,
    CHECK (length(body) > 0 OR images IS NOT NULL OR files IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread, seq);
CREATE INDEX IF NOT EXISTS messages_session ON messages (session, author, seq);
CREATE UNIQUE INDEX IF NOT EXISTS messages_ref ON messages (session, ref) WHERE ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS guests (
    thread TEXT NOT NULL REFERENCES threads (id),
    session TEXT NOT NULL CHECK (length(session) > 0),
    invited_by TEXT NOT NULL,
    invited_at TEXT NOT NULL,
    read_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (thread, session)
);
CREATE INDEX IF NOT EXISTS guests_session ON guests (session);
"""


_last_now = ""


def _now() -> str:
    """The current time to the second, never earlier than one this process
    already stamped: the wall clock can step back (VM time sync), and threads
    sort by these stamps."""
    global _last_now
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _last_now = stamp = max(stamp, _last_now)
    return stamp


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


_IMAGE_NAME = re.compile(r"[0-9a-f]{20}\.(png|jpg|gif|webp)")


def _images(value) -> list[dict]:
    """``images`` of a write: ``[{name, type, bytes}]`` as the route stored
    them (daemon/thread_images.py). Only the shape is checked here; the
    files themselves are the route's."""
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ThreadError("images must be a list")
    if len(value) > MAX_IMAGES:
        raise ThreadError(f"a message carries at most {MAX_IMAGES} images ({len(value)} given)")
    out = []
    for item in value:
        if not (isinstance(item, dict) and isinstance(item.get("name"), str)
                and _IMAGE_NAME.fullmatch(item["name"])):
            raise ThreadError(f"{item!r} is not a stored thread image")
        out.append({"name": item["name"], "type": str(item.get("type") or ""),
                    "bytes": int(item.get("bytes") or 0)})
    return out


_FILE_ID = re.compile(r"[0-9a-f]{20}")


def _files(value) -> list[dict]:
    """``files`` of a write: ``[{id, name, bytes}]`` as the route kept them
    (daemon/uploads.py). Only the shape is checked here."""
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ThreadError("files must be a list")
    if len(value) > MAX_FILES:
        raise ThreadError(f"a message carries at most {MAX_FILES} files ({len(value)} given)")
    out = []
    for item in value:
        if not (isinstance(item, dict) and isinstance(item.get("id"), str) and _FILE_ID.fullmatch(item["id"])
                and isinstance(item.get("name"), str) and item["name"]):
            raise ThreadError(f"{item!r} is not a kept thread file")
        out.append({"id": item["id"], "name": item["name"], "bytes": int(item.get("bytes") or 0)})
    return out


def _excerpt(body: str) -> str:
    """The first non-blank line of a message, cut to RAIL_EXCERPT."""
    line = next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")
    return line if len(line) <= RAIL_EXCERPT else line[:RAIL_EXCERPT - 1] + "…"


def _reply_excerpt(body: str, images, files=None) -> str:
    """A reply's notice line: its excerpt, with the pictures it carries
    counted (claunch-euaz6) so an images-only reply is not a blank card --
    the same wording as the threads pane's list (threads-pane.js lastLine)."""
    text = _excerpt(body)
    n = len(json.loads(images)) if images else 0
    k = len(json.loads(files)) if files else 0
    if n:
        text = f"{text} 🖼{n}" if text else f"🖼 이미지 {n}장"
    if k:
        text = f"{text} 📎{k}" if text else f"📎 파일 {k}개"
    return text


def _default_title(db, session: str) -> str:
    """``Thread #N`` for a thread opened without a title: N counts the
    session's threads (closed ones too), moved past a title already taken."""
    n = db.execute("SELECT COUNT(*) FROM threads WHERE session = ?", (session,)).fetchone()[0] + 1
    while db.execute("SELECT 1 FROM threads WHERE session = ? AND title = ?",
                     (session, f"Thread #{n}")).fetchone():
        n += 1
    return f"Thread #{n}"


def _author(actor: str) -> str:
    return USER if actor == USER else AGENT


def _thread(r: sqlite3.Row, unread: int = 0, last: Optional[dict] = None,
            guests: Optional[list] = None) -> dict:
    out = {
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
    if guests is not None:
        out["guests"] = guests
    return out


def _message(r: sqlite3.Row) -> dict:
    raw = r["images"]
    out = {"seq": r["seq"], "thread": r["thread"], "author": r["author"], "body": r["body"], "at": r["at"],
           "images": json.loads(raw) if raw else [], "kind": r["kind"]}
    if r["files"]:
        out["files"] = json.loads(r["files"])
    if r["ref"] is not None:
        out["ref"] = r["ref"]
    if r["data"] is not None:
        out["data"] = json.loads(r["data"])
    if r["guest"] is not None:
        out["guest"] = r["guest"]
    return out


def _kind(value) -> str:
    value = value or "message"
    if value not in KINDS:
        raise ThreadError("kind is one of " + ", ".join(KINDS))
    return value


def _data(value) -> Optional[str]:
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise ThreadError("data must be an object")
    return json.dumps(value, ensure_ascii=False)


#: v3 (claunch-shl2r): ``messages`` gains ``images`` and a body may be empty
#: when a picture is the message. v4 (claunch-iw5i8.12.1): it gains ``kind``,
#: ``ref`` and ``data``, and ``author`` may be ``system``. v5 (claunch-5gpcf):
#: it gains ``files``, and a body may be empty when files are the message. v6 (claunch-p9wes):
#: it gains ``guest`` and ``author`` may be ``guest``. SQLite cannot change
#: a CHECK, so an older table is rebuilt under the new definition with every
#: row and seq kept; ``{cols}`` is the columns the old table has.
_REBUILD_MESSAGES = """
ALTER TABLE messages RENAME TO messages_old;
DROP INDEX IF EXISTS messages_thread;
DROP INDEX IF EXISTS messages_session;
CREATE TABLE messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    thread TEXT NOT NULL REFERENCES threads (id),
    session TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('user', 'agent', 'system', 'guest')),
    body TEXT NOT NULL,
    at TEXT NOT NULL,
    images TEXT,
    kind TEXT NOT NULL DEFAULT 'message' CHECK (kind IN ('message', 'ask', 'dispatch', 'system')),
    ref TEXT,
    data TEXT,
    files TEXT,
    guest TEXT,
    CHECK (length(body) > 0 OR images IS NOT NULL OR files IS NOT NULL)
);
INSERT INTO messages ({cols}) SELECT {cols} FROM messages_old ORDER BY seq;
DROP TABLE messages_old;
"""


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
            mcols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            if mcols and "guest" not in mcols:
                cols = ", ".join(c for c in ("seq", "thread", "session", "author", "body", "at", "images",
                                             "kind", "ref", "data", "files") if c in mcols)
                db.executescript("BEGIN IMMEDIATE;" + _REBUILD_MESSAGES.format(cols=cols) + "COMMIT;")
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
    def _guest_row(db, tid: str, session: str) -> Optional[sqlite3.Row]:
        return db.execute("SELECT * FROM guests WHERE thread = ? AND session = ?", (tid, session)).fetchone()

    def _writer(self, db, row, actor: str) -> str:
        """Who ``actor`` writes as in thread ``row``: the user, the owner
        (``agent``) or one of its guests; anyone else is refused."""
        if actor == USER:
            return USER
        if row["session"] == actor:
            return AGENT
        if self._guest_row(db, row["id"], actor) is not None:
            return GUEST
        raise Forbidden(f"thread {row['id']} belongs to session {row['session']}; only it, its guests "
                        "(or the user) may write there")

    @staticmethod
    def _guests(db, tid: str) -> list[dict]:
        return [{"session": r["session"], "invited_by": r["invited_by"], "invited_at": r["invited_at"]}
                for r in db.execute("SELECT * FROM guests WHERE thread = ? ORDER BY invited_at, session", (tid,))]

    def _view(self, db, row, last: Optional[dict] = None, *, find_last: bool = True) -> dict:
        """The thread as every write and read returns it: with its unread
        count, its last message and its guests."""
        if last is None and find_last:
            last = self._last(db, row["id"])
        return _thread(row, self._unread(db, row), last, self._guests(db, row["id"]))

    @staticmethod
    def _unread(db, row) -> int:
        # A guest's message is input to the owner the way the user's is.
        return int(db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread = ? AND author IN ('user', 'guest') AND seq > ?",
            (row["id"], row["agent_read"]),
        ).fetchone()[0])

    @staticmethod
    def _last(db, tid: str) -> Optional[dict]:
        r = db.execute("SELECT * FROM messages WHERE thread = ? ORDER BY seq DESC LIMIT 1", (tid,)).fetchone()
        return _message(r) if r else None

    def _append(self, db, row, actor: str, body: str, images: Optional[list] = None, *,
                kind: str = "message", ref: Optional[str] = None, data: Optional[str] = None,
                author: Optional[str] = None, at: Optional[str] = None,
                files: Optional[list] = None, guest: Optional[str] = None,
                reopen: bool = True) -> dict:
        """Append one message to ``row``'s thread. ``reopen`` false keeps a
        closed thread closed (a daemon notice is a record, not a reply)."""
        now = at or _now()
        author = author or _author(actor)
        images = images or []
        try:
            cur = db.execute(
                "INSERT INTO messages (thread, session, author, body, at, images, kind, ref, data, files, guest)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["session"], author, body, now, json.dumps(images) if images else None,
                 kind, ref, data, json.dumps(files, ensure_ascii=False) if files else None, guest),
            )
        except sqlite3.IntegrityError as exc:
            if ref is not None and "messages" in str(exc) and "ref" in str(exc):
                raise Conflict(f"{row['session']} already has a message {ref!r}") from None
            raise
        seq = int(cur.lastrowid)
        # The agent writing in a thread has seen everything in it so far.
        # A guest's message does not reopen it (claunch-mgh8x), and a notice
        # that must not count as activity passes ``reopen`` false.
        db.execute(
            "UPDATE threads SET last_seq = ?, updated_at = ?"
            + (", status = 'open'" if author != GUEST and reopen else "")
            + (", agent_read = ?" if author == AGENT else "") + " WHERE id = ?",
            (seq, now, seq, row["id"]) if author == AGENT else (seq, now, row["id"]),
        )
        if author == GUEST and guest:
            # So has a guest writing in it.
            db.execute("UPDATE guests SET read_seq = ? WHERE thread = ? AND session = ?", (seq, row["id"], guest))
        return _message(db.execute("SELECT * FROM messages WHERE seq = ?", (seq,)).fetchone())

    # -- writes ------------------------------------------------------------

    def open(self, actor: str, session: str, title, text=None, *, qa=None, images=None, files=None) -> dict:
        """A new thread in ``session``, optionally with its first message
        (text, attached ``images`` and ``files``, any of them) and the QA question it is about
        (``qa``). A blank title is given the session's next default name
        (``Thread #N``)."""
        session = str(session or "").strip()
        if not session:
            raise ThreadError("session is required: the session the thread belongs to")
        if actor != USER and actor != session:
            raise Forbidden(f"session {actor} may open threads only in its own conversation, not in {session}")
        title = _text(title, "title", MAX_TITLE, required=False)
        body = _text(text, "text", MAX_BODY, required=False)
        images = _images(images)
        files = _files(files)
        qa = _qa_id(qa)
        with self._write() as db:
            if not title:
                title = _default_title(db, session)
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
            message = (self._append(db, self._row(db, tid), actor, body, images, files=files)
                       if body or images or files else None)
            row = self._row(db, tid)
            return {"thread": self._view(db, row, message, find_last=False), "message": message}

    def post(self, actor: str, tid: str, text, *, images=None, kind=None, ref=None, data=None,
             system: bool = False, files=None) -> dict:
        """A message: text, attached ``images``, or both. ``kind``, ``ref``
        and ``data`` give it an operator conversation's structure; ``system``
        writes it as the daemon (``author`` ``system``) on ``actor``'s
        authority."""
        images = _images(images)
        files = _files(files)
        body = _text(text, "text", MAX_BODY, required=not (images or files))
        kind, data = _kind(kind), _data(data)
        with self._write() as db:
            row = self._row(db, tid)
            who = self._writer(db, row, actor)
            reopened = row["status"] == "closed" and (system or who != GUEST)
            message = self._append(db, row, actor, body, images, kind=kind, ref=ref or None, data=data,
                                   author=SYSTEM if system else who, files=files,
                                   guest=actor if who == GUEST and not system else None)
            row = self._row(db, tid)
            return {"thread": self._view(db, row, message), "message": message,
                    "reopened": reopened, "as": who}

    def set_status(self, actor: str, tid: str, status: str) -> dict:
        if status not in ("open", "closed"):
            raise ThreadError("status is open or closed")
        with self._write() as db:
            row = self._row(db, tid)
            self._may(row, actor)
            if row["status"] != status:
                db.execute("UPDATE threads SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), tid))
                row = self._row(db, tid)
            return {"thread": self._view(db, row)}

    def rename(self, actor: str, tid: str, title) -> dict:
        title = _text(title, "title", MAX_TITLE)
        with self._write() as db:
            row = self._row(db, tid)
            self._may(row, actor)
            db.execute("UPDATE threads SET title = ?, updated_at = ? WHERE id = ?", (title, _now(), tid))
            row = self._row(db, tid)
            return {"thread": self._view(db, row)}

    def invite(self, actor: str, tid: str, guest) -> dict:
        """Make session ``guest`` a guest of thread ``tid``: the user or the
        owner may. ``added`` is false when it already was one."""
        guest = str(guest or "").strip()
        if not guest:
            raise ThreadError("guest is required: the session to invite")
        if guest.startswith(OPERATOR_PREFIX):
            raise ThreadError("an operator cannot be a thread's guest")
        with self._write() as db:
            row = self._row(db, tid)
            if str(row["session"]).startswith(OPERATOR_PREFIX):
                raise ThreadError(f"thread {tid} is an operator's conversation; it takes no guests")
            self._may(row, actor)
            if guest == row["session"]:
                raise ThreadError(f"{guest} owns thread {tid}; it cannot be its guest")
            added = self._guest_row(db, tid, guest) is None
            if added:
                n = db.execute("SELECT COUNT(*) FROM guests WHERE thread = ?", (tid,)).fetchone()[0]
                if n >= MAX_GUESTS:
                    raise Conflict(f"thread {tid} already has {n} guests; remove one first")
                db.execute("INSERT INTO guests (thread, session, invited_by, invited_at) VALUES (?,?,?,?)",
                           (tid, guest, _author(actor) if actor == USER else actor, _now()))
            return {"thread": self._view(db, row), "guest": guest, "added": added}

    def uninvite(self, actor: str, tid: str, guest) -> dict:
        """Remove guest ``guest`` from thread ``tid``: the user, the owner,
        or the guest itself (leaving) may."""
        guest = str(guest or "").strip()
        with self._write() as db:
            row = self._row(db, tid)
            if actor != guest:
                self._may(row, actor)
            if self._guest_row(db, tid, guest) is None:
                raise NotFound(f"{guest or '(nobody)'} is not a guest of thread {tid}")
            db.execute("DELETE FROM guests WHERE thread = ? AND session = ?", (tid, guest))
            return {"thread": self._view(db, row), "guest": guest, "removed": True}

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
            elif reader != USER and messages:
                # A guest's read position (no-op for a session that is none).
                db.execute("UPDATE guests SET read_seq = MAX(read_seq, ?) WHERE thread = ? AND session = ?",
                           (messages[-1]["seq"], tid, reader))
            more = bool(rows) and len(rows) == limit and rows[-1]["seq"] < row["last_seq"]
            return {"thread": self._view(db, row), "messages": messages, "more": more}

    def tail(self, tid: str, *, limit: int = 12) -> list[dict]:
        """The thread's last ``limit`` messages, oldest first.

        :meth:`read` pages from the start, which is what a reader working
        through the conversation wants; a reader that has to judge what the
        conversation is about now wants its end (claunch-v5nhp). No read
        cursor moves: this is a look, not a read.
        """
        try:
            limit = max(1, min(PAGE, int(limit or 12)))
        except (TypeError, ValueError):
            raise ThreadError("limit is an integer") from None
        with self._connect() as db:
            self._row(db, tid)
            rows = db.execute(
                "SELECT * FROM messages WHERE thread = ? ORDER BY seq DESC LIMIT ?", (tid, limit)
            ).fetchall()
        return [_message(r) for r in reversed(rows)]

    def list(self, session: str, *, closed: bool = True) -> dict:
        with self._connect() as db:
            sql = "SELECT * FROM threads WHERE session = ?"
            if not closed:
                sql += " AND status = 'open'"
            rows = db.execute(sql + " ORDER BY status = 'closed', updated_at DESC, id", (session,)).fetchall()
            threads = [self._view(db, r) for r in rows]
            agent_seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session = ? AND author = 'agent'", (session,)
            ).fetchone()[0]
            guest_threads = self._guest_threads(db, session, closed=closed)
        return {
            "session": session,
            "threads": threads,
            "guest_threads": guest_threads,
            "open": sum(1 for t in threads if t["status"] == "open"),
            "agent_seq": int(agent_seq),
        }

    def _guest_threads(self, db, session: str, *, closed: bool = True) -> list[dict]:
        """The threads ``session`` is a guest in, each with ``guest``: who
        invited it, when, how far it has read, how many messages of the
        others it has not (``unread``) and how many of those the owner wrote
        (``owner_unread``, claunch-p9wes.1)."""
        sql = ("SELECT t.*, g.invited_by AS g_by, g.invited_at AS g_at, g.read_seq AS g_read"
               " FROM guests g JOIN threads t ON t.id = g.thread WHERE g.session = ?")
        if not closed:
            sql += " AND t.status = 'open'"
        out = []
        for r in db.execute(sql + " ORDER BY t.status = 'closed', t.updated_at DESC, t.id", (session,)).fetchall():
            t = self._view(db, r)
            unread = db.execute(
                "SELECT COUNT(*) FROM messages WHERE thread = ? AND seq > ? AND author != 'system'"
                " AND NOT (author = 'guest' AND guest = ?)", (r["id"], r["g_read"], session)).fetchone()[0]
            owner_unread = db.execute(
                "SELECT COUNT(*) FROM messages WHERE thread = ? AND seq > ? AND author = 'agent'",
                (r["id"], r["g_read"])).fetchone()[0]
            t["guest"] = {"invited_by": r["g_by"], "invited_at": r["g_at"], "read_seq": r["g_read"],
                          "unread": int(unread), "owner_unread": int(owner_unread)}
            out.append(t)
        return out

    def guests_of(self, tid: str) -> list[str]:
        """The names of thread ``tid``'s guests."""
        with self._connect() as db:
            return [g["session"] for g in self._guests(db, str(tid or ""))]

    def get(self, tid: str) -> dict:
        """One thread's row (no messages, read position untouched)."""
        with self._connect() as db:
            row = self._row(db, tid)
            return self._view(db, row)

    def has_image(self, tid: str, name: str) -> bool:
        """Whether a message of thread ``tid`` carries the image ``name``."""
        if not _IMAGE_NAME.fullmatch(str(name or "")):
            return False
        with self._connect() as db:
            for r in db.execute("SELECT images FROM messages WHERE thread = ? AND images IS NOT NULL", (str(tid),)):
                if any(i.get("name") == name for i in json.loads(r["images"])):
                    return True
        return False

    def has_file(self, tid: str, file_id: str, name: str) -> bool:
        """Whether a message of thread ``tid`` carries the file ``file_id``/``name``."""
        if not _FILE_ID.fullmatch(str(file_id or "")):
            return False
        with self._connect() as db:
            for r in db.execute("SELECT files FROM messages WHERE thread = ? AND files IS NOT NULL", (str(tid),)):
                if any(f.get("id") == file_id and f.get("name") == name for f in json.loads(r["files"])):
                    return True
        return False

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
        # Operator conversations are not a session's (claunch-iw5i8.12.1).
        where, args = ["session NOT LIKE ?"], [OPERATOR_PREFIX + "%"]
        if session:
            where.append("session = ?")
            args.append(str(session))
        if status:
            where.append("status = ?")
            args.append(status)
        cond = " WHERE " + " AND ".join(where)
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
                "SELECT session, COUNT(*) AS n FROM threads WHERE session NOT LIKE ? GROUP BY session"
                " ORDER BY MAX(updated_at) DESC, session", (OPERATOR_PREFIX + "%",))]
        return {
            "threads": threads,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, -(-total // per_page)),
            "sessions": sessions,
        }

    def index(self, *, session=None, status=None) -> dict:
        """Every session's threads without their conversations, most recently
        active first, for the threads page's list panel (claunch-zny1o): each
        thread's row with ``messages`` (its message count) and ``excerpt``
        (the first line of its last message) instead of the messages.
        ``sessions`` groups them by session (threads, open, the latest
        ``updated_at``), most recently active first. ``all_sessions`` is every
        session that has threads whatever the filters say (as ``history``
        lists them), for the page's session filter. At most ``INDEX_MAX``
        threads; ``truncated`` says the rest were left out."""
        if status not in (None, "", "open", "closed"):
            raise ThreadError("status is open or closed")
        where, args = ["t.session NOT LIKE ?"], [OPERATOR_PREFIX + "%"]
        if session:
            where.append("t.session = ?")
            args.append(str(session))
        if status:
            where.append("t.status = ?")
            args.append(status)
        cond = " WHERE " + " AND ".join(where)
        with self._connect() as db:
            total = int(db.execute("SELECT COUNT(*) FROM threads t" + cond, args).fetchone()[0])
            rows = db.execute(
                "SELECT t.*,"
                " (SELECT COUNT(*) FROM messages m WHERE m.thread = t.id) AS n,"
                " (SELECT COUNT(*) FROM messages m WHERE m.thread = t.id AND m.author IN ('user', 'guest')"
                "  AND m.seq > t.agent_read) AS unread,"
                " (SELECT body FROM messages m WHERE m.thread = t.id ORDER BY m.seq DESC LIMIT 1) AS last_body"
                " FROM threads t" + cond + " ORDER BY t.updated_at DESC, t.id LIMIT ?",
                (*args, INDEX_MAX),
            ).fetchall()
            every = [{"session": r["session"], "threads": int(r["n"])} for r in db.execute(
                "SELECT session, COUNT(*) AS n FROM threads WHERE session NOT LIKE ? GROUP BY session"
                " ORDER BY MAX(updated_at) DESC, session", (OPERATOR_PREFIX + "%",))]
        threads, groups = [], {}
        for r in rows:
            t = _thread(r, int(r["unread"]))
            del t["last"]
            t["messages"] = int(r["n"])
            t["excerpt"] = _excerpt(r["last_body"]) if r["last_body"] else ""
            threads.append(t)
            g = groups.setdefault(t["session"], {"session": t["session"], "threads": 0, "open": 0,
                                                 "updated_at": t["updated_at"]})
            g["threads"] += 1
            g["open"] += t["status"] == "open"
        return {"threads": threads, "sessions": list(groups.values()), "all_sessions": every,
                "total": total, "truncated": total > len(threads)}

    def rail(self) -> dict:
        """Per session with threads: how many are open, the seq of the
        last agent message (the page lights its button when that passes
        what this browser has seen), and the last RAIL_REPLIES agent
        messages, oldest first.

        ``replies`` (seq, thread, title, excerpt, at) is what the dashboard
        turns into a notice card and a row of its notification history for
        each reply it has not announced yet (claunch-gfv7l), the way a QA
        question is announced from ``waiting`` in the QA rail."""
        out: dict[str, dict] = {}

        def row(session: str) -> dict:
            return out.setdefault(session, {"open": 0, "agent_seq": 0, "replies": []})

        # Sessions only: an operator's conversation has its own view
        # (claunch-iw5i8.12.1).
        skip = OPERATOR_PREFIX + "%"
        with self._connect() as db:
            for r in db.execute(
                "SELECT session, SUM(status = 'open') AS open FROM threads WHERE session NOT LIKE ? GROUP BY session",
                (skip,),
            ):
                row(r["session"])["open"] = int(r["open"] or 0)
            for r in db.execute(
                "SELECT session, MAX(seq) AS seq FROM messages WHERE author = 'agent' AND session NOT LIKE ?"
                " GROUP BY session", (skip,),
            ):
                row(r["session"])["agent_seq"] = int(r["seq"] or 0)
            for r in db.execute(
                "SELECT m.seq, m.session, m.thread, m.body, m.images, m.files, m.at, t.title FROM ("
                " SELECT seq, session, thread, body, images, files, at,"
                " ROW_NUMBER() OVER (PARTITION BY session ORDER BY seq DESC) AS n"
                " FROM messages WHERE author = 'agent' AND session NOT LIKE ?) AS m"
                " JOIN threads t ON t.id = m.thread WHERE m.n <= ? ORDER BY m.seq",
                (skip, RAIL_REPLIES),
            ):
                row(r["session"])["replies"].append({
                    "seq": r["seq"], "thread": r["thread"], "title": r["title"],
                    "excerpt": _reply_excerpt(r["body"], r["images"], r["files"]), "at": r["at"],
                })
        return out

    # -- operator conversations (claunch-iw5i8.12.1) -----------------------

    def import_thread(self, owner: str, title: str, rows: list[dict]) -> Optional[str]:
        """One thread of ``owner`` holding ``rows`` as they were written
        (``author``, ``body``, ``at``, ``kind``, ``ref``, ``data``,
        ``images``), in one transaction: either every row lands or none
        does. The thread is dated by its first and last rows. None when
        there are no rows."""
        if not rows:
            return None
        title = _text(title, "title", MAX_TITLE)
        with self._write() as db:
            tid = "th-" + secrets.token_hex(3)
            while db.execute("SELECT 1 FROM threads WHERE id = ?", (tid,)).fetchone():
                tid = "th-" + secrets.token_hex(3)
            first = rows[0].get("at") or _now()
            db.execute(
                "INSERT INTO threads (id, session, title, opened_by, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (tid, owner, title, USER, first, first),
            )
            row = self._row(db, tid)
            for r in rows:
                author = r.get("author") or AGENT
                if author not in (USER, AGENT, SYSTEM):
                    raise ThreadError(f"author {author!r} is not one of user, agent, system")
                body = str(r.get("body") or "").strip() or "(내용 없음)"
                self._append(db, row, author, body[:MAX_BODY], _images(r.get("images")),
                             kind=_kind(r.get("kind")), ref=r.get("ref"), data=_data(r.get("data")),
                             author=author, at=r.get("at"))
            # An imported conversation is history: nothing in it is the
            # owner's unread input by the store's count.
            db.execute("UPDATE threads SET agent_read = last_seq WHERE id = ?", (tid,))
            return tid

    def by_ref(self, owner: str, ref: str) -> Optional[dict]:
        """The message of ``owner`` whose ``ref`` is ``ref``, or None."""
        with self._connect() as db:
            r = db.execute("SELECT * FROM messages WHERE session = ? AND ref = ?",
                           (owner, str(ref or ""))).fetchone()
            return _message(r) if r else None

    def by_request(self, owner: str, request_id: str) -> Optional[dict]:
        """The message of ``owner`` whose ``data.request_id`` is
        ``request_id`` (a retried write), or None."""
        with self._connect() as db:
            r = db.execute(
                "SELECT * FROM messages WHERE session = ? AND data IS NOT NULL"
                " AND json_extract(data, '$.request_id') = ? ORDER BY seq LIMIT 1",
                (owner, str(request_id)),
            ).fetchone()
            return _message(r) if r else None

    def update_data(self, owner: str, seq: int, data: dict, *, touch: bool = True) -> dict:
        """Replace the ``data`` of ``owner``'s message ``seq`` (an ask that
        was answered); its thread counts as active now unless ``touch`` is
        false (a read mark is not activity)."""
        raw = _data(data)
        with self._write() as db:
            r = db.execute("SELECT * FROM messages WHERE seq = ? AND session = ?", (int(seq), owner)).fetchone()
            if r is None:
                raise NotFound(f"{owner} has no message {seq}")
            db.execute("UPDATE messages SET data = ? WHERE seq = ?", (raw, int(seq)))
            if touch:
                db.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (_now(), r["thread"]))
            return _message(db.execute("SELECT * FROM messages WHERE seq = ?", (int(seq),)).fetchone())

    def open_asks(self, owner: str) -> int:
        """How many of ``owner``'s asks (the agent's, in any of its threads)
        have no answer yet -- the bound a new thread_ask is checked against."""
        with self._connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM messages WHERE session = ? AND kind = 'ask'"
                " AND author = 'agent' AND json_extract(data, '$.answer') IS NULL",
                (owner,),
            ).fetchone()[0]

    def answer(self, actor: str, tid: str, seq: int, *, decision=None, text=None) -> dict:
        """Record ``actor``'s answer to the agent's ask ``seq`` of thread
        ``tid``: only the user answers (the asking session waits, as in the
        operator conversation). ``decision`` is 'approve'/'deny' for an
        'approve' ask and one of the ask's choices for a 'choice' ask; a
        'text' ask takes the answer as its note. Answering twice with a
        different answer is a conflict; the same answer is idempotent."""
        if actor != USER:
            raise Forbidden("only the user answers an ask; the asking session waits for them")
        with self._write() as db:
            row = self._row(db, tid)
            self._may(row, actor)
            r = db.execute("SELECT * FROM messages WHERE seq = ? AND thread = ?", (int(seq), tid)).fetchone()
            if r is None or r["kind"] != "ask" or r["author"] != AGENT:
                raise NotFound(f"thread {tid} has no ask {seq}")
            data = json.loads(r["data"] or "{}")
            ask_type = data.get("type", "text")
            if ask_type == "approve":
                if decision not in ("approve", "deny"):
                    raise ThreadError("decision must be 'approve' or 'deny'")
                note = _text(text, "text", MAX_BODY, required=False)
            elif ask_type == "choice":
                if decision not in (data.get("choices") or []):
                    raise ThreadError("decision must be one of the ask's choices")
                note = _text(text, "text", MAX_BODY, required=False)
            else:
                decision = None
                note = _text(text, "answer", MAX_BODY)
            answer = {"decision": decision, "text": note or None, "at": _now()}
            if (data.get("answer") or {}).get("at"):
                old = {k: data["answer"].get(k) for k in answer}
                if old != answer:
                    raise Conflict("ask already answered")
                return _message(r)
            data["answer"] = answer
            db.execute("UPDATE messages SET data = ? WHERE seq = ?", (_data(data), int(seq)))
            db.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (_now(), tid))
            return _message(db.execute("SELECT * FROM messages WHERE seq = ?", (int(seq),)).fetchone())

    def scan(self, owner: str, *, kinds=None, author: Optional[str] = None, after: int = 0,
             thread: Optional[str] = None, newest: bool = False, limit: Optional[int] = None) -> list[dict]:
        """``owner``'s messages across its threads (or in ``thread``), in seq
        order (``newest`` first when asked), filtered by ``kinds``, ``author``
        and a seq floor."""
        sql, args = "SELECT * FROM messages WHERE session = ? AND seq > ?", [owner, int(after or 0)]
        if thread:
            sql += " AND thread = ?"
            args.append(str(thread))
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args.extend(kinds)
        if author:
            sql += " AND author = ?"
            args.append(author)
        sql += " ORDER BY seq" + (" DESC" if newest else "")
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._connect() as db:
            return [_message(r) for r in db.execute(sql, args)]

    def counts(self, owner: str) -> dict:
        """Messages per thread of ``owner``: ``{thread id: n}``."""
        with self._connect() as db:
            return {r["thread"]: int(r["n"]) for r in db.execute(
                "SELECT thread, COUNT(*) AS n FROM messages WHERE session = ? GROUP BY thread", (owner,))}

    def drop_guest(self, session: str) -> list[dict]:
        """Remove ``session`` from every thread it is a guest in (it ended,
        claunch-tee5g); returns the threads it left, each with ``guest`` set
        to ``session``. Its messages stay in those threads.

        Each thread gets a ``system`` message saying the guest is gone
        (claunch-p9wes.1) -- written where a reader of the conversation sees
        it, and kept from reopening a closed thread. The owner's notification
        is the route's (daemon/threads_api.py)."""
        session = str(session or "")
        left = []
        with self._write() as db:
            rows = db.execute(
                "SELECT t.* FROM guests g JOIN threads t ON t.id = g.thread WHERE g.session = ?"
                " ORDER BY t.status = 'closed', t.id", (session,)).fetchall()
            for row in rows:
                self._append(db, row, session,
                             f"Guest session {session} ended and is no longer in this thread. "
                             "Its messages stay above.",
                             kind="system", author=SYSTEM, reopen=False)
                db.execute("DELETE FROM guests WHERE thread = ? AND session = ?", (row["id"], session))
                left.append({**self._view(db, row), "guest": session})
        return left

    def delete_owner(self, owner: str) -> int:
        """Remove every thread and message of ``owner`` (an operator that was
        deleted); returns how many threads went."""
        with self._write() as db:
            db.execute("DELETE FROM messages WHERE session = ?", (owner,))
            db.execute("DELETE FROM guests WHERE thread IN (SELECT id FROM threads WHERE session = ?)", (owner,))
            return db.execute("DELETE FROM threads WHERE session = ?", (owner,)).rowcount
