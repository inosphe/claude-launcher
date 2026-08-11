"""A minimal MCP stdio server for mesh: talk to peers, and build the team.

Same newline-delimited JSON-RPC 2.0 subset as :mod:`claude_launcher.cflow.mcp`
(initialize / tools/list / tools/call / ping), no SDK dependency. Spawned by
Claude Code as ``claunch mesh mcp``.

A convenience wrapper around the daemon's HTTP API for agents that prefer
tools over shell, in two groups:

- **Talking** — ``send``, ``members``, ``history``. Sending only: there is
  deliberately no ``recv``, because incoming messages are typed into the
  member's terminal by the daemon, so receiving needs no tool, no polling and
  no cooperation (the design's core invariant; see ``docs/mesh-design.md``).
- **Building** — ``spawn``, ``children``, ``connect``, ``disconnect``. A
  session can create further sessions, enrol them in its mesh and decide who
  they may talk to. What it may spawn is capped by the ``spawn`` policy (see
  :mod:`claude_launcher.spawn`); which edges it may rewire is capped by the
  session tree — an agent rewires only what it spawned.

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
    {
        "name": "spawn",
        "description": (
            "Create a CHILD agent session on this daemon and optionally put "
            "it in a mesh with you. The child inherits your harness, profile "
            "and working directory — you choose who it is, not what it runs. "
            "It starts connected to YOU only: use 'connect' to let it reach "
            "other members. Check your budget with 'children' first; limits "
            "come from the 'spawn' block in ~/.claunch.yaml."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "session name (auto-generated if omitted)",
                },
                "mesh": {
                    "type": "string",
                    "description": (
                        "mesh to enrol the child in — normally one you are "
                        "already a member of, so it can reach you"
                    ),
                },
                "handle": {
                    "type": "string",
                    "description": (
                        "the child's handle in that mesh (default: its "
                        "session name). Its leading word picks the role "
                        "unless 'role' is given"
                    ),
                },
                "role": {
                    "type": "string",
                    "description": (
                        "the child's mesh role, which sets its stance — see "
                        "'claunch mesh roles MESH' for this mesh's vocabulary"
                    ),
                },
                "connect": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "extra member handles the child may message, besides "
                        "you. Everyone else in the mesh is cut off from it"
                    ),
                },
                "workflow": {
                    "type": "string",
                    "description": (
                        "a cflow workflow to start for the child, scoped to "
                        "its own session so the run is its work, not yours"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "context string for that workflow run",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "opening instruction typed into the child once it "
                        "has booted — what it is for, in its own words"
                    ),
                },
                "harness": {
                    "type": "string",
                    "description": (
                        "a different harness (only if spawn.allow_harness "
                        "lists it; otherwise yours is inherited)"
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "children",
        "description": (
            "The sessions you spawned (and theirs), plus how many more you "
            "may spawn and which fields you are allowed to choose. Call this "
            "before spawning rather than provoking a refusal."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "connect",
        "description": (
            "Let two members of a mesh message each other. At least one of "
            "them must be a session you spawned (or a descendant of one) — "
            "you wire up your own team, not somebody else's."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "a": {"type": "string", "description": "one member handle"},
                "b": {"type": "string", "description": "the other member handle"},
            },
            "required": ["mesh", "a", "b"],
        },
    },
    {
        "name": "disconnect",
        "description": (
            "Stop two members of a mesh from messaging each other. Same "
            "restriction as 'connect'. Sends across a cut pair are refused — "
            "there is no routing around it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "a": {"type": "string", "description": "one member handle"},
                "b": {"type": "string", "description": "the other member handle"},
            },
            "required": ["mesh", "a", "b"],
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


def _session() -> str:
    """The calling session's name — the identity every write-side tool needs."""
    sender = os.environ.get("CLAUNCH_SESSION")
    if not sender:
        raise MeshMcpError(
            "no $CLAUNCH_SESSION — this tool only works inside a managed "
            "claunch session"
        )
    return sender


def _call_tool(name: str, args: dict) -> dict:
    # The team-building tools are not scoped to a mesh (a child can be
    # spawned without one), so they are dispatched before the mesh check the
    # messaging tools share.
    if name == "children":
        return _client().get(f"/api/sessions/{_session()}/children")
    if name == "spawn":
        return _spawn(args)

    mesh = str(args.get("mesh") or "")
    if not mesh:
        raise MeshMcpError("'mesh' is required")
    if name in ("connect", "disconnect"):
        a, b = str(args.get("a") or ""), str(args.get("b") or "")
        if not a or not b:
            raise MeshMcpError("'a' and 'b' are both required")
        return _client().patch(
            f"/api/mesh/{mesh}/members/{a}/links/{b}",
            {"enabled": name == "connect", "actor": _session()},
        )
    if name == "send":
        sender = _session()
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
        # Who *you* can actually message, resolved here rather than left for
        # the agent to derive from the edge list — an agent that has to
        # compute its own reachability will sooner or later address a peer it
        # cannot reach and read the refusal as a bug.
        links = info.get("member_links") or []
        me = _my_handle(info.get("members") or [])
        reachable = sorted(
            (edge["b"] if edge["a"] == me else edge["a"])
            for edge in links
            if edge.get("enabled") and me in (edge.get("a"), edge.get("b"))
        )
        return {
            "you": me,
            "reachable": reachable if me else None,
            "members": info.get("members", []),
            "member_links": links,
            "peers": info.get("peers", []),
            "relay": _relay_summary(info.get("relay")),
        }
    if name == "history":
        limit = int(args.get("limit") or 50)
        return _client().get(f"/api/mesh/{mesh}/messages?limit={limit}")
    raise MeshMcpError(f"unknown tool {name!r}")


def _my_handle(members: list) -> str:
    """This session's handle in a mesh, from the roster it just fetched."""
    session = os.environ.get("CLAUNCH_SESSION") or ""
    for member in members:
        if isinstance(member, dict) and member.get("session") == session:
            return str(member.get("handle") or "")
    return ""


#: Keys passed straight through to the spawn endpoint. Listed rather than
#: forwarded wholesale so a mistyped argument is dropped here instead of
#: reaching the daemon as a field it silently ignores.
_SPAWN_KEYS = (
    "name", "mesh", "handle", "role", "connect", "workflow", "context",
    "task", "harness", "profile", "cwd", "args", "env",
)


def _spawn(args: dict) -> dict:
    payload = {k: args[k] for k in _SPAWN_KEYS if args.get(k) not in (None, "")}
    return _client().post(f"/api/sessions/{_session()}/children", payload)


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
