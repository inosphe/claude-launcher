"""The cflow v2 agent tools (``flow_*``, ``docs/cflow-v2.md`` section 5).

Served by the merged ``claunch mcp`` server next to the v1 cflow tools, whose
names they never share. The run is the one in this process's working
directory under the session's scope (``$CLAUNCH_SESSION``, else ``default``);
every tool takes an optional ``scope`` for the rare driver that works a run
of another slot.
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
    "by a person with 'claunch cflow2 select'."
)

_SCOPE = {
    "type": "string",
    "description": "run slot (session name). Omit: this session's run",
}

TOOLS = [
    {
        "name": "flow_start",
        "description": (
            "Start a cflow v2 run here. 'workflow' is a v2 definition name (found "
            "in .claunch/workflows/v2/, the global workflows/v2/, or the package) "
            "or a path to a v2 .yaml file (for example a generated procedure "
            "under .cflow2/generated/). The definition is compiled and its IR is "
            "snapshotted, so later edits do not change this run. Returns the "
            "same payload as flow_status: the active tasks with rendered prompts, "
            "choices, waits and delegations. Refused while a run is still live "
            "here (continue it with flow_status). A finished run is archived."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow": {"type": "string", "description": "v2 definition name or .yaml path"},
                "params": {"type": "object", "description": "values for the definition's params"},
                "scope": _SCOPE,
                "force": {
                    "type": "boolean",
                    "description": "abort and archive a live run first -- only with the "
                    "user's explicit go-ahead",
                },
            },
            "required": ["workflow"],
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
            "its max and a person must extend it. Several tasks can be active at "
            "once under par/foreach/group: each has its own 'instance' id."
        ),
        "inputSchema": {"type": "object", "properties": {"scope": _SCOPE}},
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
                "scope": _SCOPE,
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
                "scope": _SCOPE,
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
                "scope": _SCOPE,
            },
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
            "parent's run."
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
        "name": "flow_rule",
        "description": (
            "Read one of the run's rules: its text, and 'why' (the incident or "
            "reason behind it, which task prompts do not carry)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "scope": _SCOPE},
            "required": ["name"],
        },
    },
    {
        "name": "flow_abort",
        "description": (
            "Abort the v2 run here (everything active is cancelled and "
            "journaled). Only when the user asked for it or the run cannot "
            "continue; say why in 'reason'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "scope": _SCOPE},
            "required": ["reason"],
        },
    },
]


def _scope(args: dict) -> Any:
    return str(args.get("scope") or "").strip() or None


def call_tool(name: str, args: dict) -> Dict[str, Any]:
    cwd = os.getcwd()
    scope = _scope(args)
    if name == "flow_start":
        from . import layers

        workflow = str(args.get("workflow") or "").strip()
        if not workflow:
            raise runtime.FlowError("'workflow' is required")
        params = args.get("params")
        if params is not None and not isinstance(params, dict):
            raise runtime.FlowError("'params' must be an object")
        chart = layers.load(workflow, cwd)
        return runtime.start(chart, cwd=cwd, scope=scope, params=params,
                             caps=runtime.resolve_caps(), force=bool(args.get("force")))
    if name == "flow_status":
        st = store.RunStore(cwd, scope)
        if st.exists() and st.load_state().get("status") in runtime.LIVE_STATUSES:
            return runtime.wait_tick(probe_runner=runtime.default_probe_runner,
                                     cwd=cwd, scope=scope)
        return runtime.status(cwd=cwd, scope=scope)
    if name == "flow_report":
        return runtime.report(
            str(args.get("node") or "").strip() or None,
            str(args.get("outcome") or ir.OUTCOME_OK), args.get("value"),
            args.get("text"), cwd=cwd, scope=scope,
        )
    if name == "flow_select":
        return runtime.select(
            str(args.get("node") or "").strip() or None, str(args.get("option") or ""),
            args.get("reason"), caller=runtime.CALLER_AGENT, cwd=cwd, scope=scope,
        )
    if name == "flow_fork":
        return runtime.fork(str(args.get("group") or "").strip() or None,
                            args.get("with"), cwd=cwd, scope=scope)
    if name == "flow_delegate":
        # A 'session' delegation is submitted by a child session that stands
        # in its own directory (often its own worktree) under its own name:
        # it names the run's slot explicitly.
        cwd = str(args.get("cwd") or "").strip() or cwd
        did = str(args.get("delegation") or "").strip() or None
        submitting = any(args.get(k) is not None for k in ("outcome", "value", "receipt_path"))
        if not submitting:
            return runtime.delegate_brief(did, cwd=cwd, scope=scope)
        if not did:
            raise runtime.FlowError("submitting needs 'delegation'")
        return runtime.delegate_submit(did, args.get("outcome"), args.get("value"),
                                       args.get("receipt_path"), cwd=cwd, scope=scope)
    if name == "flow_rule":
        return runtime.rule(str(args.get("name") or ""), cwd=cwd, scope=scope)
    if name == "flow_abort":
        return runtime.abort(str(args.get("reason") or ""), cwd=cwd, scope=scope)
    raise runtime.FlowError(f"unknown tool {name!r}")


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
