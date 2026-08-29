"""The cflow half of claunch's MCP surface.

The wire protocol lives in :mod:`claude_launcher.mcp_rpc`; this module is the
tools and what they do. Normally these are served alongside the mesh tools by
one ``claunch mcp`` process (see :mod:`claude_launcher.mcp_server`); the
standalone ``claunch cflow mcp`` entry point remains for installs written
before the servers were merged.

Exposed tools: ``start``, ``report``, ``next``, ``select``, ``status`` for the
run this session drives, ``request_goto`` to ask a person for a position the
workflow declares no route to, ``request_child_goto`` for a leader asking the
same of a DESCENDANT's run (routed through the daemon's approval gate — a
person answers it, or its deadline does), plus ``asks`` and ``answer`` for
decisions *other* sessions' runs are waiting on it for.

``request_goto`` is the one that most needs its name read carefully: it files
a REQUEST and moves nothing. The move it asks for is ``engine.goto``, which
stays a human command — so an off-graph jump costs the agent a stop and a
person's answer, exactly like a gate, instead of being refused with nothing
recorded (which is what a shell ``claunch cflow goto`` gets: the harness deny
rules block it, and the run learns nothing).

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
            "project's copy of a name shadows the global one (or LAYERS over "
            "it with 'extends:', in which case 'extends' names the bases), "
            "and the payload "
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
            "filed. Re-filing overwrites (e.g. after fixing a failed verify). "
            "Both fields are MARKDOWN and are rendered as markdown on the "
            "dashboard: line breaks are kept, so write evidence as '- ' list "
            "items one per line, put commands/paths/test ids in `backticks` "
            "and multi-line output in a fenced block. Not one wall of prose."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "markdown: 2-4 honest sentences on the step's outcome"
                    ),
                },
                "details": {
                    "type": "string",
                    "description": (
                        "optional evidence/specifics as markdown: commands run, "
                        "test names, failure lines, files touched. A '- ' list "
                        "one fact per line, `backticks` for identifiers, a "
                        "fenced ``` block for output that must keep its spacing"
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
                "reason": {
                    "type": "string",
                    # Not a note to the file. On a user-chooser step this is
                    # what the CLI and the dashboard show the person at the
                    # moment they confirm, so it is read as the case for the
                    # option -- 'seems right' asks them to ratify a decision
                    # they were given no way to check.
                    "description": (
                        "why. Journaled, and shown to whoever confirms -- "
                        "give the evidence, not an assertion"
                    ),
                },
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
        "name": "recall",
        "description": (
            "Hand back the full text behind a content id (read-only). A "
            "reminder that has already given you this position's text once "
            "quotes its id instead of pasting it again; call this with that "
            "id when the id is NOT in your context any more, which means the "
            "text is gone with it. Do not call it when you can still see the "
            "text — you already have what this returns. If the id names "
            "something other than this run's current position the call says "
            "so rather than guessing, and 'status' is what re-reads the "
            "position itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "the content id from a block header, e.g. the 'step "
                        "text id' line of a cflow reminder"
                    ),
                }
            },
            "required": ["id"],
        },
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
    {
        "name": "request_goto",
        "description": (
            "Ask a HUMAN to move this run to a step the workflow declares no "
            "transition to — the exit for when reality out-runs the graph (a "
            "merge turns up work belonging to a step already passed, a "
            "finding invalidates a step's outcome). It records a request and "
            "moves nothing: the run stops advancing until a person approves "
            "or refuses, so file it, then STOP YOUR TURN and write them the "
            "decision brief (where the run has to go, what you found that "
            "the workflow declared no route for, what redoing that step "
            "costs, what continuing on the declared route costs, your "
            "recommendation and the weakest part of your case). They answer "
            "with 'claunch cflow goto --approve' / '--deny' or from the "
            "dashboard's workflow panel, and may send the run somewhere else "
            "entirely. You cannot approve it and must not simulate approval. "
            "Not for a route the workflow DOES declare — use 'next'/'select' "
            "there; not for a step you are already on. Pass cancel:true to "
            "withdraw a request whose reason stopped being true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "step": {
                    "type": "string",
                    "description": (
                        "the step id to move to ('end' force-finishes the run)"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "why the declared route cannot carry this — journaled, "
                        "shown on the dashboard, and the whole basis the person "
                        "answering has. Required"
                    ),
                },
                "cancel": {
                    "type": "boolean",
                    "description": (
                        "withdraw the pending request instead of filing one "
                        "('step' and 'reason' are then ignored)"
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "request_child_goto",
        "description": (
            "Ask a HUMAN to move a CHILD session's run to a step its "
            "workflow declares no transition to — the leader's half of "
            "'request_goto', for when you have verified a descendant's run "
            "is parked somewhere wrong (a gate defect, an abandoned branch) "
            "and moving it is not yours to do unilaterally. The request is "
            "filed on the child's run (which then holds, waiting_goto) AND "
            "opened as an approval card in the web UI; an unanswered card "
            "counts as approved after its deadline (5 minutes by default) "
            "and the move is applied. You are told the outcome in your "
            "terminal; the child is nudged. Authority is checked by the "
            "daemon: the target must be a session you spawned or one of its "
            "descendants — never a peer, never your parent, never your own "
            "run (that is plain 'request_goto'). State 'session', 'step' "
            "and 'reason' — the reason is the whole basis the person "
            "answering has: what you verified and how (the git command and "
            "its output, the journal line), not adjectives. To take a "
            "pending request back, pass 'withdraw' with the request id this "
            "tool returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": (
                        "the child session whose run should move (must be in "
                        "your subtree)"
                    ),
                },
                "step": {
                    "type": "string",
                    "description": (
                        "the step id to move the child's run to ('end' "
                        "force-finishes it)"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "why this run must leave the route its workflow "
                        "declares — journaled, shown on the approval card, "
                        "and the whole basis the person answering has. "
                        "Required"
                    ),
                },
                "withdraw": {
                    "type": "string",
                    "description": (
                        "the id of your pending request to take back instead "
                        "of filing a new one ('session'/'step'/'reason' are "
                        "then ignored)"
                    ),
                },
            },
            "required": [],
        },
    },
]

#: Tools that write to the run, and so must be fenced against a replacement.
_MUTATING = ("report", "next", "select", "request_goto")

#: Tools that act on ANOTHER session's run. They are outside the fence in
#: both directions: they are not refused when this slot was replaced (they
#: were never about this slot), and their payloads never re-arm it.
_FOREIGN = ("asks", "answer", "request_child_goto")

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


def _request_child_goto(args: dict) -> dict:
    """The leader's ask, handed to the daemon's goto gate.

    The run to move lives in the CHILD's directory, not this one, and the
    two things that make the request legitimate — that the target is this
    session's descendant, and that a person answered (or let the deadline
    answer) — are both the daemon's to own (daemon/goto_gate.py). So this
    tool is a courier: it names the asker from the environment, posts the
    request, and hands back the gate's record. A daemon that is not running
    is a clear error, not a fabricated hold.
    """
    from .. import daemon_client

    session = _session()
    if not session:
        raise engine.CflowError(
            "this tool runs inside a managed session, and none is named in "
            "this environment — there is no 'who is asking' to file under"
        )
    client = daemon_client.connect()
    if client is None:
        raise engine.CflowError(
            "the daemon is not running, and the goto gate lives in it — "
            "start it ('claunch daemon start') or ask a human to run "
            "'claunch cflow goto <step> -t <child session>' directly"
        )
    withdraw = str(args.get("withdraw") or "").strip()
    try:
        if withdraw:
            resp = client.post(
                f"/api/cflow/goto-requests/{withdraw}/withdraw",
                {"session": session},
            )
            record = (resp or {}).get("request") or {}
            return {
                "status": "child_goto_withdrawn",
                "request": record,
                "note": (
                    "withdrawn; the child's run continues from where it "
                    "stands and is nudged"
                ),
            }
        resp = client.post(
            "/api/cflow/goto-requests",
            {
                "session": session,
                "target_session": str(args.get("session") or ""),
                "step": str(args.get("step") or ""),
                "reason": str(args.get("reason") or ""),
            },
        )
    except daemon_client.DaemonClientError as exc:
        raise engine.CflowError(str(exc))
    record = (resp or {}).get("request") or {}
    return {
        "status": "child_goto_requested",
        "request": record,
        "note": (
            f"filed; the web UI decides for {record.get('target_session')!r}'s "
            f"run, and an unanswered request counts as approved at "
            f"{record.get('deadline') or 'its deadline'}. The child's run is "
            f"held until then. You are told the outcome in this terminal — "
            f"carry on with other work rather than polling, and take the "
            f"request back with 'withdraw' if the reason stops being true"
        ),
    }


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
    elif name == "request_goto":
        if bool(args.get("cancel")):
            payload = engine.cancel_goto_request(by=_session() or "agent")
        else:
            payload = engine.request_goto(
                str(args.get("step") or ""),
                str(args.get("reason") or ""),
                by=_session() or "agent",
            )
    elif name == "request_child_goto":
        payload = _request_child_goto(args)
    elif name == "recall":
        payload = engine.recall(str(args.get("id") or ""))
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
