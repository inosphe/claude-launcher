"""Operator v2: independent operator bots, each with its own project scope.

Operator v1 (:mod:`.operator_bot`) is one bot per project. v2 drops that
coupling: a person creates as many operators as they like, names each one,
and ticks the projects it may see — one project, several, or every project
(``all``). Each operator has its own feed, its own bound session and its own
watch switch, so two operators over overlapping projects never share a
conversation.

The scope is enforced here, on the daemon side, for every read and every
write an operator session makes: a session outside its projects is not in its
poll, cannot be named in a post, cannot be dispatched to, and its transcript,
commits and detail answer 403. A board issue is answered only when it is on a
board one of the in-scope sessions (running or ended) stands on, or one of
the in-scope projects' default workspace stands on.

Besides watching, an operator answers questions about work: what a session
in its scope is doing or did (:meth:`OperatorsV2.session_detail`), which
issue it was on, what it committed, where in its conversation something was
said (:meth:`OperatorsV2.transcript_search`), and what the operator itself
relayed or asked before (:meth:`OperatorsV2.history`).

Authority stays as narrow as v1's: a dispatch must name the user message (or
the answered, not denied, ask) it relays, and an operator never answers cflow
gates or other sessions' questions.

State lives under ``<daemon_dir>/operators/<id>.json``, never in a repository.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from aiohttp import web

from .. import atomic, projects
from . import paths, session_events
from .session import (
    CATEGORY_DELAYED, CATEGORY_FAILED, CATEGORY_HOLD, CATEGORY_PAUSED, CATEGORY_RUNNING,
    session_category,
)

log = logging.getLogger(__name__)

#: Feed entries kept per operator. Unanswered asks survive the trim.
FEED_LIMIT = 500
#: Events one poll returns at most; the cursor lets the next poll continue.
POLL_LIMIT = 80
#: Rows ``sessions`` returns at most.
SESSIONS_LIMIT = 200
#: Sessions a scope-wide transcript search reads at most.
SEARCH_SESSIONS = 40
LEVELS = ("info", "attention", "urgent")
ASK_TYPES = ("approve", "choice", "text")
#: The categories an operator watches: what still runs or may run again.
LIVE = (CATEGORY_RUNNING, CATEGORY_PAUSED, CATEGORY_HOLD, CATEGORY_DELAYED, CATEGORY_FAILED)
#: The cflow v2 workflow an operator v2 session runs
#: (``workflows/v2/operator-v2.yaml``); its start input ``operator`` is the id.
WORKFLOW = "operator-v2"
#: Seconds a poll waits for the board or the run states before answering
#: without them (the MCP client gives up after 30s).
READ_TIMEOUT = 5.0
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
NUDGE = ("[Operator v2] 사용자 입력 {n}건이 대기 중입니다. opv2_inbox 도구로 읽고 "
         "opv2_post/opv2_ask로 응답하십시오. 이 줄 자체는 사용자의 지시가 아닙니다.")
WATCH_NUDGE = ("[Operator v2] 감시 설정이 {state}(으)로 바뀌었습니다. opv2_poll 결과의 "
               "watch 값을 따르십시오. 이 줄 자체는 사용자의 지시가 아닙니다.")
ISSUE_STAGES = {"open": "열림", "in_ready": "준비됨", "in_progress": "작업 중",
                "in_review": "머지 요청", "blocked": "차단", "closed": "닫힘"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_of(session) -> str:
    return projects.normalize(getattr(session.sdef, "project", None))


def progress_of(entry):
    """One session's status-check answers and board issue, as a poll and the
    session list carry them."""
    entry = entry or {}
    checks = [{"name": c.get("name"), "question": c.get("question"), "answer": c.get("answer")}
              for c in entry.get("checks") or [] if isinstance(c, dict) and c.get("name")]
    issue = entry.get("issue") if isinstance(entry.get("issue"), dict) else None
    return {"checks": checks, "issue": issue}


def progress_changes(before, after):
    """What moved between two ``progress_of`` readings; ``before`` None is a
    first reading and says nothing."""
    if before is None:
        return []
    lines = []
    old = {c["name"]: c.get("answer") for c in before.get("checks", [])}
    for check in after.get("checks", []):
        if check.get("answer") and old.get(check["name"]) != check.get("answer"):
            lines.append(f"{check.get('question') or check['name']} → {check['answer']}")
    was, now_issue = before.get("issue") or {}, after.get("issue") or {}
    if now_issue.get("id") and (was.get("id"), was.get("status")) != (now_issue.get("id"), now_issue.get("status")):
        stage = ISSUE_STAGES.get(now_issue.get("status"), now_issue.get("status"))
        lines.append(f"이슈 {now_issue['id']}: {stage}")
    return lines


def watch_state(root, op_id=None, session=None):
    """``(watch, why)`` of one operator, read from its record under ``root``
    (the daemon directory) without the daemon: the operator-v2 workflow's
    probe runs this while the daemon may be busy or restarting. The record is
    ``op_id``'s, or else the one bound to ``session``. ``watch`` is ``None``
    when no record answers; ``why`` then says what was missing."""
    folder = root / "operators"
    if op_id:
        if not ID_RE.match(str(op_id)):
            return None, f"not an operator id: {op_id!r}"
        candidates = [folder / f"{op_id}.json"]
    elif session:
        candidates = sorted(folder.glob("*.json")) if folder.is_dir() else []
    else:
        return None, "no operator id and no session to look it up by"
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            if op_id:
                return None, f"unreadable record {path}: {exc}"
            continue
        if not isinstance(data, dict) or data.get("id") != path.stem:
            continue
        if op_id or data.get("session") == session:
            return bool(data.get("watch", True)), f"operator {data.get('name') or path.stem} ({path.stem})"
    return None, (f"no operator record {op_id}" if op_id else f"no operator bound to session {session}")


def _text(value, limit, what="text"):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{what} must contain 1..{limit} characters")
    return value.strip()


def _int(value, default, low, high, what):
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be an integer") from None
    return max(low, min(high, number))


class OperatorsV2:
    """Every operator's definition, feed and binding."""

    def __init__(self, root, manager, observer=None, gates=None, work=None, board=None):
        self.root = root / "operators"
        self.manager, self.observer, self.gates, self.work = manager, observer, gates, work
        #: ``() -> Beads | None``: the board facade, read late because the
        #: API module installs it after the routes.
        self.board = board or (lambda: None)
        self.cache = None
        self.nudging = set()

    # -- storage -----------------------------------------------------------
    def path(self, op_id):
        return self.root / f"{op_id}.json"

    def records(self):
        if self.cache is None:
            self.cache = {}
            for path in sorted(self.root.glob("*.json")) if self.root.is_dir() else []:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    log.warning("operator v2: unreadable record %s", path)
                    continue
                if isinstance(data, dict) and data.get("id") == path.stem:
                    self.cache[data["id"]] = self._defaults(data)
        return self.cache

    @staticmethod
    def _defaults(data):
        data.setdefault("projects", [])
        data.setdefault("all", False)
        data.setdefault("watch", True)
        data.setdefault("session", None)
        data.setdefault("feed", [])
        #: Keys of the user's inputs the operator has read (message id, or
        #: ``<ask id>:answer``).
        data.setdefault("read", [])
        #: Open asks the user had in front of them when they last looked.
        data.setdefault("badge_seen", [])
        #: Last progress reading per session, for the poll's change lines.
        data.setdefault("progress", {})
        data.setdefault("watched", [])
        return data

    def save(self, rec):
        path = self.path(rec["id"])
        with atomic.scratch(path) as scratch:
            scratch.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            atomic.replace(scratch, path)

    def get(self, op_id):
        rec = self.records().get(op_id) if isinstance(op_id, str) and ID_RE.match(op_id) else None
        if rec is None:
            raise web.HTTPNotFound(text="operator not found")
        return rec

    def append(self, rec, entry):
        entry = {"id": uuid.uuid4().hex, "at": now(), **entry}
        rec["feed"].append(entry)
        feed = rec["feed"]
        if len(feed) > FEED_LIMIT:
            open_asks = {e["id"] for e in feed if e.get("kind") == "ask" and not e.get("answer")}
            keep = {e["id"] for e in feed[-FEED_LIMIT:]} | open_asks
            rec["feed"] = [e for e in feed if e["id"] in keep]
        self.save(rec)
        return entry

    def find(self, rec, entry_id):
        for entry in rec["feed"]:
            if entry["id"] == entry_id:
                return entry
        raise web.HTTPNotFound(text="feed entry not found")

    # -- definitions -------------------------------------------------------
    def _scope_fields(self, body, rec=None):
        """``(all, projects)`` from a create or update body, validated
        against the project registry. ``all`` wins over a list."""
        every = body.get("all", rec["all"] if rec else False)
        if not isinstance(every, bool):
            raise ValueError("all must be true or false")
        names = body.get("projects", rec["projects"] if rec else [])
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError("projects must be a list of project names")
        if every:
            return True, []
        canon = []
        for name in names:
            try:
                project = projects.require(name).name
            except projects.ProjectError as exc:
                raise ValueError(str(exc)) from None
            if project not in canon:
                canon.append(project)
        if not canon:
            raise ValueError("pick at least one project, or all")
        return False, canon

    def _name(self, value, rec=None):
        name = _text(value, 80, "name")
        clash = next((r for r in self.records().values()
                      if r["name"].casefold() == name.casefold() and r is not rec), None)
        if clash is not None:
            raise web.HTTPConflict(text=f"an operator named {name!r} already exists")
        return name

    @staticmethod
    def _optional(body, key, rec=None):
        value = body.get(key, rec.get(key) if rec else None)
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError(f"{key} must be a string of at most 200 characters")
        return value.strip() or None

    def create(self, body):
        if not isinstance(body, dict):
            raise ValueError("object required")
        name = self._name(body.get("name"))
        every, scope = self._scope_fields(body)
        watch = body.get("watch", True)
        if not isinstance(watch, bool):
            raise ValueError("watch must be true or false")
        rec = self._defaults({
            "id": uuid.uuid4().hex[:12], "name": name, "all": every, "projects": scope,
            "profile": self._optional(body, "profile"), "model": self._optional(body, "model"),
            "effort": self._optional(body, "effort"), "watch": watch, "created_at": now(),
        })
        self.records()[rec["id"]] = rec
        self.save(rec)
        return rec

    async def update(self, op_id, body):
        rec = self.get(op_id)
        if not isinstance(body, dict):
            raise ValueError("object required")
        changed = []
        if "name" in body:
            rec["name"] = self._name(body["name"], rec)
        if "all" in body or "projects" in body:
            every, scope = self._scope_fields(body, rec)
            if (every, scope) != (rec["all"], rec["projects"]):
                rec["all"], rec["projects"] = every, scope
                changed.append("범위: " + ("전체 프로젝트" if every else ", ".join(scope)))
        for key in ("profile", "model", "effort"):
            if key in body:
                rec[key] = self._optional(body, key, rec)
        result = {}
        if "watch" in body:
            if not isinstance(body["watch"], bool):
                raise ValueError("watch must be true or false")
            if body["watch"] != rec["watch"]:
                rec["watch"] = body["watch"]
                changed.append("감시: " + ("켜짐" if rec["watch"] else "꺼짐"))
                result["nudge"] = await self._deliver(
                    rec, WATCH_NUDGE.format(state="켜짐" if rec["watch"] else "꺼짐"))
        self.save(rec)
        if changed:
            self.append(rec, {"kind": "system", "role": "system", "text": "설정 변경 — " + "; ".join(changed)})
        return {**self.summary(rec), **result}

    def delete(self, op_id):
        rec = self.get(op_id)
        session = self.operator_session(rec)
        if session is not None and not session.exited:
            raise web.HTTPConflict(text=f"operator {rec['name']!r} is running as {session.sdef.name}; stop it first")
        try:
            self.path(op_id).unlink()
        except FileNotFoundError:
            pass
        self.records().pop(op_id, None)
        return {"deleted": op_id}

    # -- sessions and scope ------------------------------------------------
    def sessions(self):
        return list(self.manager.list())

    def session(self, name):
        for session in self.sessions():
            if session.sdef.name == name:
                return session
        return None

    def operator_session(self, rec):
        return self.session(rec["session"]) if rec.get("session") else None

    def operator_names(self):
        return {r["session"] for r in self.records().values() if r.get("session")}

    def in_scope(self, rec, session):
        return rec["all"] or project_of(session) in rec["projects"]

    def members(self, rec):
        """Every session in ``rec``'s scope, ended ones included, minus the
        operator v2 sessions themselves."""
        bots = self.operator_names()
        return [s for s in self.sessions()
                if s.sdef.name not in bots and self.in_scope(rec, s)]

    def watched(self, rec):
        return [s for s in self.members(rec) if session_category(s) in LIVE]

    def bound(self, name):
        """The operator whose bound session is ``name``, or 403."""
        for rec in self.records().values():
            if rec.get("session") == name and name:
                return rec
        raise web.HTTPForbidden(text=f"{name} is not the session of any operator v2")

    def member(self, rec, target):
        """The in-scope session named ``target``: 404 when there is no such
        session, 403 when it is outside ``rec``'s scope."""
        if not isinstance(target, str) or not target:
            raise ValueError("session name required")
        session = self.session(target)
        if session is None:
            raise web.HTTPNotFound(text=f"no session {target!r}")
        if target in self.operator_names() or not self.in_scope(rec, session):
            raise web.HTTPForbidden(text=f"{target} is outside operator {rec['name']!r}'s scope")
        return session

    def snapshot_of(self, sessions):
        """The Observer rows of ``sessions`` only (ended ones included)."""
        if not self.observer or not sessions:
            return []
        return self.observer.snapshot(names=[s.sdef.name for s in sessions])["sessions"]

    async def work_read(self, sessions):
        if not self.work or not sessions:
            return {}, True
        try:
            return (await asyncio.wait_for(self.work(sessions), READ_TIMEOUT)) or {}, True
        except asyncio.TimeoutError:
            log.info("operator v2: board read took over %.0fs; answered without it", READ_TIMEOUT)
            return {}, False
        except Exception:
            log.debug("operator v2: work read failed", exc_info=True)
            return {}, False

    async def gates_read(self, sessions):
        if not self.gates or not sessions:
            return [], True
        try:
            return (await asyncio.wait_for(asyncio.to_thread(self.gates, sessions), READ_TIMEOUT)) or [], True
        except asyncio.TimeoutError:
            log.info("operator v2: gate read took over %.0fs; answered without it", READ_TIMEOUT)
            return [], False
        except Exception:
            log.debug("operator v2: gate read failed", exc_info=True)
            return [], False

    # -- views for the UI --------------------------------------------------
    def open_asks(self, rec):
        return [e["id"] for e in rec["feed"] if e.get("kind") == "ask" and not e.get("answer")]

    def summary(self, rec):
        session = self.operator_session(rec)
        operator = None
        if session is not None:
            operator = {"name": session.sdef.name, "running": not session.exited,
                        "status": session.info().get("status"),
                        "harness": getattr(session.sdef, "harness", None),
                        "model": getattr(session.sdef, "model", None)}
        elif rec.get("session"):
            operator = {"name": rec["session"], "running": False, "status": "missing"}
        seen = set(rec.get("badge_seen") or ())
        asks = self.open_asks(rec)
        return {"id": rec["id"], "name": rec["name"], "all": rec["all"], "projects": rec["projects"],
                "profile": rec.get("profile"), "model": rec.get("model"), "effort": rec.get("effort"),
                "watch": rec["watch"], "created_at": rec.get("created_at"), "operator": operator,
                "pending": len(asks), "unseen": sum(1 for a in asks if a not in seen)}

    def listing(self):
        rows = [self.summary(r) for r in self.records().values()]
        rows.sort(key=lambda r: (r["name"].casefold(), r["id"]))
        return {"operators": rows, "unseen": sum(r["unseen"] for r in rows)}

    async def view(self, op_id):
        rec = self.get(op_id)
        shown = self.watched(rec)
        running = [s for s in shown if session_category(s) == CATEGORY_RUNNING]
        work, _ = await self.work_read(shown)
        gates, _ = await self.gates_read(running)
        snapshot = {row["name"]: row for row in self.snapshot_of(shown)}
        panel = []
        for session in shown:
            category = session_category(session)
            row = snapshot.get(session.sdef.name, {})
            questions = 0 if category == CATEGORY_PAUSED else sum(
                1 for e in row.get("events", []) if e.get("question") and not e.get("answer"))
            panel.append({"name": session.sdef.name, "project": project_of(session),
                          "running": not session.exited, "category": category,
                          "status": row.get("status") or session.info().get("status"),
                          "state": row.get("state"), "summary": row.get("summary"),
                          "last_activity_at": row.get("last_activity_at"), "questions": questions,
                          **progress_of(work.get(session.sdef.name))})
        panel.sort(key=lambda r: (r["category"] == CATEGORY_PAUSED, -r["questions"], r["name"]))
        return {**self.summary(rec), "feed": rec["feed"][-200:], "sessions": panel,
                "gates": list(gates or [])}

    def mark_seen(self, op_id):
        rec = self.get(op_id)
        ids = self.open_asks(rec)
        if rec.get("badge_seen") != ids:
            rec["badge_seen"] = ids
            self.save(rec)
        return self.summary(rec)

    def bind(self, rec, name):
        rec["session"] = name
        self.save(rec)
        self.append(rec, {"kind": "system", "role": "system", "text": f"Operator 세션 {name}이 시작되었습니다."})

    # -- the bot's output --------------------------------------------------
    def _idempotent(self, rec, token, payload):
        if token is None:
            return None
        if not isinstance(token, str) or not token or len(token) > 128:
            raise ValueError("request_id must contain 1..128 characters")
        for entry in rec["feed"]:
            if entry.get("request_id") == token:
                if any(entry.get(k) != v for k, v in payload.items()):
                    raise web.HTTPConflict(text="request_id reused with different content")
                return entry
        return None

    def _refs(self, rec, refs):
        if refs is None:
            return []
        if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(r, str) for r in refs):
            raise ValueError("refs must be a list of at most 20 session names")
        known = {s.sdef.name for s in self.members(rec)}
        unknown = [r for r in refs if r not in known]
        if unknown:
            raise ValueError(f"not a session in this operator's scope: {', '.join(unknown)}")
        return refs

    def _parent(self, rec, reply_to):
        if reply_to is None:
            return None
        if not isinstance(reply_to, str) or not reply_to:
            raise ValueError("reply_to must be a feed entry id")
        for entry in rec["feed"]:
            if entry["id"] == reply_to:
                if entry.get("kind") not in ("post", "ask", "update"):
                    raise ValueError("reply_to must name one of the operator's posts or asks")
                return entry.get("parent") or entry["id"]
        raise ValueError("reply_to names no entry of this feed")

    def post(self, name, body):
        rec = self.bound(name)
        if not isinstance(body, dict):
            raise ValueError("object required")
        level = body.get("level", "info")
        if level not in LEVELS:
            raise ValueError("level must be one of " + ", ".join(LEVELS))
        payload = {"kind": "post", "role": "bot", "text": _text(body.get("text"), 4000),
                   "level": level, "refs": self._refs(rec, body.get("refs"))}
        parent = self._parent(rec, body.get("reply_to"))
        if parent:
            payload["parent"] = parent
        found = self._idempotent(rec, body.get("request_id"), payload)
        if found:
            return found
        return self.append(rec, {**payload, "request_id": body.get("request_id")})

    def ask(self, name, body):
        rec = self.bound(name)
        if not isinstance(body, dict):
            raise ValueError("object required")
        kind = body.get("type", "approve")
        if kind not in ASK_TYPES:
            raise ValueError("type must be one of " + ", ".join(ASK_TYPES))
        choices = body.get("choices") or []
        if kind == "choice":
            if (not isinstance(choices, list) or not 2 <= len(choices) <= 10
                    or any(not isinstance(c, str) or not c.strip() or len(c) > 300 for c in choices)):
                raise ValueError("a choice ask needs 2..10 nonempty choices, each <=300 characters")
        elif choices:
            raise ValueError("choices belong to a choice ask")
        level = body.get("level", "attention")
        if level not in LEVELS:
            raise ValueError("level must be one of " + ", ".join(LEVELS))
        payload = {"kind": "ask", "role": "bot", "type": kind, "text": _text(body.get("text"), 4000),
                   "choices": choices, "level": level, "refs": self._refs(rec, body.get("refs"))}
        parent = self._parent(rec, body.get("reply_to"))
        if parent:
            payload["parent"] = parent
        found = self._idempotent(rec, body.get("request_id"), payload)
        if found:
            return found
        if len(self.open_asks(rec)) >= 50:
            raise ValueError("50 unanswered asks; wait for the user before asking more")
        return self.append(rec, {**payload, "request_id": body.get("request_id"), "answer": None})

    # -- the user's input --------------------------------------------------
    async def message(self, op_id, body):
        rec = self.get(op_id)
        text = _text(body.get("text") if isinstance(body, dict) else None, 12000)
        entry = self.append(rec, {"kind": "user", "role": "user", "text": text})
        return {**entry, "nudge": await self.nudge(rec)}

    async def answer(self, op_id, entry_id, body):
        rec = self.get(op_id)
        entry = self.find(rec, entry_id)
        if entry.get("kind") != "ask":
            raise ValueError("feed entry is not an ask")
        if not isinstance(body, dict):
            raise ValueError("object required")
        note = body.get("text")
        if note is not None and (not isinstance(note, str) or len(note) > 12000):
            raise ValueError("text must be at most 12000 characters")
        if entry["type"] == "approve":
            decision = body.get("decision")
            if decision not in ("approve", "deny"):
                raise ValueError("decision must be 'approve' or 'deny'")
        elif entry["type"] == "choice":
            decision = body.get("decision")
            if decision not in entry["choices"]:
                raise ValueError("decision must be one of the ask's choices")
        else:
            decision = None
            note = _text(note, 12000, "answer")
        answer = {"decision": decision, "text": (note or "").strip() or None}
        if entry.get("answer"):
            if {k: entry["answer"].get(k) for k in answer} != answer:
                raise web.HTTPConflict(text="ask already answered")
            return entry
        entry["answer"] = {**answer, "at": now()}
        self.save(rec)
        return {**entry, "nudge": await self.nudge(rec)}

    @staticmethod
    def input_key(entry):
        return entry["id"] if entry.get("kind") == "user" else entry["id"] + ":answer"

    def unread(self, rec):
        read = set(rec["read"])
        return [e for e in rec["feed"]
                if (e.get("kind") == "user" or (e.get("kind") == "ask" and e.get("answer")))
                and self.input_key(e) not in read]

    async def _deliver(self, rec, text):
        session = self.operator_session(rec)
        if session is None or session.exited:
            return "no-operator"
        try:
            sent = await asyncio.wait_for(session.deliver(text), timeout=20)
            return "sent" if sent else "pending"
        except Exception:
            log.warning("operator v2 delivery failed for %s", rec["id"], exc_info=True)
            return "unknown"

    async def nudge(self, rec):
        """One line to the operator's terminal that there is input to read;
        never the input itself."""
        waiting = len(self.unread(rec))
        if not waiting:
            return "nothing"
        if rec["id"] in self.nudging:
            return "coalesced"
        self.nudging.add(rec["id"])
        try:
            return await self._deliver(rec, NUDGE.format(n=waiting))
        finally:
            self.nudging.discard(rec["id"])

    def inbox(self, name):
        rec = self.bound(name)
        rows = self.unread(rec)
        if rows:
            rec["read"] = (rec["read"] + [self.input_key(e) for e in rows])[-FEED_LIMIT:]
            self.save(rec)
        return {"operator": rec["id"], "watch": rec["watch"], "messages": [
            {"id": e["id"], "kind": e["kind"], "at": e["at"], "text": e["text"],
             **({"type": e["type"], "choices": e.get("choices") or [], "answer": e["answer"]}
                if e["kind"] == "ask" else {})}
            for e in rows]}

    # -- relaying the user's instruction ------------------------------------
    async def dispatch(self, name, body):
        rec = self.bound(name)
        if not isinstance(body, dict):
            raise ValueError("object required")
        text = _text(body.get("text"), 12000)
        basis = self.find(rec, body.get("on_behalf_of") or "")
        if not (basis.get("kind") == "user" or (basis.get("kind") == "ask" and basis.get("answer")
                                               and basis["answer"].get("decision") != "deny")):
            raise web.HTTPForbidden(text="on_behalf_of must name a user message or an answered, not denied, ask")
        session = self.member(rec, body.get("target"))
        if session.exited:
            raise ValueError(f"{session.sdef.name} is not running")
        message = f"[Operator relay — 사용자 지시, operator {rec['name']}] {text}"
        try:
            sent = await asyncio.wait_for(session.deliver(message), timeout=20)
            delivery = "sent" if sent else "pending"
        except Exception:
            delivery = "unknown"
        return self.append(rec, {"kind": "dispatch", "role": "bot", "target": session.sdef.name,
                                 "text": text, "on_behalf_of": basis["id"], "delivery": delivery})

    # -- what the operator polls -------------------------------------------
    async def poll(self, name, since=None):
        rec = self.bound(name)
        members = {s.sdef.name: s for s in self.watched(rec)}
        category = {n: session_category(s) for n, s in members.items()}
        running = [s for n, s in members.items() if category[n] == CATEGORY_RUNNING]
        snapshot = self.snapshot_of(list(members.values()))
        gates, gates_ok = await self.gates_read(running)
        work, work_ok = await self.work_read(running)
        degraded = [what for what, ok in (("gates", gates_ok), ("board", work_ok)) if not ok]
        cutoff = session_events.timestamp(since) if since else None
        events, attention = [], []
        paused = [{"session": n, "paused_at": getattr(s, "paused_at", None)}
                  for n, s in members.items() if category[n] == CATEGORY_PAUSED]
        for row in snapshot:
            if row["name"] not in members:
                continue
            live = category[row["name"]] == CATEGORY_RUNNING
            for event in row.get("events", []):
                stamp = session_events.timestamp(event.get("at"))
                question = live and event.get("question") and not event.get("answer")
                if question:
                    # An open question waits whatever the cursor says.
                    attention.append({"session": row["name"], "kind": "observer_ask", "id": event.get("id"),
                                      "text": str(event.get("text", ""))[:400],
                                      "choices": event.get("choices") or []})
                if cutoff is not None and stamp <= cutoff:
                    continue
                events.append({"session": row["name"], "project": project_of(members[row["name"]]),
                               "at": event.get("at"), "kind": event.get("kind"),
                               "origin": event.get("origin"), "text": str(event.get("text", ""))[:400],
                               "needs_action": bool(event.get("needs_action")) and live,
                               "category": category[row["name"]]})
            if live and row.get("state") == "blocked":
                attention.append({"session": row["name"], "kind": "blocked", "text": row.get("summary")})
        for gate in gates:
            attention.append({"kind": "cflow_gate", **gate})
        events.sort(key=lambda e: session_events.timestamp(e["at"]))
        if since:
            more = len(events) > POLL_LIMIT
            events = events[:POLL_LIMIT]
        else:
            more = False
            events = events[-POLL_LIMIT:]
        cursor = events[-1]["at"] if events else since
        seen = rec["progress"]
        progress = []
        dirty = False
        if work:
            for session in running:
                n = session.sdef.name
                now_progress = progress_of(work.get(n))
                lines = progress_changes(seen.get(n), now_progress)
                if lines:
                    progress.append({"session": n, "changes": lines, **now_progress})
                seen[n] = now_progress
            dirty = True
        before = set(rec.get("watched") or ())
        ended = sorted(before - set(members))
        if before != set(members):
            rec["watched"] = sorted(members)
            dirty = True
        if dirty:
            self.save(rec)
        sessions = [{"name": n, "project": project_of(s), "running": not s.exited,
                     "category": category[n], "status": s.info().get("status"),
                     **progress_of(work.get(n))}
                    for n, s in members.items()]
        return {"operator": rec["id"], "watch": rec["watch"],
                "scope": {"all": rec["all"], "projects": rec["projects"]},
                "cursor": cursor, "more": more, "events": events, "attention": attention,
                "paused": paused, "progress": progress, "degraded": degraded, "ended": ended,
                "sessions": sessions, "unread_user_input": len(self.unread(rec))}

    # -- questions about work ----------------------------------------------
    def history(self, name, query):
        """The operator's own record: what it posted, asked, relayed and
        what the user said to it, newest first, optionally filtered."""
        rec = self.bound(name)
        kinds = [k for k in (query.get("kind") or "").split(",") if k]
        needle = (query.get("q") or "").casefold()
        target = query.get("session") or ""
        limit = _int(query.get("limit"), 50, 1, 200, "limit")
        rows = []
        for entry in reversed(rec["feed"]):
            if kinds and entry.get("kind") not in kinds:
                continue
            if target and target != entry.get("target") and target not in (entry.get("refs") or ()):
                continue
            if needle and needle not in str(entry.get("text", "")).casefold():
                continue
            rows.append(entry)
            if len(rows) >= limit:
                break
        return {"operator": rec["id"], "entries": rows}

    async def session_list(self, name, query):
        """The sessions in scope with their project, state, issue and status
        checks. ``state`` is ``live`` (default), ``ended`` or ``all``."""
        rec = self.bound(name)
        state = query.get("state") or "live"
        if state not in ("live", "ended", "all"):
            raise ValueError("state must be live, ended or all")
        project = query.get("project") or ""
        limit = _int(query.get("limit"), 100, 1, SESSIONS_LIMIT, "limit")
        rows = []
        for session in self.members(rec):
            category = session_category(session)
            live = category in LIVE
            if (state == "live" and not live) or (state == "ended" and live):
                continue
            if project and project_of(session) != projects.normalize(project):
                continue
            rows.append(session)
        total = len(rows)
        rows = rows[:limit]
        work, work_ok = await self.work_read(rows)
        return {"operator": rec["id"], "total": total, "truncated": total > len(rows),
                "degraded": [] if work_ok else ["board"],
                "sessions": [{"name": s.sdef.name, "project": project_of(s),
                              "category": session_category(s), "status": s.info().get("status"),
                              "cwd": getattr(s.sdef, "cwd", None),
                              **progress_of(work.get(s.sdef.name))} for s in rows]}

    async def session_detail(self, name, target):
        rec = self.bound(name)
        session = self.member(rec, target)
        rows = self.snapshot_of([session])
        row = rows[0] if rows else {}
        work, _ = await self.work_read([session])
        task = getattr(session.sdef, "task", None) or ""
        commits = await asyncio.to_thread(self._commits, session, 10)
        return {"operator": rec["id"], "name": session.sdef.name, "project": project_of(session),
                "category": session_category(session), "status": session.info().get("status"),
                "cwd": getattr(session.sdef, "cwd", None), "task": task[:2000],
                "state": row.get("state"), "summary": row.get("summary"),
                "last_activity_at": row.get("last_activity_at"),
                "events": [{"at": e.get("at"), "kind": e.get("kind"), "text": str(e.get("text", ""))[:400]}
                           for e in (row.get("events") or [])[-30:]],
                "commits": commits, **progress_of(work.get(session.sdef.name))}

    @staticmethod
    def _commits(session, limit):
        from .. import session_commits

        cwd = getattr(session.sdef, "cwd", None)
        if not cwd:
            return []
        return session_commits.for_session(cwd, session.sdef.name, limit=limit)

    async def commits(self, name, target, limit=None):
        rec = self.bound(name)
        session = self.member(rec, target)
        limit = _int(limit, 30, 1, 200, "limit")
        return {"operator": rec["id"], "session": session.sdef.name,
                "commits": await asyncio.to_thread(self._commits, session, limit)}

    async def transcript_search(self, name, query):
        """Where something was said: in one in-scope session's conversation
        (``session``), or across the scope's sessions, newest first."""
        from . import transcript_view

        rec = self.bound(name)
        text = query.get("q") or ""
        if not text:
            raise ValueError("'q' is required")
        limit = _int(query.get("limit"), 20, 1, 100, "limit")
        regex = query.get("regex") in ("1", "true")
        prose = query.get("prose") in ("1", "true")
        if query.get("session"):
            targets = [self.member(rec, query["session"])]
        else:
            targets = self.members(rec)
        searched, skipped, hits = [], max(0, len(targets) - SEARCH_SESSIONS), []

        def run():
            for session in targets[:SEARCH_SESSIONS]:
                try:
                    view = transcript_view.search(session.sdef.name, session.sdef, query=text,
                                                  regex=regex, limit=limit, prose_only=prose)
                except ValueError:
                    raise  # a bad pattern is the caller's, not one session's
                except Exception:
                    log.debug("operator v2: search of %s failed", session.sdef.name, exc_info=True)
                    continue
                searched.append(session.sdef.name)
                for match in view.get("matches") or []:
                    hits.append({"session": session.sdef.name, **match})

        await asyncio.to_thread(run)
        hits.sort(key=lambda h: str(h.get("ts") or h.get("at") or ""), reverse=True)
        return {"operator": rec["id"], "query": text, "searched": searched,
                "skipped_sessions": skipped, "matches": hits[:limit]}

    async def issue(self, name, issue_id):
        """One board issue, answered only from a board in this operator's
        scope: the boards its sessions (running or ended) stand on, and its
        projects' default workspaces."""
        rec = self.bound(name)
        board = self.board()
        if not isinstance(issue_id, str) or not re.match(r"^[A-Za-z0-9._-]{1,80}$", issue_id):
            raise ValueError("issue id required")
        if board is None or not board.available():
            raise web.HTTPServiceUnavailable(text="the board is not available")
        cwds = []
        for session in self.members(rec):
            cwd = getattr(session.sdef, "cwd", None)
            if cwd and cwd not in cwds:
                cwds.append(cwd)
        for project in (projects.names() if rec["all"] else rec["projects"]):
            cwd = projects.default_cwd(project)
            if cwd and cwd not in cwds:
                cwds.append(cwd)
        roots = []
        for cwd in cwds:
            try:
                root = await board.root_for(cwd)
            except Exception:
                continue
            if root is not None and root not in roots and board.has_board(root):
                roots.append(root)
        from ..cli_beads import BeadsError

        for root in roots:
            try:
                found = await board.show(root, issue_id)
            except BeadsError:
                continue
            linked = [s.sdef.name for s in self.members(rec)
                      if self._links(found, s)]
            return {"operator": rec["id"], "board": str(root), "issue": found, "sessions": linked}
        raise web.HTTPForbidden(text=f"{issue_id} is on no board in operator {rec['name']!r}'s scope")

    @staticmethod
    def _links(issue, session):
        from . import beads as beads_mod

        sdef = session.sdef
        return bool(beads_mod.match([issue], sdef.name, issue=getattr(sdef, "issue", None),
                                    task=getattr(sdef, "task", None)))


def install(app, *, create=None, gates=None, work=None):
    """Mount the operator v2 routes. ``create`` starts a session from a
    session-create body (``POST /api/sessions``'s own path), ``gates`` lists
    cflow gates waiting on sessions and ``work`` reads their status checks and
    board issues; the API module passes all three."""
    ops = OperatorsV2(paths.daemon_dir(), app["manager"], app.get("observer"), gates, work,
                      board=lambda: app.get("beads"))
    app["operators_v2"] = ops

    def fail(exc):
        return web.json_response({"error": str(exc)}, status=400)

    async def body_of(request):
        try:
            return await request.json()
        except ValueError:
            raise ValueError("JSON body required") from None

    def guarded(handler):
        async def run(request):
            try:
                return web.json_response(await handler(request))
            except ValueError as exc:
                return fail(exc)
        return run

    @guarded
    async def listing(request):
        return ops.listing()

    async def create_op(request):
        try:
            return web.json_response(ops.summary(ops.create(await body_of(request))), status=201)
        except ValueError as exc:
            return fail(exc)

    @guarded
    async def view(request):
        return await ops.view(request.match_info["id"])

    @guarded
    async def update(request):
        return await ops.update(request.match_info["id"], await body_of(request))

    @guarded
    async def delete(request):
        return ops.delete(request.match_info["id"])

    async def start(request):
        try:
            rec = ops.get(request.match_info["id"])
            body = await request.json() if request.can_read_body else {}
        except ValueError as exc:
            return fail(exc)
        body = body if isinstance(body, dict) else {}
        current = ops.operator_session(rec)
        if current is not None and not current.exited:
            return web.json_response({"error": f"operator {rec['name']!r} is already running as {current.sdef.name}",
                                      "operator": current.sdef.name}, status=409)
        profile = str(body.get("profile") or rec.get("profile") or "").strip()
        if not profile:
            return fail("profile is required")
        scope = "전체 프로젝트" if rec["all"] else ", ".join(rec["projects"])
        spec = {"profile": profile, "project": projects.DEFAULT if rec["all"] else rec["projects"][0],
                "context": json.dumps({"operator_v2": rec["id"]}), "beads": False,
                "name": str(body.get("name") or "").strip(),
                "task": (f"너는 Operator v2 '{rec['name']}'(id {rec['id']})이다. 담당 범위: {scope}. "
                         "사용자와의 대화는 터미널이 아니라 opv2_* 도구로 한다. opv2_poll로 범위 안 "
                         "세션을 관찰하고, 사용자 입력은 opv2_inbox로 읽고, opv2_post/opv2_ask로 답한다. "
                         "작업 내용 질문에는 opv2_sessions, opv2_session, opv2_issue, opv2_commits, "
                         "opv2_transcript_search, opv2_history로 근거를 찾아 답한다. 다른 세션에 대한 "
                         "지시는 사용자의 메시지나 승인된 ask를 근거로 opv2_dispatch로만 전달한다.")}
        if WORKFLOW:
            spec["workflow"] = WORKFLOW
            spec["cflow_inputs"] = {"operator": rec["id"]}
        for key in ("model", "effort"):
            value = str(body.get(key) or rec.get(key) or "").strip()
            if value:
                spec[key] = value
        status, payload = await create(request, spec)
        if status >= 300:
            return web.json_response(payload, status=status)
        ops.bind(rec, payload["name"])
        return web.json_response(payload, status=201)

    async def stop(request):
        from . import actors, kill_gate

        rec = ops.get(request.match_info["id"])
        session = ops.operator_session(rec)
        if session is None or session.exited:
            return web.json_response({"id": rec["id"], "stopped": False, "reason": "not running"})
        by = actors.from_request(request)
        if kill_gate.is_gated(by, session.sdef.name):
            return web.json_response({"error": "an agent may not stop an operator; ask the user"}, status=403)
        request.app["manager"].kill(session.sdef.name, by=by)
        ops.append(rec, {"kind": "system", "role": "system", "text": f"Operator 세션 {session.sdef.name}을 정지했습니다."})
        return web.json_response({"id": rec["id"], "stopped": True, "session": session.sdef.name})

    @guarded
    async def message(request):
        return await ops.message(request.match_info["id"], await body_of(request))

    @guarded
    async def answer(request):
        return await ops.answer(request.match_info["id"], request.match_info["entry"], await body_of(request))

    @guarded
    async def seen(request):
        return ops.mark_seen(request.match_info["id"])

    @guarded
    async def agent_post(request):
        return ops.post(request.match_info["name"], await body_of(request))

    @guarded
    async def agent_ask(request):
        return ops.ask(request.match_info["name"], await body_of(request))

    @guarded
    async def agent_inbox(request):
        return ops.inbox(request.match_info["name"])

    @guarded
    async def agent_poll(request):
        return await ops.poll(request.match_info["name"], request.query.get("since") or None)

    @guarded
    async def agent_dispatch(request):
        return await ops.dispatch(request.match_info["name"], await body_of(request))

    @guarded
    async def agent_history(request):
        return ops.history(request.match_info["name"], request.query)

    @guarded
    async def agent_sessions(request):
        return await ops.session_list(request.match_info["name"], request.query)

    @guarded
    async def agent_session(request):
        return await ops.session_detail(request.match_info["name"], request.match_info["target"])

    @guarded
    async def agent_commits(request):
        return await ops.commits(request.match_info["name"], request.match_info["target"],
                                 request.query.get("limit"))

    @guarded
    async def agent_search(request):
        return await ops.transcript_search(request.match_info["name"], request.query)

    @guarded
    async def agent_issue(request):
        return await ops.issue(request.match_info["name"], request.match_info["issue"])

    r = app.router
    r.add_get("/api/operators", listing)
    r.add_post("/api/operators", create_op)
    agent = "/api/operators/agent/{name}"
    r.add_post(agent + "/post", agent_post)
    r.add_post(agent + "/ask", agent_ask)
    r.add_get(agent + "/inbox", agent_inbox)
    r.add_get(agent + "/poll", agent_poll)
    r.add_post(agent + "/dispatch", agent_dispatch)
    r.add_get(agent + "/history", agent_history)
    r.add_get(agent + "/sessions", agent_sessions)
    r.add_get(agent + "/sessions/{target}", agent_session)
    r.add_get(agent + "/sessions/{target}/commits", agent_commits)
    r.add_get(agent + "/transcript-search", agent_search)
    r.add_get(agent + "/issues/{issue}", agent_issue)
    r.add_get("/api/operators/{id}", view)
    r.add_patch("/api/operators/{id}", update)
    r.add_delete("/api/operators/{id}", delete)
    r.add_post("/api/operators/{id}/start", start)
    r.add_post("/api/operators/{id}/stop", stop)
    r.add_post("/api/operators/{id}/message", message)
    r.add_post("/api/operators/{id}/asks/{entry}/answer", answer)
    r.add_post("/api/operators/{id}/seen", seen)
    return ops
