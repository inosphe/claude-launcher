"""Dashboard and MCP routes for workspace glossaries; disk work stays off-loop."""
from __future__ import annotations

import asyncio
import json
from aiohttp import web
from .. import glossary


def install(app):
    async def handle(request):
        try:
            body = {}
            if request.method in ("POST", "PUT"):
                try:
                    body = await request.json()
                except (ValueError, UnicodeError):
                    raise glossary.GlossaryError("body must be a JSON object")
                if not isinstance(body, dict):
                    raise glossary.GlossaryError("body must be a JSON object")
            ws = glossary.workspace(request.query.get("workspace") or body.get("workspace"),
                                    request.query.get("cwd") or body.get("cwd"))
            operation = request.match_info.get("operation", "")
            ident = request.match_info.get("id", "")
            if operation == "resolve":
                result = await asyncio.to_thread(glossary.resolve, ws, body.get("text", ""))
            elif operation == "suggestions":
                if request.method == "GET":
                    result = {"suggestions": await asyncio.to_thread(glossary.suggestions, ws, request.query.get("status", "pending"))}
                elif ident:
                    result = await asyncio.to_thread(glossary.review, ws, ident, body.get("decision"))
                else:
                    result = {"suggestion": await asyncio.to_thread(glossary.suggest, ws, body, body.get("proposed_by", ""))}
            elif request.method == "GET":
                try:
                    limit = int(request.query.get("limit", "50"))
                except ValueError:
                    raise glossary.GlossaryError("limit must be an integer 1..100")
                result = {"terms": await asyncio.to_thread(glossary.search, ws, request.query.get("q", ""), limit)}
            elif request.method == "POST":
                result = {"term": await asyncio.to_thread(glossary.create, ws, body)}
            elif request.method == "PUT":
                result = {"term": await asyncio.to_thread(glossary.update, ws, ident, body)}
            else:
                await asyncio.to_thread(glossary.delete, ws, ident)
                result = {"ok": True}
            return web.json_response({"workspace": ws.name, **result}, dumps=lambda d: json.dumps(d, ensure_ascii=False))
        except glossary.GlossaryError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)

    async def session_glossary(request):
        # The target session owns the workspace, never the browser/daemon cwd.
        session = request.app["manager"].get(request.match_info["name"])
        try:
            ws = glossary.workspace(cwd=session.sdef.cwd or "")
            if request.method == "GET":
                result = {"terms": await asyncio.to_thread(
                    glossary.search, ws, request.query.get("q", ""), 8)}
            else:
                try:
                    body = await request.json()
                except (ValueError, UnicodeError):
                    raise glossary.GlossaryError("body must be a JSON object")
                if not isinstance(body, dict):
                    raise glossary.GlossaryError("body must be a JSON object")
                action = body.get("action", "analyze")
                if action not in ("analyze", "compose"):
                    raise glossary.GlossaryError("invalid glossary action")
                result = await asyncio.to_thread(
                    glossary.compose if action == "compose" else glossary.analyze,
                    ws, body.get("text", ""))
            return web.json_response({"workspace": ws.name, **result},
                                     dumps=lambda d: json.dumps(d, ensure_ascii=False))
        except glossary.GlossaryError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)

    r = app.router
    r.add_get("/api/sessions/{name}/glossary", session_glossary)
    r.add_post("/api/sessions/{name}/glossary", session_glossary)
    r.add_post("/api/glossary/{operation:resolve}", handle)
    r.add_get("/api/glossary/{operation:suggestions}", handle)
    r.add_post("/api/glossary/{operation:suggestions}", handle)
    r.add_post("/api/glossary/{operation:suggestions}/{id}/review", handle)
    r.add_get("/api/glossary", handle)
    r.add_post("/api/glossary", handle)
    r.add_put("/api/glossary/{id}", handle)
    r.add_delete("/api/glossary/{id}", handle)
