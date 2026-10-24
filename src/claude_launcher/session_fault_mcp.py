"""MCP read of session faults (claunch-pczoc).

``session_faults`` answers "which sessions are stopped by the harness
itself" for one session or the whole fleet. The detection and the record
live in the daemon (``daemon/session_fault.py``); this is a read-only view
of the same rows the rail draws, so an agent does not have to capture a
pane to learn that its peer hit a weekly limit.
"""

from __future__ import annotations

from urllib.parse import urlencode

from . import daemon_client, mcp_rpc

TOOLS = [
    {
        "name": "session_faults",
        "description": (
            "List session faults: harness messages that say the account, the "
            "credentials or the service has stopped a session (an exhausted "
            "usage plan, a rejected login, an API status error, a network "
            "outage, a policy refusal). Read from the bottom of each session's "
            "terminal by the daemon; this tool only reads the record. Defaults "
            "to active faults across the fleet. Pass 'session' for one "
            "session, 'active' false for expired records too, or 'kind'/"
            "'harness' to narrow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "session name; omit for every session",
                },
                "active": {
                    "type": "boolean",
                    "description": "true (default): only faults still inside their window; false: expired records too",
                },
                "kind": {
                    "type": "string",
                    "description": "fault kind, e.g. usage_limit, account_suspended, auth, api_status, overloaded, network, policy_blocked, rate_limit",
                },
                "harness": {
                    "type": "string",
                    "description": "harness name, e.g. claude, codex, pi, kimi",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "maximum rows to return",
                },
            },
        },
    },
]


class SessionFaultMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise SessionFaultMcpError(
            f"cannot read session faults: {daemon_client.unreachable_reason(why)}"
        )
    return client


def call_tool(name: str, args: dict) -> dict:
    if name != "session_faults":
        raise SessionFaultMcpError(f"unknown tool {name!r}")
    args = args or {}
    query = {"active": "all" if args.get("active") is False else "active"}
    for key in ("session", "kind", "harness", "limit"):
        if args.get(key) is not None:
            query[key] = args[key]
    return _client().get("/api/session-faults?" + urlencode(query))


SERVER = mcp_rpc.Server(
    name="claunch-session-faults",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(SessionFaultMcpError, daemon_client.DaemonClientError),
)
