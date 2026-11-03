"""Session retrospectives as records (claunch-lmnlv).

A retrospective is what a session writes about itself at the end of its work:
(A) what it was asked, what came of it, what went wrong and why, and (B) a
review of the cflow definition it followed, with proposals. The text is the
``retrospective`` prompt pack's (prompt_packs/retrospective.md); it reaches a
session two ways -- improv-worker-v2's ``retrospective`` node, after the
settle and before the end decision, and a person's ``claunch retro <session>``
for a session whose run did not end normally.

Until this module the result lived only as a ``RETRO @ <run>`` comment on
the session's issue, where no page gathered it. Here each one is a record,
listed on the dashboard's Retrospection page.

A record is written only through a wrap-up draft of kind ``retrospection``
(wrapup/store.py, daemon/wrapup_api.py): the session files the draft, and it
is written here when the user confirms it in the session's wrap-up pane -- or
at once, without the user, when the draft was filed with ``auto`` (the run
input ``retrospection_auto_record``, default true; the ``auto record:`` line
of a block a person sent). So this module has no writer of its own beyond
:func:`create`, which the confirm calls.

One SQLite file under the launcher home (not per daemon instance and not in a
worktree), every workspace's records in it with the workspace's name, so the
page can show one workspace or all of them. Records are read-only once
written: a retrospective is a record of what a session saw at the time.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

TRIGGERS = ("run", "manual")
MAX_TITLE = 200
MAX_BODY = 50_000
MAX_LIST = 200


class RetrospectionError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def db_path() -> Path:
    return config.launcher_home() / "retrospection.sqlite3"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    session TEXT NOT NULL,
    trigger TEXT NOT NULL,
    run TEXT NOT NULL,
    workflow TEXT NOT NULL,
    issue TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    problems INTEGER NOT NULL,
    proposals INTEGER NOT NULL,
    tags TEXT NOT NULL,
    draft TEXT NOT NULL,
    auto INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_workspace ON records (workspace, created_at);
CREATE INDEX IF NOT EXISTS records_session ON records (session, created_at);
"""


@contextmanager
def connection(path: Optional[Path] = None):
    path = Path(path or db_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(_SCHEMA)
            with db:
                yield db
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        raise RetrospectionError(f"cannot access retrospection store: {exc}", 500) from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, name, cap, required=False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RetrospectionError(f"{name} must be a string")
    value = value.strip()
    if len(value) > cap or (required and not value):
        raise RetrospectionError(f"{name} must contain {'1' if required else '0'}..{cap} characters")
    return value


def _count(value, name) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetrospectionError(f"{name} must be a non-negative integer")
    return value


def _tags(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > 30:
        raise RetrospectionError("tags must be an array of at most 30 strings")
    kept = []
    for item in value:
        item = _text(item, "tags", 200)
        if item and item not in kept:
            kept.append(item)
    return kept


def clean(body) -> dict:
    """A retrospection draft's payload as the store takes it.

    ``title`` and ``body`` (markdown: the pack's ``## A`` and ``## B``) are
    required; ``run``, ``workflow``, ``issue`` name what it looked back on,
    ``problems`` and ``proposals`` are its counts, ``trigger`` says which door
    asked for it (``run``: a run's node; ``manual``: a person's ``claunch
    retro``)."""
    if not isinstance(body, dict):
        raise RetrospectionError("a retrospection is an object")
    trigger = body.get("trigger") or "run"
    if trigger not in TRIGGERS:
        raise RetrospectionError("trigger must be one of " + ", ".join(TRIGGERS))
    return {
        "title": _text(body.get("title"), "title", MAX_TITLE, True),
        "body": _text(body.get("body"), "body", MAX_BODY, True),
        "trigger": trigger,
        "run": _text(body.get("run"), "run", 200),
        "workflow": _text(body.get("workflow"), "workflow", 200),
        "issue": _text(body.get("issue"), "issue", 200),
        "problems": _count(body.get("problems"), "problems"),
        "proposals": _count(body.get("proposals"), "proposals"),
        "tags": _tags(body.get("tags")),
    }


def _row(row, *, full=True) -> dict:
    out = dict(row)
    out["tags"] = json.loads(out["tags"])
    out["auto"] = bool(out["auto"])
    if not full:
        body = out.pop("body")
        out["excerpt"] = body[:280]
        out["chars"] = len(body)
    return out


def create(*, workspace: str, session: str, payload, draft: str = "", auto: bool = False,
           path: Optional[Path] = None) -> dict:
    value = clean(payload)
    workspace = _text(workspace, "workspace", 500, True)
    session = _text(session, "session", 200, True)
    ident = "rt-" + uuid.uuid4().hex[:10]
    with connection(path) as db:
        db.execute(
            "INSERT INTO records (id, workspace, session, trigger, run, workflow, issue, title, body, "
            "problems, proposals, tags, draft, auto, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident, workspace, session, value["trigger"], value["run"], value["workflow"], value["issue"],
             value["title"], value["body"], value["problems"], value["proposals"],
             json.dumps(value["tags"], ensure_ascii=False), draft or "", 1 if auto else 0, _now()))
        return get(ident, db=db)


def get(ident: str, *, db=None, path: Optional[Path] = None) -> dict:
    if db is None:
        with connection(path) as conn:
            return get(ident, db=conn)
    row = db.execute("SELECT * FROM records WHERE id = ?", (ident,)).fetchone()
    if row is None:
        raise RetrospectionError("retrospection record not found", 404)
    return _row(row)


def search(*, workspace: str = "", session: str = "", q: str = "", limit: int = 50,
           path: Optional[Path] = None) -> list:
    """Newest first, without the body (an excerpt and its length instead).

    ``q`` matches casefolded substrings of title, body, session, issue, run
    and workflow -- the same lexical search the knowledge page uses."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
        raise RetrospectionError(f"limit must be an integer 1..{MAX_LIST}")
    sql, args = "SELECT * FROM records WHERE 1=1", []
    if workspace:
        sql += " AND workspace = ?"
        args.append(workspace)
    if session:
        sql += " AND session = ?"
        args.append(session)
    needle = (q or "").strip().casefold()
    with connection(path) as db:
        rows = db.execute(sql + " ORDER BY created_at DESC, rowid DESC", args).fetchall()
    out = []
    for row in rows:
        if needle:
            hay = " ".join(str(row[k]) for k in ("title", "body", "session", "issue", "run", "workflow"))
            if needle not in hay.casefold():
                continue
        out.append(_row(row, full=False))
        if len(out) >= limit:
            break
    return out


def facets(path: Optional[Path] = None) -> dict:
    """What the page's filters offer: workspaces and sessions with counts."""
    with connection(path) as db:
        ws = db.execute("SELECT workspace, COUNT(*) AS n FROM records GROUP BY workspace ORDER BY workspace")
        workspaces = [{"name": r["workspace"], "count": r["n"]} for r in ws]
        ss = db.execute("SELECT session, workspace, COUNT(*) AS n, MAX(created_at) AS at FROM records "
                        "GROUP BY session, workspace ORDER BY at DESC")
        sessions = [{"name": r["session"], "workspace": r["workspace"], "count": r["n"], "latest": r["at"]}
                    for r in ss]
    return {"workspaces": workspaces, "sessions": sessions}
