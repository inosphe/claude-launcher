"""Session plan store (claunch-7jzp8): the plan a session shows the user in
its plan pane, and the decisions the user takes there.

One person aligning with tens of sessions cannot read each one's prose. A
plan here is the same intent cut down to what that person decides on:

  steps     a tree of one-to-few-line steps. A ``step`` carries a status
            the session moves (todo, doing, done, skipped, blocked); a
            ``decision`` is a branch point whose options each state an
            expected effect and a risk, and the user picks one. A plain
            decision the session takes itself as a ``default`` option
            (answered ``by: agent``, waits on nobody, the user may change
            it); an option the session ruled out stays listed as
            ``rejected`` with the reason. A decision's child nodes may sit
            ``under`` one option -- only where they depend on that choice --
            and count only while it is the path taken (claunch-vmcg7).
  closure   what keeps the session open (th-0b38dc): ``verify`` items a
            person has to check by hand (passed / failed / deferred -- 보류,
            cannot be checked yet -- with a note) and
            ``followup`` candidates the user disposes of (continue here,
            hand over as an issue, drop). The session marks a followup
            done once it has acted on "here" or "issue" -- or on its own,
            when where it goes is plain (it is this round's work, or it was
            filed as an issue): a followup the session wrote ``done`` waits
            on nobody (th-62379d). A ``note`` is for the record only (a
            further recommendation, a measurement aside): it never waits and
            never keeps the session open; the user reads the notes once, at
            the end. The user may still take a note into this round or hand
            it over as an issue (claunch-i0whx) -- an optional result that
            reaches the session like a followup's and gates nothing.

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

Rounds (claunch-r3j1o.1). A session that runs a workflow round after round
writes one plan per round. ``new_round(run, round)`` seals the current plan
into ``plan_rounds`` (read back with ``rounds`` and ``get_round``) and opens
the next one with an empty carry index, so a decision of the old round never
answers the new one -- even when the session reuses the node id and text.
It is idempotent on (run, round): a second call for the round the plan is
already in, or for a round already sealed, changes nothing, so the daemon's
round trigger and the workflow's own call may both arrive. What the user
still owes the old round (a verify not checked or failed, a followup not
disposed of or kept and not done) is carried into the new plan's closure
with ``from`` naming its round, and a plain ``set`` that leaves such a node
out keeps it: it is the user's to settle, not the session's to drop.
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
CLOSURE_KINDS = ("verify", "followup", "note")
STEP_STATUS = ("todo", "doing", "done", "skipped", "blocked")
FOLLOWUP_STATUS = ("open", "done")
VERDICTS = ("passed", "failed", "deferred")
ACTIONS = ("here", "issue", "drop")
#: what the user may do with a note (claunch-i0whx): take it into this round
#: or hand it over as an issue. Both are optional -- a note with no result
#: still waits on nobody -- so there is no "drop": leaving it alone is that.
NOTE_ACTIONS = ("here", "issue")
FINISHED = ("done", "skipped")

#: The document keys a plan row stores. ``run`` and ``round`` are written by
#: ``new_round`` only; every other write keeps them.
DOC_KEYS = ("title", "goal", "issue", "context", "steps", "closure", "run", "round")
MAX_RUN = 120

#: What the user decides on, per kind: a change to any of these moves the
#: node's ``rev`` and takes its result away -- except on a closure node whose
#: result :func:`_kept` holds (claunch-jw1v2).
CONTENT = {"step": ("title", "detail", "under"), "decision": ("title", "detail", "options", "under"),
           "verify": ("title", "detail"), "followup": ("title", "detail"), "note": ("title", "detail")}


def _kept(node: Optional[dict]) -> bool:
    """A closure result that survives the session rewording its node.

    A passed or deferred check and a disposed followup stay settled when the
    session only edits the words: across rounds a session writes progress
    into a verify item's detail ("checked last round", an issue id) and that
    edit used to put a check the user had already passed back in front of
    them (s6451, th-2a4fcd). A failed check is the exception: the session is
    told to fix it and reword it so the user checks again. Asking again on
    purpose is ``patch(..., reopen=<reason>)``."""
    if not node or node.get("kind") not in CLOSURE_KINDS:
        return False
    res = node.get("result") or {}
    if node["kind"] == "verify":
        return res.get("verdict") in ("passed", "deferred")
    return bool(res.get("action"))


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
CREATE TABLE IF NOT EXISTS plan_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL CHECK (length(session) > 0),
    run TEXT NOT NULL DEFAULT '',
    round INTEGER,
    issue TEXT,
    revision INTEGER NOT NULL,
    doc TEXT NOT NULL CHECK (json_valid(doc)),
    summary TEXT NOT NULL CHECK (json_valid(summary)),
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_rounds_session ON plan_rounds (session, id);
CREATE INDEX IF NOT EXISTS plan_rounds_key ON plan_rounds (session, run, round);
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
    """A decision's branches (claunch-vmcg7).

    ``recommended`` marks the session's pick. ``default`` (at most one, and
    it is the recommended one) has the session take that pick at once: the
    decision is answered ``by: agent`` and waits on nobody, and the user may
    still pick another option later. ``rejected`` is the reason the session
    set an option aside: the pane shows it struck out with the reason so the
    user sees what was ruled out, and nobody can pick it. A decision keeps
    two options the user can pick -- one when the session took a default."""
    if not isinstance(value, list) or len(value) < 2:
        raise PlanError("a decision needs options: a list of at least two {label, effect, risk}")
    if len(value) > MAX_OPTIONS:
        raise PlanError(f"a decision has at most {MAX_OPTIONS} options")
    out, seen, recommended, default = [], set(), 0, 0
    for item in value:
        if not isinstance(item, dict):
            raise PlanError("each option is {label, effect, risk, recommended?, default?, rejected?}")
        label = _text(item.get("label"), "option label", MAX_TITLE, required=True)
        if label in seen:
            raise PlanError(f"option {label!r} is listed twice")
        seen.add(label)
        opt = {
            "label": label,
            "effect": _text(item.get("effect"), f"option {label!r} effect", MAX_OPTION_TEXT, required=True),
            "risk": _text(item.get("risk"), f"option {label!r} risk", MAX_OPTION_TEXT, required=True),
        }
        rejected = item.get("rejected")
        if rejected not in (None, "", False):
            if rejected is True:
                raise PlanError(f"option {label!r}: rejected takes the reason it was ruled out, not true")
            opt["rejected"] = _text(rejected, f"option {label!r} rejected", MAX_OPTION_TEXT, required=True)
        if item.get("default"):
            if "rejected" in opt:
                raise PlanError(f"option {label!r} is rejected and cannot be the default")
            opt["default"] = True
            opt["recommended"] = True
            default += 1
        if item.get("recommended"):
            if "rejected" in opt:
                raise PlanError(f"option {label!r} is rejected and cannot be recommended")
            opt["recommended"] = True
        if opt.get("recommended"):
            recommended += 1
        out.append(opt)
    if default > 1:
        raise PlanError("at most one option is the default")
    if recommended > 1:
        raise PlanError("at most one option is recommended" + (" (the default is the recommended one)"
                                                               if default else ""))
    open_ = sum(1 for o in out if "rejected" not in o)
    if open_ < (1 if default else 2):
        raise PlanError("a decision needs two options the user can pick (rejected ones do not count); "
                        "one is enough when you take it as the default")
    return out


def _default_of(node: dict) -> Optional[str]:
    return next((o["label"] for o in node.get("options") or [] if o.get("default")), None)


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
        self.reworded: list = []

    def node(self, raw, kinds: tuple, depth: int, parent: Optional[dict] = None) -> dict:
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
        under = raw.get("under")
        if under not in (None, ""):
            under = _text(under, f"node {nid!r} under", MAX_TITLE, required=True)
            if parent is None or parent.get("kind") != "decision":
                raise PlanError(f"node {nid!r}: under names an option of the decision it sits in; "
                                "put it among that decision's children")
            opt = next((o for o in parent["options"] if o["label"] == under), None)
            if opt is None:
                raise PlanError(f"node {nid!r}: under {under!r} is not an option of {parent['id']}: one of "
                                + ", ".join(o["label"] for o in parent["options"]))
            if opt.get("rejected"):
                raise PlanError(f"node {nid!r}: option {under!r} of {parent['id']} is rejected; nothing "
                                "sits under it")
            node["under"] = under
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
        if self.carry(node, self.old.get(nid)):
            self.reworded.append(nid)
        prior = (self.old.get(nid) or {}).get("result")
        if kind == "decision" and not node["result"] and _default_of(node) and \
                (not prior or prior.get("by") == "agent"):
            # the session's own pick: taken now, waits on nobody, the user may change it.
            # Never over a choice the user made: when rewording set that aside, the
            # decision goes back to the user instead (s6452-rev3, claunch-vmcg7)
            node["result"] = {"choice": _default_of(node), "by": "agent", "note": "", "at": _now()}
        if kind in STEP_KINDS:
            node["children"] = [self.node(c, STEP_KINDS, depth + 1, node) for c in children]
        return node

    @staticmethod
    def carry(node: dict, old: Optional[dict]) -> bool:
        """The user's result survives only on the content it was given for,
        or on a settled closure node (:func:`_kept`) whatever its words.
        True when the words changed under a result that was kept."""
        if old is None or old["kind"] != node["kind"]:
            node["rev"] = 1
            node["result"] = None
            return False
        if old.get("from"):
            # a node carried in from a sealed round keeps saying where it came from
            node["from"] = old["from"]
        same = _content(old) == _content(node)
        if same or _kept(old):
            node["rev"] = old["rev"]
            node["result"] = old.get("result")
            for key in ("previous", "reopened"):
                if old.get(key):
                    node[key] = old[key]
            return not same
        node["rev"] = old["rev"] + 1
        node["result"] = None
        gone = old.get("result") or old.get("previous")
        if gone:
            node["previous"] = gone
        return False


def walk(nodes: list) -> Iterator[dict]:
    for n in nodes:
        yield n
        yield from walk(n.get("children") or [])


def branch_open(node: dict, parent: Optional[dict]) -> bool:
    """Whether a node under a decision's option is on the path taken: a node
    without ``under`` belongs to the whole decision; one with ``under`` waits
    until that option is the decision's choice, and drops out when another
    option is picked (claunch-vmcg7)."""
    under = node.get("under")
    if not under or parent is None:
        return True
    return (parent.get("result") or {}).get("choice") == under


def walk_active(nodes: list, parent: Optional[dict] = None) -> Iterator[dict]:
    """:func:`walk` without the branches of options not taken."""
    for n in nodes:
        if not branch_open(n, parent):
            continue
        yield n
        yield from walk_active(n.get("children") or [], n)


def _parents(doc: Optional[dict]) -> dict:
    out: dict = {}

    def go(nodes, parent):
        for n in nodes:
            out[n["id"]] = parent
            go(n.get("children") or [], n)
    go((doc or {}).get("steps") or [], None)
    return out


def _index(doc: Optional[dict]) -> dict:
    if not doc:
        return {}
    return {n["id"]: n for n in [*walk(doc.get("steps") or []), *(doc.get("closure") or [])]}


def _owed(node: dict) -> bool:
    """A closure node the user (or the session, on the user's word) still
    owes: a verify not checked or failed, a followup not disposed of or kept
    and not done. These move into the next round (d-carry)."""
    res = node.get("result") or {}
    if node["kind"] == "verify":
        return not res or res.get("verdict") == "failed"
    if not res:
        return True
    return res.get("action") != "drop" and node.get("status") != "done"


def _round_value(value, name: str = "round") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlanError(f"{name} must be an integer >= 0")
    return value


def round_key(run: Optional[str], rnd: Optional[int]) -> str:
    """``<run>/<round>`` as the API spells a round; ``unknown`` for a plan
    sealed before it carried one."""
    return f"{run or ''}/{'unknown' if rnd is None else rnd}"


def summarize(doc: dict) -> dict:
    """Counts, what waits on the user, and whether the session may close.

    ``waiting`` is what the user can act on now (an unchosen decision, an
    unchecked verify item, an undisposed followup). ``waiting_decisions``
    is the part of it a session must have before it starts work: the
    unchosen decisions alone. A start gate reads that one, because a verify
    item left from the round before checks work that already landed and is
    the settle gate's to wait on (claunch-jw1v2). A deferred verify item
    ("보류": cannot be checked yet) waits on nobody and keeps nothing open;
    the session hands it over and ``deferred`` counts them. ``closable``
    holds when nothing waits, every step is done or skipped and every
    followup the user kept ("here", "issue") is done; ``blockers`` lists
    each node that keeps it from holding, with the reason.

    Only the path taken counts (claunch-vmcg7): a node sitting ``under`` an
    option that is not the decision's choice -- not picked yet, or another
    one picked -- neither waits nor blocks, and its ids are listed in
    ``inactive``. A decision the session answered with its default
    (``by: agent``) waits on nobody; ``auto`` counts them."""
    every = list(walk(doc.get("steps") or []))
    steps = list(walk_active(doc.get("steps") or []))
    live = {n["id"] for n in steps}
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
            elif res.get("verdict") == "deferred":
                pass
            elif res.get("verdict") == "failed":
                block(n, "check failed", "session")
        elif n["kind"] == "note":
            continue
        else:
            if not res:
                if n.get("status") != "done":   # done by the session: it disposed of it itself
                    block(n, "not disposed of", "user")
            elif res.get("action") != "drop" and n.get("status") != "done":
                block(n, f"followup kept ({res.get('action')}) and not done", "session")
    waiting = [b for b in blockers if b["who"] == "user"]
    return {
        "steps": s_total, "done": s_done,
        "decisions": sum(1 for n in steps if n["kind"] == "decision"),
        "auto": sum(1 for n in steps if n["kind"] == "decision" and (n.get("result") or {}).get("by") == "agent"),
        "inactive": [n["id"] for n in every if n["id"] not in live],
        "verify": sum(1 for n in closure if n["kind"] == "verify"),
        "followups": sum(1 for n in closure if n["kind"] == "followup"),
        "notes": sum(1 for n in closure if n["kind"] == "note"),
        "waiting": len(waiting),
        "waiting_decisions": sum(1 for b in waiting if b["kind"] == "decision"),
        "deferred": sum(1 for n in closure if n["kind"] == "verify"
                        and (n.get("result") or {}).get("verdict") == "deferred"),
        "blocked": sum(1 for n in steps if n["kind"] == "step" and n.get("status") == "blocked"),
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

    def _store(self, db, session: str, before: Optional[dict], doc: dict, *, reopen: bool = False) -> dict:
        now = _now()
        body = {k: doc[k] for k in DOC_KEYS if k in doc and (k not in ("run", "round") or doc[k] is not None)}
        summary = summarize(body)
        if before is None:
            db.execute("INSERT INTO plans (session, revision, doc, summary, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (session, 1, json.dumps(body, ensure_ascii=False), json.dumps(summary, ensure_ascii=False),
                        now, now))
        elif reopen:
            # A new round: same row, so the revision keeps counting up and a
            # write made against the old round's revision is refused.
            db.execute("UPDATE plans SET revision = revision + 1, doc = ?, summary = ?, created_at = ?, "
                       "updated_at = ? WHERE session = ?",
                       (json.dumps(body, ensure_ascii=False), json.dumps(summary, ensure_ascii=False), now, now,
                        session))
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

    # -- the daemon's copy (a quick-fork, claunch-yydhs) -------------------

    def copy(self, source: str, target: str, *, actor: str = "daemon") -> bool:
        """Copy ``source``'s plan onto ``target`` as a new plan: the same
        steps, statuses, the user's decisions and the closure items, so a
        quick-fork that asked to carry the plan shows what the origin shows.
        The copy starts at revision 1 with its own ids unchanged, because
        node ids are the session's and a decision's result rides its node.
        Nothing lands when the source has no plan or the target already has
        one; returns whether a plan was written."""
        with self._write() as db:
            if self._load(db, target) is not None:
                return False
            src = self._load(db, source)
            if src is None:
                return False
            body = {k: src.get(k) for k in ("title", "goal", "issue", "context", "steps", "closure")}
            self._store(db, target, None, body)
            self._event(db, target, actor, "copy", 1, data={"from": source})
            return True

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
                   "closure": [build.node(n, CLOSURE_KINDS, 1) for n in closure or []],
                   "run": (before or {}).get("run"), "round": (before or {}).get("round")}
            # A node carried from an earlier round that the user still owes
            # stays even when this write leaves it out.
            for old in (before or {}).get("closure") or []:
                if old.get("from") and old["id"] not in build.ids and _owed(old):
                    doc["closure"].append(json.loads(json.dumps(old)))
            after = self._store(db, session, before, doc)
            data = {"nodes": build.count}
            if build.reworded:
                data["reworded_kept"] = build.reworded
            self._event(db, session, actor, "set", after["revision"], data=data)
        return after

    def patch(self, actor: str, session: str, node_id: str, *, reopen=None, **fields) -> dict:
        """Change one node's title, detail, status or options. No revision:
        a step's status is the session's alone, and a change to what the
        user decides on moves the node's ``rev`` (which the user's click
        names), so it cannot land on a decision taken against other text.

        ``reopen`` (a reason) asks the user again about a settled closure
        node -- the one way to do that now that rewording keeps the result
        (:func:`_kept`). The old result moves to ``previous`` and the reason
        is kept on the node for the pane to show."""
        self._owner(session, actor)
        fields = {k: v for k, v in fields.items() if v is not None}
        unknown = set(fields) - {"title", "detail", "status", "options"}
        if unknown:
            raise PlanError(f"a node patch takes title, detail, status and options, not {', '.join(sorted(unknown))}")
        reason = _text(reopen, "reopen", MAX_NOTE) if reopen is not None else ""
        if reopen is not None and not reason:
            raise PlanError("reopen takes a reason: what changed in what the user checked")
        if not fields and not reason:
            raise PlanError("nothing to change: give title, detail, status, options or reopen")
        with self._write() as db:
            before = self._load(db, session)
            if before is None:
                raise NotFound(f"{session} has no plan; write one with plan_set first")
            old = _index(before).get(node_id)
            if old is None:
                raise NotFound(f"the plan of {session} has no node {node_id!r}")
            doc = json.loads(json.dumps({k: before[k] for k in DOC_KEYS if k in before}))
            target = _index(doc)[node_id]
            raw = {**{k: target.get(k) for k in ("id", "kind", "title", "detail", "status", "options", "under")},
                   **fields}
            build = _Builder({node_id: old})
            fresh = build.node({**raw, "children": []}, (target["kind"],), 1, _parents(doc).get(node_id))
            fresh.pop("children", None)
            if fresh["kind"] == "decision":
                for child in target.get("children") or []:
                    opt = next((o for o in fresh["options"] if o["label"] == child.get("under")), None)
                    if child.get("under") and (opt is None or opt.get("rejected")):
                        raise PlanError(f"node {child['id']} sits under option {child['under']!r}, which these "
                                        f"options drop or reject; move it with plan_set first")
            if reason:
                if not old.get("result") or old["kind"] not in CLOSURE_KINDS:
                    raise PlanError(f"node {node_id} has no settled result to reopen (only a checked verify item "
                                    "or a disposed followup reopens)")
                fresh["previous"] = old["result"]
                fresh["result"] = None
                fresh["rev"] = old["rev"] + 1
                fresh["reopened"] = {"reason": reason, "at": _now()}
                if fresh["kind"] == "followup":
                    fresh["status"] = "open"
            keep = target.get("children")
            target.clear()
            target.update(fresh)
            if keep is not None:
                target["children"] = keep
            after = self._store(db, session, before, doc)
            data = {"fields": sorted(fields)}
            if build.reworded:
                data["reworded_kept"] = True
            if reason:
                data["reopen"] = reason
            self._event(db, session, actor, "patch", after["revision"], node_id, data)
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
            sealed = self._seal(db, session, before)
            db.execute("DELETE FROM plans WHERE session = ?", (session,))
            self._event(db, session, actor, "clear", revision, data={"snapshot": before, "sealed": sealed})
        return {"cleared": session, "sealed": sealed}

    # -- rounds ------------------------------------------------------------

    def new_round(self, actor: str, session: str, *, run, round, title=None, goal=None, issue=None,
                  context: Optional[dict] = None) -> dict:
        """Seal the current plan and open the plan of (``run``, ``round``).

        Returns ``{"plan", "opened", "sealed", "carried", "why"}``. ``opened``
        is False -- and nothing is written -- when the plan is already that
        round's, or when that round was sealed before and a later plan stands
        (a late or repeated call must not overwrite the round that followed
        it). After ``clear`` there is no plan to overwrite, so the round opens
        again; its earlier copy stays sealed beside it."""
        self._owner(session, actor)
        run = _text(run, "run", MAX_RUN, required=True)
        rnd = _round_value(round)
        title = _text(title, "title", MAX_TITLE)
        goal = _text(goal, "goal", MAX_GOAL)
        issue = _text(issue, "issue", 80) or None
        with self._write() as db:
            before = self._load(db, session)
            if before is not None and before.get("run") == run and before.get("round") == rnd:
                return {"plan": before, "opened": False, "sealed": None, "carried": [],
                        "why": f"the plan is already round {round_key(run, rnd)}"}
            if before is not None and db.execute(
                    "SELECT 1 FROM plan_rounds WHERE session = ? AND run = ? AND round = ?",
                    (session, run, rnd)).fetchone():
                return {"plan": before, "opened": False, "sealed": None, "carried": [],
                        "why": f"round {round_key(run, rnd)} was sealed already"}
            sealed = None
            carried: list = []
            if before is not None:
                sealed = self._seal(db, session, before)
                origin = {"run": before.get("run") or "", "round": before.get("round")}
                for node in before.get("closure") or []:
                    if _owed(node):
                        node = json.loads(json.dumps(node))
                        node.setdefault("from", origin)
                        carried.append(node)
            doc = {"title": title or (before or {}).get("title") or f"round {rnd}",
                   "goal": goal, "issue": issue,
                   "context": context if context is not None else (before or {}).get("context") or {},
                   "steps": [], "closure": carried, "run": run, "round": rnd}
            after = self._store(db, session, before, doc, reopen=True)
            self._event(db, session, actor, "round", after["revision"],
                        data={"run": run, "round": rnd, "sealed": sealed,
                              "carried": [n["id"] for n in carried]})
        return {"plan": after, "opened": True, "sealed": sealed, "carried": [n["id"] for n in carried],
                "why": ""}

    @staticmethod
    def _seal(db, session: str, plan: dict) -> dict:
        """Keep ``plan`` in plan_rounds. A plan written before rounds existed
        (no run, no round) is sealed as round ``unknown``."""
        run = plan.get("run") or ""
        rnd = plan.get("round")
        body = {k: plan[k] for k in DOC_KEYS if k in plan}
        db.execute("INSERT INTO plan_rounds (session, run, round, issue, revision, doc, summary, opened_at, "
                   "closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (session, run, rnd, plan.get("issue"), plan["revision"], json.dumps(body, ensure_ascii=False),
                    json.dumps(plan["summary"], ensure_ascii=False), plan["created_at"], _now()))
        return {"run": run, "round": rnd, "key": round_key(run, rnd)}

    def rounds(self, session: str) -> dict:
        """The session's sealed rounds, oldest first, and the round the
        current plan is in. Each row carries its summary without blockers."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM plan_rounds WHERE session = ? ORDER BY id", (session,)).fetchall()
            current = self._load(db, session)
        out = []
        for r in rows:
            doc = json.loads(r["doc"])
            summary = json.loads(r["summary"])
            summary.pop("blockers", None)
            out.append({"run": r["run"], "round": r["round"], "key": round_key(r["run"], r["round"]),
                        "title": doc.get("title", ""), "issue": r["issue"], "revision": int(r["revision"]),
                        "opened_at": r["opened_at"], "closed_at": r["closed_at"], **summary})
        cur = None
        if current is not None:
            cur = {"run": current.get("run"), "round": current.get("round"),
                   "key": round_key(current.get("run"), current.get("round")) if current.get("run") else None,
                   "title": current.get("title", ""), "revision": current["revision"]}
        return {"session": session, "rounds": out, "current": cur}

    def get_round(self, session: str, key: str) -> Optional[dict]:
        """One sealed round by its ``<run>/<round>`` key (``<run>/unknown``
        for a plan sealed without one; the latest row of that key). The plan is
        shaped like ``get``'s, plus ``sealed: true`` and ``closed_at``."""
        if not isinstance(key, str) or "/" not in key:
            raise PlanError("a round is named <run>/<round> (or <run>/unknown)")
        run, _, tail = key.rpartition("/")
        with self._connect() as db:
            if tail == "unknown":
                r = db.execute("SELECT * FROM plan_rounds WHERE session = ? AND run = ? AND round IS NULL "
                               "ORDER BY id DESC LIMIT 1", (session, run)).fetchone()
            else:
                try:
                    rnd = int(tail)
                except ValueError:
                    raise PlanError(f"round {tail!r} is not a number (or 'unknown')") from None
                r = db.execute("SELECT * FROM plan_rounds WHERE session = ? AND run = ? AND round = ? "
                               "ORDER BY id DESC LIMIT 1", (session, run, rnd)).fetchone()
        if r is None:
            return None
        return {"session": session, "revision": int(r["revision"]), **json.loads(r["doc"]),
                "run": r["run"], "round": r["round"],
                "summary": json.loads(r["summary"]), "created_at": r["opened_at"], "updated_at": r["closed_at"],
                "closed_at": r["closed_at"], "key": round_key(r["run"], r["round"]), "sealed": True}

    # -- the user's write --------------------------------------------------

    def decide(self, actor: str, session: str, node_id: str, rev, *, choice=None, verdict=None, action=None,
               note=None) -> dict:
        """The user's result on one node: a decision's ``choice`` (an option
        label), a verify item's ``verdict`` (passed / failed / deferred), a followup's
        ``action`` (here / issue / drop) or a note's optional ``action`` (here / issue). ``rev`` is the node's as the user
        saw it. Returns ``{"plan", "node", "previous"}``."""
        if actor != USER:
            raise Forbidden("only the user decides on a plan")
        rev = _revision(rev, "rev")
        note = _text(note, "note", MAX_NOTE)
        with self._write() as db:
            before = self._load(db, session)
            if before is None:
                raise NotFound(f"{session} has no plan")
            doc = json.loads(json.dumps({k: before[k] for k in DOC_KEYS if k in before}))
            node = _index(doc).get(node_id)
            if node is None:
                raise NotFound(f"the plan of {session} has no node {node_id!r}", current=before)
            if node["rev"] != rev:
                raise Conflict(f"node {node_id} is at rev {node['rev']}, not {rev}: the session changed it since "
                               "you read it. Read it again and decide on what it says now.", current=before)
            kind = node["kind"]
            given = {k for k, v in (("choice", choice), ("verdict", verdict), ("action", action)) if v}
            want = {"decision": "choice", "verify": "verdict", "followup": "action", "note": "action"}.get(kind)
            if want is None:
                raise PlanError(f"node {node_id} is a {kind}: the session moves it, there is nothing to decide")
            if given != {want}:
                raise PlanError(f"a {kind} takes {want} (and an optional note)")
            if kind == "decision":
                labels = [o["label"] for o in node["options"]]
                if choice not in labels:
                    raise PlanError(f"{choice!r} is not an option of {node_id}: one of {', '.join(labels)}")
                picked = next(o for o in node["options"] if o["label"] == choice)
                if picked.get("rejected"):
                    raise PlanError(f"{choice!r} was ruled out by the session ({picked['rejected']}); pick "
                                    "another option, or comment on the plan to argue for it")
                if node_id not in {n["id"] for n in walk_active(doc.get("steps") or [])}:
                    raise PlanError(f"{node_id} sits under an option that is not the path taken; decide on "
                                    "the decision above it first")
                result = {"choice": choice, "by": USER}
            elif kind == "verify":
                if verdict not in VERDICTS:
                    raise PlanError(f"a verdict is one of {', '.join(VERDICTS)}")
                if verdict == "failed" and not note:
                    raise PlanError("a failed check needs a note: what went wrong")
                result = {"verdict": verdict}
            elif kind == "note":
                if action not in NOTE_ACTIONS:
                    raise PlanError(f"a note's action is one of {', '.join(NOTE_ACTIONS)} (leaving it alone "
                                    "is the default; it never waits on anyone)")
                result = {"action": action}
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
            node.pop("reopened", None)   # answered: the reason was for this ask
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
