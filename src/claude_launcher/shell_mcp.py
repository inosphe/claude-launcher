"""MCP tools for this session's own shell (the session's ``cli`` pane).

The daemon keeps one shell per agent session, with the user's own privileges
(``daemon/clipty.py``'s :class:`~claude_launcher.daemon.clipty.SessionShell`,
``daemon/session_shells.py``). A session's share of it is deliberately
two-faced: it may **read** what the shell printed, and it may **propose**
input the user can accept in that session's pane. There is no tool that
types into the shell — a proposal reaches the PTY only when the user accepts
it, and the daemon refuses an accept that comes in shaped like a session's
call (``daemon/shell_api.py``).

The tools are thin clients of ``/api/sessions/<name>/shell/*``, where
``<name>`` is always ``$CLAUNCH_SESSION``. There is no argument that names
another session's shell: a proposal can only be made to the shell of the
session making it, and the daemon refuses a body that disagrees with the
path.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc

INSTRUCTIONS = """\
Your session's shell (shell_* tools): a shell with the user's privileges,
shown in your session's CLI pane. Read it (shell_status/shell_read); propose
input with shell_suggest; only the user's accept types it. Guidance: prompt
pack 'mcp-shell'.
"""

_TOOLS = [
    {
        "name": "shell_status",
        "description": (
            "Your session's shell: running or exited, its size, the output "
            "cursor (offset) and how many input proposals wait for the user."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "shell_read",
        "description": (
            "Read your session's shell output without typing anything. "
            "Without offset you get the tail; pass the previous response's "
            "'offset' to continue. 'dropped' says how many bytes fell out of "
            "the ring before you asked; 'pid' changes when the shell "
            "restarts, and 'reset' says your cursor was beyond the new "
            "incarnation's output (its numbering began again at 0); 'exited' "
            "says the shell is gone."
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
            "Propose input for your session's shell. It is queued in this "
            "session's CLI pane and typed into the shell ONLY if the user "
            "accepts it. One line or a short script; keep 'why' to one "
            "sentence."
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
        "description": "Your session's shell proposal queue, with each one's state and proposer.",
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


def _base() -> str:
    """``/api/sessions/<this session>/shell`` — the only shell these tools
    can address. Nothing an agent supplies picks the session."""
    return "/api/sessions/" + urllib.parse.quote(_session(), safe="") + "/shell"


def call_tool(name: str, args: dict) -> dict:
    c = _client()
    base = _base()
    if name == "shell_status":
        return c.get(base)
    if name == "shell_read":
        q = {}
        if args.get("offset") is not None:
            q["offset"] = int(args["offset"])
        if args.get("limit") is not None:
            q["limit"] = int(args["limit"])
        return c.get(base + "/output" + ("?" + urllib.parse.urlencode(q) if q else ""))
    if name == "shell_suggestions":
        q = {"state": args["state"]} if args.get("state") else {}
        return c.get(base + "/suggestions" + ("?" + urllib.parse.urlencode(q) if q else ""))
    me = _session()
    if name == "shell_suggest":
        return c.post(base + "/suggestions", {
            "session": me,
            "text": args.get("text") or "",
            "why": args.get("why") or "",
        })
    if name == "shell_withdraw":
        return c.post(f"{base}/suggestions/{_sid(args)}/withdraw", {"session": me})
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
