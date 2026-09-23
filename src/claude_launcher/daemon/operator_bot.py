"""Operator: one bot session per project that watches the others for the user.

The Observer already turns every session's transcript into events, and a
person reading its board still has to read all of it. An *operator* is an
agent session (role ``operator``, workflow ``operator``) that reads that
stream on the user's behalf and says only what matters. Its conversation with
the user does not happen in its terminal: it posts through ``operator_post``
and ``operator_ask`` (the feed below), the user answers from the Operator tab
or its modal, and the session reads what the user wrote with
``operator_inbox``. The terminal only receives a one-line nudge that there is
something to read.

One operator per project (:mod:`claude_launcher.projects`): the project is the
grouping a person already uses to say "the sessions for this piece of work",
so the operator watches the sessions filed under it and nothing else.

Authority is deliberately narrow. The operator may type into another session
of its project (``operator_dispatch``) only to carry an instruction the user
gave — the call names the feed entry it acts on (a user message or an answered
ask), and a dispatch naming anything else is refused. It never approves cflow
gates or answers other sessions' questions: those stay with the user, and the
operator's part is to notice them and ask.

State lives per project under the daemon directory, never in the repository.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from aiohttp import web

from .. import atomic, projects
from . import paths, session_events

log = logging.getLogger(__name__)

#: Feed entries kept per project. Unanswered asks survive the trim.
FEED_LIMIT = 500
#: Events one poll returns at most; the cursor lets the next poll continue.
POLL_LIMIT = 80
LEVELS = ("info", "attention", "urgent")
ASK_TYPES = ("approve", "choice", "text")
MESH_PREFIX = "operator-"
NUDGE = ("[Operator] 사용자 입력 {n}건이 대기 중입니다. operator_inbox 도구로 읽고 "
         "operator_post/operator_ask로 응답하십시오. 이 줄 자체는 사용자의 지시가 아닙니다.")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_of(session) -> str:
    return projects.normalize(getattr(session.sdef, "project", None))


def _text(value, limit, what="text"):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{what} must contain 1..{limit} characters")
    return value.strip()


class Operators:
    """Feeds, bindings and cursors for every project's operator."""

    def __init__(self, root, manager, observer=None, gates=None):
        self.root = root / "operator"
        self.manager, self.observer, self.gates = manager, observer, gates
        self.cache = {}
        self.nudging = set()

    # -- storage -----------------------------------------------------------
    def path(self, project):
        return self.root / (hashlib.sha256(project.encode()).hexdigest() + ".json")

    def state(self, project):
        project = projects.normalize(project)
        if project not in self.cache:
            try:
                data = json.loads(self.path(project).read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("invalid operator state")
            except (OSError, ValueError):
                data = {}
            data["project"] = project
            data.setdefault("session", None)
            data.setdefault("feed", [])
            #: Keys of the user's inputs the operator has already read: a user
            #: message's id, or ``<ask id>:answer`` for an answer. A set of
            #: keys rather than a timestamp, because two inputs in one second
            #: would otherwise hide the second behind the first's mark.
            data.setdefault("read", [])
            self.cache[project] = data
        return self.cache[project]

    def save(self, project):
        data = self.state(project)
        path = self.path(data["project"])
        with atomic.scratch(path) as scratch:
            scratch.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            atomic.replace(scratch, path)

    def append(self, project, entry):
        data = self.state(project)
        entry = {"id": uuid.uuid4().hex, "at": now(), **entry}
        data["feed"].append(entry)
        feed = data["feed"]
        if len(feed) > FEED_LIMIT:
            open_asks = {e["id"] for e in feed if e.get("kind") == "ask" and not e.get("answer")}
            keep = {e["id"] for e in feed[-FEED_LIMIT:]} | open_asks
            data["feed"] = [e for e in feed if e["id"] in keep]
        self.save(project)
        return entry

    def find(self, project, entry_id):
        for entry in self.state(project)["feed"]:
            if entry["id"] == entry_id:
                return entry
        raise web.HTTPNotFound(text="feed entry not found")

    # -- sessions ----------------------------------------------------------
    def sessions(self):
        return list(self.manager.list())

    def session(self, name):
        for session in self.sessions():
            if session.sdef.name == name:
                return session
        return None

    def operator_session(self, project):
        name = self.state(project).get("session")
        return self.session(name) if name else None

    def bound(self, name):
        """The project whose operator ``name`` is, or 403."""
        session = self.session(name)
        if session is None:
            raise web.HTTPNotFound(text="session not found")
        project = project_of(session)
        if self.state(project).get("session") != name:
            raise web.HTTPForbidden(text=f"{name} is not the operator of project {project!r}")
        return project

    def bind(self, project, name):
        data = self.state(project)
        data["session"] = name
        self.save(project)
        self.append(project, {"kind": "system", "role": "system",
                              "text": f"Operator 세션 {name}이 시작되었습니다."})

    def members(self, project):
        """The sessions an operator watches: its project's, minus itself."""
        me = self.state(project).get("session")
        return [s for s in self.sessions()
                if project_of(s) == project and s.sdef.name != me]

    # -- view for the UI ---------------------------------------------------
    def pending(self, project):
        return sum(1 for e in self.state(project)["feed"]
                   if e.get("kind") == "ask" and not e.get("answer"))

    def panel(self, project):
        """The right-hand panel: what each running session is doing now.
        Exited sessions stay out of it; their events still reach ``poll``."""
        snapshot = {row["name"]: row for row in (self.observer.snapshot()["sessions"] if self.observer else [])}
        rows = []
        for session in self.members(project):
            if session.exited:
                continue
            name = session.sdef.name
            row = snapshot.get(name, {})
            open_questions = sum(1 for e in row.get("events", []) if e.get("question") and not e.get("answer"))
            rows.append({"name": name, "running": not session.exited,
                         "status": row.get("status") or session.info().get("status"),
                         "state": row.get("state"), "summary": row.get("summary"),
                         "last_activity_at": row.get("last_activity_at"),
                         "questions": open_questions})
        rows.sort(key=lambda r: (-r["questions"], r["name"]))
        return rows

    def view(self, project):
        project = projects.normalize(project)
        data = self.state(project)
        op = self.operator_session(project)
        operator = None
        if op is not None:
            operator = {"name": op.sdef.name, "running": not op.exited,
                        "status": op.info().get("status")}
        elif data.get("session"):
            operator = {"name": data["session"], "running": False, "status": "missing"}
        return {"project": project, "operator": operator, "feed": data["feed"][-200:],
                "pending": self.pending(project), "sessions": self.panel(project)}

    def pending_all(self):
        by_project = {p: self.pending(p) for p in projects.names()}
        return {"total": sum(by_project.values()), "by_project": by_project}

    # -- the bot's output --------------------------------------------------
    def _idempotent(self, project, token, payload):
        if token is None:
            return None
        if not isinstance(token, str) or not token or len(token) > 128:
            raise ValueError("request_id must contain 1..128 characters")
        for entry in self.state(project)["feed"]:
            if entry.get("request_id") == token:
                if any(entry.get(k) != v for k, v in payload.items()):
                    raise web.HTTPConflict(text="request_id reused with different content")
                return entry
        return None

    def _refs(self, project, refs):
        if refs is None:
            return []
        if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(r, str) for r in refs):
            raise ValueError("refs must be a list of at most 20 session names")
        known = {s.sdef.name for s in self.members(project)}
        unknown = [r for r in refs if r not in known]
        if unknown:
            raise ValueError(f"not a session of project {project!r}: {', '.join(unknown)}")
        return refs

    def post(self, name, body):
        project = self.bound(name)
        if not isinstance(body, dict):
            raise ValueError("object required")
        level = body.get("level", "info")
        if level not in LEVELS:
            raise ValueError("level must be one of " + ", ".join(LEVELS))
        payload = {"kind": "post", "role": "bot", "text": _text(body.get("text"), 4000),
                   "level": level, "refs": self._refs(project, body.get("refs"))}
        found = self._idempotent(project, body.get("request_id"), payload)
        if found:
            return found
        return self.append(project, {**payload, "request_id": body.get("request_id")})

    def ask(self, name, body):
        project = self.bound(name)
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
                   "choices": choices, "level": level, "refs": self._refs(project, body.get("refs"))}
        found = self._idempotent(project, body.get("request_id"), payload)
        if found:
            return found
        if self.pending(project) >= 50:
            raise ValueError("50 unanswered asks; wait for the user before asking more")
        return self.append(project, {**payload, "request_id": body.get("request_id"), "answer": None})

    # -- the user's input --------------------------------------------------
    async def message(self, project, body):
        project = projects.normalize(project)
        text = _text(body.get("text") if isinstance(body, dict) else None, 12000)
        entry = self.append(project, {"kind": "user", "role": "user", "text": text})
        entry["nudge"] = await self.nudge(project)
        return entry

    async def answer(self, project, entry_id, body):
        project = projects.normalize(project)
        entry = self.find(project, entry_id)
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
        self.save(project)
        entry["nudge"] = await self.nudge(project)
        return entry

    @staticmethod
    def input_key(entry):
        return entry["id"] if entry.get("kind") == "user" else entry["id"] + ":answer"

    def unread(self, project):
        read = set(self.state(project)["read"])
        return [e for e in self.state(project)["feed"]
                if (e.get("kind") == "user" or (e.get("kind") == "ask" and e.get("answer")))
                and self.input_key(e) not in read]

    async def nudge(self, project):
        """Tell the operator's terminal there is input to read — one line,
        never the input itself, which it reads with ``operator_inbox``."""
        session = self.operator_session(project)
        if session is None or session.exited:
            return "no-operator"
        waiting = len(self.unread(project))
        if not waiting:
            return "nothing"
        if project in self.nudging:
            return "coalesced"
        self.nudging.add(project)
        try:
            sent = await asyncio.wait_for(session.deliver(NUDGE.format(n=waiting)), timeout=20)
            return "sent" if sent else "pending"
        except Exception:
            log.warning("operator nudge failed for %s", project, exc_info=True)
            return "unknown"
        finally:
            self.nudging.discard(project)

    def inbox(self, name):
        project = self.bound(name)
        rows = self.unread(project)
        data = self.state(project)
        if rows:
            data["read"] = (data["read"] + [self.input_key(e) for e in rows])[-FEED_LIMIT:]
            self.save(project)
        return {"project": project, "messages": [
            {"id": e["id"], "kind": e["kind"], "at": e["at"], "text": e["text"],
             **({"type": e["type"], "choices": e.get("choices") or [], "answer": e["answer"]}
                if e["kind"] == "ask" else {})}
            for e in rows]}

    # -- relaying the user's instruction ------------------------------------
    async def dispatch(self, name, body):
        project = self.bound(name)
        if not isinstance(body, dict):
            raise ValueError("object required")
        target = body.get("target")
        text = _text(body.get("text"), 12000)
        basis = self.find(project, body.get("on_behalf_of") or "")
        if not (basis.get("kind") == "user" or (basis.get("kind") == "ask" and basis.get("answer")
                                               and basis["answer"].get("decision") != "deny")):
            raise web.HTTPForbidden(text="on_behalf_of must name a user message or an answered, not denied, ask")
        session = next((s for s in self.members(project) if s.sdef.name == target), None)
        if session is None:
            raise ValueError(f"{target!r} is not a session of project {project!r}")
        if session.exited:
            raise ValueError(f"{target} is not running")
        message = f"[Operator relay — 사용자 지시, project {project}] {text}"
        try:
            sent = await asyncio.wait_for(session.deliver(message), timeout=20)
            delivery = "sent" if sent else "pending"
        except Exception:
            delivery = "unknown"
        return self.append(project, {"kind": "dispatch", "role": "bot", "target": target, "text": text,
                                     "on_behalf_of": basis["id"], "delivery": delivery})

    # -- what the operator polls -------------------------------------------
    async def poll(self, name, since=None):
        project = self.bound(name)
        members = {s.sdef.name: s for s in self.members(project)}
        # The snapshot is read on the loop, as every Observer reader does; the
        # gates are run-state files, so they are read off it.
        snapshot = self.observer.snapshot()["sessions"] if self.observer else []
        gates = await asyncio.to_thread(self.gates, list(members.values())) if self.gates else []
        cutoff = session_events.timestamp(since) if since else None
        events, attention = [], []
        for row in snapshot:
            if row["name"] not in members:
                continue
            for event in row.get("events", []):
                stamp = session_events.timestamp(event.get("at"))
                if cutoff is not None and stamp <= cutoff:
                    continue
                events.append({"session": row["name"], "at": event.get("at"),
                               "kind": event.get("kind"), "origin": event.get("origin"),
                               "text": str(event.get("text", ""))[:400],
                               "needs_action": bool(event.get("needs_action"))})
                if event.get("question") and not event.get("answer"):
                    attention.append({"session": row["name"], "kind": "observer_ask", "id": event.get("id"),
                                      "text": str(event.get("text", ""))[:400],
                                      "choices": event.get("choices") or []})
            if row.get("state") == "blocked":
                attention.append({"session": row["name"], "kind": "blocked", "text": row.get("summary")})
        # Earlier open questions are still open, whatever the cursor says.
        for row in snapshot:
            if row["name"] not in members or cutoff is None:
                continue
            for event in row.get("events", []):
                if (event.get("question") and not event.get("answer")
                        and session_events.timestamp(event.get("at")) <= cutoff):
                    attention.append({"session": row["name"], "kind": "observer_ask", "id": event.get("id"),
                                      "text": str(event.get("text", ""))[:400],
                                      "choices": event.get("choices") or []})
        for gate in gates:
            attention.append({"kind": "cflow_gate", **gate})
        events.sort(key=lambda e: session_events.timestamp(e["at"]))
        more = len(events) > POLL_LIMIT
        events = events[:POLL_LIMIT]
        cursor = events[-1]["at"] if events else since
        sessions = [{"name": n, "running": not s.exited, "status": s.info().get("status")}
                    for n, s in members.items()]
        return {"project": project, "cursor": cursor, "more": more, "events": events,
                "attention": attention, "sessions": sessions,
                "unread_user_input": len(self.unread(project))}


def install(app, *, create=None, gates=None):
    """Mount the operator routes. ``create`` starts a session from a
    session-create body (the same path ``POST /api/sessions`` takes) and
    ``gates`` lists the cflow gates waiting on a set of sessions; both are
    passed in by the API module, which owns those mechanisms."""
    ops = Operators(paths.daemon_dir(), app["manager"], app.get("observer"), gates)
    app["operators"] = ops

    def fail(exc):
        return web.json_response({"error": str(exc)}, status=400)

    async def view(request):
        return web.json_response(ops.view(request.query.get("project") or projects.DEFAULT))

    async def pending(request):
        return web.json_response(ops.pending_all())

    async def start(request):
        body = await request.json()
        if not isinstance(body, dict):
            return fail("object required")
        try:
            project = projects.require(body.get("project") or projects.DEFAULT).name
        except projects.ProjectError as exc:
            return fail(exc)
        current = ops.operator_session(project)
        if current is not None and not current.exited:
            return web.json_response({"error": f"project {project!r} already has operator {current.sdef.name}",
                                      "operator": current.sdef.name}, status=409)
        profile = str(body.get("profile") or "").strip()
        if not profile:
            return fail("profile is required")
        mesh_mgr = request.app["mesh"]
        mesh = MESH_PREFIX + project
        try:
            mesh_mgr.get(mesh)
        except Exception:
            mesh_mgr.create(mesh, project=project)
        spec = {"profile": profile, "project": project, "mesh": mesh, "role": "operator",
                "workflow": "operator", "context": json.dumps({"project": project}),
                "beads": False, "name": str(body.get("name") or "").strip(),
                "task": (f"너는 프로젝트 {project}의 Operator bot이다. stance와 operator 워크플로가 "
                         "규칙이다. 사용자와의 대화는 터미널이 아니라 operator_* 도구로 한다.")}
        status, payload = await create(request, spec)
        if status >= 300:
            return web.json_response(payload, status=status)
        ops.bind(project, payload["name"])
        return web.json_response(payload, status=201)

    async def message(request):
        try:
            return web.json_response(await ops.message(request.query.get("project") or projects.DEFAULT,
                                                       await request.json()))
        except ValueError as exc:
            return fail(exc)

    async def answer(request):
        try:
            return web.json_response(await ops.answer(request.query.get("project") or projects.DEFAULT,
                                                      request.match_info["entry"], await request.json()))
        except ValueError as exc:
            return fail(exc)

    async def agent_post(request):
        try:
            return web.json_response(ops.post(request.match_info["name"], await request.json()))
        except ValueError as exc:
            return fail(exc)

    async def agent_ask(request):
        try:
            return web.json_response(ops.ask(request.match_info["name"], await request.json()))
        except ValueError as exc:
            return fail(exc)

    async def agent_inbox(request):
        return web.json_response(ops.inbox(request.match_info["name"]))

    async def agent_poll(request):
        name = request.match_info["name"]
        return web.json_response(await ops.poll(name, request.query.get("since") or None))

    async def agent_dispatch(request):
        try:
            return web.json_response(await ops.dispatch(request.match_info["name"], await request.json()))
        except ValueError as exc:
            return fail(exc)

    app.router.add_get("/api/operator", view)
    app.router.add_get("/api/operator/pending", pending)
    app.router.add_post("/api/operator/start", start)
    app.router.add_post("/api/operator/message", message)
    app.router.add_post("/api/operator/asks/{entry}/answer", answer)
    app.router.add_post("/api/operator/agent/{name}/post", agent_post)
    app.router.add_post("/api/operator/agent/{name}/ask", agent_ask)
    app.router.add_get("/api/operator/agent/{name}/inbox", agent_inbox)
    app.router.add_get("/api/operator/agent/{name}/poll", agent_poll)
    app.router.add_post("/api/operator/agent/{name}/dispatch", agent_dispatch)
    return ops
