"""MCP access to the machine measurement window.

The cancellation tool always supplies the calling managed session.  Agents
can clear their own abandoned waits, while administrative cancellation of a
different session remains on the authenticated CLI/API surface.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc


TOOLS = [
    {
        "name": "window_status",
        "description": (
            "Read the machine measurement window: current holders, queued "
            "requests, capacity, and the recommended pytest worker count."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "window_history",
        "description": (
            "Read the measurement window's run log and statistics: per class "
            "the runs, their results, wait and hold times (p50/p90/max), and "
            "requests that gave up waiting. The daemon records these itself; "
            "the log keeps at most 7 days."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "number", "description": "look back (at most 7)"},
                "class": {"type": "string", "enum": ["sweep", "targeted"]},
                "session": {"type": "string", "description": "only this holder"},
                "limit": {"type": "integer", "description": "entries (default 20)"},
            },
        },
    },
    {
        "name": "window_cancel",
        "description": (
            "Withdraw this managed session's waiting measurement requests. "
            "It never releases a held grant and cannot cancel another session."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class WindowMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise WindowMcpError(
            f"cannot ask the measurement window: {daemon_client.unreachable_reason(why)}"
        )
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise WindowMcpError(
            "no $CLAUNCH_SESSION — window_cancel only works inside a managed claunch session"
        )
    return session


def call_tool(name: str, args: dict) -> dict:
    if name == "window_status":
        return _client().get("/api/window")
    if name == "window_history":
        query = {"limit": int(args.get("limit") or 20)}
        for key in ("days", "class", "session"):
            if args.get(key):
                query[key] = args[key]
        return _client().get("/api/window/history?" + urllib.parse.urlencode(query))
    if name == "window_cancel":
        session = _session()
        return _client().post("/api/window/cancel", {"session": session})
    raise WindowMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-window",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(WindowMcpError, daemon_client.DaemonClientError),
)
