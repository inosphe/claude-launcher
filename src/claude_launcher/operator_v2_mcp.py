"""Operator v2's tools, exposed by the shared claunch MCP server.

Every tool acts as the calling session ($CLAUNCH_SESSION), and the daemon
refuses all of them unless that session is the bound session of an operator
v2 (see :mod:`claude_launcher.daemon.operators_v2`); every session, issue,
transcript and commit they read is checked against that operator's project
scope. The ``opv2_`` prefix keeps them apart from v1's ``operator_*`` tools,
which still run beside them until v1 is removed.
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlencode

from . import daemon_client, mcp_rpc

REFS = {"type": "array", "items": {"type": "string"}, "maxItems": 20,
        "description": "Session names this item is about (must be sessions in your scope). The UI links them."}
LEVEL = {"type": "string", "enum": ["info", "attention", "urgent"],
         "description": "info = worth knowing; attention = the user should look soon; urgent = the user must act now."}
REPLY_TO = {"type": "string", "description": "Feed id of your earlier post or ask this one follows up."}
REQUEST_ID = {"type": "string", "description": "Stable unique id. Reuse it on retry to avoid a duplicate."}
SESSION = {"type": "string", "description": "a session in your scope"}
THREAD = {"type": "string", "description": "Thread id (th-...) of your conversation with the user to write in: the "
                                          "`thread` of the inbox item you answer. Omit to use the most recently "
                                          "active thread."}
NONE = {"type": "object", "properties": {}}

TOOLS = [
    {"name": "opv2_poll",
     "description": "Read what happened in the sessions of your scope since `since` (the `cursor` of your previous "
                    "poll): Observer events, plus `attention` (open observer_ask questions, blocked sessions, cflow "
                    "gates waiting on a person) whatever the cursor. `watch` is the user's switch: when false, do not "
                    "keep a polling loop; answer the user's input only. `progress` lists status-check and issue "
                    "changes once; `ended` names sessions that left your watch since the last poll.",
     "inputSchema": {"type": "object", "properties": {"since": {"type": "string", "description": "cursor from the previous poll; omit on the first"}}}},
    {"name": "opv2_post",
     "description": "Post one item to your conversation with the user in the Operator view. The conversation is a "
                    "set of threads: answer in the thread the user wrote in (`thread` from opv2_inbox); a reply_to "
                    "follows its entry's thread; with neither, the most recently active thread. This is your only "
                    "output channel — terminal text is never shown to the user.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Markdown, <=4000 chars. Lead with the point; cite session names, ids, hashes."},
         "thread": THREAD, "level": LEVEL, "refs": REFS, "reply_to": REPLY_TO, "request_id": REQUEST_ID},
         "required": ["text", "request_id"]}},
    {"name": "opv2_ask",
     "description": "Ask the user for a decision in one of your threads (`thread`, as in opv2_post). type=approve "
                    "shows approve/deny, type=choice shows `choices`, type=text a text box. Returns at once; the "
                    "answer arrives through opv2_inbox. A missing answer never means yes.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "The question with the facts needed to answer it, <=4000 chars."},
         "type": {"type": "string", "enum": ["approve", "choice", "text"]},
         "choices": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 10},
         "thread": THREAD, "level": LEVEL, "refs": REFS, "reply_to": REPLY_TO, "request_id": REQUEST_ID},
         "required": ["text", "type", "request_id"]}},
    {"name": "opv2_inbox",
     "description": "Read the user's new input: messages typed to you and answers to your asks. Each item is "
                    "returned once and names its `thread` (id and `thread_title`); answer in that thread. A message "
                    "may carry `images` with a `path` on this machine you can open. Call it whenever you are nudged.",
     "inputSchema": NONE},
    {"name": "opv2_thread_open",
     "description": "Open a new thread in your conversation with the user, for a topic that should not run on in "
                    "an existing one (a new incident, a separate piece of work), with its first post. Returns the "
                    "thread id; write there later with `thread`.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "the topic, <=200 chars"},
         "text": {"type": "string", "description": "the first post, markdown, <=4000 chars"},
         "level": LEVEL, "refs": REFS, "request_id": REQUEST_ID}, "required": ["title", "text", "request_id"]}},
    {"name": "opv2_threads",
     "description": "Your threads with the user, most recently active first: id, title, status, entry count, "
                    "unanswered asks and the last entry.",
     "inputSchema": NONE},
    {"name": "opv2_dispatch",
     "description": "Type an instruction into a running session of your scope on the user's behalf. `on_behalf_of` "
                    "must be the id of the user message (or answered, not denied, ask) that instructs it. The "
                    "dispatch report is shown in that message's thread unless `thread` names another.",
     "inputSchema": {"type": "object", "properties": {
         "target": SESSION,
         "text": {"type": "string", "description": "the instruction, faithful to what the user said"},
         "on_behalf_of": {"type": "string", "description": "id from opv2_inbox"},
         "thread": THREAD},
         "required": ["target", "text", "on_behalf_of"]}},
    {"name": "opv2_spawn",
     "description": "Ask the user for a NEW session to do work that no running session in your scope is doing "
                    "(check opv2_sessions first; a running session doing it gets opv2_dispatch instead). The "
                    "request waits on a notification card for 5 minutes: the user approves, rejects, extends it or "
                    "creates the session by hand. Approved, the new session is top-level, in the project's "
                    "default workspace on a fresh worktree, running improv-worker-v2. With no answer no top-level "
                    "session is made: the same session is created as YOUR child instead (project, task, issue, "
                    "name and workflow kept). Returns at once; the outcome is typed into your terminal and posted "
                    "to your feed.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "a project in your scope (omit when your scope is one project)"},
         "task": {"type": "string", "description": "the new session's opening task, faithful to what the user said"},
         "issue": {"type": "string", "description": "board issue the session takes (must be on a board in your scope)"},
         "name": {"type": "string", "description": "session name; omit for a generated one"},
         "reason": {"type": "string", "description": "why a new session: the user reads this on the card"},
         "on_behalf_of": {"type": "string", "description": "id from opv2_inbox of the user message that asks for the work"}},
         "required": ["task", "reason", "on_behalf_of"]}},
    {"name": "opv2_resume",
     "description": "Ask the user to resume an EXITED session of your scope that was doing this work. The request "
                    "waits on a notification card for 5 minutes: the user resumes it, asks for a new session "
                    "instead, rejects, or extends it. No answer means the session is NOT resumed and `fallback` is "
                    "filed as an opv2_spawn request. Returns at once; the outcome is typed into your terminal.",
     "inputSchema": {"type": "object", "properties": {
         "target": {"type": "string", "description": "the exited session in your scope"},
         "reason": {"type": "string", "description": "why this session: the user reads this on the card"},
         "on_behalf_of": {"type": "string", "description": "id from opv2_inbox"},
         "fallback": {"type": "object", "description": "the new session to ask for if the resume is not approved: "
                                                       "task (required), project, issue, name as in opv2_spawn",
                      "properties": {"task": {"type": "string"}, "project": {"type": "string"},
                                     "issue": {"type": "string"}, "name": {"type": "string"}},
                      "required": ["task"]}},
         "required": ["target", "reason", "on_behalf_of", "fallback"]}},
    {"name": "opv2_history",
     "description": "Your own record, newest first: what you posted, asked and dispatched, and what the user told "
                    "you. Use it to answer 'what did you do about X'.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "description": "comma list of post, ask, dispatch, user, system, update"},
         "session": {"type": "string", "description": "only entries naming this session"},
         "thread": {"type": "string", "description": "only entries of this thread"},
         "q": {"type": "string", "description": "substring to find in the text"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "opv2_sessions",
     "description": "The sessions in your scope with project, state, board issue and status-check answers. "
                    "`state`: live (default: running, paused, hold, delayed, failed), ended, or all.",
     "inputSchema": {"type": "object", "properties": {
         "state": {"type": "string", "enum": ["live", "ended", "all"]},
         "project": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "opv2_landing",
     "description": "Where the work of the sessions in your scope stands, in one call: board issue and its status, "
                    "branch, recorded PRs and `landed` (merged, open, closed, no-pr, unknown). Judge landing by "
                    "`landed`: a squash merge leaves a branch's commits off master, so ancestry calls it unlanded. "
                    "PR states are the recorded ones (no network read); `unknown` means a PR with no state on record. "
                    "Same `state`/`project`/`limit` as opv2_sessions.",
     "inputSchema": {"type": "object", "properties": {
         "state": {"type": "string", "enum": ["live", "ended", "all"]},
         "project": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "opv2_session",
     "description": "One session of your scope in detail, running or ended: its task, Observer state and summary, "
                    "last events, board issue, status checks and latest commits.",
     "inputSchema": {"type": "object", "properties": {"session": SESSION}, "required": ["session"]}},
    {"name": "opv2_commits",
     "description": "The commits a session of your scope made, newest first.",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["session"]}},
    {"name": "opv2_transcript_search",
     "description": "Where something was said in a conversation: in one session (`session`) or across the sessions "
                    "of your scope (at most 40 read; `skipped_sessions` says how many were not). Newest first.",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "literal text, or a pattern with regex=true"},
         "session": SESSION, "regex": {"type": "boolean"},
         "prose": {"type": "boolean", "description": "match what was said, not tool calls"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["q"]}},
    {"name": "opv2_issue",
     "description": "One board issue with its comments, if it is on a board of your scope (the boards your "
                    "sessions stand on, and your projects' default workspaces), and which sessions in scope it "
                    "belongs to.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]


class OperatorV2McpError(Exception):
    pass


def _query(args, keys):
    out = {}
    for key in keys:
        value = args.get(key)
        if value is None or value == "":
            continue
        out[key] = ("1" if value else "0") if isinstance(value, bool) else str(value)
    return ("?" + urlencode(out)) if out else ""


def call_tool(name, args):
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise OperatorV2McpError("Operator v2 tools require a managed CLAUNCH_SESSION")
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise OperatorV2McpError(daemon_client.unreachable_reason(why))
    base = "/api/operators/agent/" + quote(session, safe="")

    def pick(*keys):
        return {k: args.get(k) for k in keys if k in args}

    if name == "opv2_poll":
        return client.get(base + "/poll" + _query(args, ("since",)))
    if name == "opv2_post":
        return client.post(base + "/post", pick("text", "thread", "level", "refs", "reply_to", "request_id"))
    if name == "opv2_ask":
        return client.post(base + "/ask", pick("text", "type", "choices", "thread", "level", "refs", "reply_to",
                                               "request_id"))
    if name == "opv2_inbox":
        return client.get(base + "/inbox")
    if name == "opv2_thread_open":
        return client.post(base + "/threads", pick("title", "text", "level", "refs", "request_id"))
    if name == "opv2_threads":
        return client.get(base + "/threads")
    if name == "opv2_dispatch":
        return client.post(base + "/dispatch", pick("target", "text", "on_behalf_of", "thread"))
    if name == "opv2_spawn":
        return client.post(base + "/spawn", pick("project", "task", "issue", "name", "reason", "on_behalf_of"))
    if name == "opv2_resume":
        return client.post(base + "/resume", pick("target", "reason", "on_behalf_of", "fallback"))
    if name == "opv2_history":
        return client.get(base + "/history" + _query(args, ("kind", "session", "thread", "q", "limit")))
    if name == "opv2_sessions":
        return client.get(base + "/sessions" + _query(args, ("state", "project", "limit")))
    if name == "opv2_landing":
        return client.get(base + "/landing" + _query(args, ("state", "project", "limit")))
    if name in ("opv2_session", "opv2_commits") and not args.get("session"):
        raise OperatorV2McpError("session is required")
    target = quote(str(args.get("session") or ""), safe="")
    if name == "opv2_session":
        return client.get(base + "/sessions/" + target)
    if name == "opv2_commits":
        return client.get(base + "/sessions/" + target + "/commits" + _query(args, ("limit",)))
    if name == "opv2_transcript_search":
        return client.get(base + "/transcript-search" + _query(args, ("q", "session", "regex", "prose", "limit")))
    if name == "opv2_issue":
        return client.get(base + "/issues/" + quote(str(args.get("id") or ""), safe=""))
    raise OperatorV2McpError("unknown Operator v2 tool")


SERVER = mcp_rpc.Server(name="claunch-operator-v2", tools=tuple(TOOLS), dispatch=call_tool,
                        errors=(OperatorV2McpError, daemon_client.DaemonClientError, OSError, ValueError))
