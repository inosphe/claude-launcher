"""The cflow half of claunch's MCP surface.

The wire protocol lives in :mod:`claude_launcher.mcp_rpc`; this module is the
tools and what they do. Normally these are served alongside the mesh tools by
one ``claunch mcp`` process (see :mod:`claude_launcher.mcp_server`); the
standalone ``claunch cflow mcp`` entry point remains for installs written
before the servers were merged.

Exposed tools: ``start``, ``report``, ``next``, ``select``, ``status``. There
is deliberately **no approve tool** and no user-side select confirmation here:
human gates are only operable via the CLI (``claunch cflow approve|select``),
outside the agent's reach.

This process is one of two writers of a run (the daemon is the other), so it
also carries a **fence**: the run id it last handed to the agent. If the slot
holds a different run when a mutating tool is called — someone archived and
started another one, or forced a start from the dashboard — the call is
refused rather than silently applied to a run this agent has never read.
"""

from __future__ import annotations

from typing import Optional

from .. import mcp_rpc
from . import engine, model, state as state_mod

TOOLS = [
    {
        "name": "start",
        "description": (
            "Start a claunch workflow in the current directory. Returns the "
            "first step. Workflows are YAML files in .claunch/workflows/ "
            "(project) or ~/.claude-launcher/workflows/. Errors if a run is "
            "still active here — resume it instead, or have it archived "
            "first; finished (done/aborted) runs are archived automatically. "
            "Also the way a human's 'pending_start' request (see 'status') is "
            "carried out: you start it, so you always know what you are "
            "running."
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
            "nudged to see whether a gate/selection was granted, and to pick "
            "up a 'pending_start' — a workflow a human asked (from the "
            "dashboard or CLI) for you to start here."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

#: Tools that write to the run, and so must be fenced against a replacement.
_MUTATING = ("report", "next", "select")

#: The run id last handed to this agent. ``None`` = nothing read yet, so the
#: next call adopts whatever is on disk.
_seen_run: Optional[str] = None


def _check_fence(name: str) -> None:
    """Refuse a mutating call aimed at a run this agent has never read.

    Only a *replacement* is fenced. An emptied slot needs no guard: the engine
    already answers "no active cflow run here", which says the same thing and
    is what the protocol handles.
    """
    global _seen_run
    if name not in _MUTATING or _seen_run is None:
        return
    actual = engine.current_run_id()
    if actual is None:
        _seen_run = None
        return
    if actual == _seen_run:
        return
    raise engine.CflowError(
        f"the run you were driving ({_seen_run}) is not the run here any more "
        f"({actual} is) — it was archived and replaced while you worked. "
        f"Nothing was applied. Call 'status' to re-read the current position "
        f"before doing anything else, and tell the user the run changed "
        f"under you."
    )


def call_tool(name: str, args: dict) -> dict:
    global _seen_run
    _check_fence(name)
    if name == "start":
        payload = engine.start(
            str(args.get("workflow") or ""),
            context=args.get("context") or None,
            force=bool(args.get("force")),
        )
    elif name == "report":
        payload = engine.report(
            str(args.get("summary") or ""), args.get("details") or None
        )
    elif name == "next":
        payload = engine.next_step()
    elif name == "select":
        payload = engine.select(
            str(args.get("option") or ""), args.get("reason") or None, by="agent"
        )
    elif name == "status":
        payload = engine.status()
    else:
        raise engine.CflowError(f"unknown tool {name!r}")
    # Every payload that names a run re-arms the fence; 'status' on an idle
    # slot disarms it (there is nothing to be superseded).
    if payload.get("run"):
        _seen_run = str(payload["run"])
    elif name == "status" and payload.get("status") == "idle":
        _seen_run = None
    return payload


SERVER = mcp_rpc.Server(
    name="cflow",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(engine.CflowError, model.WorkflowError, state_mod.StateError),
)


def _handle(msg: dict):
    """Return a response dict, or None for notifications."""
    return SERVER.handle(msg)


def serve() -> int:
    """Blocking stdio loop; returns when stdin closes."""
    return SERVER.serve()
