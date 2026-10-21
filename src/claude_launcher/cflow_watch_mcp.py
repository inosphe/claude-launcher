"""Watch other sessions' cflow runs (the ``runs``/``subscribe`` family).

Exposed by the shared claunch MCP server. Every call acts as the calling
session (``$CLAUNCH_SESSION``); the daemon decides whether that session may
read or watch a target (its subtree, or a mesh-connected member) and refuses
the rest with the reason.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlencode

from . import daemon_client, mcp_rpc


TARGETS = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": 16,
    "description": "session names to read (a session in your subtree, or one you are connected to in the mesh)",
}

KINDS = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": 32,
    "description": (
        "event names to deliver (default: reported, done, aborted, failed, "
        "select_held, ask_routed/ask_timeout/ask_answered, round_requested, "
        "step_report, waiting_selection/approval). Node transitions "
        "('entered'/'left') are pull-only."
    ),
}


TOOLS = [
    {
        "name": "runs",
        "description": (
            "Where the cflow runs of other sessions stand, read-only: version "
            "(1|2), workflow, run id, status, current nodes, what a person "
            "owes, and a cursor. Omit 'targets' to read every run you have "
            "subscribed to. A target you may not read (not in your subtree, "
            "not connected) comes back under 'refused' with the reason."
        ),
        "inputSchema": {"type": "object", "properties": {"targets": TARGETS}},
    },
    {
        "name": "subscribe",
        "description": (
            "Start receiving state changes of another session's cflow run as "
            "one compressed fyi block in your terminal. The daemon reads the "
            "run's own journal from the cursor it records now; the cursor "
            "moves only when the block is accepted, so a missed delivery is "
            "retried. Use 'runs' to read the run now, and 'subscriptions' to "
            "see the cursors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "session to watch"}, "kinds": KINDS},
            "required": ["target"],
        },
    },
    {
        "name": "unsubscribe",
        "description": "Stop watching a session's cflow run. Returns removed: false when there was no such subscription.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "subscriptions",
        "description": (
            "The runs you watch, each with the event kinds, the stored cursor "
            "and since when. Call it after a compaction or a long absence: "
            "the cursor is where your next delivery resumes."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class CflowWatchMcpError(Exception):
    pass


def _client():
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise CflowWatchMcpError("cflow watch tools require a managed CLAUNCH_SESSION")
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise CflowWatchMcpError(daemon_client.unreachable_reason(why))
    return client, session


def call_tool(name: str, args: dict) -> dict:
    client, session = _client()
    base = "/api/cflow-watch"
    if name == "runs":
        targets = args.get("targets") or []
        if not isinstance(targets, list):
            raise CflowWatchMcpError("'targets' must be an array of session names")
        query = {"session": session}
        if targets:
            query["targets"] = ",".join(str(t) for t in targets)
        return client.get(base + "/runs?" + urlencode(query))
    if name == "subscriptions":
        return client.get(base + "/subscriptions?" + urlencode({"session": session}))
    if name == "subscribe":
        target = str(args.get("target") or "").strip()
        if not target:
            raise CflowWatchMcpError("target is required")
        kinds = args.get("kinds")
        if kinds is not None and not isinstance(kinds, list):
            raise CflowWatchMcpError("kinds must be an array of event names")
        return client.post(base + "/subscribe", {"session": session, "target": target, "kinds": kinds})
    if name == "unsubscribe":
        target = str(args.get("target") or "").strip()
        if not target:
            raise CflowWatchMcpError("target is required")
        return client.post(base + "/unsubscribe", {"session": session, "target": target})
    raise CflowWatchMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-cflow-watch",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(CflowWatchMcpError, daemon_client.DaemonClientError, OSError, ValueError),
)
