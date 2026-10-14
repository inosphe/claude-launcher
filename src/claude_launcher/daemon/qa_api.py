"""Session QA routes (claunch-l8vyi): questions a session puts to the user,
answered in the session's QA pane.

  GET  /api/qa?session=<name>[&closed=0]   one session's questions
  GET  /api/qa/rail                        per session: unresolved, agent_seq
  POST /api/qa                             ask      {session, title, body?, options?, multi?, thread?}
  POST /api/qa/{id}                        edit     {session, revision, title?, body?, options?, multi?}
  POST /api/qa/{id}/close                  close    {session, revision, note?}
  POST /api/qa/{id}/delete                 delete   {session, revision}
  POST /api/qa/{id}/answer                 answer   {revision, text?, choices?}   (the user)
  POST /api/qa/{id}/recheck                recheck  {revision, note?}             (the user)
  POST /api/qa/{id}/deliver                deliver the waiting notification now     (the user)
  POST /api/qa/nudge                       nudge    {to}                          (the user)
  POST /api/qa/nudge/{id}/nothing          nothing  {session, note?}   id "latest" = the waiting one

Who is calling follows sim_api: a body naming ``session`` is that session
(the qa_* MCP tools send ``$CLAUNCH_SESSION``), a body without one is the
user (the dashboard). The store refuses the user's hand on the question and
a session's hand on the answer.

An answer saved by the user is typed into the owning session as a
``[claunch qa]`` block carrying the question, the answer and -- for a
rewrite -- the answer it replaced, so the agent acts on it without a read.

Delivery is tracked (claunch-564kn). ``queue_delivery`` accepts a block and
types it once the terminal is free -- which can be minutes later while an
unsent line sits in the session's composer (qa-d12e3c41: five saves of one
answer, all typed together 80 seconds after the first). So acceptance is not
reported as delivery: each question's latest notification is kept here with
its state (``pending`` / ``held`` / ``delivered`` / ``failed`` /
``replaced``) and the list route carries it as ``delivery``. A newer
notification for the same question replaces one still waiting (its factory
comes back empty, so only the latest answer is typed), and the user can
press "지금 전달" to force it past the composer the way the dashboard's other
"deliver now" buttons do (:meth:`Session.deliver` with ``force``). The record
lives as long as the daemon -- after a restart a question shows no state.

A closed question still takes a rewritten answer, and the user may ask for
a direction check on an answered or closed one (``recheck``): both reopen it
as ``answered`` and type a block asking the session to compare its current
work with the answer and settle it with qa_close and a note.

A nudge ("질문 요청" in the pane, claunch-l8vyi.1) is typed in the same way:
it asks the session to file whatever it needs from the user with qa_ask,
or to reply qa_nothing when it has nothing to ask. It is sent whether or
not the session has questions; the pane shows how it was settled.

Thread links (claunch-78dpm, daemon/talk_links.py). A question may be filed
out of one of the session's threads (``thread`` on ask); the thread must
exist and be the same session's. Every question in a list carries
``links``: the thread it came from and the threads opened about it. The
answer block names those threads, so the agent can carry the answer back
into the conversation that led to it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from aiohttp import web

from ..qa import store as qa_store
from . import paths, talk_links

log = logging.getLogger(__name__)

PREFIX = "[claunch qa]"


def _quote(text: str) -> str:
    return "\n".join("  > " + line for line in (text or "").splitlines()) or "  > (empty)"


def _answer_lines(answer: Optional[dict]) -> list[str]:
    if not answer:
        return ["  (none)"]
    lines = []
    if answer.get("choices"):
        lines.append("  choices: " + "; ".join(answer["choices"]))
    if answer.get("text"):
        lines.append("  text:")
        lines.append(_quote(answer["text"]))
    return lines


def _thread_lines(q: dict) -> list[str]:
    live = [t for t in q.get("links") or [] if t["status"] != "deleted"]
    if not live:
        return []
    named = ", ".join(f"{t['id']} \"{t['title']}\"" + (" (filed from)" if t["rel"] == "source" else "")
                      for t in live)
    return [f"Threads on this question: {named}. Where the answer settles what was discussed there, "
            "say so in the thread (thread_reply)."]


def answered_text(q: dict, previous: Optional[dict], *, reopened: bool = False) -> str:
    verb = "changed their answer to" if previous else "answered"
    lines = [
        f"{PREFIX} The user {verb} your question {q['id']} (revision {q['revision']}):",
        _quote(q["title"]),
        "answer:",
        *_answer_lines(q["answer"]),
    ]
    if previous:
        lines += ["previous answer (replaced):", *_answer_lines(previous)]
    if reopened:
        lines.append(
            "You had closed this question; the new answer reopens it. Check whether the work you did "
            "on the earlier answer still holds, and change course where it does not."
        )
    lines += _thread_lines(q)
    lines.append(
        "Act on it. qa_list shows every question of yours; when this one needs nothing more, "
        f"qa_close it (revision {q['revision']}). Editing it marks the answer stale and asks the user again."
    )
    return "\n".join(lines)


def recheck_text(q: dict, note: str, *, reopened: bool = False) -> str:
    lines = [
        f"{PREFIX} The user asks you to check your current work against your question {q['id']} "
        f"(revision {q['revision']}) -- a direction check:",
        _quote(q["title"]),
        "their answer:",
        *_answer_lines(q["answer"]),
    ]
    if note:
        lines += ["what they want checked:", _quote(note)]
    if reopened:
        lines.append("You had closed this question; the check reopens it.")
    lines.append(
        "Compare what you are doing now (plan, code, branch, decisions since the answer) with that answer. "
        "Where you have drifted from it, change course. Then qa_close it "
        f"(revision {q['revision']}) with a note saying whether you were on track and what you changed -- "
        "the note is the user's reply in the QA pane. If the answer itself no longer fits the work, "
        "qa_update the question to ask again instead."
    )
    return "\n".join(lines)


def nudge_text(n: dict) -> str:
    nid = n["id"]
    return "\n".join([
        f"{PREFIX} The user asks whether you have questions for them (nudge {nid}).",
        "Look at your current work: decisions you are waiting on, assumptions you made without the user's "
        "confirmation, ambiguities in the task, blockers. File each as its own question with qa_ask "
        "(options where they fit) -- not in the terminal. Questions already in your pane count; "
        "qa_update one that has changed.",
        f"If you have nothing to ask, call qa_nothing (nudge \"{nid}\") with a one-line note on why.",
        "Either reply settles this nudge in the user's QA pane. Reply before going on with other work.",
    ])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Outbox:
    """The latest notification per question (or nudge) and what became of
    it. In memory: a daemon restart drops both the queue and this record."""

    KEEP = 1000   # settled records kept for the pane, oldest dropped first

    def __init__(self, manager):
        self.manager = manager
        self.latest: dict[str, dict] = {}
        self._seq = 0

    @staticmethod
    def _settle(rec: dict, was: str, state: str) -> None:
        # Only the path that owns the record settles it: a queued copy
        # finishing (typing nothing) while "지금 전달" types the text must
        # not write "failed" over the forced outcome.
        if rec["state"] == was:
            rec["state"] = state
            rec["settled_at"] = _now()

    def _restoring(self, session: str) -> bool:
        try:
            return bool(self.manager.message_restore_pending(session))
        except Exception:  # noqa: BLE001 -- a manager without the notion
            return False

    def view(self, key: str) -> Optional[dict]:
        rec = self.latest.get(key)
        if rec is None:
            return None
        out = {k: rec[k] for k in ("kind", "state", "queued_at", "settled_at", "forced")}
        if rec["state"] == "pending":
            # Why it waits, when the session can say: a human's unsent line
            # holds every automated message until it is sent or cleared.
            try:
                draft = self.manager.get(rec["session"]).draft_open
                out["draft"] = bool(draft())
            except Exception:  # noqa: BLE001 -- gone, or a double without it
                out["draft"] = False
        return out

    def send(self, key: str, session: str, kind: str, make: Callable[[], str]) -> dict:
        """Queue ``make()`` for ``session`` as ``key``'s latest notification;
        one still waiting for ``key`` is replaced (typed never)."""
        old = self.latest.get(key)
        if old is not None and old["state"] in ("pending", "held"):
            old["state"] = "replaced"
            old["settled_at"] = _now()
        self._seq += 1
        rec = {"n": self._seq, "kind": kind, "session": session, "make": make, "state": "pending",
               "queued_at": _now(), "settled_at": None, "forced": False}
        self.latest[key] = rec
        while len(self.latest) > self.KEEP:
            # Oldest first (insertion order); a waiting one is never dropped.
            stale = next((k for k, r in self.latest.items() if r["state"] not in ("pending", "typing", "forcing")), None)
            if stale is None:
                break
            del self.latest[stale]

        queued = False

        def text() -> str:
            # Inside queue_delivery this is the preview (or the text a held
            # session's wake queue keeps). Called again when the terminal is
            # free: a record replaced (or forced through) meanwhile types
            # nothing, and one about to be typed is marked so "지금 전달"
            # leaves it alone rather than typing it a second time.
            if not queued:
                return make()
            if rec["state"] != "pending":
                return ""
            rec["state"] = "typing"
            return make()

        def done(ok: bool) -> None:
            state = "delivered" if ok else "failed"
            self._settle(rec, "typing", state)
            self._settle(rec, "pending", state)

        try:
            s = self.manager.get(session)
            # A session being woken has no held_at yet but its messages
            # still go to the wake queue, which reports no outcome.
            held = bool(getattr(s, "held_at", None)) or self._restoring(session)
            accepted = s.queue_delivery(text, on_done=done)
        except Exception:  # noqa: BLE001 -- gone or not deliverable
            held, accepted = False, False
        queued = True
        if accepted and held:
            rec["state"] = "held"   # typed when the session is woken
        elif not accepted:
            try:
                woke = bool(self.manager.wake_soon(session, by="qa", deliver=make()))
            except Exception:  # noqa: BLE001
                woke = False
            if woke:
                rec["state"] = "held"
            else:
                log.info("qa: could not deliver to %s", session)
                self._settle(rec, "pending", "failed")
        return rec

    async def force(self, key: str) -> Optional[dict]:
        """Type ``key``'s waiting notification now, past an unsent line in
        the composer (that line is submitted first). None when nothing
        waits."""
        rec = self.latest.get(key)
        if rec is None or rec["state"] != "pending":
            return None
        rec["state"] = "forcing"    # the queued copy now types nothing
        rec["forced"] = True
        try:
            ok = bool(await self.manager.get(rec["session"]).deliver(rec["make"](), force=True))
        except Exception:  # noqa: BLE001 -- gone
            ok = False
        self._settle(rec, "forcing", "delivered" if ok else "failed")
        return rec


def install(app, store: Optional[qa_store.QaStore] = None):
    manager = app["manager"]
    qa = store or qa_store.QaStore(paths.qa_db())
    app["qa"] = qa
    outbox = Outbox(manager)
    app["qa_outbox"] = outbox

    def notify(key: str, session: str, kind: str, make: Callable[[], str]) -> dict:
        outbox.send(key, session, kind, make)
        return outbox.view(key)

    def link(questions: list[dict], session: str) -> None:
        talk_links.attach_to_questions(questions, app.get("threads"), session)

    def known(name: str) -> bool:
        try:
            manager.get(name)
            return True
        except Exception:  # noqa: BLE001 -- no such session
            return False

    def thread_of(tid: str) -> Optional[dict]:
        threads = app.get("threads")
        if threads is None:
            return None
        try:
            return threads.get(tid)
        except Exception:  # noqa: BLE001 -- no such thread
            return None

    async def body_of(request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}',
                                     content_type="application/json")
        return body

    def actor_of(body: dict) -> str:
        s = body.get("session")
        return s if isinstance(s, str) and s else qa_store.USER

    async def call(fn, *args, **kw):
        try:
            return await asyncio.to_thread(fn, *args, **kw), None
        except qa_store.QaError as exc:
            doc = {"error": str(exc)}
            if exc.current is not None:
                doc["current"] = exc.current
            return None, web.json_response(doc, status=exc.status)

    async def h_list(request):
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        closed = request.query.get("closed", "1") not in ("0", "false", "no")
        res, err = await call(qa.list, session, closed=closed)
        if err:
            return err
        for q in res["questions"]:
            q["delivery"] = outbox.view(q["id"])
        if res.get("nudge"):
            res["nudge"]["delivery"] = outbox.view(res["nudge"]["id"])
        await asyncio.to_thread(link, res["questions"], session)
        return web.json_response(res)

    async def h_rail(_request):
        res, err = await call(qa.rail)
        return err or web.json_response({"sessions": res})

    async def h_ask(request):
        body = await body_of(request)
        actor = actor_of(body)
        if actor != qa_store.USER and not known(actor):
            return web.json_response({"error": f"no session {actor!r} on this daemon"}, status=404)
        tid = body.get("thread")
        if tid and actor != qa_store.USER:
            t = await asyncio.to_thread(thread_of, str(tid))
            if t is None:
                return web.json_response({"error": f"no thread {tid!r}"}, status=404)
            if t["session"] != actor:
                return web.json_response(
                    {"error": f"thread {tid} belongs to session {t['session']}, not {actor}"}, status=400)
        res, err = await call(qa.ask, actor, title=body.get("title"), body=body.get("body"),
                              options=body.get("options"), multi=body.get("multi", False), thread=tid)
        if err:
            return err
        await asyncio.to_thread(link, [res], res["session"])
        return web.json_response({"question": res})

    async def h_update(request):
        body = await body_of(request)
        res, err = await call(qa.update, actor_of(body), request.match_info["id"], body.get("revision"),
                              title=body.get("title"), body=body.get("body"),
                              options=body.get("options"), multi=body.get("multi"))
        return err or web.json_response({"question": res})

    async def h_close(request):
        body = await body_of(request)
        res, err = await call(qa.close, actor_of(body), request.match_info["id"], body.get("revision"),
                              note=body.get("note"))
        return err or web.json_response({"question": res})

    async def h_delete(request):
        body = await body_of(request)
        res, err = await call(qa.delete, actor_of(body), request.match_info["id"], body.get("revision"))
        return err or web.json_response(res)

    async def h_answer(request):
        body = await body_of(request)
        res, err = await call(qa.answer, actor_of(body), request.match_info["id"], body.get("revision"),
                              text=body.get("text"), choices=body.get("choices"))
        if err:
            return err
        q = res["question"]
        await asyncio.to_thread(link, [q], q["session"])
        text = answered_text(q, res["previous"], reopened=res["reopened"])
        res["delivery"] = notify(q["id"], q["session"], "answer", lambda: text)
        # Accepted for delivery (queued, or queued for a held session's
        # wake) -- ``delivery.state`` says whether it has been typed yet.
        res["delivered"] = res["delivery"]["state"] != "failed"
        return web.json_response(res)

    async def h_recheck(request):
        body = await body_of(request)
        res, err = await call(qa.recheck, actor_of(body), request.match_info["id"], body.get("revision"),
                              note=body.get("note"))
        if err:
            return err
        q = res["question"]
        text = recheck_text(q, res["check"].get("note", ""), reopened=res["reopened"])
        res["delivery"] = notify(q["id"], q["session"], "recheck", lambda: text)
        return web.json_response(res)

    async def h_deliver(request):
        qid = request.match_info["id"]
        rec = await outbox.force(qid)
        if rec is None:
            return web.json_response({"error": f"no notification for {qid} is waiting", "delivery": outbox.view(qid)},
                                     status=409)
        return web.json_response({"delivery": outbox.view(qid)})

    async def h_nudge(request):
        body = await body_of(request)
        to = body.get("to")
        if not isinstance(to, str) or not to:
            return web.json_response({"error": "to (the session to nudge) is required"}, status=400)
        if not known(to):
            return web.json_response({"error": f"no session {to!r} on this daemon"}, status=404)
        res, err = await call(qa.nudge, actor_of(body), to)
        if err:
            return err
        text = nudge_text(res)
        delivery = notify(res["id"], to, "nudge", lambda: text)
        res["delivery"] = delivery
        return web.json_response({"nudge": res, "delivered": delivery["state"] != "failed"})

    async def h_nothing(request):
        body = await body_of(request)
        nid = request.match_info["id"]
        res, err = await call(qa.nothing, actor_of(body), None if nid == "latest" else nid,
                              note=body.get("note"))
        return err or web.json_response({"nudge": res})

    # The nudge routes go first: "/api/qa/{id}" would take "nudge" as an id.
    app.router.add_post("/api/qa/nudge", h_nudge)
    app.router.add_post("/api/qa/nudge/{id}/nothing", h_nothing)
    app.router.add_get("/api/qa", h_list)
    app.router.add_get("/api/qa/rail", h_rail)
    app.router.add_post("/api/qa", h_ask)
    app.router.add_post("/api/qa/{id}", h_update)
    app.router.add_post("/api/qa/{id}/close", h_close)
    app.router.add_post("/api/qa/{id}/delete", h_delete)
    app.router.add_post("/api/qa/{id}/answer", h_answer)
    app.router.add_post("/api/qa/{id}/recheck", h_recheck)
    app.router.add_post("/api/qa/{id}/deliver", h_deliver)
    return qa
