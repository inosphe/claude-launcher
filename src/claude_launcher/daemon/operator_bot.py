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
from .session import CATEGORY_PAUSED, CATEGORY_RUNNING, session_category

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


#: How a board status reads in a progress line: in_review is the landing
#: request (improv-worker's integration-request moves the issue there), and a
#: close is what wrapup does once landed proved the merge.
ISSUE_STAGES = {"open": "열림", "in_ready": "준비됨", "in_progress": "작업 중",
                "in_review": "머지 요청", "blocked": "차단", "closed": "닫힘"}


def progress_of(entry):
    """One session's work progress as the panel and poll carry it: its
    status-check answers (commit / tests / merge, as the user configured
    them) and the board issue it is on."""
    entry = entry or {}
    checks = [{"name": c.get("name"), "question": c.get("question"), "answer": c.get("answer")}
              for c in entry.get("checks") or [] if isinstance(c, dict) and c.get("name")]
    issue = entry.get("issue") if isinstance(entry.get("issue"), dict) else None
    return {"checks": checks, "issue": issue}


def progress_changes(before, after):
    """The lines that say what moved between two ``progress_of`` readings:
    a status check whose answer changed, or the issue's status. ``before``
    None is a first reading, which is history and says nothing."""
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


#: How a session's category reads in a thread line.
CATEGORY_LABELS = {"running": "실행 중", "paused": "일시정지", "killed": "종료", "archived": "보관",
                   None: "목록에 없음"}
#: Cards (the bot's posts and asks that name sessions) whose sessions are
#: followed; older cards keep the thread they have.
TRACK_CARDS = 40
#: Seconds between two follow-up reads of one project: the view polls every
#: few seconds, and a board read per poll would be waste.
TRACK_INTERVAL = 15.0
#: Seconds a poll, the view or a follow-up read waits for the board or the
#: run states before answering without them. The operator's MCP client gives
#: up after 30s; a poll that waits behind a busy board lock past that is one
#: the operator reads as the daemon being down (2026-09-23 22:52, s739: three
#: failed polls while the daemon swept exited sessions' issues on restart).
READ_TIMEOUT = 5.0


def _local(stamp):
    """An ISO stamp as this machine's local time, for a line a person reads."""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return stamp or "기록 없음"


def restart_text(current, previous):
    """The feed line for a daemon restart: when the new daemon came up, which
    one it replaced, and whether anything on this machine asked for it (the
    restart ledger, :mod:`.restart_notice`). A boot nothing asked for is said
    as such; its cause is not recorded anywhere, so none is guessed."""
    lines = [f"데몬이 재시작되었습니다. 새 데몬 기동: {_local(current.get('started_at'))}"]
    if previous:
        lines.append(f"이전 데몬 기동: {_local(previous.get('started_at'))} (pid {previous.get('pid')})")
    if current.get("requested"):
        who = ", ".join(current.get("requested_by") or []) or "사용자 셸 또는 웹 UI"
        at = current.get("requested_at")
        via = current.get("requested_via")
        lines.append(f"재시작 요청: {who}" + (f", 요청 시각 {_local(at)}" if at else "")
                     + (f" ({via})" if via else ""))
    else:
        lines.append("재시작 요청 기록 없음: 이전 데몬이 요청 없이 끝났습니다(원인 미상).")
    lines.append("중단 구간의 세션 이벤트는 Operator가 이어서 읽습니다.")
    return "\n".join(lines)


def watch_of(category, gate, work):
    """What a card follows about one of its sessions: its category, the cflow
    gate it waits at (running sessions only; a stopped run waits on nobody)
    and, when the board was read, its progress."""
    reading = {"category": category}
    if category == CATEGORY_RUNNING:
        reading["gate"] = gate
    if work is not None:
        reading["progress"] = progress_of(work)
    return reading


def watch_changes(before, after):
    """The thread lines for what moved between two ``watch_of`` readings.
    A field only one of the two readings has is not compared: a board that
    could not be read this time is not a change."""
    lines = []
    if before.get("category") != after.get("category"):
        lines.append(f"상태: {CATEGORY_LABELS.get(before.get('category'), before.get('category'))}"
                     f" → {CATEGORY_LABELS.get(after.get('category'), after.get('category'))}")
    if "gate" in before and "gate" in after and before["gate"] != after["gate"]:
        if before["gate"]:
            lines.append(f"게이트 {before['gate']} 해소")
        if after["gate"]:
            lines.append(f"게이트 {after['gate']} 대기")
    if "progress" in before and "progress" in after:
        lines.extend(progress_changes(before["progress"], after["progress"]))
    return lines


def _text(value, limit, what="text"):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{what} must contain 1..{limit} characters")
    return value.strip()


class Operators:
    """Feeds, bindings and cursors for every project's operator."""

    def __init__(self, root, manager, observer=None, gates=None, work=None):
        self.root = root / "operator"
        self.manager, self.observer, self.gates = manager, observer, gates
        #: async ``work(sessions) -> {name: {"checks": [...], "issue": {...}}}``:
        #: each session's status-check answers and its board issue, passed in
        #: by the API module, which owns the board.
        self.work = work
        self.cache = {}
        self.nudging = set()
        #: project -> loop time of its last follow-up read (``track``).
        self.tracked = {}

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

    def panel(self, project, work=None):
        """The right-hand panel: what each running or paused session is doing.
        Killed and archived sessions stay out of it. A paused session is
        listed with its category and never counts its open questions: it is
        stopped on purpose, and nothing in it waits on the user until it is
        resumed."""
        snapshot = {row["name"]: row for row in (self.observer.snapshot()["sessions"] if self.observer else [])}
        work = work or {}
        rows = []
        for session in self.members(project):
            category = session_category(session)
            if category not in (CATEGORY_RUNNING, CATEGORY_PAUSED):
                continue
            name = session.sdef.name
            row = snapshot.get(name, {})
            open_questions = 0 if category == CATEGORY_PAUSED else sum(
                1 for e in row.get("events", []) if e.get("question") and not e.get("answer"))
            rows.append({"name": name, "running": not session.exited, "category": category,
                         "status": row.get("status") or session.info().get("status"),
                         "state": row.get("state"), "summary": row.get("summary"),
                         "last_activity_at": row.get("last_activity_at"),
                         "questions": open_questions,
                         **progress_of(work.get(name))})
        rows.sort(key=lambda r: (r["category"] == CATEGORY_PAUSED, -r["questions"], r["name"]))
        return rows

    async def work_read(self, sessions):
        """``(work, ok)``: ``self.work`` for ``sessions``, and whether it was
        read. Not wired or nothing to read is ok; a board that fails or takes
        longer than :data:`READ_TIMEOUT` is not, and gives nothing."""
        if not self.work or not sessions:
            return {}, True
        try:
            return (await asyncio.wait_for(self.work(sessions), READ_TIMEOUT)) or {}, True
        except asyncio.TimeoutError:
            log.info("operator: board read took over %.0fs; answered without it", READ_TIMEOUT)
            return {}, False
        except Exception:  # a board read failing must not take the panel down
            log.debug("operator: work read failed", exc_info=True)
            return {}, False

    async def work_of(self, sessions):
        """``self.work`` for ``sessions``, or nothing when it is not wired or
        the board cannot be read (the panel then shows no progress)."""
        return (await self.work_read(sessions))[0]

    async def gates_read(self, sessions):
        """``(gates, ok)``: the cflow gates waiting on a person among
        ``sessions``, read off the loop and bounded like :meth:`work_read`."""
        if not self.gates or not sessions:
            return [], True
        try:
            return (await asyncio.wait_for(asyncio.to_thread(self.gates, sessions), READ_TIMEOUT)) or [], True
        except asyncio.TimeoutError:
            log.info("operator: gate read took over %.0fs; answered without it", READ_TIMEOUT)
            return [], False
        except Exception:  # a run-state read failing must not stop the rest
            log.debug("operator: gate read failed", exc_info=True)
            return [], False

    # -- daemon restarts ---------------------------------------------------
    def announce_boot(self, current, previous):
        """Write a restart entry into every project that has an operator,
        once per boot: the daemon says it restarted itself rather than leave
        it to the bot, which only sees that its calls failed for a while. The
        next poll carries the entry once (``restart``). Returns the entries."""
        started = (current or {}).get("started_at")
        if not started:
            return []
        out = []
        for path in sorted(self.root.glob("*.json")) if self.root.is_dir() else []:
            try:
                project = json.loads(path.read_text(encoding="utf-8")).get("project")
            except (OSError, ValueError, AttributeError):
                continue
            if not isinstance(project, str):
                continue
            data = self.state(project)
            if not data.get("session") or data.get("boot") == started:
                continue
            entry = self.append(project, {
                "kind": "system", "role": "system", "event": "daemon_restart",
                "text": restart_text(current, previous),
                "boot": {"started_at": started, "previous_started_at": (previous or {}).get("started_at"),
                         "requested": bool(current.get("requested")),
                         "requested_by": current.get("requested_by") or [],
                         "requested_at": current.get("requested_at")}})
            data["boot"] = started
            data["restart_unpolled"] = entry["id"]
            self.save(project)
            out.append(entry)
        return out

    async def watch_boot(self, *, attempts=60, delay=1.0):
        """At startup: wait until this boot is in the restart ledger (it is
        written just after the daemon starts listening), then announce it."""
        from . import restart_notice, runtime_state

        for _ in range(attempts):
            started = (runtime_state.read_daemon_json() or {}).get("started_at")
            boots = restart_notice.read_ledger().get("boots") or []
            if started and boots and boots[-1].get("started_at") == started:
                return self.announce_boot(boots[-1], boots[-2] if len(boots) > 1 else None)
            await asyncio.sleep(delay)
        log.info("operator: this boot never reached the restart ledger; no restart entry written")
        return []

    async def track(self, project, *, force=False):
        """Follow the sessions each recent card names, and when one of them
        moved by some other path (paused, a gate cleared, committed, merged)
        append an ``update`` entry under that card: it reads in time order in
        the feed and in the card's thread (``parent``). The first reading of
        a card is its baseline and says nothing. Returns the new entries."""
        project = projects.normalize(project)
        loop = asyncio.get_running_loop()
        if not force and loop.time() - self.tracked.get(project, -TRACK_INTERVAL) < TRACK_INTERVAL:
            return []
        self.tracked[project] = loop.time()
        data = self.state(project)
        cards = [e for e in data["feed"] if e.get("kind") in ("post", "ask") and e.get("refs")
                 and not e.get("parent")][-TRACK_CARDS:]
        watch = data.get("watch") or {}
        if not cards:
            if watch:
                data["watch"] = {}
                self.save(project)
            return []
        members = {s.sdef.name: s for s in self.members(project)}
        named = {r for card in cards for r in card["refs"]}
        category = {n: session_category(members[n]) if n in members else None for n in named}
        running = [members[n] for n in named if category[n] == CATEGORY_RUNNING]
        gates, gates_ok = await self.gates_read(running)
        if not gates_ok:
            gates = None
        work, _ = await self.work_read([members[n] for n in named if n in members])
        waiting = {g.get("session"): g.get("step_id") for g in gates or []}
        readings = {}
        for n in named:
            reading = watch_of(category[n], waiting.get(n), work.get(n) if work else None)
            if gates is None:
                reading.pop("gate", None)
            readings[n] = reading
        added, kept = [], {}
        for card in cards:
            seen = watch.get(card["id"]) or {}
            now_seen = {}
            for n in card["refs"]:
                before = seen.get(n)
                # A field not read this time keeps its last value; a gate is
                # only a running session's, so it goes when the session stops.
                now_seen[n] = {**(before or {}), **readings[n]}
                if readings[n]["category"] != CATEGORY_RUNNING:
                    now_seen[n].pop("gate", None)
                lines = watch_changes(before, readings[n]) if before is not None else []
                if lines:
                    added.append({"kind": "update", "role": "system", "parent": card["id"],
                                  "session": n, "text": "\n".join(lines)})
            kept[card["id"]] = now_seen
        data["watch"] = kept
        entries = [self.append(project, entry) for entry in added]
        if not entries:
            self.save(project)
        return entries

    def view(self, project, work=None, gates=None):
        project = projects.normalize(project)
        data = self.state(project)
        op = self.operator_session(project)
        operator = None
        if op is not None:
            # Harness and model say which bot this is: the same feed reads
            # differently from a small model than from a large one.
            operator = {"name": op.sdef.name, "running": not op.exited,
                        "status": op.info().get("status"),
                        "harness": getattr(op.sdef, "harness", None),
                        "model": getattr(op.sdef, "model", None)}
        elif data.get("session"):
            operator = {"name": data["session"], "running": False, "status": "missing"}
        return {"project": project, "operator": operator, "feed": data["feed"][-200:],
                "pending": self.pending(project), "sessions": self.panel(project, work),
                "gates": list(gates or [])}

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

    def _parent(self, project, reply_to):
        """The card a post continues (``reply_to``), which must be one of the
        bot's own posts or asks in this project's feed; a reply to a reply
        goes to that reply's card, so a thread is one level deep."""
        if reply_to is None:
            return None
        if not isinstance(reply_to, str) or not reply_to:
            raise ValueError("reply_to must be a feed entry id")
        for entry in self.state(project)["feed"]:
            if entry["id"] == reply_to:
                if entry.get("kind") not in ("post", "ask", "update"):
                    raise ValueError("reply_to must name one of the operator's posts or asks")
                return entry.get("parent") or entry["id"]
        raise ValueError("reply_to names no entry of this feed")

    def post(self, name, body):
        project = self.bound(name)
        if not isinstance(body, dict):
            raise ValueError("object required")
        level = body.get("level", "info")
        if level not in LEVELS:
            raise ValueError("level must be one of " + ", ".join(LEVELS))
        payload = {"kind": "post", "role": "bot", "text": _text(body.get("text"), 4000),
                   "level": level, "refs": self._refs(project, body.get("refs"))}
        parent = self._parent(project, body.get("reply_to"))
        if parent:
            payload["parent"] = parent
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
        parent = self._parent(project, body.get("reply_to"))
        if parent:
            payload["parent"] = parent
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
        category = {n: session_category(s) for n, s in members.items()}
        running = [s for n, s in members.items() if category[n] == CATEGORY_RUNNING]
        # The snapshot is read on the loop, as every Observer reader does; the
        # gates are run-state files, so they are read off it.
        snapshot = self.observer.snapshot()["sessions"] if self.observer else []
        gates, gates_ok = await self.gates_read(running)
        work, work_ok = await self.work_read(running)
        # What this poll could not read in time: it answers without it rather
        # than run into the client's timeout, and says so.
        degraded = [what for what, ok in (("gates", gates_ok), ("board", work_ok)) if not ok]
        cutoff = session_events.timestamp(since) if since else None
        events, attention = [], []
        # A paused session is stopped on purpose and a killed or archived one
        # is gone: their questions and blocked state wait on nobody now, so
        # only running sessions raise attention. Paused ones are named apart,
        # so the operator can say they exist without calling them urgent.
        paused = [{"session": n, "paused_at": getattr(s, "paused_at", None)}
                  for n, s in members.items() if category[n] == CATEGORY_PAUSED]
        for row in snapshot:
            if row["name"] not in members:
                continue
            live = category[row["name"]] == CATEGORY_RUNNING
            for event in row.get("events", []):
                stamp = session_events.timestamp(event.get("at"))
                if cutoff is not None and stamp <= cutoff:
                    continue
                events.append({"session": row["name"], "at": event.get("at"),
                               "kind": event.get("kind"), "origin": event.get("origin"),
                               "text": str(event.get("text", ""))[:400],
                               "needs_action": bool(event.get("needs_action")) and live,
                               "category": category[row["name"]]})
                if live and event.get("question") and not event.get("answer"):
                    attention.append({"session": row["name"], "kind": "observer_ask", "id": event.get("id"),
                                      "text": str(event.get("text", ""))[:400],
                                      "choices": event.get("choices") or []})
            if live and row.get("state") == "blocked":
                attention.append({"session": row["name"], "kind": "blocked", "text": row.get("summary")})
        # Earlier open questions are still open, whatever the cursor says.
        for row in snapshot:
            if row["name"] not in members or cutoff is None or category[row["name"]] != CATEGORY_RUNNING:
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
        # Progress (commit, tests, landing request, merge) is compared with
        # what the last poll saw rather than cut by the cursor: it is state,
        # not an event stream, and a change is reported once.
        data = self.state(project)
        seen = data.setdefault("progress", {})
        progress = []
        for session in running:
            n = session.sdef.name
            now_progress = progress_of(work.get(n))
            if not work:
                continue  # nothing read this time is no evidence of change
            lines = progress_changes(seen.get(n), now_progress)
            if lines:
                progress.append({"session": n, "changes": lines, **now_progress})
            seen[n] = now_progress
        if work:
            self.save(project)
        await self.track(project)
        restart = None
        if data.get("restart_unpolled"):
            restart = next((e for e in data["feed"] if e["id"] == data["restart_unpolled"]), None)
            data.pop("restart_unpolled")
            self.save(project)
        sessions = [{"name": n, "running": not s.exited, "category": category[n],
                     "status": s.info().get("status"), **progress_of(work.get(n))}
                    for n, s in members.items()]
        return {"project": project, "cursor": cursor, "more": more, "events": events,
                "attention": attention, "paused": paused, "progress": progress,
                "degraded": degraded, "restart": restart,
                "sessions": sessions, "unread_user_input": len(self.unread(project))}


def install(app, *, create=None, gates=None, work=None):
    """Mount the operator routes. ``create`` starts a session from a
    session-create body (the same path ``POST /api/sessions`` takes),
    ``gates`` lists the cflow gates waiting on a set of sessions and ``work``
    reads their status checks and board issues; all three are passed in by
    the API module, which owns those mechanisms."""
    ops = Operators(paths.daemon_dir(), app["manager"], app.get("observer"), gates, work)
    app["operators"] = ops

    async def watch_boot(app):
        ops.boot_task = asyncio.create_task(ops.watch_boot())

    async def stop_boot_watch(app):
        task = getattr(ops, "boot_task", None)
        if task is not None:
            task.cancel()

    app.on_startup.append(watch_boot)
    app.on_cleanup.append(stop_boot_watch)

    def fail(exc):
        return web.json_response({"error": str(exc)}, status=400)

    async def view(request):
        project = projects.normalize(request.query.get("project") or projects.DEFAULT)
        shown = [s for s in ops.members(project) if session_category(s) in (CATEGORY_RUNNING, CATEGORY_PAUSED)]
        await ops.track(project)
        gates, _ = await ops.gates_read([s for s in shown if session_category(s) == CATEGORY_RUNNING])
        return web.json_response(ops.view(project, await ops.work_of(shown), gates))

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
        # The model and effort are the new-session form's own fields, checked
        # by the same create path against the profile's harness; an empty
        # answer is the harness default and is not sent at all.
        for key in ("model", "effort"):
            value = str(body.get(key) or "").strip()
            if value:
                spec[key] = value
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
