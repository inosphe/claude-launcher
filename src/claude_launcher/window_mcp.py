"""MCP access to the machine measurement window.

The cancellation tool always supplies the calling managed session.  Agents
can clear their own abandoned waits, while administrative cancellation of a
different session remains on the authenticated CLI/API surface.
"""

from __future__ import annotations

import os

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
