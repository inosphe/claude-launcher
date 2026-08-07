"""A minimal MCP stdio server for mesh: send / members / history.

Same newline-delimited JSON-RPC 2.0 subset as :mod:`claude_launcher.cflow.mcp`
(initialize / tools/list / tools/call / ping), no SDK dependency. Spawned by
Claude Code as ``claunch mesh mcp``.

This is a *convenience* wrapper around the daemon's HTTP API for agents that
prefer tools over shell — sending only. There is deliberately no ``recv``:
incoming messages are typed into the member's terminal by the daemon, so
receiving needs no tool, no polling and no cooperation (the design's core
invariant; see ``docs/mesh-design.md``).

The caller is identified by ``$CLAUNCH_SESSION`` — the env var every managed
session's children inherit — exactly like the CLI.
"""

from __future__ import annotations

import json
import os
import sys

from . import __version__, daemon_client

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "send",
        "description": (
            "Send a message to mesh members. '*' broadcasts to every other "
            "member; delivery types the message into each recipient's "
            "terminal (remote members receive it over the relay). The sender "
            "is this session ($CLAUNCH_SESSION) — join the mesh first with "
            "'claunch mesh join <mesh>'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "to": {
                    "type": "string",
                    "description": "'*', a member handle, or comma-separated handles",
                },
                "body": {"type": "string", "description": "message text"},
                "type": {
                    "type": "string",
                    "description": (
                        "message INTENT (not your role or a label): 'say' "
                        "(default) or 'ask' invite a reply; 'fyi' and 'ack' "
                        "do not — use them for status/acknowledgements so "
                        "peers don't reply-all"
                    ),
                },
                "reply_to": {
                    "type": "string",
                    "description": (
                        "id of the message this answers (ids appear in "
                        "delivery blocks and history)"
                    ),
                },
                "sections": {
                    "type": "object",
                    "description": (
                        "BATCH send: {handle: text} or {handle: {text, type}} "
                        "per-recipient addenda. 'body' becomes the shared "
                        "preamble; each recipient is delivered only body + "
                        "its own section (never another's instructions), and "
                        "the log keeps one composite message. Every section "
                        "key must be in 'to' (or covered by '*'). A section "
                        "'type' overrides the top-level intent for that "
                        "recipient — e.g. fyi for the peer who only needs to "
                        "know, ask for the one who must act."
                    ),
                },
            },
            "required": ["mesh", "to", "body"],
        },
    },
    {
        "name": "members",
        "description": (
            "List a mesh's members: handle, role, machine/session, "
            "reachability, pending message count — plus linked peer daemons "
            "and the relay connection state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"mesh": {"type": "string", "description": "mesh name"}},
            "required": ["mesh"],
        },
    },
    {
        "name": "history",
        "description": "Recent messages in a mesh (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "limit": {"type": "number", "description": "max messages (default 50)"},
            },
            "required": ["mesh"],
        },
    },
]


class MeshMcpError(Exception):
    pass


def _client():
    client = daemon_client.connect()
    if client is None:
        raise MeshMcpError(
            "the claunch daemon is not running (start it with any claunch "
            "session command, or 'claunch daemon start')"
        )
    return client


def _call_tool(name: str, args: dict) -> dict:
    mesh = str(args.get("mesh") or "")
    if not mesh:
        raise MeshMcpError("'mesh' is required")
    if name == "send":
        sender = os.environ.get("CLAUNCH_SESSION")
        if not sender:
            raise MeshMcpError(
                "no $CLAUNCH_SESSION — this tool only works inside a managed "
                "claunch session"
            )
        to_raw = str(args.get("to") or "")
        to = to_raw if to_raw == "*" or "," not in to_raw else [
            t.strip() for t in to_raw.split(",") if t.strip()
        ]
        payload = {
            "from": sender,
            "to": to,
            "body": str(args.get("body") or ""),
            "type": str(args.get("type") or "say"),
        }
        if args.get("reply_to"):
            payload["reply_to"] = str(args["reply_to"])
        if isinstance(args.get("sections"), dict):
            payload["sections"] = args["sections"]
        result = _client().post(f"/api/mesh/{mesh}/messages", payload)
        relay = result.pop("relay", None)
        result["relay"] = _relay_summary(relay)
        return result
    if name == "members":
        info = _client().get(f"/api/mesh/{mesh}")
        return {
            "members": info.get("members", []),
            "peers": info.get("peers", []),
            "relay": _relay_summary(info.get("relay")),
        }
    if name == "history":
        limit = int(args.get("limit") or 50)
        return _client().get(f"/api/mesh/{mesh}/messages?limit={limit}")
    raise MeshMcpError(f"unknown tool {name!r}")


def _relay_summary(relay) -> str:
    from .cli_mesh import relay_line

    return relay_line(relay if isinstance(relay, dict) else None)


def _handle(msg: dict):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "claunch-mesh", "version": __version__},
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            payload = _call_tool(name, args if isinstance(args, dict) else {})
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            return _result(
                msg_id, {"content": [{"type": "text", "text": text}], "isError": False}
            )
        except (MeshMcpError, daemon_client.DaemonClientError) as exc:
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True},
            )
    if msg_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in iter(stdin.readline, b""):
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        response = _handle(msg)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
    return 0
