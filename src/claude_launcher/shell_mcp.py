"""MCP tools for the shared user shell (the dashboard's CLI tab).

The daemon keeps one persistent, unmanaged shell with the user's own
privileges (``daemon/clipty.py``). A session's share of it is deliberately
two-faced: it may **read** what the shell printed, and it may **propose**
input the user can accept from the CLI tab. There is no tool that types into
the shell — a proposal reaches the PTY only when the user accepts it, and the
daemon refuses an accept that comes in shaped like a session's call
(``daemon/shell_api.py``).

The tools are thin clients of ``/api/shell*``; every call carries
``$CLAUNCH_SESSION`` so a proposal is attributed to its session.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc

INSTRUCTIONS = """\
Shared user shell (shell_* tools): the dashboard CLI tab, with the user's
privileges. Read it (shell_status/shell_read); propose input with
shell_suggest. Only the user's accept types it. Guidance: prompt pack
'mcp-shell'.
"""

_TOOLS = [
    {
        "name": "shell_status",
        "description": (
            "The user's shared shell: running or exited, its size, the output "
            "cursor (offset) and how many input proposals wait for the user."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "shell_read",
        "description": (
            "Read the shared shell's output without typing anything. Without "
            "offset you get the tail; pass the previous response's 'offset' to "
            "continue. 'dropped' says how many bytes fell out of the ring "
            "before you asked; 'pid' changes when the shell restarts, and "
            "'reset' says your cursor was beyond the new incarnation's output "
            "(its numbering began again at 0); 'exited' says the shell is gone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offset": {"type": "integer",
                           "description": "byte offset a previous read returned"},
                "limit": {"type": "integer",
                          "description": "maximum bytes (default 65536, cap 65536)"},
            },
        },
    },
    {
        "name": "shell_suggest",
        "description": (
            "Propose input for the user's shared shell. It is queued in the "
            "CLI tab and typed into the shell ONLY if the user accepts it. "
            "One line or a short script; keep 'why' to one sentence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the exact input to run"},
                "why": {"type": "string", "description": "why you need it (shown to the user)"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "shell_suggestions",
        "description": "The shared shell's proposal queue, with each one's state and proposer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string",
                          "description": "pending, accepted, rejected or withdrawn"},
            },
        },
    },
    {
        "name": "shell_withdraw",
        "description": "Take back a pending proposal you made (only your own).",
        "inputSchema": {
            "type": "object",
            "properties": {"suggestion": {"type": "string", "description": "suggestion id (sh-xxxxxx)"}},
            "required": ["suggestion"],
        },
    },
]


class ShellMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise ShellMcpError(
            f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}"
        )
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise ShellMcpError("no $CLAUNCH_SESSION -- a shell suggestion needs a session")
    return session


def _sid(args: dict) -> str:
    sid = str(args.get("suggestion") or "")
    if not sid:
        raise ShellMcpError("suggestion is required")
    return urllib.parse.quote(sid, safe="")


def call_tool(name: str, args: dict) -> dict:
    c = _client()
    if name == "shell_status":
        return c.get("/api/shell")
    if name == "shell_read":
        q = {}
        if args.get("offset") is not None:
            q["offset"] = int(args["offset"])
        if args.get("limit") is not None:
            q["limit"] = int(args["limit"])
        return c.get("/api/shell/output" + ("?" + urllib.parse.urlencode(q) if q else ""))
    if name == "shell_suggestions":
        q = {"state": args["state"]} if args.get("state") else {}
        return c.get("/api/shell/suggestions" + ("?" + urllib.parse.urlencode(q) if q else ""))
    me = _session()
    if name == "shell_suggest":
        return c.post("/api/shell/suggestions", {
            "session": me,
            "text": args.get("text") or "",
            "why": args.get("why") or "",
        })
    if name == "shell_withdraw":
        return c.post(f"/api/shell/suggestions/{_sid(args)}/withdraw", {"session": me})
    raise ShellMcpError(f"unknown tool {name!r}")


#: The family's tools, public like every other *_mcp module's (the merged
#: server's inventory test and the installer's summary read it).
TOOLS = tuple(_TOOLS)


SERVER = mcp_rpc.Server(
    name="claunch-shell",
    tools=TOOLS,
    dispatch=call_tool,
    errors=(ShellMcpError, daemon_client.DaemonClientError),
    instructions=INSTRUCTIONS,
)
