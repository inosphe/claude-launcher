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

import os
from typing import Any, Dict

from .. import mcp_rpc
from . import ir, runtime, store

AUTHORITY = (
    "cflow v2 (flow_*) tools are the intended agent interface for a v2 workflow "
    "run in this directory: call them within the task without extra human "
    "confirmation. The engine enforces the workflow's structure, guards and "
    "who may choose; a choice whose chooser is 'user' is refused here and taken "
    "by a person with 'claunch cflow2 select'. A task's ask is answered by the "
    "responders it was routed to (flow_answer) or a person ('claunch cflow2 "
    "approve'); flow_approve is accepted only for an ask that fell through to "
    "'otherwise: self'. A halted run refuses every move until a person resumes it. "
    "Aborting a run is the user's ('claunch cflow2 abort'); there is no tool for it."
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
            "once under par/foreach/group: each has its own 'instance' id."
        ),
        "inputSchema": {"type": "object",
                        "properties": {"scope": _SCOPE, "cwd": _CWD, "side": _SIDE}},
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
            "evidence). A choice whose chooser is 'user' is refused: show the "
            "user the options and your recommendation instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "choice instance id or label"},
                "option": {"type": "string"},
                "reason": {"type": "string", "description": "why; journaled"},
                "scope": _SCOPE, "cwd": _CWD, "side": _SIDE,
            },
            "required": ["option"],
        },
    },
    {
        "name": "flow_fork",
        "description": (
            "Add a member to a running group (flow_status 'groups' with "
            "can_fork). The member runs the group's procedure with 'with' as its "
            "params, in parallel with the rest of the run; its tasks appear in "
            "flow_status under their own instance ids. Refused past the group's "
            "limit or once its do-part finished."
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
            "'session' delegation calls this with the 'scope' and 'cwd' of its "
            "parent's run. A delegation whose brief names a 'nested' run is not "
            "submitted here: drive that run with the flow_* tools under its scope. "
            "Only the run's driver submits a subagent delegation and only the "
            "assigned child session a session delegation. "
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
                "reason": {"type": "string"},
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
            "cannot answer its own question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ask_id": {"type": "string"},
                "decision": {"type": "string"},
                "reason": {"type": "string"},
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


def _where(args: dict) -> tuple:
    """``(cwd, scope)`` a tool works on, refused unless the caller may drive
    that run (:func:`runtime.authorize`)."""
    cwd = str(args.get("cwd") or "").strip() or os.getcwd()
    scope = _scope(args)
    runtime.authorize(cwd=cwd, scope=scope, session=_session())
    return cwd, scope


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


def call_tool(name: str, args: dict) -> Dict[str, Any]:
    out = _call(name, args)
    if name in ("flow_start", "flow_status", "flow_report", "flow_select", "flow_fork",
                "flow_call",
                "flow_approve", "flow_delegate", "flow_collection", "flow_input") and isinstance(out, dict) and "run" in out:
        cwd = str(args.get("cwd") or "").strip() or os.getcwd()
        out = _route(cwd, _scope(args), out)
    return out


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
                             driver=session, caller=runtime.CALLER_AGENT, mesh=mesh)
    if name == "flow_status":
        cwd, scope = _where(args)
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
        cwd, scope = _where(args)
        return runtime.select(
            str(args.get("node") or "").strip() or None, str(args.get("option") or ""),
            args.get("reason"), caller=runtime.CALLER_AGENT, cwd=cwd, scope=scope,
        )
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

        return asks.answer_by_id(str(args.get("ask_id") or "").strip(),
                                 str(args.get("decision") or ""), args.get("reason"),
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
                                 session=session, cwd=cwd, scope=scope)
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
    if name == "flow_rule":
        cwd, scope = _where(args)
        return runtime.rule(str(args.get("name") or ""), cwd=cwd, scope=scope)
    raise runtime.FlowError(f"unknown tool {name!r}")


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
