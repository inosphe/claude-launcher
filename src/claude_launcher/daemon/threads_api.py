"""Session thread routes (claunch-doj98): sub-topic conversations between the
user and a session, held in the session's threads pane.

  GET  /api/threads?session=<name>[&closed=0]    one session's threads
  GET  /api/threads/rail                         per session: open, agent_seq
  POST /api/threads                              open    {session | to, title, text?}
  GET  /api/threads/{id}[?after=&limit=&reader=] the thread and its messages
  POST /api/threads/{id}/messages                post    {session?, text}
  POST /api/threads/{id}/close                   close   {session?}
  POST /api/threads/{id}/reopen                  reopen  {session?}
  POST /api/threads/{id}/title                   rename  {session?, title}

Who is calling follows sim_api: a body naming ``session`` is that session
(the thread_* MCP tools send ``$CLAUNCH_SESSION``), a body without one is
the user (the dashboard), who names the session a new thread belongs to with
``to``. A read with ``reader=<session>`` moves that session's read position
when it owns the thread.

A message the user writes is typed into the owning session as a
``[claunch thread <id>]`` block carrying the thread's title and the message,
so the agent can answer into the right thread without a read; a held session
is woken for it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from ..threads import store as thread_store
from . import paths

log = logging.getLogger(__name__)

PREFIX = "[claunch thread"


def _quote(text: str) -> str:
    return "\n".join("  > " + line for line in (text or "").splitlines()) or "  > (empty)"


def user_wrote_text(thread: dict, message: dict, *, opened: bool = False, reopened: bool = False) -> str:
    tid = thread["id"]
    if opened:
        head = "The user opened a new thread with you:"
    elif reopened:
        head = "The user reopened this thread and wrote:"
    else:
        head = "The user wrote in this thread:"
    return "\n".join([
        f"{PREFIX} {tid} \"{thread['title']}\"] {head}",
        _quote(message["body"]),
        f"Answer in the thread, not the terminal: thread_reply(thread_id=\"{tid}\", text=...). "
        f"thread_read(thread_id=\"{tid}\") has the whole conversation; thread_list shows all your threads. "
        "Keep this sub-topic's discussion in this thread.",
    ])


def install(app, store: Optional[thread_store.ThreadStore] = None):
    manager = app["manager"]
    threads = store or thread_store.ThreadStore(paths.threads_db())
    app["threads"] = threads

    def deliver(name: str, text: str) -> bool:
        """Type ``text`` into session ``name``; a held session is woken for
        it. False when there is nobody to tell (exited, unknown)."""
        try:
            if manager.get(name).queue_delivery(text):
                return True
        except Exception:  # noqa: BLE001 -- gone or not deliverable
            pass
        try:
            return bool(manager.wake_soon(name, by="thread", deliver=text))
        except Exception:  # noqa: BLE001
            log.info("threads: could not deliver to %s", name)
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
        return s if isinstance(s, str) and s else thread_store.USER

    async def call(fn, *args, **kw):
        try:
            return await asyncio.to_thread(fn, *args, **kw), None
        except thread_store.ThreadError as exc:
            return None, web.json_response({"error": str(exc)}, status=exc.status)

    async def h_list(request):
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        closed = request.query.get("closed", "1") not in ("0", "false", "no")
        res, err = await call(threads.list, session, closed=closed)
        return err or web.json_response(res)

    async def h_rail(_request):
        res, err = await call(threads.rail)
        return err or web.json_response({"sessions": res})

    async def h_open(request):
        body = await body_of(request)
        actor = actor_of(body)
        target = actor if actor != thread_store.USER else str(body.get("to") or "")
        if not target:
            return web.json_response({"error": "to is required: the session the thread is with"}, status=400)
        if not known(target):
            return web.json_response({"error": f"no session {target!r} on this daemon"}, status=404)
        res, err = await call(threads.open, actor, target, body.get("title"), body.get("text"))
        if err:
            return err
        if actor == thread_store.USER and res["message"]:
            res["delivered"] = deliver(target, user_wrote_text(res["thread"], res["message"], opened=True))
        return web.json_response(res)

    async def h_read(request):
        q = request.query
        res, err = await call(threads.read, request.match_info["id"], after=q.get("after") or 0,
                              limit=q.get("limit") or thread_store.PAGE,
                              reader=q.get("reader") or thread_store.USER)
        return err or web.json_response(res)

    async def h_post(request):
        body = await body_of(request)
        actor = actor_of(body)
        res, err = await call(threads.post, actor, request.match_info["id"], body.get("text"))
        if err:
            return err
        if actor == thread_store.USER:
            t = res["thread"]
            res["delivered"] = deliver(t["session"], user_wrote_text(t, res["message"], reopened=res["reopened"]))
        return web.json_response(res)

    def status_route(status: str):
        async def handler(request):
            body = await body_of(request)
            res, err = await call(threads.set_status, actor_of(body), request.match_info["id"], status)
            return err or web.json_response(res)
        return handler

    async def h_title(request):
        body = await body_of(request)
        res, err = await call(threads.rename, actor_of(body), request.match_info["id"], body.get("title"))
        return err or web.json_response(res)

    r = app.router
    r.add_get("/api/threads", h_list)
    r.add_get("/api/threads/rail", h_rail)
    r.add_post("/api/threads", h_open)
    r.add_get("/api/threads/{id}", h_read)
    r.add_post("/api/threads/{id}/messages", h_post)
    r.add_post("/api/threads/{id}/close", status_route("closed"))
    r.add_post("/api/threads/{id}/reopen", status_route("open"))
    r.add_post("/api/threads/{id}/title", h_title)
    return threads
