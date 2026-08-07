"""A minimal MCP (Model Context Protocol) stdio server for cflow.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout — just enough of the MCP
surface (initialize / tools/list / tools/call / ping) for Claude Code and
compatible clients, with no SDK dependency.

Exposed tools: ``start``, ``report``, ``next``, ``select``, ``status``. There
is deliberately **no approve tool** and no user-side select confirmation here:
human gates are only operable via the CLI (``claunch cflow approve|select``),
outside the agent's reach.
"""

from __future__ import annotations

import json
import sys

from .. import __version__
from . import engine, model, state as state_mod

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "start",
        "description": (
            "Start a claunch workflow in the current directory. Returns the "
            "first step. Workflows are YAML files in .claunch/workflows/ "
            "(project) or ~/.claude-launcher/workflows/. Errors if a run is "
            "still active here — resume it instead, or have it archived "
            "first; finished (done/aborted) runs are archived automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow": {
                    "type": "string",
                    "description": "workflow name (file stem) or an explicit .yaml path",
                },
                "context": {
                    "type": "string",
                    "description": "task context carried into the run and its journal",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "abort the active run, archive it (journal included), "
                        "and start fresh — pass only with the user's explicit "
                        "go-ahead, never on your own initiative"
                    ),
                },
            },
            "required": ["workflow"],
        },
    },
    {
        "name": "report",
        "description": (
            "File the completion report for the current step BEFORE advancing: "
            "what actually happened, including failures. Journaled and shown "
            "live on the daemon web dashboard; 'next' is refused until it is "
            "filed. Re-filing overwrites (e.g. after fixing a failed verify)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-4 honest sentences on the step's outcome",
                },
                "details": {
                    "type": "string",
                    "description": (
                        "optional evidence/specifics: commands run, test names, "
                        "failure lines, files touched"
                    ),
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "next",
        "description": (
            "Advance past the current step and receive the next one. Requires "
            "the step's completion report to be filed first (see 'report'), "
            "then runs the step's verify command, if any, refusing to advance "
            "when it fails. Also used to re-fetch the current position after a "
            "gate was approved."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "select",
        "description": (
            "Choose an option at a decision point. When the step's chooser is "
            "'user' this records a proposal only — a human confirms via "
            "'claunch cflow select'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "option": {"type": "string", "description": "one of the offered option names"},
                "reason": {"type": "string", "description": "why (journaled)"},
            },
            "required": ["option"],
        },
    },
    {
        "name": "status",
        "description": (
            "Current run position and state (read-only). Call after being "
            "nudged to see whether a gate/selection was granted."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _call_tool(name: str, args: dict) -> dict:
    if name == "start":
        return engine.start(
            str(args.get("workflow") or ""),
            context=args.get("context") or None,
            force=bool(args.get("force")),
        )
    if name == "report":
        return engine.report(
            str(args.get("summary") or ""), args.get("details") or None
        )
    if name == "next":
        return engine.next_step()
    if name == "select":
        return engine.select(
            str(args.get("option") or ""), args.get("reason") or None, by="agent"
        )
    if name == "status":
        return engine.status()
    raise engine.CflowError(f"unknown tool {name!r}")


def _handle(msg: dict):
    """Return a response dict, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cflow", "version": __version__},
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
        except (
            engine.CflowError,
            model.WorkflowError,
            state_mod.StateError,
        ) as exc:
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True},
            )
    if msg_id is None:
        return None  # unknown notification: ignore
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve() -> int:
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
        response = _handle(msg)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
    return 0
