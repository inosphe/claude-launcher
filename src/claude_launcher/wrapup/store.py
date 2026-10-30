"""Session wrap-up drafts (claunch-ar8jn).

A session's round leaves entries for three workspace-wide stores: glossary
terms (glossary.py), category placements (category.py) and knowledge records
(knowledge.py). Those stores are shared by every session of the workspace,
so an entry a session writes straight into them is a change nobody looked
at. Here the session files a *draft* instead: a row the dashboard draws in
the session's wrap-up pane, where the user edits it, discards it or confirms
it. Only a confirm writes the entry into its store (daemon/wrapup_api.py
does the writing; this module keeps the rows).

Two writers share a row -- the owning session (file, edit, withdraw) and the
user (edit, discard, confirm) -- so every write is a compare-and-swap on the
row's ``revision``, as in qa/store.py: the caller names the revision it last
read, the write runs inside ``BEGIN IMMEDIATE`` and a moved revision is
refused with :class:`Conflict` carrying the current row. A user confirming
an entry the agent just rewrote gets the new entry back, never a confirm of
text they did not see.

Status:
  draft      waiting on the user
  applying   the user confirmed; the daemon is writing it into its store
  confirmed  written (``applied`` says where)
  failed     the store refused it (``error`` says why); editable, and a
             confirm tries again
  discarded  the user deleted it, or the session withdrew it

``draft`` and ``failed`` are unresolved: they light the pane's button and,
once the session has ended, raise the dashboard card. A row left
``applying`` by a daemon that went down mid-write is opened again as
``failed`` the next time the store is opened -- whether the write landed is
not known, so the user looks before confirming again.

Ended sessions. The daemon's exit hook marks a session ended
(:meth:`WrapupStore.mark_ended`); :meth:`WrapupStore.rail` reports the mark
with the unresolved counts, and the dashboard raises a card for an ended
session that still has unresolved drafts. A new draft from the session (it
was resumed) clears the mark.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

USER = "user"
KINDS = ("glossary", "category", "knowledge")
UNRESOLVED = ("draft", "failed")
OPEN = ("draft", "failed")  # the statuses an edit, discard or confirm accepts
SCHEMA_VERSION = 1

MAX_REASON = 2_000
MAX_ERROR = 2_000
MAX_PAYLOAD = 60_000
MAX_LIVE_PER_SESSION = 100
CATEGORY_OPS = ("node_id", "new", "fold", "split", "group")
ITEM_KINDS = ("session", "issue")


class WrapupError(Exception):
    status = 400

    def __init__(self, message: str, *, current: Optional[dict] = None):
        super().__init__(message)
        self.current = current


class NotFound(WrapupError):
    status = 404


class Forbidden(WrapupError):
    status = 403


class Conflict(WrapupError):
    status = 409


_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL CHECK (length(session) > 0),
    workspace TEXT NOT NULL CHECK (length(workspace) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('glossary', 'category', 'knowledge')),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'applying', 'confirmed', 'failed', 'discarded')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    error TEXT NOT NULL DEFAULT '',
    applied TEXT CHECK (applied IS NULL OR json_valid(applied)),
    edited_by_user INTEGER NOT NULL DEFAULT 0 CHECK (edited_by_user IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_by TEXT,
    CHECK ((status = 'confirmed') = (applied IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS drafts_session ON drafts (session, status);
CREATE TABLE IF NOT EXISTS ended (
    session TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS drafts_revision_advances
BEFORE UPDATE ON drafts
WHEN NEW.revision <> OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'wrapup: a write must advance revision by exactly one');
END;
CREATE TRIGGER IF NOT EXISTS drafts_identity_fixed
BEFORE UPDATE OF id, session, workspace, kind, created_at ON drafts
WHEN NEW.id <> OLD.id OR NEW.session <> OLD.session OR NEW.workspace <> OLD.workspace
     OR NEW.kind <> OLD.kind OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'wrapup: a draft keeps its id, owner, workspace, kind and creation time');
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, name: str, cap: int) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise WrapupError(f"{name} must be a string")
    value = value.strip()
    if len(value) > cap:
        raise WrapupError(f"{name} is longer than {cap} characters")
    return value


def _items(value) -> list[dict]:
    if not isinstance(value, list):
        raise WrapupError("category items must be a list of {kind, ref}")
    out = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in ITEM_KINDS \
                or not isinstance(item.get("ref"), str) or not item["ref"].strip():
            raise WrapupError("each category item is {kind: session|issue, ref}")
        out.append({"kind": item["kind"], "ref": item["ref"].strip()})
    return out


def category_payload(value) -> dict:
    """A ``category_assign`` body: exactly one of node_id / new / fold /
    split / group, with optional ``items`` and ``reason``. What each
    operation needs inside is category.py's to check, at confirm time."""
    if not isinstance(value, dict):
        raise WrapupError("a category draft is a category_assign body (an object)")
    ops = [k for k in CATEGORY_OPS if value.get(k) not in (None, "", {})]
    if len(ops) != 1:
        raise WrapupError("a category draft gives exactly one of node_id, new, fold, split or group")
    op = ops[0]
    out: dict = {}
    if op == "node_id":
        if not isinstance(value["node_id"], str):
            raise WrapupError("node_id must be a string")
        out["node_id"] = value["node_id"].strip()
    else:
        if not isinstance(value[op], dict):
            raise WrapupError(f"{op} must be an object")
        out[op] = value[op]
    if value.get("items") is not None:
        out["items"] = _items(value["items"])
    if value.get("reason"):
        out["reason"] = _text(value["reason"], "reason", MAX_REASON)
    return out


def clean_payload(kind: str, payload) -> dict:
    """The payload as its store will take it. Glossary and knowledge are
    checked by their own ``clean`` now, so a draft that could never be
    confirmed is refused when it is filed, not when the user presses
    confirm."""
    if kind not in KINDS:
        raise WrapupError("kind must be one of " + ", ".join(KINDS))
    if kind == "category":
        out = category_payload(payload)
    elif kind == "glossary":
        from .. import glossary
        try:
            out = glossary.clean(payload)
        except glossary.GlossaryError as exc:
            raise WrapupError(f"glossary: {exc}") from None
    else:
        from .. import knowledge
        try:
            out = knowledge.clean(payload)
        except knowledge.KnowledgeError as exc:
            raise WrapupError(f"knowledge: {exc}") from None
    if len(json.dumps(out, ensure_ascii=False)) > MAX_PAYLOAD:
        raise WrapupError(f"payload is larger than {MAX_PAYLOAD} characters")
    return out


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    d["applied"] = json.loads(d["applied"]) if d["applied"] else None
    d["edited_by_user"] = bool(d["edited_by_user"])
    return d


class WrapupStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise WrapupError(f"{self.path} has schema version {version}; this claunch knows {SCHEMA_VERSION}")
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute(
                "UPDATE drafts SET status='failed', revision=revision+1, updated_at=?, "
                "error='the daemon stopped while writing this entry; check the store before confirming again' "
                "WHERE status='applying'", (_now(),))

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
    def _load(db, did: str) -> dict:
        r = db.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone()
        if r is None:
            raise NotFound(f"no wrap-up draft {did!r}")
        return _row(r)

    @staticmethod
    def _expect(d: dict, revision, statuses=OPEN) -> None:
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise WrapupError("revision (an integer, the one you last read) is required")
        if d["revision"] != revision:
            raise Conflict(
                f"draft {d['id']} is at revision {d['revision']}, not {revision}: it was changed since you "
                "read it. Read the current draft and decide again.", current=d)
        if d["status"] not in statuses:
            raise Conflict(f"draft {d['id']} is {d['status']}; it no longer takes this change", current=d)

    @staticmethod
    def _may_edit(d: dict, actor: str) -> None:
        if actor != USER and actor != d["session"]:
            raise Forbidden(f"draft {d['id']} belongs to session {d['session']}; only it or the user may change it")

    # -- reads -------------------------------------------------------------

    def get(self, did: str) -> dict:
        with self._connect() as db:
            return self._load(db, did)

    def list(self, session: str, *, settled: bool = True) -> dict:
        with self._connect() as db:
            sql = "SELECT * FROM drafts WHERE session = ?"
            if not settled:
                sql += " AND status IN ('draft', 'applying', 'failed')"
            rows = [_row(r) for r in db.execute(sql + " ORDER BY created_at, rowid", (session,))]
            ended = db.execute("SELECT at FROM ended WHERE session = ?", (session,)).fetchone()
        counts = {k: sum(1 for d in rows if d["kind"] == k and d["status"] in UNRESOLVED) for k in KINDS}
        return {
            "session": session,
            "drafts": rows,
            "unresolved": sum(counts.values()),
            "counts": counts,
            "ended_at": ended["at"] if ended else None,
        }

    def unresolved(self, session: str) -> int:
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM drafts WHERE session = ? AND status IN ('draft', 'failed')",
                (session,)).fetchone()[0])

    def rail(self) -> dict:
        """Per session with any draft: unresolved counts by kind, the latest
        change, and when the session ended (None while it runs)."""
        out: dict[str, dict] = {}
        with self._connect() as db:
            for r in db.execute(
                "SELECT session, kind, SUM(status IN ('draft','failed')) AS n, MAX(updated_at) AS at "
                "FROM drafts GROUP BY session, kind"
            ):
                s = out.setdefault(r["session"], {"unresolved": 0, "counts": {k: 0 for k in KINDS},
                                                  "updated_at": "", "ended_at": None})
                s["counts"][r["kind"]] = int(r["n"] or 0)
                s["unresolved"] += int(r["n"] or 0)
                s["updated_at"] = max(s["updated_at"], r["at"] or "")
            for r in db.execute("SELECT session, at FROM ended"):
                if r["session"] in out:
                    out[r["session"]]["ended_at"] = r["at"]
        return out

    # -- writes ------------------------------------------------------------

    def draft(self, actor: str, *, kind: str, payload, workspace: str, reason=None) -> dict:
        if not actor or actor == USER:
            raise Forbidden("drafts are filed by a session")
        value = clean_payload(kind, payload)
        workspace = _text(workspace, "workspace", 500)
        if not workspace:
            raise WrapupError("workspace is required")
        reason = _text(reason, "reason", MAX_REASON)
        did = "wd-" + secrets.token_hex(4)
        at = _now()
        with self._write() as db:
            live = db.execute(
                "SELECT COUNT(*) FROM drafts WHERE session = ? AND status IN ('draft', 'failed', 'applying')",
                (actor,)).fetchone()[0]
            if live >= MAX_LIVE_PER_SESSION:
                raise WrapupError(f"session {actor} already has {live} unresolved drafts "
                                  f"(at most {MAX_LIVE_PER_SESSION}); wait for the user to settle them")
            db.execute(
                "INSERT INTO drafts (id, session, workspace, kind, payload, reason, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (did, actor, workspace, kind, json.dumps(value, ensure_ascii=False), reason, at, at))
            db.execute("DELETE FROM ended WHERE session = ?", (actor,))
            return self._load(db, did)

    def update(self, actor: str, did: str, revision, *, payload=None, reason=None) -> dict:
        with self._write() as db:
            d = self._load(db, did)
            self._may_edit(d, actor)
            self._expect(d, revision)
            value = clean_payload(d["kind"], payload) if payload is not None else d["payload"]
            new_reason = _text(reason, "reason", MAX_REASON) if reason is not None else d["reason"]
            db.execute(
                "UPDATE drafts SET payload=?, reason=?, status='draft', error='', revision=revision+1, "
                "edited_by_user=?, updated_at=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False), new_reason,
                 1 if actor == USER else int(d["edited_by_user"]), _now(), did))
            return self._load(db, did)

    def discard(self, actor: str, did: str, revision) -> dict:
        with self._write() as db:
            d = self._load(db, did)
            self._may_edit(d, actor)
            self._expect(d, revision)
            db.execute(
                "UPDATE drafts SET status='discarded', revision=revision+1, updated_at=?, settled_by=? WHERE id=?",
                (_now(), actor, did))
            return self._load(db, did)

    def claim(self, actor: str, did: str, revision) -> dict:
        """The user's confirm, first half: the row moves to ``applying`` so a
        second confirm (another tab, a double click) is refused while the
        daemon writes it."""
        if actor != USER:
            raise Forbidden("only the user confirms a wrap-up draft")
        with self._write() as db:
            d = self._load(db, did)
            self._expect(d, revision)
            db.execute(
                "UPDATE drafts SET status='applying', error='', revision=revision+1, updated_at=? WHERE id=?",
                (_now(), did))
            return self._load(db, did)

    def settle(self, did: str, *, applied: Optional[dict] = None, error: str = "") -> dict:
        """The confirm's second half: ``applied`` (where the entry went) or
        ``error`` (why its store refused it)."""
        with self._write() as db:
            d = self._load(db, did)
            if d["status"] != "applying":
                raise Conflict(f"draft {did} is {d['status']}, not applying", current=d)
            if applied is not None:
                db.execute(
                    "UPDATE drafts SET status='confirmed', applied=?, revision=revision+1, updated_at=?, "
                    "settled_by=? WHERE id=?",
                    (json.dumps(applied, ensure_ascii=False), _now(), USER, did))
            else:
                db.execute(
                    "UPDATE drafts SET status='failed', error=?, revision=revision+1, updated_at=? WHERE id=?",
                    ((error or "the store refused it")[:MAX_ERROR], _now(), did))
            return self._load(db, did)

    def mark_ended(self, session: str) -> None:
        """Record that ``session`` ended. A session without drafts leaves no
        mark: the rail reports sessions that have drafts."""
        with self._write() as db:
            if db.execute("SELECT 1 FROM drafts WHERE session = ? LIMIT 1", (session,)).fetchone():
                db.execute("INSERT OR REPLACE INTO ended (session, at) VALUES (?, ?)", (session, _now()))
