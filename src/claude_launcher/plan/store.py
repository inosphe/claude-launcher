"""Session plan store (claunch-7jzp8): the plan a session shows the user in
its plan pane, and the decisions the user takes there.

One person aligning with tens of sessions cannot read each one's prose. A
plan here is the same intent cut down to what that person decides on:

  steps     a tree of one-to-few-line steps. A ``step`` carries a status
            the session moves (todo, doing, done, skipped, blocked); a
            ``decision`` is a branch point whose options each state an
            expected effect and a risk, and the user picks one.
  closure   what keeps the session open (th-0b38dc): ``verify`` items a
            person has to check by hand (passed / failed, with a note) and
            ``followup`` candidates the user disposes of (continue here,
            hand over as an issue, drop). The session marks a followup
            done once it has acted on "here" or "issue".

The plan is one JSON document per session, written whole by the session
(``set``) or one node at a time (``patch``). Two writers share it:

* The session's whole-plan write is a compare-and-swap on the document
  ``revision`` (0 for a session that has none yet). Every write -- the
  user's included -- advances it, so a session that rewrites its plan over
  a choice it has not read gets :class:`Conflict` and the current plan.
* The user's decision is a compare-and-swap on the node's ``rev``, which
  moves only when the session changes what the user decides on (title,
  detail, a decision's options). A session marking a step done therefore
  does not refuse the user's click on a branch; a session rewording that
  branch does.

A node rewritten under a result loses it: the result moves to ``previous``
and the node waits on the user again -- the same rule as an edited QA
question (qa/store.py). Node ids are the session's (or minted here) and
carry results across whole-plan rewrites, so a session rewrites its plan
from ``plan_get`` keeping the ids.

The summary (counts, ``waiting``, ``closable`` and the ``blockers`` that
keep it from closing) is computed on every write and stored beside the
document, so the rail that lights every session's button reads one column.
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
SCHEMA_VERSION = 1

MAX_TITLE = 200
MAX_DETAIL = 2_000
MAX_GOAL = 2_000
MAX_NOTE = 4_000
MAX_NODES = 200
MAX_DEPTH = 5
MAX_OPTIONS = 8
MAX_OPTION_TEXT = 1_000

STEP_KINDS = ("step", "decision")
CLOSURE_KINDS = ("verify", "followup")
STEP_STATUS = ("todo", "doing", "done", "skipped", "blocked")
FOLLOWUP_STATUS = ("open", "done")
VERDICTS = ("passed", "failed")
ACTIONS = ("here", "issue", "drop")
FINISHED = ("done", "skipped")

#: What the user decides on, per kind: a change to any of these moves the
#: node's ``rev`` and takes its result away.
CONTENT = {"step": ("title", "detail"), "decision": ("title", "detail", "options"),
           "verify": ("title", "detail"), "followup": ("title", "detail")}


class PlanError(Exception):
    status = 400

    def __init__(self, message: str, *, current: Optional[dict] = None):
        super().__init__(message)
        self.current = current


class NotFound(PlanError):
    status = 404


class Forbidden(PlanError):
    status = 403


class Conflict(PlanError):
    status = 409


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    session TEXT PRIMARY KEY CHECK (length(session) > 0),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    doc TEXT NOT NULL CHECK (json_valid(doc)),
    summary TEXT NOT NULL CHECK (json_valid(summary)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    node TEXT,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER,
    at TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_session ON events (session, seq);
CREATE TRIGGER IF NOT EXISTS plans_revision_advances
BEFORE UPDATE ON plans
WHEN NEW.revision <> OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'plan: a write must advance revision by exactly one');
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, name: str, cap: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PlanError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise PlanError(f"{name} is required")
    if len(value) > cap:
        raise PlanError(f"{name} is longer than {cap} characters")
    return value


def _revision(value, name: str = "revision", *, floor: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        raise PlanError(f"{name} is required: the one you last read (an integer >= {floor})")
    return value


def _options(value) -> list[dict]:
    if not isinstance(value, list) or len(value) < 2:
        raise PlanError("a decision needs options: a list of at least two {label, effect, risk}")
    if len(value) > MAX_OPTIONS:
        raise PlanError(f"a decision has at most {MAX_OPTIONS} options")
    out, seen, recommended = [], set(), 0
    for item in value:
        if not isinstance(item, dict):
            raise PlanError("each option is {label, effect, risk, recommended?}")
        label = _text(item.get("label"), "option label", MAX_TITLE, required=True)
        if label in seen:
            raise PlanError(f"option {label!r} is listed twice")
        seen.add(label)
        opt = {
            "label": label,
            "effect": _text(item.get("effect"), f"option {label!r} effect", MAX_OPTION_TEXT, required=True),
            "risk": _text(item.get("risk"), f"option {label!r} risk", MAX_OPTION_TEXT, required=True),
        }
        if item.get("recommended"):
            opt["recommended"] = True
            recommended += 1
        out.append(opt)
    if recommended > 1:
        raise PlanError("at most one option is recommended")
    return out


def _status(kind: str, value, default: str) -> str:
    allowed = STEP_STATUS if kind == "step" else FOLLOWUP_STATUS if kind == "followup" else ()
    if not allowed:
        if value not in (None, ""):
            raise PlanError(f"a {kind} has no status the session sets; the user's result is its state")
        return ""
    if value in (None, ""):
        return default
    if value not in allowed:
        raise PlanError(f"a {kind}'s status is one of {', '.join(allowed)}, not {value!r}")
    return value


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")


def _content(node: dict) -> tuple:
    return tuple(json.dumps(node.get(k), ensure_ascii=False, sort_keys=True) for k in CONTENT[node["kind"]])


class _Builder:
    """Normalises the session's nodes and carries the user's results over
    from the plan they replace (by id, while the content stays the same)."""

    def __init__(self, old_nodes: dict):
        self.old = old_nodes
        self.ids: set = set()
        self.count = 0

    def node(self, raw, kinds: tuple, depth: int) -> dict:
        if not isinstance(raw, dict):
            raise PlanError("each node is an object {kind, title, ...}")
        self.count += 1
        if self.count > MAX_NODES:
            raise PlanError(f"a plan has at most {MAX_NODES} nodes")
        kind = raw.get("kind") or kinds[0]
        if kind not in kinds:
            raise PlanError(f"node kind {kind!r} does not belong here: one of {', '.join(kinds)}")
        nid = raw.get("id")
        if nid in (None, ""):
            nid = "n-" + secrets.token_hex(3)
            while nid in self.ids or nid in self.old:
                nid = "n-" + secrets.token_hex(3)
        elif not isinstance(nid, str) or not _ID.fullmatch(nid):
            raise PlanError(f"node id {nid!r}: letters, digits, '_', '.', '-' (at most 40)")
        if nid in self.ids:
            raise PlanError(f"node id {nid!r} is used twice")
        self.ids.add(nid)
        node = {"id": nid, "kind": kind,
                "title": _text(raw.get("title"), "node title", MAX_TITLE, required=True),
                "detail": _text(raw.get("detail"), "node detail", MAX_DETAIL)}
        if kind == "decision":
            node["options"] = _options(raw.get("options"))
        elif raw.get("options") not in (None, []):
            raise PlanError(f"only a decision has options (node {nid!r} is a {kind})")
        status = _status(kind, raw.get("status"), "todo" if kind == "step" else "open")
        if status:
            node["status"] = status
        children = raw.get("children") or []
        if children and kind not in STEP_KINDS:
            raise PlanError(f"a {kind} has no children")
        if not isinstance(children, list):
            raise PlanError("children must be a list of nodes")
        if children and depth >= MAX_DEPTH:
            raise PlanError(f"the step tree is at most {MAX_DEPTH} levels deep")
        self.carry(node, self.old.get(nid))
        if kind in STEP_KINDS:
            node["children"] = [self.node(c, STEP_KINDS, depth + 1) for c in children]
        return node

    @staticmethod
    def carry(node: dict, old: Optional[dict]) -> None:
        """The user's result survives only on the content it was given for."""
        if old is None or old["kind"] != node["kind"]:
            node["rev"] = 1
            node["result"] = None
            return
        if _content(old) == _content(node):
            node["rev"] = old["rev"]
            node["result"] = old.get("result")
            if old.get("previous"):
                node["previous"] = old["previous"]
            return
        node["rev"] = old["rev"] + 1
        node["result"] = None
        gone = old.get("result") or old.get("previous")
        if gone:
            node["previous"] = gone


def walk(nodes: list) -> Iterator[dict]:
    for n in nodes:
        yield n
        yield from walk(n.get("children") or [])


def _index(doc: Optional[dict]) -> dict:
    if not doc:
        return {}
    return {n["id"]: n for n in [*walk(doc.get("steps") or []), *(doc.get("closure") or [])]}


def summarize(doc: dict) -> dict:
    """Counts, what waits on the user, and whether the session may close.

    ``waiting`` is what the user can act on now (an unchosen decision, an
    unchecked or failed verify item, an undisposed followup). ``closable``
    holds when nothing waits, every step is done or skipped and every
    followup the user kept ("here", "issue") is done; ``blockers`` lists
    each node that keeps it from holding, with the reason."""
    steps = [n for n in walk(doc.get("steps") or [])]
    closure = doc.get("closure") or []
    blockers = []

    def block(n, why, who):
        blockers.append({"id": n["id"], "kind": n["kind"], "title": n["title"], "why": why, "who": who})

    s_total = s_done = 0
    for n in steps:
        if n["kind"] == "step":
            s_total += 1
            if n.get("status") in FINISHED:
                s_done += 1
            else:
                block(n, f"step {n.get('status')}", "session")
        elif not n.get("result"):
            block(n, "decision not taken", "user")
    for n in closure:
        res = n.get("result") or {}
        if n["kind"] == "verify":
            if not res:
                block(n, "not checked yet", "user")
            elif res.get("verdict") == "failed":
                block(n, "check failed", "session")
        else:
            if not res:
                block(n, "not disposed of", "user")
            elif res.get("action") != "drop" and n.get("status") != "done":
                block(n, f"followup kept ({res.get('action')}) and not done", "session")
    waiting = [b for b in blockers if b["who"] == "user"]
    return {
        "steps": s_total, "done": s_done,
        "decisions": sum(1 for n in steps if n["kind"] == "decision"),
        "verify": sum(1 for n in closure if n["kind"] == "verify"),
        "followups": sum(1 for n in closure if n["kind"] == "followup"),
        "waiting": len(waiting),
        "closable": not blockers,
        "blockers": blockers,
        "current": next((n["title"] for n in steps if n.get("status") == "doing"), None),
    }


class PlanStore:
    """One SQLite file for the daemon (``daemon_dir()/plan.sqlite3``)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise PlanError(f"{self.path} has schema version {version}; this claunch knows {SCHEMA_VERSION}")
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
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        return {"session": r["session"], "revision": int(r["revision"]), **json.loads(r["doc"]),
                "summary": json.loads(r["summary"]), "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def _load(self, db, session: str) -> Optional[dict]:
        return self._row(db.execute("SELECT * FROM plans WHERE session = ?", (session,)).fetchone())

    @staticmethod
    def _event(db, session: str, actor: str, kind: str, revision: int, node: Optional[str] = None,
               data: Optional[dict] = None) -> None:
        db.execute("INSERT INTO events (session, node, actor, kind, revision, at, data) VALUES (?,?,?,?,?,?,?)",
                   (session, node, actor, kind, revision, _now(), json.dumps(data or {}, ensure_ascii=False)))

    def _store(self, db, session: str, before: Optional[dict], doc: dict) -> dict:
        now = _now()
        body = {k: doc[k] for k in ("title", "goal", "issue", "context", "steps", "closure")}
        summary = summarize(body)
        if before is None:
            db.execute("INSERT INTO plans (session, revision, doc, summary, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (session, 1, json.dumps(body, ensure_ascii=False), json.dumps(summary, ensure_ascii=False),
                        now, now))
        else:
            db.execute("UPDATE plans SET revision = revision + 1, doc = ?, summary = ?, updated_at = ? "
                       "WHERE session = ?",
                       (json.dumps(body, ensure_ascii=False), json.dumps(summary, ensure_ascii=False), now, session))
        return self._load(db, session)

    @staticmethod
    def _owner(session: str, actor: str) -> None:
        if actor == USER:
            raise Forbidden("the user decides on a plan; writing it is the session's")
        if actor != session:
            raise Forbidden(f"the plan of {session} is written by {session} alone")

    # -- reads -------------------------------------------------------------

    def get(self, session: str) -> Optional[dict]:
        with self._connect() as db:
            return self._load(db, session)

    def rail(self) -> dict:
        """Per session with a plan: its title, revision, context and summary
        (without the blockers' list; the pane reads that from the plan)."""
        out: dict = {}
        with self._connect() as db:
            for r in db.execute("SELECT session, revision, doc, summary, updated_at FROM plans"):
                doc = json.loads(r["doc"])
                summary = json.loads(r["summary"])
                summary.pop("blockers", None)
                out[r["session"]] = {"title": doc.get("title", ""), "revision": int(r["revision"]),
                                     "issue": doc.get("issue"), "context": doc.get("context") or {},
                                     "updated_at": r["updated_at"], **summary}
        return out

    # -- the session's writes ---------------------------------------------

    def set(self, actor: str, session: str, revision, *, title, goal=None, issue=None, steps=None, closure=None,
            context: Optional[dict] = None) -> dict:
        """Write the whole plan. ``revision`` is the one last read, 0 when
        the session has no plan yet."""
        self._owner(session, actor)
        revision = _revision(revision, floor=0)
        if steps is not None and not isinstance(steps, list):
            raise PlanError("steps must be a list of nodes")
        if closure is not None and not isinstance(closure, list):
            raise PlanError("closure must be a list of nodes")
        title = _text(title, "title", MAX_TITLE, required=True)
        goal = _text(goal, "goal", MAX_GOAL)
        issue = _text(issue, "issue", 80) or None
        with self._write() as db:
            before = self._load(db, session)
            at = before["revision"] if before else 0
            if at != revision:
                raise Conflict(
                    f"the plan of {session} is at revision {at}, not {revision}: it changed since you read it "
                    "(the user may have decided on it). Read it again and rewrite it from what it says now.",
                    current=before)
            build = _Builder(_index(before))
            doc = {"title": title, "goal": goal, "issue": issue,
                   "context": context if context is not None else (before or {}).get("context") or {},
                   "steps": [build.node(n, STEP_KINDS, 1) for n in steps or []],
                   "closure": [build.node(n, CLOSURE_KINDS, 1) for n in closure or []]}
            after = self._store(db, session, before, doc)
            self._event(db, session, actor, "set", after["revision"], data={"nodes": build.count})
        return after

    def patch(self, actor: str, session: str, node_id: str, **fields) -> dict:
        """Change one node's title, detail, status or options. No revision:
        a step's status is the session's alone, and a change to what the
        user decides on moves the node's ``rev`` (which the user's click
        names), so it cannot land on a decision taken against other text."""
        self._owner(session, actor)
        fields = {k: v for k, v in fields.items() if v is not None}
        unknown = set(fields) - {"title", "detail", "status", "options"}
        if unknown:
            raise PlanError(f"a node patch takes title, detail, status and options, not {', '.join(sorted(unknown))}")
        if not fields:
            raise PlanError("nothing to change: give title, detail, status or options")
        with self._write() as db:
            before = self._load(db, session)
            if before is None:
                raise NotFound(f"{session} has no plan; write one with plan_set first")
            old = _index(before).get(node_id)
            if old is None:
                raise NotFound(f"the plan of {session} has no node {node_id!r}")
            doc = json.loads(json.dumps({k: before[k] for k in ("title", "goal", "issue", "context", "steps",
                                                                 "closure")}))
            target = _index(doc)[node_id]
            raw = {**{k: target.get(k) for k in ("id", "kind", "title", "detail", "status", "options")}, **fields}
            fresh = _Builder({node_id: old}).node({**raw, "children": []}, (target["kind"],), 1)
            fresh.pop("children", None)
            keep = target.get("children")
            target.clear()
            target.update(fresh)
            if keep is not None:
                target["children"] = keep
            after = self._store(db, session, before, doc)
            self._event(db, session, actor, "patch", after["revision"], node_id, {"fields": sorted(fields)})
        return {"plan": after, "node": _index(after)[node_id]}

    def clear(self, actor: str, session: str, revision) -> dict:
        self._owner(session, actor)
        revision = _revision(revision)
        with self._write() as db:
            before = self._load(db, session)
            if before is None:
                raise NotFound(f"{session} has no plan")
            if before["revision"] != revision:
                raise Conflict(f"the plan of {session} is at revision {before['revision']}, not {revision}",
                               current=before)
            db.execute("DELETE FROM plans WHERE session = ?", (session,))
            self._event(db, session, actor, "clear", revision, data={"snapshot": before})
        return {"cleared": session}

    # -- the user's write --------------------------------------------------

    def decide(self, actor: str, session: str, node_id: str, rev, *, choice=None, verdict=None, action=None,
               note=None) -> dict:
        """The user's result on one node: a decision's ``choice`` (an option
        label), a verify item's ``verdict`` (passed / failed) or a followup's
        ``action`` (here / issue / drop). ``rev`` is the node's as the user
        saw it. Returns ``{"plan", "node", "previous"}``."""
        if actor != USER:
            raise Forbidden("only the user decides on a plan")
        rev = _revision(rev, "rev")
        note = _text(note, "note", MAX_NOTE)
        with self._write() as db:
            before = self._load(db, session)
            if before is None:
                raise NotFound(f"{session} has no plan")
            doc = json.loads(json.dumps({k: before[k] for k in ("title", "goal", "issue", "context", "steps",
                                                                 "closure")}))
            node = _index(doc).get(node_id)
            if node is None:
                raise NotFound(f"the plan of {session} has no node {node_id!r}", current=before)
            if node["rev"] != rev:
                raise Conflict(f"node {node_id} is at rev {node['rev']}, not {rev}: the session changed it since "
                               "you read it. Read it again and decide on what it says now.", current=before)
            kind = node["kind"]
            given = {k for k, v in (("choice", choice), ("verdict", verdict), ("action", action)) if v}
            want = {"decision": "choice", "verify": "verdict", "followup": "action"}.get(kind)
            if want is None:
                raise PlanError(f"node {node_id} is a {kind}: the session moves it, there is nothing to decide")
            if given != {want}:
                raise PlanError(f"a {kind} takes {want} (and an optional note)")
            if kind == "decision":
                labels = [o["label"] for o in node["options"]]
                if choice not in labels:
                    raise PlanError(f"{choice!r} is not an option of {node_id}: one of {', '.join(labels)}")
                result = {"choice": choice}
            elif kind == "verify":
                if verdict not in VERDICTS:
                    raise PlanError(f"a verdict is one of {', '.join(VERDICTS)}")
                if verdict == "failed" and not note:
                    raise PlanError("a failed check needs a note: what went wrong")
                result = {"verdict": verdict}
            else:
                if action not in ACTIONS:
                    raise PlanError(f"an action is one of {', '.join(ACTIONS)}")
                result = {"action": action}
                if node.get("status") == "done":
                    node["status"] = "open"   # a new disposition is new work
            result.update({"note": note, "at": _now()})
            previous = node.get("result")
            if previous:
                node["previous"] = previous
            node["result"] = result
            after = self._store(db, session, before, doc)
            self._event(db, session, USER, "decide", after["revision"], node_id, {"result": result})
        return {"plan": after, "node": _index(after)[node_id], "previous": previous}

    def feedback(self, actor: str, session: str, *, text, node=None) -> dict:
        """The user's free comment on the plan (or one node of it). Kept as
        an event; the route types it into the session."""
        if actor != USER:
            raise Forbidden("feedback on a plan is the user's")
        text = _text(text, "feedback", MAX_NOTE, required=True)
        with self._write() as db:
            plan = self._load(db, session)
            if plan is None:
                raise NotFound(f"{session} has no plan")
            target = None
            if node:
                target = _index(plan).get(node)
                if target is None:
                    raise NotFound(f"the plan of {session} has no node {node!r}")
            self._event(db, session, USER, "feedback", plan["revision"], node, {"text": text})
        return {"plan": plan, "node": target, "text": text}
