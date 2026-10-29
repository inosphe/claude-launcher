"""MCP read of a session's opening context items (gds6-bfu7).

A session can be created with context items beside its task -- an error log,
a spec excerpt -- each stored as its own file (``daemon/opening_contexts.py``).
Its opening names every item with a path on the daemon's machine; these tools
read the same items through the daemon, for a harness that cannot open that
path, and for a session that wants a long item a window at a time.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlencode

from . import daemon_client, mcp_rpc

TOOLS = [
    {
        "name": "context_list",
        "description": (
            "List the context items a session was created with: the separate "
            "pieces (an error log, a spec excerpt, notes) the person or parent "
            "attached beside the opening task. Each row has its number 'n', "
            "'title', size ('bytes', 'chars', 'lines') and 'path' on this "
            "machine; read one with context_read. Defaults to this session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "session name; omit for this session",
                },
            },
        },
    },
    {
        "name": "context_read",
        "description": (
            "Read one context item of a session by its number 'n' (as the "
            "opening's '[claunch context] <n>. ...' line and context_list name "
            "it). Returns 'text' from character 'offset', at most 'max_chars' "
            "characters (default 60000); when 'truncated' is true, call again "
            "with offset = 'next_offset' for the rest. Defaults to this session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "description": "the item's number"},
                "session": {
                    "type": "string",
                    "description": "session name; omit for this session",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "character offset to start from (default 0)",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "at most this many characters (default 60000)",
                },
            },
            "required": ["n"],
        },
    },
]


class ContextMcpError(Exception):
    pass


def _target(args: dict) -> str:
    session = str(args.get("session") or os.environ.get("CLAUNCH_SESSION") or "").strip()
    if not session:
        raise ContextMcpError("name a session: this process has no $CLAUNCH_SESSION")
    return quote(session, safe="")


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise ContextMcpError(
            f"cannot read context items: {daemon_client.unreachable_reason(why)}"
        )
    return client


def call_tool(name: str, args: dict) -> dict:
    args = args or {}
    if name == "context_list":
        return _client().get(f"/api/sessions/{_target(args)}/contexts")
    if name == "context_read":
        try:
            n = int(args.get("n"))
        except (TypeError, ValueError):
            raise ContextMcpError("context_read needs 'n', the item's number") from None
        query = {k: int(args[k]) for k in ("offset", "max_chars") if args.get(k) is not None}
        path = f"/api/sessions/{_target(args)}/contexts/{n}"
        return _client().get(path + ("?" + urlencode(query) if query else ""))
    raise ContextMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-contexts",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(ContextMcpError, daemon_client.DaemonClientError),
)
