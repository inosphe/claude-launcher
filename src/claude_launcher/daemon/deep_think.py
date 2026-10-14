"""deep_think: a session asks itself a question, and a copy of it answers.

The asking session (the *origin*) calls the MCP ``deep_think`` tool with a
question. The daemon quick-forks it (:func:`api.stage_quick_fork` -- the same
copy of the conversation a person's press makes, see :mod:`daemon.handoff`),
puts the question on top of the copy, and returns at once. The copy (the
*fork*, ``<origin>-dt<n>``) explores and reasons with everything the origin
knew at the moment it asked, and hands its answer in with the ``handoff``
tool -- a quick-fork merge -- which types it into the origin as a new block
and ends the fork. What the origin saves is context: the exploration happens
in the fork's window, and only the answer comes back into its own.

So the answer is ASYNCHRONOUS, and every surface says so: the tool's reply,
the tool description, and the answer block itself, which arrives as input in
a later turn.

Around that one move:

* **Notes.** The fork shares the origin's checkout (a fork cannot have its
  own, see :func:`handoff.compose_marker`) and is allowed to edit. It tells
  the origin what it is changing with ``deep_think {op: notify}``; the daemon
  types the note into the origin.
* **The message log.** Notes and the answer reach the origin by that direct
  path, and are also written into the log of every mesh the origin belongs
  to (:meth:`MeshManager.record_only`), delivered to nobody, so the room's
  record shows them.
* **The record.** Question and answer are kept as a search record of the
  fork (:mod:`daemon.search_records`), so they outlive the session and are
  found by ``search``.
* **Limits.** At most ``daemon.deep_think_max`` forks per origin run at once.
  Each has a deadline (``daemon.deep_think_timeout`` or the call's own); past
  it the origin is asked to extend or cancel, and if it does neither within
  ``daemon.deep_think_grace`` the fork is asked for what it has, and after
  one more grace it is ended. That ladder lives on the fork's definition
  (``SessionDef.deep_think``) so a daemon restart picks it back up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from aiohttp import web

from .. import store
from . import handoff as handoff_mod
from . import search_records

log = logging.getLogger("claunch.deep_think")

TAG = "dt"
RECORD_ID = "deep-think"

RUNNING = "running"
#: The origin has been asked to extend or cancel.
ASKED = "asked"
#: The fork has been asked to hand in what it has.
WRAPPING = "wrapping"

DEFAULT_MAX = 3
DEFAULT_TIMEOUT = 1800.0
DEFAULT_GRACE = 300.0
#: The longest a single question may be given, by the call or an extension.
MAX_TIMEOUT = 24 * 3600.0
#: How often the deadline sweep looks.
SWEEP_INTERVAL = 15.0


class DeepThinkError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ---- time and settings ------------------------------------------------------ #

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(at: datetime) -> str:
    return at.isoformat()


def _parse(text) -> Optional[datetime]:
    try:
        at = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _setting(key: str, default: float) -> float:
    try:
        value = float(store.daemon_config().get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _timeout(value) -> float:
    """Seconds a question may run: the call's own, or the setting."""
    if value in (None, ""):
        return _setting("deep_think_timeout", DEFAULT_TIMEOUT)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise DeepThinkError(400, f"'timeout' must be a number of seconds, not {value!r}") from None
    if not 0 < seconds <= MAX_TIMEOUT:
        raise DeepThinkError(
            400, f"'timeout' must be between 1 and {int(MAX_TIMEOUT)} seconds"
        )
    return seconds


# ---- the blocks ------------------------------------------------------------- #

def _head(question: str, limit: int = 200) -> str:
    line = " ".join(str(question or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def compose_question(*, origin: str, fork: str, question: str, deadline: str) -> str:
    """The copy's task, under the quick-fork marker: what it is for and how
    it ends. The copy holds the origin's whole conversation, including the
    origin's own task, run and mesh traffic, so the first thing it must be
    told is that none of that is its to continue."""
    return "\n".join([
        "---",
        "# claunch deep-think: QUESTION (machine-generated, not typed by the user)",
        f"asked_by: {origin}",
        f"id: {fork}",
        f"deadline: {deadline}",
        f"role: {origin} asked ITSELF the question below with the deep_think "
        f"tool, and this session is the copy that answers it. Answer it and "
        f"nothing else: {origin}'s task, its cflow run, its board issues and "
        f"its mesh conversations stay {origin}'s -- it is still running them. "
        "Explore the code, read, run read-only commands, reason as deeply as "
        "the question needs.",
        f"edits: you may change files, but you stand in {origin}'s checkout "
        f"and it may be writing too. Before you write to a file, and again "
        f"after, tell {origin} with the MCP 'deep_think' tool "
        "{op: \"notify\", text: \"...\"}: which files, and why. Each note is "
        f"typed into {origin}'s terminal, so keep notes short.",
        "answer: when you have it, hand it in with the MCP 'handoff' tool "
        f"{{text: ...}} and no 'to'. The daemon types it into {origin} as the "
        "answer to this question and ends this session. There is no length "
        "limit: write what the asker needs, with the evidence it rests on "
        "(files, lines, commands, what you could not check). Do not hand in "
        "before you have answered -- the answer is the only thing of this "
        "session that survives.",
        f"time: past the deadline {origin} is asked whether to extend or cancel; "
        "if it says neither you are asked for what you have so far.",
        "---",
        "",
        "Question:",
        str(question).strip(),
    ])


def compose_answer(*, fork: str, record: dict, text: str) -> str:
    return "\n".join([
        "---",
        f"# claunch deep-think: ANSWER from {fork} (machine-generated, not typed by the user)",
        f"id: {fork}",
        f"asked_at: {record.get('asked_at', '?')}",
        f"question: {_head(record.get('question', ''))}",
        "note: this is the answer to a question you asked yourself with the "
        f"deep_think tool. {fork} worked in your checkout and has been ended; "
        "files it changed are named below and in the notes it sent.",
        "---",
    ]) + "\n" + text.strip()


def compose_note(*, fork: str, record: dict, text: str) -> str:
    return "\n".join([
        "---",
        f"# claunch deep-think: NOTE from {fork} (machine-generated, not typed by the user)",
        f"id: {fork}",
        f"question: {_head(record.get('question', ''), 120)}",
        f"note: {fork} is still working on your question; this is a note it "
        "sent about your shared checkout. No reply is needed, and the answer "
        "is still to come.",
        "---",
    ]) + "\n" + text.strip()


def compose_overdue(*, fork: str, record: dict, grace: float) -> str:
    return "\n".join([
        "---",
        f"# claunch deep-think: OVERDUE {fork} (machine-generated, not typed by the user)",
        f"id: {fork}",
        f"asked_at: {record.get('asked_at', '?')}",
        f"deadline: {record.get('deadline', '?')}",
        f"question: {_head(record.get('question', ''))}",
        f"decide: {fork} has run past its deadline without answering. Give it "
        f"more time with the MCP 'deep_think' tool {{op: \"extend\", id: "
        f"\"{fork}\", minutes: N}}, or end it without an answer with "
        f"{{op: \"cancel\", id: \"{fork}\"}}. If you do neither within "
        f"{int(grace // 60) or 1} min, {fork} is asked to hand in what it has "
        "so far, and is then ended.",
        "---",
    ])


def compose_lost(*, fork: str, record: dict, why: str) -> str:
    return "\n".join([
        "---",
        f"# claunch deep-think: NO ANSWER from {fork} (machine-generated, not typed by the user)",
        f"id: {fork}",
        f"question: {_head(record.get('question', ''))}",
        f"note: {why} No answer is coming for this question.",
        "---",
    ])


# ---- reading the fleet ------------------------------------------------------ #

def record_of(session) -> Optional[dict]:
    sdef = getattr(session, "sdef", None)
    if sdef is None or not sdef.quick_fork_of or not sdef.deep_think:
        return None
    return sdef.deep_think


def running_for(manager, origin: str) -> list:
    """The origin's deep-think forks that are still alive, oldest first."""
    out = [
        s for s in manager.list()
        if not s.exited and s.sdef.quick_fork_of == origin and record_of(s)
    ]
    out.sort(key=lambda s: str(record_of(s).get("asked_at", "")))
    return out


def row(session) -> dict:
    record = record_of(session) or {}
    return {
        "id": session.sdef.name,
        "question": record.get("question", ""),
        "asked_at": record.get("asked_at", ""),
        "deadline": record.get("deadline", ""),
        "state": record.get("state", RUNNING),
        "model": record.get("model", ""),
        "effort": record.get("effort", ""),
        "status": session.status() if hasattr(session, "status") else "",
    }


def _set_record(manager, session, **changes) -> dict:
    record = {**(record_of(session) or {}), **changes}
    session.sdef = replace(session.sdef, deep_think=record)
    manager.persist()
    return record


def _fork_of(manager, origin: str, fork: str):
    from .manager import ManagerError  # local: manager imports the api that imports us

    try:
        session = manager.get(fork)
    except ManagerError:
        raise DeepThinkError(404, f"no deep_think question {fork!r}") from None
    if session.sdef.quick_fork_of != origin or not record_of(session):
        raise DeepThinkError(
            403, f"{fork!r} is not a deep_think question {origin!r} asked"
        )
    if session.exited:
        raise DeepThinkError(400, f"{fork!r} has already ended")
    return session


# ---- the record ------------------------------------------------------------- #

def _remember(fork: str, record: dict, answer: str = "", outcome: str = "") -> None:
    parts = [f"deep_think question asked by {record.get('origin', '?')}:", record.get("question", "")]
    if answer:
        parts += ["", f"answer from {fork}:", answer.strip()]
    if outcome:
        parts += ["", f"outcome: {outcome}"]
    try:
        search_records.remember(fork, [{
            "id": RECORD_ID, "kind": RECORD_ID, "origin": "record",
            "at": _iso(_now()), "text": "\n".join(parts).strip(),
            "source": RECORD_ID, "needs_action": False,
        }])
    except Exception:  # a record that did not land must not undo the work
        log.exception("deep_think: could not record %r", fork)


def _log(app, origin: str, fork: str, kind: str, body: str) -> List[str]:
    mesh = app.get("mesh") if hasattr(app, "get") else None
    if mesh is None:
        return []
    try:
        return mesh.record_only(
            origin, sender=f"deep-think:{fork}", body=f"[{kind}] {body.strip()}",
            ref={"deep_think": {"fork": fork, "kind": kind}},
        )
    except Exception:
        log.exception("deep_think: could not log %s from %r", kind, fork)
        return []


async def _deliver(session, block: str) -> bool:
    timeout = _setting("handoff_deliver_timeout", handoff_mod.DEFAULT_DELIVER_TIMEOUT)
    try:
        return bool(await asyncio.wait_for(session.deliver(block), timeout))
    except asyncio.TimeoutError:
        return False


# ---- the operations --------------------------------------------------------- #

async def ask(request: web.Request, origin: str, body: dict) -> dict:
    from . import api  # local: api imports this module

    manager = request.app["manager"]
    question = str(body.get("question") or "").strip()
    if not question:
        raise DeepThinkError(400, "'question' is required: what you want answered")
    try:
        asker = manager.get(origin)
    except Exception:
        raise DeepThinkError(404, f"no session named {origin!r}") from None
    if record_of(asker):
        raise DeepThinkError(
            400,
            f"{origin!r} is itself a deep_think fork: answer the question you "
            "were given instead of asking another",
        )
    # The request is judged before the room: a bad timeout is the caller's
    # to fix whether or not a slot is free.
    timeout = _timeout(body.get("timeout"))
    cap = int(_setting("deep_think_max", DEFAULT_MAX))
    live = running_for(manager, origin)
    if len(live) >= cap:
        raise DeepThinkError(
            409,
            f"{origin!r} already has {len(live)} deep_think question(s) running "
            f"(daemon.deep_think_max = {cap}): "
            + ", ".join(s.sdef.name for s in live)
            + ". Wait for an answer, or cancel one.",
        )
    asked = _now()
    deadline = _iso(asked + timedelta(seconds=timeout))
    model = str(body.get("model") or "").strip()
    effort = str(body.get("effort") or "").strip()
    record = {
        "origin": origin, "question": question, "asked_at": _iso(asked),
        "deadline": deadline, "timeout": timeout, "state": RUNNING,
        "state_at": _iso(asked),
    }
    if model:
        record["model"] = model
    if effort:
        record["effort"] = effort
    taken = {s.sdef.name for s in manager.list()}
    fork = str(body.get("name") or "").strip() or handoff_mod.fork_name(origin, taken, tag=TAG)
    extra = {k: v for k, v in (("model", model), ("effort", effort)) if v}

    def before_launch(session) -> None:
        session.sdef = replace(session.sdef, deep_think=dict(record))
        manager.persist()

    try:
        out = await api.stage_quick_fork(
            request, origin,
            {"name": fork, "task": compose_question(
                origin=origin, fork=fork, question=question, deadline=deadline,
            )},
            tag=TAG, spawn_extra=extra, before_launch=before_launch,
            # The answer is handed in as a merge the fork makes on its own;
            # there is no operator press to wait for.
            self_merge_allowed=True,
        )
    except api.QuickForkError as exc:
        raise DeepThinkError(exc.status, str(exc)) from None
    _remember(fork, record)
    reply = {
        "id": fork,
        "asked_at": record["asked_at"],
        "deadline": deadline,
        "running": len(live) + 1,
        "max": cap,
        "async": True,
        "note": (
            f"Asked. The answer is NOT in this reply: {fork} is working on it "
            "and will hand it in LATER, typed into your terminal as a fenced "
            f"'claunch deep-think: ANSWER from {fork}' block that starts a new "
            "turn. Do not wait, sleep or poll for it -- carry on with other "
            "work, or end your turn. Notes from it (files it is changing in "
            "your checkout) may arrive before the answer. "
            "deep_think {op: status} lists what is running, {op: cancel} ends one."
        ),
        "shared_checkout": True,
    }
    if out.get("warnings"):
        reply["warnings"] = out["warnings"]
    return reply


def status(manager, origin: str) -> dict:
    return {
        "origin": origin,
        "running": [row(s) for s in running_for(manager, origin)],
        "max": int(_setting("deep_think_max", DEFAULT_MAX)),
    }


async def cancel(app, origin: str, fork: str) -> dict:
    manager = app["manager"]
    session = _fork_of(manager, origin, fork)
    record = dict(record_of(session))
    manager.kill(fork, force=False)
    _remember(fork, record, outcome=f"cancelled by {origin}")
    _log(app, origin, fork, "cancelled", f"{origin} cancelled the question: {_head(record.get('question', ''))}")
    return {"id": fork, "cancelled": True}


def extend(manager, origin: str, fork: str, value) -> dict:
    session = _fork_of(manager, origin, fork)
    seconds = _timeout(value)
    if value in (None, ""):
        seconds = float((record_of(session) or {}).get("timeout") or seconds)
    now = _now()
    record = _set_record(
        manager, session, deadline=_iso(now + timedelta(seconds=seconds)),
        state=RUNNING, state_at=_iso(now),
    )
    return {"id": fork, "deadline": record["deadline"], "state": RUNNING}


async def notify(app, fork: str, text: str) -> dict:
    manager = app["manager"]
    text = str(text or "").strip()
    if not text:
        raise DeepThinkError(400, "'text' is required: the note for the asker")
    try:
        session = manager.get(fork)
    except Exception:
        raise DeepThinkError(404, f"no session named {fork!r}") from None
    record = record_of(session)
    if not record:
        raise DeepThinkError(
            400, f"{fork!r} is not a deep_think fork: notify is how a fork talks to its asker"
        )
    origin = session.sdef.quick_fork_of
    try:
        target = manager.get(origin)
    except Exception:
        raise DeepThinkError(400, f"the asker {origin!r} is gone") from None
    if target.exited:
        raise DeepThinkError(400, f"the asker {origin!r} has exited")
    delivered = await _deliver(target, compose_note(fork=fork, record=record, text=text))
    logged = _log(app, origin, fork, "note", text)
    if not delivered:
        raise DeepThinkError(
            503, f"{origin!r} did not take the note (its keyboard is busy); try again"
        )
    return {"delivered": True, "to": origin, "logged_in": logged}


# ---- the merge, seen from here ---------------------------------------------- #

def _composer(manager, source, settled, text, marker):
    if settled.get("kind") != handoff_mod.MERGE:
        return None
    try:
        record = record_of(manager.get(source))
    except Exception:
        return None
    if not record:
        return None
    return compose_answer(fork=source, record=record, text=text)


def _completed(app):
    def hook(manager, source, settled, text) -> None:
        try:
            record = record_of(manager.get(source))
        except Exception:
            return
        if not record or settled.get("kind") != handoff_mod.MERGE:
            return
        _remember(source, record, answer=text, outcome="answered")
        _log(app, settled["target"], source, "answer",
             f"Q: {_head(record.get('question', ''))}\n\n{text}")
    return hook


# ---- the deadline ladder ---------------------------------------------------- #

async def sweep(app, now: Optional[datetime] = None) -> List[tuple]:
    """One pass over every live deep-think fork: move each that is due one
    rung down the ladder. Returns ``(fork, what)`` for each move, for tests
    and the log."""
    manager = app["manager"]
    now = now or _now()
    grace = _setting("deep_think_grace", DEFAULT_GRACE)
    moved: List[tuple] = []
    busy: set = app["deep_think_busy"]
    for session in list(manager.list()):
        record = record_of(session)
        if not record or session.exited or session.sdef.name in busy:
            continue
        fork = session.sdef.name
        origin_name = session.sdef.quick_fork_of
        try:
            origin = manager.get(origin_name)
        except Exception:
            origin = None
        busy.add(fork)
        try:
            if origin is None or origin.exited:
                manager.kill(fork, force=False)
                _remember(fork, record, outcome=f"ended: the asker {origin_name} is gone")
                moved.append((fork, "orphaned"))
                continue
            state = record.get("state", RUNNING)
            since = _parse(record.get("state_at")) or now
            if state == RUNNING:
                deadline = _parse(record.get("deadline"))
                if deadline is None or now < deadline:
                    continue
                if await _deliver(origin, compose_overdue(fork=fork, record=record, grace=grace)):
                    _set_record(manager, session, state=ASKED, state_at=_iso(now))
                    moved.append((fork, ASKED))
            elif state == ASKED:
                if now < since + timedelta(seconds=grace):
                    continue
                await app["handoff"].request(manager, fork)
                _set_record(manager, session, state=WRAPPING, state_at=_iso(now))
                moved.append((fork, WRAPPING))
            elif state == WRAPPING:
                if now < since + timedelta(seconds=grace):
                    continue
                manager.kill(fork, force=False)
                why = (
                    f"{fork} ran past its deadline, was asked for what it had, "
                    "and did not hand anything in; it has been ended."
                )
                await _deliver(origin, compose_lost(fork=fork, record=record, why=why))
                _remember(fork, record, outcome="timed out without an answer")
                _log(app, origin_name, fork, "timed out", why)
                moved.append((fork, "ended"))
        except Exception:
            log.exception("deep_think: sweep failed on %r", fork)
        finally:
            busy.discard(fork)
    return moved


async def _sweep_forever(app) -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            await sweep(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("deep_think: sweep failed")


# ---- the routes ------------------------------------------------------------- #

def _error(exc: DeepThinkError) -> web.Response:
    return web.json_response({"error": str(exc)}, status=exc.status)


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def h_ask(request: web.Request) -> web.Response:
    try:
        out = await ask(request, request.match_info["name"], await _body(request))
    except DeepThinkError as exc:
        return _error(exc)
    return web.json_response(out, status=201)


async def h_status(request: web.Request) -> web.Response:
    return web.json_response(status(request.app["manager"], request.match_info["name"]))


async def h_cancel(request: web.Request) -> web.Response:
    try:
        out = await cancel(request.app, request.match_info["name"], request.match_info["fork"])
    except DeepThinkError as exc:
        return _error(exc)
    return web.json_response(out)


async def h_extend(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        out = extend(
            request.app["manager"], request.match_info["name"],
            request.match_info["fork"], body.get("timeout"),
        )
    except DeepThinkError as exc:
        return _error(exc)
    return web.json_response(out)


async def h_notify(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        out = await notify(request.app, request.match_info["name"], body.get("text"))
    except DeepThinkError as exc:
        return _error(exc)
    return web.json_response(out)


def install(app) -> None:
    """Routes, the merge hooks, and the deadline sweep. Called by
    :func:`api.build_app` after ``app["handoff"]`` exists."""
    r = app.router
    r.add_post("/api/sessions/{name}/deep-think", h_ask)
    r.add_get("/api/sessions/{name}/deep-think", h_status)
    r.add_post("/api/sessions/{name}/deep-think/notify", h_notify)
    r.add_post("/api/sessions/{name}/deep-think/{fork}/cancel", h_cancel)
    r.add_post("/api/sessions/{name}/deep-think/{fork}/extend", h_extend)
    #: Forks a sweep is moving right now: a delivery can wait on a busy
    #: keyboard for minutes, and the next pass must not start the same rung.
    app["deep_think_busy"] = set()
    handoffs = app["handoff"]
    handoffs.composers.append(_composer)
    handoffs.completed_hooks.append(_completed(app))

    async def start(app_) -> None:
        app_["deep_think_sweep"] = asyncio.ensure_future(_sweep_forever(app_))

    async def stop(app_) -> None:
        task = app_.get("deep_think_sweep")
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    app.on_startup.append(start)
    app.on_shutdown.append(stop)
