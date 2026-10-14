"""Session QA store: the questions a session puts to the user (claunch-l8vyi).

A question typed into the terminal (AskUserQuestion, plain prose) scrolls
away under reminders and mesh deliveries, and an interrupted turn cancels
it. Here it is a row the daemon keeps, the dashboard draws in the session's
QA pane, and the user answers there.

Two writers share every row -- the owning session (create, edit, close,
delete) and the user (answer) -- so every write is a compare-and-swap on the
row's ``revision``: the caller names the revision it last read, the write
runs inside ``BEGIN IMMEDIATE`` and is refused with :class:`Conflict`
(carrying the current row) when the revision moved. A user typing an answer
while the agent rewrites the question therefore gets a refusal and the new
question, never an answer silently filed against text they did not see.

The file is SQLite and the rules that keep it consistent are in the schema,
not only in this module: the status/answer pairing is a CHECK constraint and
the revision may only advance by one (a trigger), so a write that bypasses
these functions still cannot leave a row half-answered or roll a revision
back.

Status:
  open      no answer yet
  answered  the user's answer is for the current revision
  stale     answered, then the agent changed the question; the old answer is
            kept and shown, and the user re-confirms or rewrites it
  closed    the agent settled it (kept for the record; not unresolved).
            The user may still rewrite the answer, or ask for a direction
            check against it (:meth:`QaStore.recheck`); either reopens it
            as ``answered`` and the session settles it again with a close
            (claunch-564kn).

``open`` and ``stale`` are what the header button lights up for.

Nudges (claunch-l8vyi.1): the user can press "질문 요청" in the pane to ask
the session whether it has anything to ask. Each press is a row in
``nudges``; it waits (``sent``) until the session either files a question
(``asked`` -- a qa_ask, or a qa_update that changes a question, settles it) or says it has none (``nothing``, with
a note). A newer nudge supersedes a waiting one, so a session has at most
one waiting nudge (a partial unique index), and a settled nudge never
changes again (a trigger).

Link to threads (claunch-78dpm). A question the session files out of a
thread conversation carries that thread's id in ``thread`` (qa_ask
``thread=``). Like the thread's ``qa`` (threads/store.py) it is set at
creation and never changes, so it sits outside the revision protocol: no
edit moves it and no answer depends on it. That the thread exists and is the
same session's is the route's check (daemon/qa_api.py).
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
UNRESOLVED = ("open", "stale")
SCHEMA_VERSION = 3

MAX_TITLE = 300
MAX_BODY = 20_000
MAX_OPTIONS = 12
MAX_LABEL = 200
MAX_DESCRIPTION = 1_000
MAX_ANSWER = 20_000
MAX_LIVE_PER_SESSION = 50


class QaError(Exception):
    status = 400

    def __init__(self, message: str, *, current: Optional[dict] = None):
        super().__init__(message)
        self.current = current


class NotFound(QaError):
    status = 404


class Forbidden(QaError):
    status = 403


class Conflict(QaError):
    status = 409


_SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL CHECK (length(session) > 0),
    title TEXT NOT NULL CHECK (length(title) > 0),
    body TEXT NOT NULL DEFAULT '',
    options TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(options)),
    multi INTEGER NOT NULL DEFAULT 0 CHECK (multi IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    status TEXT NOT NULL DEFAULT 'open',
    answer_text TEXT,
    answer_choices TEXT CHECK (answer_choices IS NULL OR json_valid(answer_choices)),
    answer_revision INTEGER,
    answer_saves INTEGER NOT NULL DEFAULT 0 CHECK (answer_saves >= 0),
    answered_at TEXT,
    thread TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'open' AND answer_saves = 0 AND answer_revision IS NULL)
        OR (status = 'answered' AND answer_saves > 0 AND answer_revision = revision)
        OR (status = 'stale' AND answer_saves > 0 AND answer_revision < revision)
        OR (status = 'closed')
    )
);
CREATE INDEX IF NOT EXISTS questions_session ON questions (session, status);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    question TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER,
    at TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_session ON events (session, seq);
CREATE TRIGGER IF NOT EXISTS questions_revision_advances
BEFORE UPDATE ON questions
WHEN NEW.revision <> OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'qa: a write must advance revision by exactly one');
END;
CREATE TABLE IF NOT EXISTS nudges (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL CHECK (length(session) > 0),
    status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'asked', 'nothing', 'superseded')),
    reply TEXT NOT NULL DEFAULT '',
    questions TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(questions)),
    created_at TEXT NOT NULL,
    settled_at TEXT,
    CHECK ((status = 'sent') = (settled_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS nudges_one_waiting ON nudges (session) WHERE status = 'sent';
CREATE TRIGGER IF NOT EXISTS nudges_settled_fixed
BEFORE UPDATE ON nudges
WHEN OLD.status <> 'sent'
BEGIN
    SELECT RAISE(ABORT, 'qa: a settled nudge does not change');
END;
CREATE TRIGGER IF NOT EXISTS questions_identity_fixed
BEFORE UPDATE OF id, session, created_at ON questions
WHEN NEW.id <> OLD.id OR NEW.session <> OLD.session OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'qa: a question keeps its id, owner and creation time');
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, name: str, cap: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise QaError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise QaError(f"{name} is required")
    if len(value) > cap:
        raise QaError(f"{name} is longer than {cap} characters")
    return value


def _options(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise QaError("options must be a list")
    if len(value) > MAX_OPTIONS:
        raise QaError(f"at most {MAX_OPTIONS} options")
    out, seen = [], set()
    for item in value:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            raise QaError("each option is a string or {label, description}")
        label = _text(item.get("label"), "option label", MAX_LABEL, required=True)
        if label in seen:
            raise QaError(f"option {label!r} is listed twice")
        seen.add(label)
        opt = {"label": label}
        desc = _text(item.get("description"), "option description", MAX_DESCRIPTION)
        if desc:
            opt["description"] = desc
        out.append(opt)
    return out


def _thread_id(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"th-[0-9a-f]{1,32}", value.strip()):
        raise QaError("thread must be a thread id (th-xxxxxx)")
    return value.strip()


def _revision(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QaError("revision is required: the question revision you last read (an integer >= 1)")
    return value


def _nudge_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "session": r["session"],
        "status": r["status"],
        "reply": r["reply"],
        "questions": json.loads(r["questions"]),
        "created_at": r["created_at"],
        "settled_at": r["settled_at"],
    }


def _row(r: sqlite3.Row) -> dict:
    answer = None
    if r["answer_saves"]:
        answer = {
            "text": r["answer_text"] or "",
            "choices": json.loads(r["answer_choices"] or "[]"),
            "revision": r["answer_revision"],
            "at": r["answered_at"],
            "saves": r["answer_saves"],
        }
    return {
        "id": r["id"],
        "session": r["session"],
        "title": r["title"],
        "body": r["body"],
        "options": json.loads(r["options"]),
        "multi": bool(r["multi"]),
        "revision": r["revision"],
        "status": r["status"],
        "answer": answer,
        "thread": r["thread"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


class QaStore:
    """One SQLite file for the daemon (``daemon_dir()/qa.sqlite3``)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise QaError(f"{self.path} has schema version {version}; this claunch knows {SCHEMA_VERSION}", )
            cols = {r[1] for r in db.execute("PRAGMA table_info(questions)")}
            if cols and "thread" not in cols:
                # v3 (claunch-78dpm): the thread a question was filed from.
                db.execute("ALTER TABLE questions ADD COLUMN thread TEXT")
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
        """One write transaction, taken before the first read so the
        revision a caller is compared against cannot move under it."""
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
    def _load(db, qid: str) -> dict:
        r = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        if r is None:
            raise NotFound(f"no question {qid!r} (deleted, or never asked)")
        return _row(r)

    @staticmethod
    def _event(db, q: dict, actor: str, kind: str, data: Optional[dict] = None) -> int:
        cur = db.execute(
            "INSERT INTO events (session, question, actor, kind, revision, at, data) VALUES (?,?,?,?,?,?,?)",
            (q["session"], q["id"], actor, kind, q.get("revision"), _now(),
             json.dumps(data or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _owner(q: dict, actor: str) -> None:
        if actor == USER:
            raise Forbidden("the user answers questions; asking, editing, closing and deleting are the session's")
        if q["session"] != actor:
            raise Forbidden(f"question {q['id']} belongs to session {q['session']}; only it may change it")

    @staticmethod
    def _expect(q: dict, revision: int, who: str) -> None:
        if q["revision"] != revision:
            moved = "the user answered it" if who != USER and (q.get("answer") or {}).get("revision") == q["revision"] \
                else "it was changed"
            raise Conflict(
                f"question {q['id']} is at revision {q['revision']}, not {revision}: {moved} since you read it. "
                "Read the current question and decide again.",
                current=q,
            )

    @staticmethod
    def _settle_nudge(db, session: str, qid: str) -> None:
        """A question filed or rewritten while the user's nudge waits is the
        reply to it (the nudge text offers both)."""
        db.execute(
            "UPDATE nudges SET status='asked', questions=?, settled_at=? WHERE session=? AND status='sent'",
            (json.dumps([qid]), _now(), session),
        )

    @staticmethod
    def _last_nudge(db, session: str) -> Optional[dict]:
        r = db.execute("SELECT * FROM nudges WHERE session = ? ORDER BY rowid DESC LIMIT 1", (session,)).fetchone()
        return _nudge_row(r) if r else None

    @staticmethod
    def _checks(db, qids: list) -> dict:
        """Per question, the user's last direction check and how the session
        settled it: the note of the first close after it, if any."""
        out: dict = {}
        if not qids:
            return out
        marks = ",".join("?" * len(qids))
        for r in db.execute(
            f"SELECT question, MAX(seq) AS seq FROM events WHERE kind = 'recheck' AND question IN ({marks}) "
            "GROUP BY question", qids,
        ):
            ev = db.execute("SELECT at, data FROM events WHERE seq = ?", (r["seq"],)).fetchone()
            data = json.loads(ev["data"] or "{}")
            reply = db.execute(
                "SELECT at, data FROM events WHERE question = ? AND kind = 'close' AND seq > ? ORDER BY seq LIMIT 1",
                (r["question"], r["seq"]),
            ).fetchone()
            out[r["question"]] = {
                "seq": int(r["seq"]),
                "at": ev["at"],
                "note": data.get("note", ""),
                "settled": reply is not None,
                "reply": json.loads(reply["data"] or "{}").get("note", "") if reply else "",
                "settled_at": reply["at"] if reply else None,
            }
        return out

    # -- reads -------------------------------------------------------------

    def get(self, qid: str) -> dict:
        with self._connect() as db:
            return self._load(db, qid)

    def list(self, session: str, *, closed: bool = True) -> dict:
        with self._connect() as db:
            sql = "SELECT * FROM questions WHERE session = ?"
            if not closed:
                sql += " AND status <> 'closed'"
            rows = [_row(r) for r in db.execute(sql + " ORDER BY created_at, id", (session,))]
            checks = self._checks(db, [q["id"] for q in rows])
            for q in rows:
                q["check"] = checks.get(q["id"])
            seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session = ? AND actor <> ?", (session, USER)
            ).fetchone()[0]
            nudge = self._last_nudge(db, session)
        return {
            "session": session,
            "questions": rows,
            "unresolved": sum(1 for q in rows if q["status"] in UNRESOLVED),
            "agent_seq": int(seq),
            "nudge": nudge,
        }

    def link_rows(self, session: str) -> list[dict]:
        """Every question of ``session``, reduced to what a thread needs to
        draw a link to it: id, title, status and the thread it came from."""
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id, title, status, thread FROM questions WHERE session = ? ORDER BY created_at, id",
                (session,))]

    def rail(self) -> dict:
        """Per session: how many questions wait on the user, and the last
        change the session made (the pane opens itself when that moves)."""
        out: dict[str, dict] = {}
        with self._connect() as db:
            for r in db.execute(
                "SELECT session, SUM(status IN ('open','stale')) AS unresolved, COUNT(*) AS total "
                "FROM questions GROUP BY session"
            ):
                out[r["session"]] = {"unresolved": int(r["unresolved"] or 0), "total": int(r["total"]),
                                     "agent_seq": 0}
            for r in db.execute(
                "SELECT session, MAX(seq) AS seq FROM events WHERE actor <> ? GROUP BY session", (USER,)
            ):
                out.setdefault(r["session"], {"unresolved": 0, "total": 0, "agent_seq": 0})
                out[r["session"]]["agent_seq"] = int(r["seq"])
        return out

    # -- the session's writes ---------------------------------------------

    def ask(self, actor: str, *, title, body=None, options=None, multi=False, thread=None) -> dict:
        if not actor or actor == USER:
            raise Forbidden("questions are asked by a session")
        title = _text(title, "title", MAX_TITLE, required=True)
        body = _text(body, "body", MAX_BODY)
        opts = _options(options)
        multi = bool(multi)
        if multi and not opts:
            raise QaError("multi needs options to choose from")
        thread = _thread_id(thread)
        now = _now()
        with self._write() as db:
            live = db.execute(
                "SELECT COUNT(*) FROM questions WHERE session = ? AND status <> 'closed'", (actor,)
            ).fetchone()[0]
            if live >= MAX_LIVE_PER_SESSION:
                raise QaError(f"session {actor} already has {live} questions that are not closed; close some first")
            qid = "qa-" + secrets.token_hex(4)
            db.execute(
                "INSERT INTO questions (id, session, title, body, options, multi, thread, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (qid, actor, title, body, json.dumps(opts, ensure_ascii=False), int(multi), thread, now, now),
            )
            q = self._load(db, qid)
            self._event(db, q, actor, "ask")
            self._settle_nudge(db, actor, qid)
        return q

    def update(self, actor: str, qid: str, revision, *, title=None, body=None, options=None, multi=None) -> dict:
        revision = _revision(revision)
        with self._write() as db:
            q = self._load(db, qid)
            self._owner(q, actor)
            self._expect(q, revision, actor)
            new = {
                "title": q["title"] if title is None else _text(title, "title", MAX_TITLE, required=True),
                "body": q["body"] if body is None else _text(body, "body", MAX_BODY),
                "options": q["options"] if options is None else _options(options),
                "multi": q["multi"] if multi is None else bool(multi),
            }
            if new["multi"] and not new["options"]:
                raise QaError("multi needs options to choose from")
            same = all(new[k] == q[k] for k in new)
            if same and q["status"] != "closed":
                return q
            # A changed question invalidates the answer it had; a closed one
            # edited again is reopened the same way.
            status = "stale" if q["answer"] else "open"
            db.execute(
                "UPDATE questions SET title=?, body=?, options=?, multi=?, status=?, "
                "revision=revision+1, updated_at=? WHERE id=?",
                (new["title"], new["body"], json.dumps(new["options"], ensure_ascii=False),
                 int(new["multi"]), status, _now(), qid),
            )
            after = self._load(db, qid)
            changed = [k for k in new if new[k] != q[k]]
            self._event(db, after, actor, "update", {"changed": changed, "was": q["status"]})
            self._settle_nudge(db, actor, qid)
        return after

    def close(self, actor: str, qid: str, revision, *, note=None) -> dict:
        revision = _revision(revision)
        note = _text(note, "note", MAX_BODY)
        with self._write() as db:
            q = self._load(db, qid)
            self._owner(q, actor)
            self._expect(q, revision, actor)
            if q["status"] == "closed":
                return q
            db.execute("UPDATE questions SET status='closed', revision=revision+1, updated_at=? WHERE id=?",
                       (_now(), qid))
            after = self._load(db, qid)
            self._event(db, after, actor, "close", {"note": note} if note else None)
        return after

    def delete(self, actor: str, qid: str, revision) -> dict:
        revision = _revision(revision)
        with self._write() as db:
            q = self._load(db, qid)
            self._owner(q, actor)
            self._expect(q, revision, actor)
            db.execute("DELETE FROM questions WHERE id = ?", (qid,))
            self._event(db, q, actor, "delete", {"snapshot": q})
        return {"deleted": qid, "session": q["session"]}

    # -- the user's write --------------------------------------------------

    def answer(self, actor: str, qid: str, revision, *, text=None, choices=None) -> dict:
        """File (or rewrite) the user's answer. Returns ``{"question",
        "previous"}`` -- ``previous`` is the answer it replaced, or None."""
        if actor != USER:
            raise Forbidden("only the user answers a question")
        revision = _revision(revision)
        text = _text(text, "answer", MAX_ANSWER)
        if choices is None:
            choices = []
        if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
            raise QaError("choices must be a list of option labels")
        with self._write() as db:
            q = self._load(db, qid)
            self._expect(q, revision, actor)
            reopened = q["status"] == "closed"
            labels = [o["label"] for o in q["options"]]
            unknown = [c for c in choices if c not in labels]
            if unknown:
                raise QaError(f"not an option of this question: {', '.join(unknown)}")
            if len(set(choices)) != len(choices):
                raise QaError("a choice is listed twice")
            if len(choices) > 1 and not q["multi"]:
                raise QaError("this question takes one choice")
            if not text and not choices:
                raise QaError("an answer needs text or a choice")
            choices = [c for c in labels if c in choices]   # the question's order
            previous = q["answer"]
            db.execute(
                "UPDATE questions SET answer_text=?, answer_choices=?, answer_revision=revision+1, "
                "answer_saves=answer_saves+1, answered_at=?, status='answered', revision=revision+1, "
                "updated_at=? WHERE id=?",
                (text, json.dumps(choices, ensure_ascii=False), _now(), _now(), qid),
            )
            after = self._load(db, qid)
            data = {"previous": previous}
            if reopened:
                data["reopened"] = True
            self._event(db, after, USER, "answer", data)
        return {"question": after, "previous": previous, "reopened": reopened}

    def recheck(self, actor: str, qid: str, revision, *, note=None) -> dict:
        """The user asks the session to check its current work against an
        answer it already has (a direction check, claunch-564kn). A closed
        question is reopened as ``answered`` so the session settles the check
        the way it settles any question: qa_close with a note. Returns
        ``{"question", "check", "reopened"}``."""
        if actor != USER:
            raise Forbidden("only the user asks for a direction check")
        revision = _revision(revision)
        note = _text(note, "note", MAX_ANSWER)
        with self._write() as db:
            q = self._load(db, qid)
            self._expect(q, revision, actor)
            if not q["answer"]:
                raise QaError(f"question {qid} has no answer to check the work against; answer it instead")
            if q["status"] == "stale":
                raise QaError(f"question {qid} changed after its answer; confirm or rewrite the answer instead")
            reopened = q["status"] == "closed"
            if reopened:
                edited = db.execute(
                    "SELECT 1 FROM events WHERE question = ? AND kind = 'update' AND revision > ? LIMIT 1",
                    (qid, q["answer"]["revision"]),
                ).fetchone()
                if edited:
                    # Closed after an edit: the answer is for wording the
                    # session no longer shows.
                    raise QaError(f"question {qid} changed after its answer; answer it again instead")
                db.execute("UPDATE questions SET status='answered', answer_revision=revision+1, "
                           "revision=revision+1, updated_at=? WHERE id=?", (_now(), qid))
                after = self._load(db, qid)
            else:
                after = q
            data = {"note": note} if note else {}
            if reopened:
                data["reopened"] = True
            seq = self._event(db, after, USER, "recheck", data)
            check = self._checks(db, [qid]).get(qid) or {"seq": seq}
        return {"question": after, "check": check, "reopened": reopened}

    # -- nudges ------------------------------------------------------------

    def nudge(self, actor: str, session: str) -> dict:
        """The user asks ``session`` whether it has questions. A nudge still
        waiting is superseded by this one."""
        if actor != USER:
            raise Forbidden("only the user nudges a session for questions")
        if not isinstance(session, str) or not session:
            raise QaError("session is required")
        now = _now()
        with self._write() as db:
            db.execute("UPDATE nudges SET status='superseded', settled_at=? WHERE session=? AND status='sent'",
                       (now, session))
            nid = "qn-" + secrets.token_hex(4)
            db.execute("INSERT INTO nudges (id, session, created_at) VALUES (?,?,?)", (nid, session, now))
            n = self._last_nudge(db, session)
            self._event(db, {"session": session, "id": nid}, USER, "nudge")
        return n

    def nothing(self, actor: str, nid: Optional[str] = None, *, note=None) -> dict:
        """The session's reply to a nudge: nothing to ask. ``nid`` None means
        the session's waiting nudge."""
        if not actor or actor == USER:
            raise Forbidden("a nudge is answered by the session it was sent to")
        note = _text(note, "note", MAX_BODY)
        with self._write() as db:
            if nid:
                r = db.execute("SELECT * FROM nudges WHERE id = ?", (nid,)).fetchone()
            else:
                r = db.execute("SELECT * FROM nudges WHERE session = ? AND status = 'sent'", (actor,)).fetchone()
            if r is None:
                raise NotFound(f"no nudge {nid!r}" if nid else f"session {actor} has no nudge waiting")
            n = _nudge_row(r)
            if n["session"] != actor:
                raise Forbidden(f"nudge {n['id']} was sent to session {n['session']}")
            if n["status"] != "sent":
                raise Conflict(f"nudge {n['id']} is already settled ({n['status']})", current=n)
            db.execute("UPDATE nudges SET status='nothing', reply=?, settled_at=? WHERE id=?",
                       (note, _now(), n["id"]))
            after = _nudge_row(db.execute("SELECT * FROM nudges WHERE id = ?", (n["id"],)).fetchone())
            self._event(db, {"session": actor, "id": n["id"]}, actor, "nothing", {"note": note} if note else None)
        return after
