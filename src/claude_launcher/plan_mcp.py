"""MCP tools for session plans (claunch-7jzp8).

Thin clients of the daemon's ``/api/plan`` routes. A session writes its plan
into its dashboard plan pane -- steps of one to a few lines, decisions with
an expected effect and a risk per option, and the closure items (checks a
person makes by hand, followup candidates) that keep it from closing. The
user decides there, and each decision is typed back as a ``[claunch plan]``
block. The daemon keeps the plan and decides who may write it; every call
carries ``$CLAUNCH_SESSION``.

There is no INSTRUCTIONS paragraph: the merged server instructions sit at
the client's budget (tests/test_mcp_instructions_packs.py), so the guidance
is in the tool descriptions below and in the workflows that call for a plan.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc

_NODE_COMMON = {
    "id": {"type": "string", "description": "stable id (letters, digits, _ . -); reuse the ids plan_get returns "
                                            "when you rewrite the plan, or the user's decisions are lost"},
    "title": {"type": "string", "description": "one line the user reads first"},
    "detail": {"type": "string", "description": "at most a few lines more; no transcripts"},
}
_OPTION = {"type": "object", "properties": {
    "label": {"type": "string"},
    "effect": {"type": "string", "description": "what the user gets by picking it"},
    "risk": {"type": "string", "description": "what can go wrong; irreversible effects, cost, other sessions hit"},
    "recommended": {"type": "boolean", "description": "your recommendation (at most one)"},
}, "required": ["label", "effect", "risk"]}
_STEP = {"type": "object", "properties": {
    **_NODE_COMMON,
    "kind": {"type": "string", "enum": ["step", "decision"], "description": "default step"},
    "status": {"type": "string", "enum": ["todo", "doing", "done", "skipped", "blocked"],
               "description": "a step's status (default todo)"},
    "options": {"type": "array", "items": _OPTION, "description": "a decision's branches (2-8)"},
    "children": {"type": "array", "items": {"type": "object"}, "description": "sub-steps, same shape"},
}, "required": ["title"]}
_CLOSURE = {"type": "object", "properties": {
    **_NODE_COMMON,
    "kind": {"type": "string", "enum": ["verify", "followup"],
             "description": "verify: a check a person must make by hand (say what and how in detail); "
                            "followup: related work the user may keep here, hand over as an issue, or drop"},
    "status": {"type": "string", "enum": ["open", "done"],
               "description": "a followup's: done once you acted on the user's 'here' or 'issue'"},
}, "required": ["kind", "title"]}

TOOLS = [
    {"name": "plan_set",
     "description": "Write your whole plan into this session's plan pane, where the user aligns with you by "
                    "reading it and deciding branches instead of reading your prose. Steps: one line each (a "
                    "few more in detail), status todo/doing/done/skipped/blocked. A decision is a branch the "
                    "user picks: give each option its expected effect and risk, and mark your recommendation. "
                    "Closure: the verify items a person must test by hand and the followup candidates -- what "
                    "keeps the session open; the pane says when it may close. The user's choices arrive as "
                    "'[claunch plan]' blocks. revision: the plan's revision as you last read it (0 for the "
                    "first plan); a refusal carries the current plan -- the user may have just decided.",
     "inputSchema": {"type": "object", "properties": {
         "revision": {"type": "integer"},
         "title": {"type": "string", "description": "the plan's name: the goal in a few words"},
         "goal": {"type": "string", "description": "the goal and scope in one to three lines"},
         "issue": {"type": "string", "description": "the beads issue this plan is for"},
         "steps": {"type": "array", "items": _STEP},
         "closure": {"type": "array", "items": _CLOSURE},
     }, "required": ["revision", "title"]}},
    {"name": "plan_node",
     "description": "Change one node of your plan: a step's status as you work (no revision needed), or its "
                    "title, detail or options. Changing what the user decides on (title, detail, options) "
                    "asks the user again: a decision or check made on the old text is set aside.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "the node id"},
         "status": {"type": "string", "enum": ["todo", "doing", "done", "skipped", "blocked", "open"]},
         "title": {"type": "string"}, "detail": {"type": "string"},
         "options": {"type": "array", "items": _OPTION},
     }, "required": ["id"]}},
    {"name": "plan_get",
     "description": "Read a plan: yours (default) or another session's (a leader reading a worker's). It "
                    "carries the revision, every node with its rev and the user's result, and the summary "
                    "(waiting on the user, closable, and the blockers that keep it from closing).",
     "inputSchema": {"type": "object", "properties": {
         "session": {"type": "string", "description": "whose plan; default this session"},
     }}},
    {"name": "plan_clear",
     "description": "Remove your plan (written by mistake, or the session's work changed altogether).",
     "inputSchema": {"type": "object", "properties": {"revision": {"type": "integer"}},
                     "required": ["revision"]}},
]

TOOL_NAMES = tuple(str(tool["name"]) for tool in TOOLS)


class PlanMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise PlanMcpError(f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise PlanMcpError("no $CLAUNCH_SESSION -- session plans only work inside a managed claunch session")
    return session


def call_tool(name: str, args: dict) -> dict:
    me = _session()
    c = _client()
    if name == "plan_get":
        who = str(args.get("session") or me)
        return c.get("/api/plan?" + urllib.parse.urlencode({"session": who}))
    if name == "plan_set":
        body = {k: args[k] for k in ("revision", "title", "goal", "issue", "steps", "closure") if k in args}
        return c.post("/api/plan", {**body, "session": me, "by": me})
    if name == "plan_node":
        nid = str(args.get("id") or "")
        if not nid:
            raise PlanMcpError("id is required")
        body = {k: args[k] for k in ("title", "detail", "status", "options") if k in args}
        return c.post(f"/api/plan/node/{urllib.parse.quote(nid, safe='')}", {**body, "session": me, "by": me})
    if name == "plan_clear":
        return c.post("/api/plan/clear", {"revision": args.get("revision"), "session": me, "by": me})
    raise PlanMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-plan",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(PlanMcpError, daemon_client.DaemonClientError),
)
