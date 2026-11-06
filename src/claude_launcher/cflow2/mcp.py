"""The cflow v2 agent tools (``flow_*``, ``docs/cflow-v2.md`` section 5).

Served by the merged ``claunch mcp`` server next to the v1 cflow tools, whose
names they never share. The run is the one in this process's working
directory under the session's scope (``$CLAUNCH_SESSION``, else ``default``).

Authority: the caller is the session in ``$CLAUNCH_SESSION`` (never an
argument). A run records its driver at start; every tool works only a run the
caller drives. ``side`` names one of the session's side runs (slot
``<session>+<side>``, doc 2.9) -- the caller drives those too, and
``flow_start`` with ``side`` starts one beside the main run that declares it.
``scope``/``cwd`` name another run only for the cases the
runtime allows (:func:`.runtime.may_drive`): a nested delegation run the
caller is the delegate of, and -- through ``flow_delegate`` -- a ``session``
delegation the caller was spawned for. ``flow_start`` starts only in the
caller's own slot. Aborting a run is a person's act (``claunch cflow2 abort``)
and has no tool.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .. import mcp_rpc
from . import ir, runtime, store

AUTHORITY = (
    "cflow v2 (flow_*) tools are the intended agent interface for a v2 workflow "
    "run in this directory: call them within the task without "
    "confirmation. The engine enforces the workflow's guards and "
    "who may choose; a choice whose chooser is 'user' is refused to the driver "
    "and taken by a person with 'claunch cflow2 select' -- or, with a parent "
    "session, by that parent via flow_select on the child's scope (flow_gates "
    "lists what your children wait on). A task's ask is "
    "answered by its routed responders (flow_answer) or a person ('claunch "
    "cflow2 approve'); flow_approve only for an ask that fell to "
    "'otherwise: self'. A halted run refuses all moves until a person resumes "
    "it. Aborting is the user's ('claunch cflow2 abort'); no tool exists."
)

_SCOPE = {
    "type": "string",
    "description": "run slot. Omit: this session's run. Name another slot only for a "
    "nested delegation run you were briefed to drive (scope '<parent>~<delegation>')",
}
_SIDE = {
    "type": "string",
    "description": "a side run of this session (the main run's 'sides:' names them, e.g. "
    "'stack'): the tool works that run (slot '<session>+<side>') instead of the main run. "
    "Omit for the main run",
}
_CWD = {
    "type": "string",
    "description": "directory of the run. Omit: this session's directory. Only with a "
    "nested run's scope, when its brief names another directory",
}

TOOLS = [
    {
        "name": "flow_start",
        "description": (
            "Start a cflow v2 run here. 'workflow' is a v2 definition name (found "
            "in .claunch/workflows/v2/, the global workflows/v2/, or the package) "
            "or the path of a generated procedure under .cflow2/generated/ (any "
            "other path is refused). The definition is compiled and its IR is "
            "snapshotted, so later edits do not change this run. Returns the "
            "same payload as flow_status: the active tasks with rendered prompts, "
            "choices, waits and delegations. Refused while a run is still live "
            "here (continue it with flow_status). A finished run is archived. The "
            "run belongs to this session (its driver). With 'side' it starts that "
            "side run of this session instead (the live main run must declare it "
            "under 'sides:'; 'workflow' may be omitted and must match if given); "
            "the side runs until it finishes or the session ends."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow": {"type": "string", "description": "v2 definition name or .yaml path"},
                "side": {"type": "string", "description": "start this session's side run of "
                         "that name (declared by the main run's 'sides:')"},
                "params": {"type": "object", "description": "values for the definition's params"},
                "inputs": {"type": "object", "description": "values for the definition's "
                           "typed start inputs (its 'inputs' schema)"},
                "mesh": {"type": "string", "description": "the mesh the run's asks are "
                         "routed in. Omit: the one team mesh this session is in now, kept "
                         "for the run ('claunch cflow2 mesh <name>' changes it later)"},
                "force": {
                    "type": "boolean",
                    "description": "abort and archive a live run of yours first -- only "
                    "with the user's explicit go-ahead; refused while that run waits on a "
                    "person (a user choice, an ask, a halt, a loop or visit limit)",
                },
            },
        },
    },
    {
        "name": "flow_status",
        "description": (
            "Where the v2 run stands. 'tasks' are the active tasks: do each one "
            "('prompt' is rendered with run data, 'done_when' says when it is "
            "done, 'returns' is the result schema you report against, 'rules' "
            "apply). 'choices' are open decisions with the options their guards "
            "allow now. 'waits' are external conditions the engine polls (this "
            "call checks them). 'delegations' with status 'pending' carry a "
            "'brief' to run in a fresh sub-agent. 'loop_limits' mean a loop hit "
            "its max and a person must extend it. 'person_picks' are choices a "
            "person took for you, newest last: each 'reason' is their note, an "
            "instruction to follow. Several tasks can be active at "
            "once under par/foreach/group: each has its own 'instance' id. "
            "'result' holds completed nodes' {outcome, value} in brief: one entry per "
            "distinct record ('same' lists the other keys holding it), a value field "
            "over 80 characters elided as '<elided: N chars>'; 'results' or 'full' "
            "reads them whole. A rule whose text the task's prompt already carries is "
            "listed by id with 'in_prompt' (flow_rule reads it). Every flow_* call "
            "that returns this payload returns it in brief."
        ),
        "inputSchema": {"type": "object",
                        "properties": {
                            "results": {"type": "array", "items": {"type": "string"},
                                        "description": "result keys (node label or id) "
                                        "to return whole"},
                            "full": {"type": "boolean", "description": "return the "
                                     "whole payload: every result value, rule text "
                                     "and data.inputs"},
                            "scope": _SCOPE, "cwd": _CWD, "side": _SIDE}},
    },
    {
        "name": "flow_report",
        "description": (
            "Report an active task's result and advance the run. 'node' is the "
            "task's instance id (or its name when only one instance is active). "
            "'outcome' must be one the task's 'returns' declares (default 'ok'); "
            "'fail' is always accepted. 'value' must carry that outcome's fields "
            "with the declared types (fields marked '?' are optional). The engine "
            "routes the outcome: a non-ok outcome stops the enclosing sequence "
            "and goes to the try/catch that names it. 'text' is your prose "
            "summary for the journal. Returns the new flow_status payload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "task instance id or task name"},
                "outcome": {"type": "string", "description": "declared outcome (default ok)"},
                "value": {"type": "object", "description": "the outcome's fields"},
                "text": {"type": "string", "description": "markdown summary of what happened"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
        },
    },
    {
        "name": "flow_select",
        "description": (
            "Pick an option of an open choice (flow_status 'choices'). Only "
            "options listed as available can be picked; their guards are "
            "re-checked. A choice with require_reason needs 'reason' (give the "
            "evidence). A choice whose chooser is 'user' is refused to the "
            "driver: show the user the options and your recommendation instead. "
            "The one exception is a child of yours -- name its run with 'scope' "
            "(and 'cwd' when it lives elsewhere) and the choice its driver may "
            "not take is yours to take, as flow_gates lists. Your answer is "
            "journaled as the parent's; the user may still answer it too."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "choice instance id or label"},
                "option": {"type": "string"},
                "reason": {"type": "string", "description": "why; journaled. Always say why as an agent -- what you read and what you saw in it, not the option's name again. Steps downstream read your reason ('end-new-task' takes its new task from it), so when you answer for a child include the work issue id and the evidence. Only a person may leave it out"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
            "required": ["option"],
        },
    },
    {
        "name": "flow_gates",
        "description": (
            "What your child sessions' runs wait on a person for: each live or "
            "held child's open 'chooser: user' choices (prompt, options, which "
            "are available now), a pending move/report request, and a loop or "
            "visit limit. This is the list the delegation exists for -- a child's "
            "end 'new-task'/hold decision and its 'move to round' request are "
            "yours to answer here instead of waiting for the user. Answer a "
            "choice with flow_select(scope=<child run>), a request with "
            "flow_move(scope=<child run>, decision approve|deny). Asks whose "
            "resolution is 'human' are listed for information: those are a "
            "person's, not the parent's. A child that is held counts as live: "
            "hold stops its program, its run and its question stay open, and "
            "your answer wakes it. A child with no live parent-answerable gate "
            "does not appear."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flow_move",
        "description": (
            "Answer a child run's pending move or report request as its parent "
            "session (flow_gates shows the request; name the child's run with "
            "'scope', and 'cwd' when it lives elsewhere). 'approve' performs what "
            "the driver asked -- moving its run to the force_target node, or "
            "reporting a task with the outcome it asked for (the task's verify "
            "still runs) -- and 'deny' leaves the run where it is with your "
            "reason kept for the driver to read. Say why in 'reason'. This is "
            "the only agent-side grant: the driving session itself can never "
            "approve its own request, and no other session can."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["approve", "deny"],
                             "description": "answer the pending request"},
                "reason": {"type": "string", "description": "why; journaled in the run. Always say why as an agent -- what you read and what you saw in it, not the option's name again. Only a person may leave it out"},
                "scope": _SCOPE, "cwd": _CWD,
            },
            "required": ["decision"],
        },
    },
    {
        "name": "flow_fork",
        "description": (
            "Add a member to a running group (flow_status 'groups' with "
            "can_fork). The member runs the group's procedure with 'with' as its "
            "params, in parallel with the rest of the run; its tasks appear in "
            "flow_status under their own instance ids. A group with "
            "'member_executor' delegates each member instead: with 'fork' the "
            "daemon quick-forks you for it (a copy of this conversation in this "
            "checkout) and the copy drives the member; when the copy cannot be "
            "started (fewer than cflow2_fork_min_context tokens left before your "
            "auto-compact, you are a quick-fork yourself, the spawn policy "
            "refuses) the member's tasks come back to you. Refused past the "
            "group's limit or once its do-part finished."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "group instance id or name"},
                "with": {"type": "object", "description": "the member's params"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
        },
    },
    {
        "name": "flow_call",
        "description": (
            "Call a procedure from an active task (doc 2.12). The task must list "
            "it in 'calls' (flow_status task 'calls'; none listed: every call is "
            "refused). The procedure is resolved by name now and starts as a nested "
            "run you drive with the flow_* tools under the scope in 'called.scope' "
            "(flow_status 'calls'). Meanwhile the task waits ('calling'); when the "
            "callee finishes, the task is active again with 'call_result' (outcome, "
            "value) and you report it as usual. One open call per task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "procedure": {"type": "string", "description": "procedure name (from 'calls')"},
                "node": {"type": "string", "description": "the calling task's instance id "
                         "or name (omit when one task is active)"},
                "with": {"type": "object", "description": "the procedure's params"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
            "required": ["procedure"],
        },
    },
    {
        "name": "flow_delegate",
        "description": (
            "Delegated subtrees (executor subagent). Without 'outcome'/'value'/"
            "'receipt_path' this returns the BRIEF of a delegation: the rendered "
            "task prompts in order, the return schema, rules and the route plan. "
            "Run it in a fresh-context sub-agent and give it only the brief. "
            "Then submit the sub-agent's single result with 'outcome' and "
            "'value', or with 'receipt_path' (a JSON file {outcome, value} the "
            "sub-agent wrote). The engine validates it against 'returns': the "
            "first invalid result is refused with the problems (re-run once with "
            "them), the second fails the node. A child session spawned for a "
            "'session' or 'fork' delegation calls this with the 'scope' and 'cwd' of its "
            "parent's run. A delegation whose brief names a 'nested' run is not "
            "submitted here: drive that run with the flow_* tools under its scope. "
            "Only the run's driver submits a subagent delegation and only the "
            "assigned child session a session or fork delegation. "
            "'receipt_path' must be in the run's receipts directory "
            "or this session's $CLAUNCH_SCRATCH."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "delegation": {"type": "string", "description": "delegation id (d1, d2, ...)"},
                "outcome": {"type": "string"},
                "value": {"type": "object"},
                "receipt_path": {"type": "string"},
                "scope": {
                    "type": "string",
                    "description": "run slot (session name). Omit for this session's run; "
                    "a child session running a 'session' delegation passes its parent's "
                    "name, as its opening task says",
                },
                "cwd": {
                    "type": "string",
                    "description": "directory the run lives in. Omit for this session's "
                    "directory; a 'session' delegation's child passes the one its opening "
                    "task names",
                },
            },
        },
    },
    {
        "name": "flow_approve",
        "description": (
            "Answer an ask of your own run (flow_status 'asks') that fell through to "
            "'otherwise: self' -- every candidate group was tried and nobody could "
            "answer, and the workflow leaves the decision to you. Refused for any other "
            "ask: those are answered by the responders they were routed to or by a "
            "person ('claunch cflow2 approve'). For an approval give nothing (enter "
            "the task) or decline=true (the task finishes with its on_decline "
            "outcome); for an ask with options give 'option'. Say why in 'reason'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "ask's task instance id or name"},
                "option": {"type": "string", "description": "the option (asks with options)"},
                "decline": {"type": "boolean"},
                "reason": {"type": "string", "description": "why; journaled. Always say why as an agent -- what you read and what you saw in it, not the option's name again. Only a person may leave it out"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
        },
    },
    {
        "name": "flow_answer",
        "description": (
            "Answer a cflow2 question another session routed to you (a mesh 'decide' "
            "message naming an ask2-... id; flow_asks lists them). 'decision' is one of "
            "the options the question lists (approve/decline for an approval), or "
            "'abstain' when you have no basis to decide -- it then goes to the next "
            "candidate group. Judge it yourself; 'reason' is journaled in the asking "
            "run. Your identity is your session, not an argument; the asking session "
            "cannot answer its own question. An 'ask-...' id is a cflow v1 ask and "
            "is answered through the v1 'answer' tool's gate either way."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ask_id": {"type": "string"},
                "decision": {"type": "string"},
                "reason": {"type": "string", "description": "why; journaled in the asking run. Always say why as an agent -- what you read and what you saw in it, not the option's name again. Only a person may leave it out"},
            },
            "required": ["ask_id", "decision"],
        },
    },
    {
        "name": "flow_asks",
        "description": (
            "cflow2 questions waiting on this session's answer (from other sessions' "
            "runs): ask id, prompt, options, deadline, the asking session. Answer each "
            "with flow_answer."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flow_collection",
        "description": (
            "Read or change the run's collections (records the workflow declares under "
            "'collections:', e.g. a landing queue or a signal log). action 'list' returns "
            "every collection with its records, counts and cursors. 'push' adds 'item' "
            "(a queue replaces the record with the same key and resets its status), "
            "'set' changes 'fields' of the queue record 'key' (status included), "
            "'remove' deletes record 'key'. You act as the run's driver: the "
            "collection's 'access' decides what the driver may do (a status reserved "
            "for the daemon, such as 'landed', is refused). Fields must match the "
            "declared item types."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "push", "set", "remove"]},
                "collection": {"type": "string", "description": "collection name "
                               "(push/set/remove; list: optional filter)"},
                "key": {"description": "the queue record's key (set, remove)"},
                "item": {"type": "object", "description": "the record to push"},
                "fields": {"type": "object", "description": "fields to change (set)"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
            "required": ["action"],
        },
    },
    {
        "name": "flow_input",
        "description": (
            "Write one start input of this running run (data.inputs.<name>, read by "
            "prompts as ${data.inputs.<name>}). Only an input whose declaration has "
            "'set_by: [agent]' and that is still empty is written; a filled one is "
            "refused with the value it holds (nothing overwrites, including a value "
            "the daemon put in at start). The value is checked like a start input "
            "(type, enum choices, input_constraints). Only the run's driver may "
            "write. Returns the new flow_status payload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "the input's name"},
                "value": {"description": "the value (text, number or list, as declared)"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
            "required": ["name", "value"],
        },
    },
    {
        "name": "flow_request_move",
        "description": (
            "Ask a person to move this run to a node the definition marks force_target "
            "(e.g. a worker's 'round', which starts a new round from intake). Use it when "
            "the run has to go somewhere its route does not lead -- typically a NEW task "
            "arriving while the run waits on something else (an end decision, a user "
            "choice). It files a REQUEST and moves nothing: until a person answers, "
            "flow_report/flow_select/flow_fork/flow_approve are refused. Then stop your "
            "turn and write the person a decision brief. They answer from the run page "
            "(Move / Refuse) or 'claunch cflow2 move --approve|--deny'. 'cancel: true' "
            "withdraws your pending request. Doing the new work outside the run instead "
            "is what this tool exists to replace. With 'outcome' it asks for the other "
            "thing only a person may do: report one of your ACTIVE tasks ('node' is its "
            "instance id or label) with that outcome and 'value' -- e.g. a task whose "
            "verify failed for a reason outside the work. The task's verify still runs "
            "on their grant. Use this instead of writing the user a "
            "'claunch cflow2 report ...' line to type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string",
                         "description": "the force_target node's id or label; with "
                                        "'outcome', the active task's instance id or label"},
                "reason": {"type": "string",
                           "description": "why the run must go there, or why the task "
                                          "cannot be reported the normal way, with the "
                                          "evidence (required): the person answering "
                                          "did not watch you work"},
                "outcome": {"type": "string",
                            "description": "ask for a person's report of the task 'node' "
                                           "with this declared outcome instead of a move"},
                "value": {"type": "object",
                          "description": "the outcome's fields (with 'outcome')"},
                "cancel": {"type": "boolean",
                           "description": "withdraw your pending request instead"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
        },
    },
    {
        "name": "flow_nudge",
        "description": (
            "Raise this session's run on the user's dashboard notification channel (a "
            "corner card, a row in the notification history, the bell's waiting count) "
            "when it waits on something only a person releases and they may not be "
            "watching this terminal: a choice whose chooser is 'user' (e.g. an end "
            "decision where a new task must be taken in), an ask that fell to a person, "
            "your flow_request_move, a loop/visit limit, a halt -- or a cflow v1 run's "
            "waiting_selection/waiting_approval. Works for v1 and v2 runs. Refused when "
            "the run waits on no person. Write your decision brief in the terminal "
            "first; 'message' is the card's one or two lines: what to decide and your "
            "recommendation. One card per run: asking again within a few minutes for the "
            "same thing only updates its message, and the card settles by itself once "
            "the person acts. Then stop your turn and wait."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "what the person is asked to do, in one or two "
                                           "lines (required): the decision and your "
                                           "recommendation"},
                "side": _SIDE,
            },
            "required": ["message"],
        },
    },
    {
        "name": "flow_rule",
        "description": (
            "Read one of the run's rules: its text, and 'why' (the incident or "
            "reason behind it, which task prompts do not carry)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "scope": _SCOPE, "cwd": _CWD, "side": _SIDE},
            "required": ["name"],
        },
    },
]


def _scope(args: dict) -> Any:
    """The slot a call names: ``side`` (this session's side run) or
    ``scope``; None for the session's own slot."""
    side = str(args.get("side") or "").strip()
    scope = str(args.get("scope") or "").strip() or None
    if not side:
        return scope
    if scope:
        raise runtime.FlowError("give 'side' or 'scope', not both")
    return store.side_scope(store.normalize_scope(None), side)


def _session() -> str:
    return os.environ.get(store.SESSION_ENV, "").strip()


def _input_catalog() -> Any:
    """The catalog that checks inputs with ``options:`` (claunch-iq1xc),
    with this session's profile read from its environment (the MCP server
    runs in the calling session)."""
    from .. import input_options

    return input_options.Catalog(session_profile=input_options.profile_from_env())


def _where(args: dict) -> tuple:
    """``(cwd, scope)`` a tool works on, refused unless the caller may drive
    that run (:func:`runtime.authorize`)."""
    cwd = str(args.get("cwd") or "").strip() or os.getcwd()
    scope = _scope(args)
    runtime.authorize(cwd=cwd, scope=scope, session=_session())
    return cwd, scope


def _where_gate(args: dict) -> tuple:
    """``(cwd, scope, authority)`` for a tool the driver's parent may also
    call (claunch-vjp4y): the same run the driver works, or a child's run the
    caller is the parent of. ``authority`` is ``runtime.CALLER_AGENT`` for the
    driver and ``runtime.CALLER_PARENT`` for the parent; any other session is
    refused by :func:`runtime.authorize` as before."""
    cwd = str(args.get("cwd") or "").strip() or os.getcwd()
    scope = _scope(args)
    authority = runtime.authorize(cwd=cwd, scope=scope, session=_session(), gate=True)
    return cwd, scope, authority


#: The line typed into a child's session after its parent answered one of its
#: gates (claunch-vjp4y). The child's turn had ended -- that is why the gate
#: was open -- and filing the answer in its run files does not wake it.
PARENT_PICKED = (
    "cflow2: your parent session {parent} answered this run's gate "
    "({what}) -- call the flow_status tool and continue (person_picks keeps "
    "the pick and its reason)"
)


def _wake_driver(cwd: str, scope: Any, message: str) -> None:
    """Ask the daemon to type ``message`` into the run's session. Best effort:
    without a daemon, or with the driver gone, the answer is recorded and the
    driver reads it on its next status."""
    from . import asks

    try:
        asks.nudge_driver(message, cwd=cwd, scope=scope)
    except Exception:  # noqa: BLE001 - the answer stands either way
        pass


def _daemon():
    """A client for the running daemon, or None. The parent edge lives in the
    session registry, so the parent-aware calls ask it -- and lose only the
    delegation when it cannot be reached, never the call itself."""
    from .. import daemon_client

    try:
        return daemon_client.connect()
    except Exception:  # noqa: BLE001 - a missing daemon is an ordinary answer here
        return None


def _daemon_parent(session: str) -> str:
    """The session that spawned ``session``, from the daemon's registry."""
    if not session:
        return ""
    client = _daemon()
    if client is None:
        return ""
    try:
        info = client.get(f"/api/sessions/{session}")
    except Exception:  # noqa: BLE001 - unknown session, unreachable daemon
        return ""
    parent = info.get("parent") if isinstance(info, dict) else None
    return str(parent or "").strip()


def _live_children(session: str):
    """``[(name, cwd)]`` of ``session``'s children that are still live or
    held, in the daemon's own order. Empty when there is no daemon or no
    session."""
    client = _daemon()
    if client is None or not session:
        return []
    try:
        answer = client.get(f"/api/sessions/{session}/children")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in (answer or {}).get("children") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        # A held child is live in the sense that matters here: its cflow run
        # is running and its question is open, only its program is stopped
        # (daemon/cflow2_triggers.live_session(include_held=True) reads the
        # same pair). Its gate must reach the parent, which is what wakes it
        # (claunch-7vf2z).
        held = bool(row.get("held"))
        ended = str(row.get("status") or "") in ("exited", "archived")
        if not name or (ended and not held):
            continue
        cwd = str(row.get("cwd") or "").strip()
        if not cwd:
            try:
                info = client.get(f"/api/sessions/{name}")
                cwd = str((info or {}).get("cwd") or "").strip()
            except Exception:  # noqa: BLE001
                cwd = ""
        if cwd:
            out.append((name, cwd))
    return out


def _child_gates(session: str) -> Dict[str, Any]:
    """What ``session``'s live or held children's runs wait on a person for
    (``flow_gates``). Read from each child's own run files, the same payload
    the child's ``flow_status`` shows; only the parts a parent may answer (or
    must know) are carried."""
    children = []
    for name, cwd in _live_children(session):
        try:
            payload = runtime.status(cwd=cwd, scope=name, tick=False)
        except Exception:  # noqa: BLE001 - a slot with no run, an unreadable one
            continue
        if payload.get("status") not in runtime.LIVE_STATUSES:
            continue
        choices = []
        for c in payload.get("choices") or []:
            if (c.get("chooser") or ir.CHOOSER_AGENT) != ir.CHOOSER_USER:
                continue
            unavailable = {str(u) for u in c.get("unavailable") or []}
            choices.append({
                "instance": c.get("instance"), "node": c.get("node"),
                "label": c.get("label"), "prompt": c.get("prompt"),
                "require_reason": bool(c.get("require_reason")),
                "options": [{"name": o.get("name"), "description": o.get("description"),
                             "available": str(o.get("name")) not in unavailable}
                            for o in c.get("options") or []],
            })
        move_req = payload.get("move_request")
        if isinstance(move_req, dict) and move_req.get("decision"):
            move_req = None
        asks = [{"instance": a.get("instance"), "label": a.get("label"),
                 "resolution": a.get("resolution"),
                 "options": [o.get("name") for o in a.get("options") or []],
                 "how": a.get("how")}
                for a in payload.get("asks") or []]
        gates = {
            "child": name, "cwd": cwd, "run": payload.get("run"),
            "workflow": payload.get("workflow"), "status": payload.get("status"),
            "choices": choices,
            "move_request": move_req,
            "asks": asks,
            "loop_limits": payload.get("loop_limits") or [],
            "visit_limits": payload.get("visit_limits") or [],
        }
        # A loop or visit limit is answered by a person (`claunch cflow2
        # extend`), and an ask that fell to 'human' is a person's: both are
        # listed so the parent knows why the child is stuck, not as something
        # it can grant.
        if not (choices or move_req or asks or gates["loop_limits"]
                or gates["visit_limits"]):
            continue
        children.append(gates)
    return {"session": session or None, "children": children,
            "note": ("answer a choice with flow_select(node, option, reason, scope=<child>), "
                     "a move/report request with flow_move(decision approve|deny, "
                     "scope=<child>, reason=...). An ask whose resolution is 'human' and a "
                     "loop or visit limit are a person's: tell the user, no tool takes them")}


def _route(cwd: str, scope: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Put a new ask's question to its responders now rather than on the
    daemon clock's next tick; re-read the status if anything moved."""
    from . import asks

    try:
        if asks.needs_routing(cwd, scope):
            asks.route(cwd=cwd, scope=scope)
            return runtime.status(cwd=cwd, scope=scope, tick=False)
    except (runtime.FlowError, store.StoreError):
        pass
    return payload


def _foreign_run(args: dict) -> bool:
    """Whether the call names a run this session does not drive and only
    reads -- a parent's look at its child's run (claunch-vjp4y). Such a read
    routes nothing: the child's asks are the child's clock's business, not
    the reader's."""
    scope = _scope(args)
    if scope is None:
        return False
    cwd = str(args.get("cwd") or "").strip() or os.getcwd()
    try:
        state = store.RunStore(cwd, scope).load_state()
    except Exception:  # noqa: BLE001 - no run there: nothing to route either
        return False
    return not runtime.may_drive(state, _session())


def call_tool(name: str, args: dict) -> Dict[str, Any]:
    out = _call(name, args)
    if name in ("flow_start", "flow_status", "flow_report", "flow_select", "flow_fork",
                "flow_move", "flow_call",
                "flow_approve", "flow_delegate", "flow_collection", "flow_input") and isinstance(out, dict) and "run" in out:
        if not _foreign_run(args):
            cwd = str(args.get("cwd") or "").strip() or os.getcwd()
            out = _route(cwd, _scope(args), out)
        if not args.get("full"):
            keep = args.get("results") if name == "flow_status" else None
            out = brief(out, keep=keep if isinstance(keep, list) else ())
    return out


# A value field longer than this (as JSON) is elided in brief payloads.
BRIEF_LIMIT = 80


def _brief_field(v: Any) -> Any:
    text = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return v if len(text) <= BRIEF_LIMIT else f"<elided: {len(text)} chars>"


def brief(payload: Dict[str, Any], keep: Any = ()) -> Dict[str, Any]:
    """The status payload an agent tool returns (claunch-ejvoy.1): the run's
    status with completed results in brief and nothing carried twice.

    The driver gets this payload after every flow_* call, and on a long run
    ``result`` re-sent each completed node's whole value, several times over:
    a node's record is stored under its label and again under each enclosing
    node it completes (``prior-context``, ``round/prior-search.try``,
    ``prior-search``). Here each distinct record appears once, under its
    first label key (else its first key), with ``same`` naming the others; a value field over
    :data:`BRIEF_LIMIT` is elided (``by``/``decision`` of an answered ask stay
    readable). Keys in ``keep`` stay whole, and so does a record whose outcome
    is not ``ok``: a wait's timeout carries the checks the next prompt reads
    (improv-leader-v2 reflect-pregate reads ``deployed-wait``'s exit and
    output from flow_status), a failure its reason. A rule whose text the task prompt
    already carries (the compiler appends it under ``── 규칙 ──``) keeps only
    its id. ``data.inputs`` is dropped when it equals the top-level
    ``inputs``. :func:`runtime.status` itself is unchanged: the web UI, CLI
    and daemon read the whole payload."""
    out = dict(payload)
    keep = {str(k) for k in keep or ()}
    result = out.get("result")
    if isinstance(result, dict) and result:
        groups: Dict[str, list] = {}
        for key, rec in result.items():
            groups.setdefault(json.dumps(rec, ensure_ascii=False, sort_keys=True), []).append(key)
        slim: Dict[str, Any] = {}
        elided = False
        for keys in groups.values():
            # a label (`peer-review`) before an id path (`round/land/...`):
            # prompts name results by label
            keys.sort(key=lambda k: "/" in k or "." in k)
            rec = result[keys[0]]
            if (keep.intersection(keys) or not isinstance(rec, dict)
                    or rec.get("outcome", ir.OUTCOME_OK) != ir.OUTCOME_OK):
                entry = dict(rec) if isinstance(rec, dict) else {"value": rec}
            else:
                entry = {k: v for k, v in rec.items() if k != "value"}
                value = rec.get("value")
                if isinstance(value, dict):
                    entry["value"] = {k: _brief_field(v) for k, v in value.items()}
                else:
                    entry["value"] = _brief_field(value)
                elided = elided or entry["value"] != value
            if len(keys) > 1:
                entry["same"] = keys[1:]
            slim[keys[0]] = entry
        out["result"] = slim
        if elided:
            out["result_elided"] = ("values marked '<elided: N chars>' are cut; "
                                    "flow_status(results=[key]) or full=true returns them")
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        out["tasks"] = [_brief_task(t) for t in tasks]
    data = out.get("data")
    if isinstance(data, dict) and "inputs" in data and data.get("inputs") == out.get("inputs"):
        out["data"] = {k: v for k, v in data.items() if k != "inputs"}
    return out


def _brief_task(task: Any) -> Any:
    if not isinstance(task, dict) or not task.get("rules"):
        return task
    prompt = str(task.get("prompt") or "")
    rules = []
    for rule in task["rules"]:
        text = (rule or {}).get("text") if isinstance(rule, dict) else None
        if text and text.strip() and text.strip() in prompt:
            rules.append({"id": rule.get("id"), "in_prompt": True})
        else:
            rules.append(rule)
    return {**task, "rules": rules}


def _call(name: str, args: dict) -> Dict[str, Any]:
    session = _session()
    if name == "flow_start":
        cwd = os.getcwd()
        own = store.normalize_scope(None)
        side = str(args.get("side") or "").strip()
        if side:
            return _start_side(side, args, cwd, own, session)
        scope = _scope(args)
        if scope and store.normalize_scope(scope) != own:
            raise runtime.FlowError(
                f"a run starts in this session's own slot ({own!r}); scope {scope!r} is "
                f"another's")
        if args.get("cwd"):
            raise runtime.FlowError("a run starts in this session's own directory")
        workflow = str(args.get("workflow") or "").strip()
        if not workflow:
            raise runtime.FlowError("'workflow' is required")
        params = args.get("params")
        if params is not None and not isinstance(params, dict):
            raise runtime.FlowError("'params' must be an object")
        inputs = args.get("inputs")
        if inputs is not None and not isinstance(inputs, dict):
            raise runtime.FlowError("'inputs' must be an object")
        force = bool(args.get("force"))
        if force:
            runtime.authorize(cwd=cwd, scope=own, session=session)
        chart = runtime.load_definition(workflow, cwd)
        mesh = str(args.get("mesh") or "").strip()
        if not mesh:
            from . import asks

            mesh = asks.home_mesh(session, cwd=cwd)
        return runtime.start(chart, cwd=cwd, scope=own, params=params, inputs=inputs,
                             caps=runtime.resolve_caps(), force=force,
                             driver=session, caller=runtime.CALLER_AGENT, mesh=mesh,
                             catalog=_input_catalog(),
                             driver_parent=_daemon_parent(session))
    if name == "flow_status":
        cwd, scope, authority = _where_gate(args)
        if authority == runtime.CALLER_PARENT:
            # a parent reads its child's run to decide its gates; ticking it
            # would let the reader advance somebody else's run
            return runtime.status(cwd=cwd, scope=scope, tick=False)
        st = store.RunStore(cwd, scope)
        if st.exists() and st.load_state().get("status") in runtime.LIVE_STATUSES:
            return runtime.wait_tick(probe_runner=runtime.default_probe_runner,
                                     cwd=cwd, scope=scope)
        return runtime.status(cwd=cwd, scope=scope)
    if name == "flow_report":
        cwd, scope = _where(args)
        return runtime.report(
            str(args.get("node") or "").strip() or None,
            str(args.get("outcome") or ir.OUTCOME_OK), args.get("value"),
            args.get("text"), cwd=cwd, scope=scope,
        )
    if name == "flow_select":
        cwd, scope, authority = _where_gate(args)
        parent = authority == runtime.CALLER_PARENT
        out = runtime.select(
            str(args.get("node") or "").strip() or None, str(args.get("option") or ""),
            args.get("reason"),
            caller=runtime.CALLER_PARENT if parent else runtime.CALLER_AGENT,
            cwd=cwd, scope=scope, by=session if parent else None,
        )
        if parent and not out.get("held"):
            _wake_driver(cwd, scope, PARENT_PICKED.format(
                parent=session or "its parent", what=str(args.get("option") or "")))
        return out
    if name == "flow_gates":
        return _child_gates(session)
    if name == "flow_move":
        cwd, scope, authority = _where_gate(args)
        if authority != runtime.CALLER_PARENT:
            raise runtime.FlowError(
                "flow_move answers a child run's move/report request as its parent; this "
                "run is yours, so its request is not something you may grant. Present it "
                "to the user ('claunch cflow2 move --approve|--deny'), or answer a child's "
                "with scope=<child run>")
        out = runtime.resolve_move(
            str(args.get("decision") or ""), reason=args.get("reason"), by=session,
            cwd=cwd, scope=scope, caller=runtime.CALLER_PARENT,
        )
        req = out.get("move_request") or {}
        what = ("report of " if req.get("kind") == runtime.MOVE_KIND_REPORT
                else "move to ")
        _wake_driver(cwd, scope, PARENT_PICKED.format(
            parent=session or "its parent",
            what=f"{req.get('decision') or 'answered'}: {what}"
                 f"{req.get('label') or req.get('node') or ''}"))
        return out
    if name == "flow_fork":
        cwd, scope = _where(args)
        return runtime.fork(str(args.get("group") or "").strip() or None,
                            args.get("with"), cwd=cwd, scope=scope)
    if name == "flow_call":
        cwd, scope = _where(args)
        return runtime.call(str(args.get("procedure") or ""),
                            str(args.get("node") or "").strip() or None,
                            args.get("with"), cwd=cwd, scope=scope)
    if name == "flow_delegate":
        # A 'session' delegation is submitted by a child session that stands
        # in its own directory (often its own worktree) under its own name:
        # it names the run's slot explicitly. The runtime checks it is that
        # delegation's assignee (or, for a subagent, the run's driver).
        cwd = str(args.get("cwd") or "").strip() or os.getcwd()
        scope = _scope(args)
        did = str(args.get("delegation") or "").strip() or None
        submitting = any(args.get(k) is not None for k in ("outcome", "value", "receipt_path"))
        if not submitting:
            return runtime.delegate_brief(did, cwd=cwd, scope=scope, session=session,
                                          via=runtime.VIA_MCP)
        if not did:
            raise runtime.FlowError("submitting needs 'delegation'")
        return runtime.delegate_submit(did, args.get("outcome"), args.get("value"),
                                       args.get("receipt_path"), cwd=cwd, scope=scope,
                                       session=session, via=runtime.VIA_MCP)
    if name == "flow_approve":
        cwd, scope = _where(args)
        option = str(args.get("option") or "").strip() or None
        return runtime.approve(
            str(args.get("node") or "").strip() or None, decision=option,
            decline=bool(args.get("decline")), reason=args.get("reason"),
            caller=runtime.CALLER_AGENT, cwd=cwd, scope=scope)
    if name == "flow_answer":
        from . import asks

        ask_id = str(args.get("ask_id") or "").strip()
        decision = str(args.get("decision") or "")
        if ask_id and not ask_id.startswith(asks.ID_PREFIX):
            # A cflow v1 ask id answered through this tool. The family is in
            # the id, so hand it to the v1 gate: otherwise the responder is
            # told a request that is open and waiting on it "moved past" it
            # (claunch-c3vq4 — the mirror of the v1 'answer' tool's case).
            from ..cflow import engine as v1_engine

            return v1_engine.answer_ask(ask_id, decision, args.get("reason"),
                                        by_session=session)
        return asks.answer_by_id(ask_id, decision, args.get("reason"),
                                 by_session=session)
    if name == "flow_asks":
        from . import asks

        return {"session": session or None, "asks": asks.pending_for(session)}
    if name == "flow_collection":
        cwd, scope = _where(args)
        action = str(args.get("action") or "list").strip()
        col = str(args.get("collection") or "").strip()
        if action == "list":
            view = runtime.collection_view(cwd, scope)
            if col:
                cols = view.get("collections") or {}
                if col not in cols:
                    raise runtime.FlowError(f"no collection {col!r} in this run "
                                            f"(declared: {', '.join(cols) or 'none'})")
                view["collections"] = {col: cols[col]}
            return view
        if action not in runtime.COLLECTION_OPS:
            raise runtime.FlowError(f"unknown action {action!r} (list, push, set, remove)")
        if not col:
            raise runtime.FlowError(f"{action} needs 'collection'")
        if action == "push" and not isinstance(args.get("item"), dict):
            raise runtime.FlowError("push needs 'item' (an object)")
        if action == "set" and not isinstance(args.get("fields"), dict):
            raise runtime.FlowError("set needs 'fields' (an object)")
        if action in ("set", "remove") and args.get("key") is None:
            raise runtime.FlowError(f"{action} needs 'key'")
        return runtime.collection_op(action, col, cwd=cwd, scope=scope,
                                     principal=runtime.PR_DRIVER, session=session,
                                     key=args.get("key"), item=args.get("item"),
                                     fields=args.get("fields"))
    if name == "flow_input":
        cwd, scope = _where(args)
        return runtime.set_input(str(args.get("name") or "").strip(), args.get("value"),
                                 session=session, cwd=cwd, scope=scope,
                                 catalog=_input_catalog())
    if name == "flow_request_move":
        cwd, scope = _where(args)
        by = session or runtime.CALLER_AGENT
        if args.get("cancel"):
            return runtime.cancel_move_request(by=by, cwd=cwd, scope=scope)
        node = str(args.get("node") or "").strip()
        if not node:
            raise runtime.FlowError("'node' is required (a force_target node's id or label, "
                                    "or with 'outcome' the active task's)")
        outcome = args.get("outcome")
        if outcome is not None:
            outcome = str(outcome).strip()
        return runtime.request_move(node, str(args.get("reason") or ""), by=by,
                                    cwd=cwd, scope=scope, outcome=outcome,
                                    value=args.get("value"))
    if name == "flow_nudge":
        return _nudge(args, session)
    if name == "flow_rule":
        cwd, scope = _where(args)
        return runtime.rule(str(args.get("name") or ""), cwd=cwd, scope=scope)
    raise runtime.FlowError(f"unknown tool {name!r}")


def _nudge(args: dict, session: str) -> Dict[str, Any]:
    """``flow_nudge``: ask the daemon to raise this session's own run (main
    or a side run) on the dashboard (``daemon/flow_nudge.py``). The daemon
    reads what the run owes a person from its files, v1 or v2 alike, and
    refuses when it owes nothing."""
    from .. import daemon_client
    from ..daemon import flow_nudge

    if not session:
        raise runtime.FlowError("flow_nudge works inside a managed claunch session "
                                "($CLAUNCH_SESSION is not set)")
    message = str(args.get("message") or "").strip()
    if not message:
        raise runtime.FlowError("'message' is required: what the person is asked to do")
    side = str(args.get("side") or "").strip()
    scope = store.side_scope(store.normalize_scope(None), side) if side else session
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise runtime.FlowError(f"cannot reach the claunch daemon: "
                                f"{daemon_client.unreachable_reason(why)}")
    try:
        result = client.post("/api/flow-nudges", {
            "session": session, "cwd": os.getcwd(), "scope": scope, "message": message,
        })
    except daemon_client.DaemonClientError as exc:
        raise runtime.FlowError(f"nudge not raised: {exc}") from None
    return flow_nudge.reply(result)


def _start_side(side: str, args: dict, cwd: str, own: str, session: str) -> Dict[str, Any]:
    if args.get("scope") or args.get("cwd"):
        raise runtime.FlowError("a side run starts beside this session's own main run; "
                                "give 'side' without 'scope' or 'cwd'")
    if store.side_of(own):
        raise runtime.FlowError("a side run is started from the session's main run")
    for key in ("params", "inputs"):
        if args.get(key) is not None and not isinstance(args.get(key), dict):
            raise runtime.FlowError(f"'{key}' must be an object")
    runtime.authorize(cwd=cwd, scope=own, session=session)
    force = bool(args.get("force"))
    if force:
        runtime.authorize(cwd=cwd, scope=store.side_scope(own, side), session=session)
    return runtime.start_side(side, cwd=cwd, main=own,
                              workflow=str(args.get("workflow") or "").strip() or None,
                              params=args.get("params"), inputs=args.get("inputs"),
                              caps=runtime.resolve_caps(), force=force, driver=session,
                              caller=runtime.CALLER_AGENT)


def _errors() -> tuple:
    from . import expr, frontend

    return (runtime.FlowError, store.StoreError, frontend.FrontendError, ir.IRError,
            expr.ExprError)


SERVER = mcp_rpc.Server(
    name="cflow2",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=_errors(),
    instructions=AUTHORITY,
)
