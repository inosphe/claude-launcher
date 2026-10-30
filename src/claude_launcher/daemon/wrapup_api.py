"""Session wrap-up routes (claunch-ar8jn): drafts a session files for the
workspace glossary, category tree and knowledge records, confirmed by the
user in the session's wrap-up pane.

  GET  /api/wrapup?session=<name>[&settled=0]  one session's drafts
  GET  /api/wrapup/rail                        per session: unresolved counts, ended_at, alive
  POST /api/wrapup                             file     {session, kind, payload, reason?}
  POST /api/wrapup/confirm-all                 confirm  {target, kind?}               (the user)
  POST /api/wrapup/{id}                        edit     {session?, revision, payload?, reason?}
  POST /api/wrapup/{id}/discard                discard  {session?, revision}
  POST /api/wrapup/{id}/confirm                confirm  {revision}                    (the user)

Who is calling follows qa_api: a body naming ``session`` is that session
(the wrapup_* MCP tools send ``$CLAUNCH_SESSION``), a body without one is
the user (the dashboard). A session files, edits and withdraws its own
drafts; only the user confirms.

A confirm writes the draft's payload into its store -- glossary.create,
knowledge.create, or the category operation category_assign would have run
(:func:`category_assign.apply_body`) -- in the workspace the session stood
in when it filed the draft. A store that refuses it (a label already taken,
a node that moved) leaves the draft ``failed`` with the store's message;
the user edits it and confirms again.

What the user does to a draft is typed into the owning session as a
``[claunch wrapup]`` block while it runs, so the agent does not file the
same entry again or build on one the user deleted. An ended session gets
nothing typed; instead the manager's exit hook marks it ended
(:meth:`WrapupStore.mark_ended`) and the rail carries that mark, from which
the dashboard raises a card for each ended session that still has drafts
waiting on the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from aiohttp import web

from .. import category, glossary, knowledge, workspaces
from ..wrapup import store as wrapup_store
from . import category_assign, paths
from .qa_api import Outbox

log = logging.getLogger(__name__)

PREFIX = "[claunch wrapup]"


def _payload_line(d: dict) -> str:
    p = d["payload"]
    if d["kind"] == "glossary":
        return f"term \"{p.get('term', '')}\""
    if d["kind"] == "knowledge":
        return f"{p.get('kind', '')} \"{p.get('title', '')}\""
    for op in wrapup_store.CATEGORY_OPS:
        if op in p:
            target = p[op] if op == "node_id" else (p[op].get("label") or "")
            return f"{op} {target}".strip()
    return ""


def settled_text(drafts: list[dict], verb: str) -> str:
    """The block typed into the session for what the user did to its drafts."""
    lines = [f"{PREFIX} The user {verb} {len(drafts)} of your wrap-up draft(s):"]
    for d in drafts:
        tail = ""
        if d["status"] == "failed":
            tail = f" -- its store refused it: {d['error']}"
        elif d["status"] == "confirmed" and d.get("applied"):
            tail = " -> " + json.dumps(d["applied"], ensure_ascii=False)[:300]
        lines.append(f"  {d['id']} [{d['kind']}] {_payload_line(d)}: {d['status']}{tail}")
    if verb == "edited":
        lines.append("Read the current text with wrapup_list before building on it; do not file it again.")
    elif verb == "discarded":
        lines.append("Do not file these entries again unless the user asks for them.")
    return "\n".join(lines)


def _workspace_of(session) -> str:
    found = workspaces.owning(getattr(session.sdef, "cwd", "") or "")
    if found is None:
        raise wrapup_store.WrapupError(
            f"session {session.sdef.name} does not stand in a registered workspace", )
    return found.name


def apply(d: dict) -> dict:
    """Write draft ``d`` into its store. Returns where it went; raises the
    store's error when it refuses."""
    payload = dict(d["payload"])
    if d["kind"] == "glossary":
        ws = glossary.workspace(name=d["workspace"])
        term = glossary.create(ws, payload)
        return {"workspace": ws.name, "id": term["id"], "term": term["term"]}
    if d["kind"] == "knowledge":
        ws = knowledge.workspace(name=d["workspace"])
        rec = knowledge.create(ws, payload, author=d["session"])
        return {"workspace": ws.name, "id": rec["id"], "title": rec["title"]}
    ws = category.workspace(name=d["workspace"])
    items = payload.pop("items", None) or [{"kind": "session", "ref": d["session"]}]
    reason = payload.pop("reason", "") or d.get("reason") or ""
    result = category_assign.apply_body(ws, payload, items, f"user (wrap-up of {d['session']})", reason)
    return {"workspace": ws.name, "items": items, **result}


STORE_ERRORS = (glossary.GlossaryError, knowledge.KnowledgeError, category.CategoryError,
                category_assign.AssignError, wrapup_store.WrapupError)


def install(app, store: Optional[wrapup_store.WrapupStore] = None):
    manager = app["manager"]
    wrap = store or wrapup_store.WrapupStore(paths.wrapup_db())
    app["wrapup"] = wrap
    outbox = Outbox(manager)
    app["wrapup_outbox"] = outbox

    def session_of(name: str):
        try:
            return manager.get(name)
        except Exception:  # noqa: BLE001 -- no such session
            return None

    def alive(name: str) -> bool:
        s = session_of(name)
        return s is not None and not getattr(s, "exited", True)

    def tell(session: str, drafts: list[dict], verb: str) -> None:
        if not drafts or not alive(session):
            return
        text = settled_text(drafts, verb)
        outbox.send(f"{session}:{drafts[0]['id']}:{verb}", session, verb, lambda: text)

    def ended(session) -> None:
        # A pause or a failure is a stop the person means to undo (as the
        # board treats them, beads.Board.session_exited); its drafts are
        # announced when the record is settled for good.
        if getattr(session, "failed_at", None) or getattr(session, "paused_at", None):
            return
        settled(session)

    def settled(session) -> None:
        try:
            wrap.mark_ended(session.sdef.name)
        except Exception:  # noqa: BLE001 -- an exit hook must not raise
            log.exception("wrapup: could not mark %s ended", session.sdef.name)

    manager.exit_hooks.append(ended)
    settle_hooks = getattr(manager, "settle_hooks", None)
    if settle_hooks is not None:
        settle_hooks.append(settled)

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
        return s if isinstance(s, str) and s else wrapup_store.USER

    def dumps(doc) -> str:
        return json.dumps(doc, ensure_ascii=False)

    async def call(fn, *args, **kw):
        try:
            return await asyncio.to_thread(fn, *args, **kw), None
        except wrapup_store.WrapupError as exc:
            doc = {"error": str(exc)}
            if exc.current is not None:
                doc["current"] = exc.current
            return None, web.json_response(doc, status=exc.status, dumps=dumps)

    async def confirm_one(d: dict) -> dict:
        """Claimed draft ``d`` (status applying) written and settled."""
        try:
            applied = await asyncio.to_thread(apply, d)
        except STORE_ERRORS as exc:
            return await asyncio.to_thread(wrap.settle, d["id"], error=str(exc))
        except Exception as exc:  # noqa: BLE001 -- settle it rather than leave it applying
            log.exception("wrapup: applying %s failed", d["id"])
            return await asyncio.to_thread(wrap.settle, d["id"], error=f"{type(exc).__name__}: {exc}")
        return await asyncio.to_thread(wrap.settle, d["id"], applied=applied)

    async def h_list(request):
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        settled_too = request.query.get("settled", "1") not in ("0", "false", "no")
        res, err = await call(wrap.list, session, settled=settled_too)
        if err:
            return err
        res["alive"] = alive(session)
        return web.json_response(res, dumps=dumps)

    async def h_rail(_request):
        res, err = await call(wrap.rail)
        if err:
            return err
        for name, row in res.items():
            row["alive"] = alive(name)
        return web.json_response({"sessions": res}, dumps=dumps)

    async def h_draft(request):
        body = await body_of(request)
        actor = actor_of(body)
        if actor == wrapup_store.USER:
            return web.json_response({"error": "drafts are filed by a session (give session)"}, status=403)
        s = session_of(actor)
        if s is None:
            return web.json_response({"error": f"no session {actor!r} on this daemon"}, status=404)
        try:
            ws = _workspace_of(s)
        except wrapup_store.WrapupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        payload = body.get("payload")
        if body.get("kind") == "category" and isinstance(payload, dict) and not payload.get("items"):
            # What "this session" means in the tree, fixed now: the issue the
            # session holds at filing time, not at the user's confirm.
            payload = {**payload, "items": category_assign.items_for(s)}
        res, err = await call(wrap.draft, actor, kind=body.get("kind"), payload=payload,
                              workspace=ws, reason=body.get("reason"))
        return err or web.json_response({"draft": res}, dumps=dumps)

    async def h_update(request):
        body = await body_of(request)
        actor = actor_of(body)
        res, err = await call(wrap.update, actor, request.match_info["id"], body.get("revision"),
                              payload=body.get("payload"), reason=body.get("reason"))
        if err:
            return err
        if actor == wrapup_store.USER:
            tell(res["session"], [res], "edited")
        return web.json_response({"draft": res}, dumps=dumps)

    async def h_discard(request):
        body = await body_of(request)
        actor = actor_of(body)
        res, err = await call(wrap.discard, actor, request.match_info["id"], body.get("revision"))
        if err:
            return err
        if actor == wrapup_store.USER:
            tell(res["session"], [res], "discarded")
        return web.json_response({"draft": res}, dumps=dumps)

    async def h_confirm(request):
        body = await body_of(request)
        claimed, err = await call(wrap.claim, actor_of(body), request.match_info["id"], body.get("revision"))
        if err:
            return err
        res = await confirm_one(claimed)
        tell(res["session"], [res], "confirmed")
        status = 200 if res["status"] == "confirmed" else 409
        doc = {"draft": res}
        if status != 200:
            doc["error"] = res["error"]
        return web.json_response(doc, status=status, dumps=dumps)

    async def h_confirm_all(request):
        body = await body_of(request)
        if actor_of(body) != wrapup_store.USER:
            return web.json_response({"error": "only the user confirms wrap-up drafts"}, status=403)
        target = body.get("target")
        if not isinstance(target, str) or not target:
            return web.json_response({"error": "target (the session whose drafts to confirm) is required"},
                                     status=400)
        kind = body.get("kind")
        listed, err = await call(wrap.list, target, settled=False)
        if err:
            return err
        out = []
        for d in listed["drafts"]:
            if d["status"] not in wrapup_store.OPEN or (kind and d["kind"] != kind):
                continue
            try:
                claimed = await asyncio.to_thread(wrap.claim, wrapup_store.USER, d["id"], d["revision"])
            except wrapup_store.WrapupError:
                continue   # moved since the list: the user sees it again
            out.append(await confirm_one(claimed))
        tell(target, out, "confirmed")
        return web.json_response({
            "drafts": out,
            "confirmed": sum(1 for d in out if d["status"] == "confirmed"),
            "failed": sum(1 for d in out if d["status"] == "failed"),
        }, dumps=dumps)

    # confirm-all goes first: "/api/wrapup/{id}" would take it as an id.
    app.router.add_get("/api/wrapup", h_list)
    app.router.add_get("/api/wrapup/rail", h_rail)
    app.router.add_post("/api/wrapup", h_draft)
    app.router.add_post("/api/wrapup/confirm-all", h_confirm_all)
    app.router.add_post("/api/wrapup/{id}", h_update)
    app.router.add_post("/api/wrapup/{id}/discard", h_discard)
    app.router.add_post("/api/wrapup/{id}/confirm", h_confirm)
    return wrap
