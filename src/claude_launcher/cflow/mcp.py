"""The cflow half of claunch's MCP surface.

The wire protocol lives in :mod:`claude_launcher.mcp_rpc`; this module is the
tools and what they do. Normally these are served alongside the mesh tools by
one ``claunch mcp`` process (see :mod:`claude_launcher.mcp_server`); the
standalone ``claunch cflow mcp`` entry point remains for installs written
before the servers were merged.

Exposed tools: ``start``, ``report``, ``next``, ``select``, ``status`` for the
run this session drives, plus ``asks`` and ``answer`` for decisions *other*
sessions' runs are waiting on it for.

There is deliberately **no approve tool** and no user-side select confirmation
here: human gates are only operable via the CLI (``claunch cflow
approve|select``), outside the agent's reach. ``answer`` is not a way around
that — it decides somebody else's run, never this one, and refuses both a
request that was not put to this session and one from its own run. Which
session is answering is read from the environment, not from the arguments, so
it identifies the process rather than the claim.

This process is one of two writers of a run (the daemon is the other), so it
also carries a **fence**: the run id it last handed to the agent. If the slot
holds a different run when a mutating tool is called — someone archived and
started another one, or forced a start from the dashboard — the call is
refused rather than silently applied to a run this agent has never read.
"""

from __future__ import annotations

import os
from typing import Optional

from .. import mcp_rpc
from . import engine, model, state as state_mod

TOOLS = [
    {
        "name": "start",
        "description": (
            "Start a claunch workflow in the current directory. Returns the "
            "first step, and the file it came from ('source'/'origin' — the "
            "project's copy of a name shadows the global one, and the payload "
            "names what it shadowed). Workflows are YAML files in "
            ".claunch/workflows/ (project) or ~/.claude-launcher/workflows/ "
            "(global, every directory). Errors if a run is "
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
                "mesh": {
                    "type": "string",
                    "description": (
                        "which mesh a delegated decision looks for its "
                        "responders in. Only needed when this session belongs "
                        "to more than one — otherwise the run finds it"
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
    {
        "name": "asks",
        "description": (
            "Decisions OTHER sessions' runs are waiting on YOU for (read-only, "
            "any directory). A workflow below you in the spawn tree reached a "
            "step it does not get to decide — an approval, or which branch to "
            "take — and named your role. Each entry carries the question, the "
            "options you may answer with, and its deadline. Call this when a "
            "message says a run needs a decision, and after any nudge."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "answer",
        "description": (
            "Decide one of the requests from 'asks'. The decision must be one "
            "of that request's declared options, or 'abstain' if you have no "
            "basis to decide — abstaining passes it to whoever is next, which "
            "is the right move when guessing is the alternative. Judge it "
            "yourself against the code, docs and tests; you were asked "
            "precisely because the run does not get to decide it. You cannot "
            "answer a request that was not put to you, nor one from your own "
            "run, and you never receive the asking step's instructions — the "
            "work stays theirs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ask": {"type": "string", "description": "the request id from 'asks'"},
                "decision": {
                    "type": "string",
                    "description": "one of the request's options, or 'abstain'",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "why — journaled, and the only part of your answer "
                        "that is free text. Cite what you checked"
                    ),
                },
            },
            "required": ["ask", "decision"],
        },
    },
]

#: Tools that write to the run, and so must be fenced against a replacement.
_MUTATING = ("report", "next", "select")

#: Tools that act on ANOTHER session's run. They are outside the fence in
#: both directions: they are not refused when this slot was replaced (they
#: were never about this slot), and their payloads never re-arm it.
_FOREIGN = ("asks", "answer")

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


def _session() -> str:
    """This agent's session name, as the daemon exported it.

    The single source of "who is answering". It is read from the process
    environment rather than taken as a tool argument on purpose: an answer
    attributable to whoever claimed it would not be an answer at all (see
    :func:`engine.answer`).
    """
    return str(os.environ.get(state_mod.SESSION_ENV) or "").strip()


def call_tool(name: str, args: dict) -> dict:
    global _seen_run
    _check_fence(name)
    if name == "start":
        payload = engine.start(
            str(args.get("workflow") or ""),
            context=args.get("context") or None,
            force=bool(args.get("force")),
            mesh=args.get("mesh") or None,
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
    elif name == "asks":
        waiting = engine.open_asks(_session())
        payload = {
            "status": "asks",
            "waiting_on_you": waiting,
            "note": (
                "decide each with the 'answer' tool. Check the actual code, "
                "docs or tests before you do — and 'abstain' rather than guess"
            )
            if waiting
            else "nothing is waiting on your decision",
        }
    elif name == "answer":
        payload = engine.answer_ask(
            str(args.get("ask") or ""),
            str(args.get("decision") or ""),
            args.get("reason") or None,
            by_session=_session(),
        )
    else:
        raise engine.CflowError(f"unknown tool {name!r}")
    # Every payload that names a run re-arms the fence; 'status' on an idle
    # slot disarms it (there is nothing to be superseded).
    #
    # `answer` is excluded because the run it names is somebody ELSE's — the
    # one that asked. Adopting that id would fence this agent's own tools
    # against a run it never drove, and the next 'report' here would be
    # refused for a replacement that never happened.
    if name not in _FOREIGN and payload.get("run"):
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
