"""Dashboard and MCP routes for workspace knowledge records; disk work stays off-loop."""
from __future__ import annotations

import asyncio
import json
from aiohttp import web
from .. import knowledge


def install(app):
    async def handle(request):
        try:
            body = {}
            if request.method in ("POST", "PUT"):
                try:
                    body = await request.json()
                except (ValueError, UnicodeError):
                    raise knowledge.KnowledgeError("body must be a JSON object")
                if not isinstance(body, dict):
                    raise knowledge.KnowledgeError("body must be a JSON object")
            ws = knowledge.workspace(request.query.get("workspace") or body.get("workspace"),
                                     request.query.get("cwd") or body.get("cwd"))
            ident = request.match_info.get("id", "")
            if request.match_info.get("operation") == "export":
                text = await asyncio.to_thread(knowledge.export_markdown, ws)
                return web.Response(text=text, content_type="text/markdown", charset="utf-8")
            if request.method == "GET":
                try:
                    limit = int(request.query.get("limit", "50"))
                except ValueError:
                    raise knowledge.KnowledgeError("limit must be an integer 1..100")
                result = {"records": await asyncio.to_thread(
                    knowledge.search, ws, request.query.get("q", ""),
                    request.query.get("kind", ""), limit,
                    request.query.get("status", "active"))}
            elif request.method == "POST":
                result = {"record": await asyncio.to_thread(
                    knowledge.create, ws, body, body.get("author", ""))}
            elif request.method == "PUT":
                result = {"record": await asyncio.to_thread(knowledge.update, ws, ident, body)}
            else:
                await asyncio.to_thread(knowledge.delete, ws, ident)
                result = {"ok": True}
            return web.json_response({"workspace": ws.name, **result},
                                     dumps=lambda d: json.dumps(d, ensure_ascii=False))
        except knowledge.KnowledgeError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)

    async def history(request):
        try:
            ws = knowledge.workspace(request.query.get("workspace"), request.query.get("cwd"))
            try:
                limit = int(request.query.get("limit", "20"))
            except ValueError:
                raise knowledge.KnowledgeError("limit must be an integer 1..100")
            versions = await asyncio.to_thread(knowledge.history, ws,
                                               request.match_info["id"], limit)
            return web.json_response({"workspace": ws.name, "versions": versions},
                                     dumps=lambda d: json.dumps(d, ensure_ascii=False))
        except knowledge.KnowledgeError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)

    r = app.router
    r.add_get("/api/knowledge/{operation:export}", handle)
    r.add_get("/api/knowledge/{id}/history", history)
    r.add_get("/api/knowledge", handle)
    r.add_post("/api/knowledge", handle)
    r.add_put("/api/knowledge/{id}", handle)
    r.add_delete("/api/knowledge/{id}", handle)
