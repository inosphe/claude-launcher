"""Fleet read of session faults (claunch-pczoc).

The per-session reading rides the session list as ``fault``; this route
answers the fleet question in one request -- "which sessions are stopped by
the harness itself right now?" -- and is what the ``session_faults`` MCP
tool reads. It is read-only: faults are detected from the terminal, never
written here.
"""

from __future__ import annotations

import json
from typing import List

from aiohttp import web


def _flag(raw: str) -> str:
    """``active`` query: '' or '1' = active only; '0' = inactive only; 'all' = any."""
    raw = (raw or "").strip().lower()
    if raw in ("all", "any", "*"):
        return "all"
    if raw in ("0", "false", "no", "inactive"):
        return "inactive"
    return "active"


def rows(manager, *, session: str = "", active: str = "active",
         kind: str = "", harness: str = "", limit: int = 0) -> List[dict]:
    """The fault rows for the sessions this daemon holds.

    A session that never faulted contributes nothing, and so does one whose
    record expired -- unless ``active='all'`` asks for the last message too.
    """
    if session:
        # An unknown name is an empty answer, not a 500: the caller is
        # asking about a session that no longer exists.
        try:
            sessions = [manager.get(session)]
        except Exception:  # noqa: BLE001 - ManagerError without the import cycle
            sessions = []
    else:
        sessions = manager.list()
    out: List[dict] = []
    for s in sessions:
        fault = getattr(s, "fault", None)
        if not callable(fault):
            continue
        view = fault()
        if not view:
            continue
        if active == "active" and not view.get("active"):
            continue
        if active == "inactive" and view.get("active"):
            continue
        if kind and view.get("kind") != kind:
            continue
        if harness and view.get("harness") != harness:
            continue
        out.append({"session": s.sdef.name, **view})
        if limit and len(out) >= limit:
            break
    # Active faults first, newest message first within each group. Two stable
    # sorts: the second only moves the active rows ahead, keeping the order
    # the first one made.
    out.sort(key=lambda row: row.get("last_seen") or "", reverse=True)
    out.sort(key=lambda row: not row.get("active"))
    return out


def install(app) -> None:
    async def h_session_faults(request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "0") or 0)
        except ValueError:
            return web.json_response({"error": "limit must be an integer"}, status=400)
        rows_out = rows(
            request.app["manager"],
            session=(request.query.get("session") or "").strip(),
            active=_flag(request.query.get("active", "")),
            kind=(request.query.get("kind") or "").strip(),
            harness=(request.query.get("harness") or "").strip(),
            limit=max(0, limit),
        )
        return web.json_response(
            {"faults": rows_out, "count": len(rows_out)},
            dumps=lambda d: json.dumps(d, ensure_ascii=False),
        )

    app.router.add_get("/api/session-faults", h_session_faults)
