"""Session plan routes (claunch-7jzp8): the plan a session writes into its
plan pane, and the decisions the user takes there.

  GET  /api/plan?session=<name>          one session's plan ({"plan": null} without one)
  GET  /api/plan/rail                    per session with a plan: title, context, summary
  GET  /api/plan/joint?session=<name>    the joint tree holding it (plan/joint.py); no session: every tree
  POST /api/plan                         set       {session, revision, title, goal?, issue?, steps?, closure?}
  POST /api/plan/node/{id}               patch     {session, title?, detail?, status?, options?}
  POST /api/plan/clear                   clear     {session, revision}
  POST /api/plan/node/{id}/decide        decide    {session, rev, choice?|verdict?|action?, note?}  (the user)
  POST /api/plan/feedback                feedback  {session, text, node?}                       (the user)
  POST /api/plan/node/{id}/deliver       deliver the waiting notification now {session}        (the user)

Who is calling follows qa_api, with one difference: every plan route names
the plan's ``session``, so the caller is told by ``by`` -- the plan_* MCP
tools send ``by: $CLAUNCH_SESSION``; a body without ``by`` is the user (the
dashboard). The store refuses the user's hand on the plan and a session's
hand on a decision, and any session's hand on another session's plan.

Reading is open: any session may read any plan (``plan_get session=``), so a
leader reads its workers' plans and the joint views (claunch-7jzp8.1, .3)
need no second surface.

A decision or a comment the user saves is typed into the owning session as
a ``[claunch plan]`` block that carries what was decided and on what, so the
agent acts on it without a read. Delivery is tracked the way QA tracks it
(qa_api.Outbox, one latest notification per node).

The plan's ``context`` (project, cwd, spawn parent) is written by the daemon
from the session's record on every set, not by the session: the aggregated
views group plans by it, and a session cannot misplace itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from ..plan import joint as plan_joint
from ..plan import store as plan_store
from . import paths
from .qa_api import Outbox

log = logging.getLogger(__name__)

PREFIX = "[claunch plan]"


def _quote(text: str) -> str:
    return "\n".join("  > " + line for line in (text or "").splitlines()) or "  > (empty)"


def _what(node: dict) -> list[str]:
    lines = [f"  {node['kind']} {node['id']} (rev {node['rev']}): {node['title']}"]
    if node.get("detail"):
        lines.append(_quote(node["detail"]))
    return lines


def decided_text(session: str, node: dict, previous: Optional[dict]) -> str:
    res = node.get("result") or {}
    kind = node["kind"]
    if kind == "decision":
        opt = next((o for o in node.get("options") or [] if o["label"] == res.get("choice")), {})
        head = f"{PREFIX} The user chose \"{res.get('choice')}\" on your plan's decision {node['id']}:"
        body = _what(node) + [f"  expected effect: {opt.get('effect', '')}", f"  risk: {opt.get('risk', '')}"]
        then = ("Follow that branch: rewrite the steps under it with plan_set (or patch them with plan_node) "
                "so the plan shows the path you now take.")
    elif kind == "verify":
        head = f"{PREFIX} The user checked your plan's verify item {node['id']}: {res.get('verdict')}."
        body = _what(node)
        then = ("Nothing to do but keep going." if res.get("verdict") == "passed" else
                "It failed by hand. Fix what the note says, then reword the item with plan_node so the user "
                "checks it again (a changed item asks again).")
    else:
        action = res.get("action")
        head = f"{PREFIX} The user disposed of your plan's followup {node['id']}: {action}."
        body = _what(node)
        then = {
            "here": "Do it in this session: add its steps to the plan, and mark the followup done "
                    "(plan_node status=done) when it is.",
            "issue": "Hand it over: file it as a beads issue (claunch beads create ... --parent <your issue>), "
                     "then mark the followup done (plan_node status=done) naming the issue id in its detail.",
            "drop": "Drop it; nothing more to do on it.",
        }.get(action, "")
    lines = [head, *body]
    if res.get("note"):
        lines += ["their note:", _quote(res["note"])]
    if previous:
        lines.append(f"(this replaces an earlier result: {previous})")
    lines.append(then)
    return "\n".join(lines)


def feedback_text(session: str, node: Optional[dict], text: str) -> str:
    on = f"on step {node['id']} ({node['title']})" if node else "on your plan"
    return "\n".join([
        f"{PREFIX} The user commented {on}:",
        _quote(text),
        "Answer by changing the plan where it calls for that (plan_set / plan_node); if it asks a question "
        "back, reply in a thread (thread_open) rather than in the terminal.",
    ])


def install(app, store: Optional[plan_store.PlanStore] = None):
    manager = app["manager"]
    plans = store or plan_store.PlanStore(paths.plan_db())
    app["plans"] = plans
    outbox = Outbox(manager)
    app["plan_outbox"] = outbox

    def context_of(name: str) -> dict:
        try:
            sdef = manager.get(name).sdef
        except Exception:  # noqa: BLE001 -- no such session
            return {}
        out = {}
        for key in ("project", "cwd", "parent"):
            value = getattr(sdef, key, None)
            if value:
                out[key] = str(value)
        return out

    def parent_of(name: str) -> Optional[str]:
        """The live spawn parent ("" for none); None for a session this
        daemon does not hold, so joint.py falls back to the plan's context."""
        try:
            sdef = manager.get(name).sdef
        except Exception:  # noqa: BLE001
            return None
        return str(getattr(sdef, "parent", None) or "")

    def known(name: str) -> bool:
        try:
            manager.get(name)
            return True
        except Exception:  # noqa: BLE001
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
        s = body.get("by")
        return s if isinstance(s, str) and s else plan_store.USER

    def session_of(body: dict) -> str:
        s = body.get("session")
        if not isinstance(s, str) or not s:
            raise web.HTTPBadRequest(text='{"error": "session (whose plan) is required"}',
                                     content_type="application/json")
        return s

    async def call(fn, *args, **kw):
        try:
            return await asyncio.to_thread(fn, *args, **kw), None
        except plan_store.PlanError as exc:
            doc = {"error": str(exc)}
            if exc.current is not None:
                doc["current"] = exc.current
            return None, web.json_response(doc, status=exc.status)

    def with_delivery(plan: Optional[dict]) -> Optional[dict]:
        if plan is None:
            return None
        for node in [*plan_store.walk(plan.get("steps") or []), *(plan.get("closure") or [])]:
            view = outbox.view(f"{plan['session']}:{node['id']}")
            if view:
                node["delivery"] = view
        return plan

    async def h_get(request):
        session = request.query.get("session") or ""
        if not session:
            return web.json_response({"error": "session is required"}, status=400)
        res, err = await call(plans.get, session)
        return err or web.json_response({"session": session, "plan": with_delivery(res)})

    async def h_rail(_request):
        res, err = await call(plans.rail)
        return err or web.json_response({"sessions": res})

    async def h_joint(request):
        session = request.query.get("session") or ""
        rail, err = await call(plans.rail)
        if err:
            return err
        if not session:
            return web.json_response({"groups": plan_joint.groups(rail, parent_of)})
        return web.json_response({"session": session, "group": plan_joint.group_of(rail, parent_of, session)})

    async def h_set(request):
        body = await body_of(request)
        session = session_of(body)
        actor = actor_of(body)
        if actor != plan_store.USER and not known(actor):
            return web.json_response({"error": f"no session {actor!r} on this daemon"}, status=404)
        res, err = await call(plans.set, actor, session, body.get("revision"), title=body.get("title"),
                              goal=body.get("goal"), issue=body.get("issue"), steps=body.get("steps"),
                              closure=body.get("closure"), context=context_of(session))
        return err or web.json_response({"plan": res})

    async def h_patch(request):
        body = await body_of(request)
        fields = {k: body.get(k) for k in ("title", "detail", "status", "options")}
        res, err = await call(plans.patch, actor_of(body), session_of(body), request.match_info["id"], **fields)
        return err or web.json_response(res)

    async def h_clear(request):
        body = await body_of(request)
        res, err = await call(plans.clear, actor_of(body), session_of(body), body.get("revision"))
        return err or web.json_response(res)

    async def h_decide(request):
        body = await body_of(request)
        session = session_of(body)
        res, err = await call(plans.decide, actor_of(body), session, request.match_info["id"], body.get("rev"),
                              choice=body.get("choice"), verdict=body.get("verdict"), action=body.get("action"),
                              note=body.get("note"))
        if err:
            return err
        node = res["node"]
        text = decided_text(session, node, res["previous"])
        key = f"{session}:{node['id']}"
        outbox.send(key, session, "decide", lambda: text)
        res["delivery"] = outbox.view(key)
        res["delivered"] = res["delivery"]["state"] != "failed"
        return web.json_response(res)

    async def h_feedback(request):
        body = await body_of(request)
        session = session_of(body)
        res, err = await call(plans.feedback, actor_of(body), session, text=body.get("text"),
                              node=body.get("node") or None)
        if err:
            return err
        text = feedback_text(session, res["node"], res["text"])
        key = f"{session}:{(res['node'] or {}).get('id', '_feedback')}"
        outbox.send(key, session, "feedback", lambda: text)
        delivery = outbox.view(key)
        return web.json_response({"delivery": delivery, "delivered": delivery["state"] != "failed"})

    async def h_deliver(request):
        body = await body_of(request)
        key = f"{session_of(body)}:{request.match_info['id']}"
        rec = await outbox.force(key)
        if rec is None:
            return web.json_response({"error": "no notification for that node is waiting",
                                      "delivery": outbox.view(key)}, status=409)
        return web.json_response({"delivery": outbox.view(key)})

    app.router.add_get("/api/plan", h_get)
    app.router.add_get("/api/plan/rail", h_rail)
    app.router.add_get("/api/plan/joint", h_joint)
    app.router.add_post("/api/plan", h_set)
    app.router.add_post("/api/plan/clear", h_clear)
    app.router.add_post("/api/plan/feedback", h_feedback)
    app.router.add_post("/api/plan/node/{id}", h_patch)
    app.router.add_post("/api/plan/node/{id}/decide", h_decide)
    app.router.add_post("/api/plan/node/{id}/deliver", h_deliver)
    return plans
