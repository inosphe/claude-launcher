"""The JSON-RPC 2.0 stdio plumbing every claunch MCP server shares.

A newline-delimited subset of MCP — initialize / tools/list / tools/call /
ping — with no SDK dependency. cflow and mesh each grew their own copy of this
loop, identical down to the error envelope; the copies are now one, so a fix to
the wire format cannot land in one server and miss the other.

A :class:`Server` is *what* is exposed (a name, a tool list, a dispatch
function, the exceptions that mean "the agent asked for something impossible"
rather than "the server broke"). :func:`merge` composes several into one, which
is how a single ``claunch mcp`` process serves both feature sets.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROTOCOL_VERSION = "2024-11-05"


class ToolNameCollision(Exception):
    """Two merged servers offer the same tool name.

    Raised at import time rather than left to whichever server the dispatch
    map happened to see last: a silently shadowed tool is a feature that
    stops working with no error anywhere.
    """


@dataclass(frozen=True)
class Server:
    """One MCP surface: its identity, its tools, and how to run them.

    ``errors`` are the exception types that become a tool result with
    ``isError: true`` — a message written for the agent to read and act on.
    Anything else propagates and kills the process, which is the right
    outcome for a bug: a server quietly answering "error" to every call is
    harder to diagnose than one that is visibly gone.
    """

    name: str
    tools: Tuple[dict, ...]
    dispatch: Callable[[str, dict], dict]
    errors: Tuple[type, ...]

    def handle(self, msg: dict) -> Optional[dict]:
        """Return a response dict, or ``None`` for notifications."""
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            from . import __version__

            return _result(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion")
                    or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": __version__},
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": list(self.tools)})
        if method == "tools/call":
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            try:
                payload = self.dispatch(name, args if isinstance(args, dict) else {})
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                return _result(
                    msg_id,
                    {"content": [{"type": "text", "text": text}], "isError": False},
                )
            except self.errors as exc:
                return _result(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": f"error: {exc}"}],
                        "isError": True,
                    },
                )
        if msg_id is None:
            return None  # unknown notification: ignore
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    def serve(self) -> int:
        """Blocking stdio loop; returns when stdin closes."""
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
            response = self.handle(msg)
            if response is not None:
                stdout.write(
                    json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
                )
                stdout.flush()
        return 0


def merge(name: str, servers: Sequence[Server]) -> Server:
    """One server offering every tool of ``servers``, dispatched by name.

    Tool names stay as they are — no prefixing. The agent-facing names are
    written into the skills and into every error message the servers produce,
    and a merge that renamed them would make the docs lie for the sake of a
    namespace nothing is currently competing for. :class:`ToolNameCollision`
    is what keeps that assumption honest as tools are added.
    """
    owner: Dict[str, Server] = {}
    tools: List[dict] = []
    for server in servers:
        for tool in server.tools:
            tool_name = str(tool.get("name") or "")
            if tool_name in owner:
                raise ToolNameCollision(
                    f"tool {tool_name!r} is offered by both "
                    f"{owner[tool_name].name!r} and {server.name!r} — rename one "
                    f"before merging them into {name!r}"
                )
            owner[tool_name] = server
            tools.append(tool)

    def dispatch(tool_name: str, args: dict) -> dict:
        server = owner.get(tool_name)
        if server is None:
            # Raised as the *first* server's error type so the envelope stays
            # an agent-readable tool error rather than a crash.
            raise servers[0].errors[0](f"unknown tool {tool_name!r}")
        return server.dispatch(tool_name, args)

    errors: Tuple[type, ...] = tuple(
        dict.fromkeys(err for server in servers for err in server.errors)
    )
    return Server(name=name, tools=tuple(tools), dispatch=dispatch, errors=errors)


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
