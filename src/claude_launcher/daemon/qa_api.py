"""Session QA routes (claunch-l8vyi): questions a session puts to the user,
answered in the session's QA pane.

  GET  /api/qa?session=<name>[&closed=0]   one session's questions
  GET  /api/qa/rail                        per session: unresolved, agent_seq
  POST /api/qa                             ask      {session, title, body?, options?, multi?}
  POST /api/qa/{id}                        edit     {session, revision, title?, body?, options?, multi?}
  POST /api/qa/{id}/close                  close    {session, revision, note?}
  POST /api/qa/{id}/delete                 delete   {session, revision}
  POST /api/qa/{id}/answer                 answer   {revision, text?, choices?}   (the user)

Who is calling follows sim_api: a body naming ``session`` is that session
(the qa_* MCP tools send ``$CLAUNCH_SESSION``), a body without one is the
user (the dashboard). The store refuses the user's hand on the question and
a session's hand on the answer.

An answer saved by the user is typed into the owning session as a
``[claunch qa]`` block carrying the question, the answer and -- for a
rewrite -- the answer it replaced, so the agent acts on it without a read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from ..qa import store as qa_store
from . import paths

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


def answered_text(q: dict, previous: Optional[dict]) -> str:
    verb = "changed their answer to" if previous else "answered"
    lines = [
        f"{PREFIX} The user {verb} your question {q['id']} (revision {q['revision']}):",
        _quote(q["title"]),
        "answer:",
        *_answer_lines(q["answer"]),
    ]
    if previous:
        lines += ["previous answer (replaced):", *_answer_lines(previous)]
    lines.append(
        "Act on it. qa_list shows every question of yours; when this one needs nothing more, "
        f"qa_close it (revision {q['revision']}). Editing it marks the answer stale and asks the user again."
    )
    return "\n".join(lines)


def install(app, store: Optional[qa_store.QaStore] = None):
    manager = app["manager"]
    qa = store or qa_store.QaStore(paths.qa_db())
    app["qa"] = qa

    def deliver(name: str, text: str) -> bool:
        """Type ``text`` into session ``name``; a held session is woken for
        it. False when there is nobody to tell (exited, unknown)."""
        try:
            if manager.get(name).queue_delivery(text):
                return True
        except Exception:  # noqa: BLE001 -- gone or not deliverable
            pass
        try:
            return bool(manager.wake_soon(name, by="qa", deliver=text))
        except Exception:  # noqa: BLE001
            log.info("qa: could not deliver to %s", name)
            return False

    def known(name: str) -> bool:
        try:
            manager.get(name)
            return True
        except Exception:  # noqa: BLE001 -- no such session
            return False

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
        return err or web.json_response(res)

    async def h_rail(_request):
        res, err = await call(qa.rail)
        return err or web.json_response({"sessions": res})

    async def h_ask(request):
        body = await body_of(request)
        actor = actor_of(body)
        if actor != qa_store.USER and not known(actor):
            return web.json_response({"error": f"no session {actor!r} on this daemon"}, status=404)
        res, err = await call(qa.ask, actor, title=body.get("title"), body=body.get("body"),
                              options=body.get("options"), multi=body.get("multi", False))
        return err or web.json_response({"question": res})

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
        res["delivered"] = deliver(q["session"], answered_text(q, res["previous"]))
        return web.json_response(res)

    app.router.add_get("/api/qa", h_list)
    app.router.add_get("/api/qa/rail", h_rail)
    app.router.add_post("/api/qa", h_ask)
    app.router.add_post("/api/qa/{id}", h_update)
    app.router.add_post("/api/qa/{id}/close", h_close)
    app.router.add_post("/api/qa/{id}/delete", h_delete)
    app.router.add_post("/api/qa/{id}/answer", h_answer)
    return qa
