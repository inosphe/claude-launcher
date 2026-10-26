"""Dashboard and MCP routes for workspace category trees (claunch-p2v4m).

Disk work runs off the loop. Tree reads carry, for each item, what the
dashboard shows beside it: a session's status and note, an issue's title and
status (from the board cache; an unreadable board leaves them blank).
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from .. import category, workspaces
from . import actors
from . import category_assign as assign_mod


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


def _error(exc):
    body = {"error": str(exc)}
    body.update(getattr(exc, "extra", None) or {})
    return _json(body, getattr(exc, "status", 400))


async def _body(request):
    if request.method not in ("POST", "PUT") or not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise category.CategoryError("body must be a JSON object")
    if not isinstance(body, dict):
        raise category.CategoryError("body must be a JSON object")
    return body


def _actor(request):
    return actors.label(actors.from_request(request))


async def _details(app, ws, items):
    """``{"session:<name>": {...}, "issue:<id>": {...}}`` for display.

    A session's ``life`` is the partition the rail filters by
    (:func:`daemon.session.session_category`: running, hold, delayed, failed,
    paused, killed, archived) and ``status`` is what a live screen reads
    (busy, idle, starting). The page shows ``running · busy`` for the live
    ones and ``killed``/``archived`` for records that are done -- the two
    filters on the page are built on ``life``.
    """
    from .session import session_category  # local: session imports late

    out = {}
    manager = app.get("manager")
    for item in items:
        if item["kind"] != "session" or manager is None:
            continue
        try:
            s = manager.get(item["ref"])
        except Exception:
            out["session:" + item["ref"]] = {"status": "gone", "life": "gone"}
            continue
        out["session:" + item["ref"]] = {
            "status": s.status() if hasattr(s, "status") else "",
            # A live session carries `exited`; a record with none is not one
            # this daemon can partition, so it shows no state chip.
            "life": session_category(s) if hasattr(s, "exited") else "",
            "note": getattr(s.sdef, "note", None) or "",
            "role": getattr(s.sdef, "role", None) or "",
            "issue": getattr(s.sdef, "issue", None) or "",
        }
    board = app.get("beads")
    if board is not None and any(i["kind"] == "issue" for i in items):
        try:
            root = await board.root_for(ws.path)
            rows = await board.issues_or_stale(root) if root else []
        except Exception:
            rows = []
        by_id = {r.get("id"): r for r in rows}
        for item in items:
            if item["kind"] == "issue" and item["ref"] in by_id:
                row = by_id[item["ref"]]
                out["issue:" + item["ref"]] = {"title": row.get("title", ""),
                                               "status": row.get("status", ""),
                                               "assignee": row.get("assignee") or ""}
    return out


def install(app):
    def ws_of(request, body):
        return category.workspace(request.query.get("workspace") or body.get("workspace"),
                                  request.query.get("cwd") or body.get("cwd"))

    def route(fn):
        async def handler(request):
            try:
                body = await _body(request)
                return _json(await fn(request, body))
            except (category.CategoryError, assign_mod.AssignError) as exc:
                return _error(exc)
        return handler

    async def tree(request, body):
        ws = ws_of(request, body)
        out = await asyncio.to_thread(category.tree, ws)
        placed = {i["ref"] for i in out["items"] if i["kind"] == "session"}
        # Live sessions of this workspace that are in no node yet: what the
        # page offers to select alongside the placed ones.
        manager = request.app.get("manager")
        loose = []
        for s in (manager.list() if manager is not None and hasattr(manager, "list") else []):
            if s.exited or s.sdef.name in placed:
                continue
            owner = workspaces.owning(s.sdef.cwd or "")
            if owner is not None and owner.path == ws.path:
                loose.append({"kind": "session", "ref": s.sdef.name})
        out["unassigned"] = loose
        out["details"] = await _details(request.app, ws, out["items"] + loose)
        return {"workspace": ws.name, **out}

    async def search(request, body):
        ws = ws_of(request, body)
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            raise category.CategoryError("limit must be an integer 1..100")
        return {"workspace": ws.name, "nodes": await asyncio.to_thread(
            category.search, ws, request.query.get("q", ""), limit)}

    async def node_items(request, body):
        ws = ws_of(request, body)
        items = await asyncio.to_thread(
            category.items_of, ws, request.match_info["id"],
            request.query.get("descendants", "1") not in ("0", "false"))
        return {"workspace": ws.name, "items": items,
                "details": await _details(request.app, ws, items)}

    async def create(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, "node": await asyncio.to_thread(
            category.create_node, ws, body, _actor(request))}

    async def update(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, "node": await asyncio.to_thread(
            category.update_node, ws, request.match_info["id"], body, _actor(request))}

    async def delete(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, **await asyncio.to_thread(
            category.delete_node, ws, request.match_info["id"], _actor(request))}

    async def assign(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, **await asyncio.to_thread(
            category.assign, ws, body.get("items"), body.get("node_id"), _actor(request),
            body.get("reason", ""))}

    async def unassign(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, **await asyncio.to_thread(
            category.unassign, ws, body.get("items"), _actor(request))}

    async def extract(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, **await asyncio.to_thread(
            category.extract, ws, body.get("node_id"), body.get("items"), body, _actor(request))}

    async def merge(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, **await asyncio.to_thread(
            category.merge, ws, body, _actor(request))}

    async def split(request, body):
        ws = ws_of(request, body)

        def make():
            # The page writes one line per new child and a line with no refs is
            # the catch-all; the parts select from what is inside the parent.
            # `category.spread` is that rule, and the agent's
            # `category_assign {split}` calls it too, so the form's promise and
            # the server's behaviour cannot drift apart.
            parent_id = body.get("parent_id")
            items = body.get("items")
            if items is None:
                if parent_id in (None, "", "root"):
                    raise category.CategoryError(
                        "a top-level split cannot tell what 'the remaining items' are; "
                        "name items explicitly or give parent_id")
                items = category.items_of(ws, parent_id, descendants=True)
            children = category.spread(items, body.get("parts") or body.get("children") or [])
            return category.split(ws, parent_id, children, _actor(request), body.get("reason", ""))

        return {"workspace": ws.name, **await asyncio.to_thread(make)}

    async def proposals(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, "proposals": await asyncio.to_thread(
            category.proposals, ws, request.query.get("status", "pending"))}

    async def review(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, **await asyncio.to_thread(
            category.review, ws, request.match_info["id"], body.get("decision"),
            _actor(request), body)}

    async def events(request, body):
        ws = ws_of(request, body)
        return {"workspace": ws.name, "events": await asyncio.to_thread(category.events, ws)}

    async def agent_assign(request, body):
        caller = str(body.get("session") or "").strip()
        if not caller:
            raise category.CategoryError("session is required (the calling session)")
        return await asyncio.to_thread(assign_mod.agent_assign, request.app["manager"], caller, body)

    async def session_view(request, body):
        manager = request.app["manager"]
        name = request.match_info["name"]
        try:
            session = manager.get(name)
        except Exception:
            raise category.CategoryError(f"no session named {name!r}", 404)
        ws = category.workspace(cwd=session.sdef.cwd or "")

        def read():
            job = assign_mod.refresh(manager, ws, name)
            items = assign_mod.items_for(session)
            return job, [{**i, **category.where(ws, i["kind"], i["ref"])} for i in items]

        job, places = await asyncio.to_thread(read)
        from .. import spawn as spawn_mod
        from .session import session_category
        return {"workspace": ws.name, "session": name, "places": places, "job": job,
                "mode": "fork" if spawn_mod.can_fork(session.sdef.to_dict()) else "inplace",
                "life": session_category(session)}

    async def session_assign(request, body):
        return await assign_mod.start(request, request.match_info["name"], body)

    r = app.router
    r.add_get("/api/sessions/{name}/category", route(session_view))
    r.add_post("/api/sessions/{name}/category/assign", route(session_assign))
    # Literal segments before the {id} routes.
    r.add_get("/api/category/search", route(search))
    r.add_get("/api/category/events", route(events))
    r.add_get("/api/category/proposals", route(proposals))
    r.add_post("/api/category/proposals/{id}/review", route(review))
    r.add_post("/api/category/assign", route(assign))
    r.add_post("/api/category/unassign", route(unassign))
    r.add_post("/api/category/extract", route(extract))
    r.add_post("/api/category/merge", route(merge))
    r.add_post("/api/category/split", route(split))
    r.add_post("/api/category/agent-assign", route(agent_assign))
    r.add_post("/api/category/nodes", route(create))
    r.add_get("/api/category/nodes/{id}/items", route(node_items))
    r.add_put("/api/category/nodes/{id}", route(update))
    r.add_delete("/api/category/nodes/{id}", route(delete))
    r.add_get("/api/category", route(tree))
    assign_mod.install_hooks(app)
