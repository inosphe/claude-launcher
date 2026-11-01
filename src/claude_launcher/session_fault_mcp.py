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
            "List session faults: harness messages that stop a session for a "
            "reason the work cannot fix -- an exhausted usage plan, a rejected "
            "login, an API status error, a network outage, a policy refusal, "
            "or a prompt waiting for a keystroke. Read from the bottom of each "
            "session's terminal by the daemon; this tool only reads the "
            "record. A session the daemon paused on a usage limit has no "
            "screen left -- its process is gone -- so its row is read from the "
            "record that pause wrote, and says so in 'source': "
            "'pause-record' rather than 'screen'. Each row carries "
            "'resolved_by' -- what clears the stop: "
            "'input' (someone presses a key on that terminal), 'account' "
            "(billing, plan, login), 'provider' (the service recovers or time "
            "passes) or 'policy' (the session changes how it asks). 'strikes' "
            "counts the runs of one kind: a sighting opens its own window and "
            "never extends the previous one, so a line that comes back after "
            "its run ended is a second sighting rather than the same stop "
            "stretched out. A run also ends early when the message leaves the "
            "bottom of a changing screen -- the stop is visibly over then -- "
            "and the row says when ('cleared'). Defaults "
            "to active faults across the fleet. Pass 'session' for one "
            "session, 'active' false for expired and cleared records too, or "
            "'kind'/'harness' to narrow."
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
                    "description": "true (default): only faults still inside their window and not cleared; false: expired and cleared records too",
                },
                "kind": {
                    "type": "string",
                    "description": "fault kind, e.g. usage_limit, account_suspended, auth, api_status, overloaded, network, policy_blocked, rate_limit, awaiting_input",
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
