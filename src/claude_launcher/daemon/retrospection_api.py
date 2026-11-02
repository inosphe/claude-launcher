"""Dashboard routes for session retrospectives (claunch-lmnlv); read-only.

  GET /api/retrospection?workspace=&session=&q=&limit=   newest first, body cut to an excerpt
  GET /api/retrospection/facets                          workspaces and sessions with counts
  GET /api/retrospection/{id}                            one record, whole

Records are written only by a confirmed wrap-up draft of kind
``retrospection`` (daemon/wrapup_api.py), so there is no POST here.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from .. import retrospection


def install(app):
    def dumps(doc):
        return json.dumps(doc, ensure_ascii=False)

    def error(exc):
        return web.json_response({"error": str(exc)}, status=exc.status)

    async def h_list(request):
        q = request.query
        try:
            try:
                limit = int(q.get("limit", "50"))
            except ValueError:
                raise retrospection.RetrospectionError(
                    f"limit must be an integer 1..{retrospection.MAX_LIST}") from None
            records = await asyncio.to_thread(
                retrospection.search, workspace=q.get("workspace", ""), session=q.get("session", ""),
                q=q.get("q", ""), limit=limit)
        except retrospection.RetrospectionError as exc:
            return error(exc)
        return web.json_response({"records": records}, dumps=dumps)

    async def h_facets(_request):
        try:
            doc = await asyncio.to_thread(retrospection.facets)
        except retrospection.RetrospectionError as exc:
            return error(exc)
        return web.json_response(doc, dumps=dumps)

    async def h_get(request):
        try:
            rec = await asyncio.to_thread(retrospection.get, request.match_info["id"])
        except retrospection.RetrospectionError as exc:
            return error(exc)
        return web.json_response({"record": rec}, dumps=dumps)

    r = app.router
    r.add_get("/api/retrospection", h_list)
    r.add_get("/api/retrospection/facets", h_facets)
    r.add_get("/api/retrospection/{id}", h_get)
