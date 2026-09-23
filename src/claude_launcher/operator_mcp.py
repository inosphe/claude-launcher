"""The Operator bot's tools, exposed by the shared claunch MCP server.

Every tool acts as the calling session ($CLAUNCH_SESSION), and the daemon
refuses all of them unless that session is the bound operator of its project
(see :mod:`claude_launcher.daemon.operator_bot`). Output to the user goes
through ``operator_post``/``operator_ask``; input from the user is read with
``operator_inbox`` — the operator's terminal is not where it talks to them.
"""
from __future__ import annotations

import os
from urllib.parse import quote

from . import daemon_client, mcp_rpc

REFS = {"type": "array", "items": {"type": "string"}, "maxItems": 20,
        "description": "Session names this item is about (must be sessions of your project). The UI links them."}
LEVEL = {"type": "string", "enum": ["info", "attention", "urgent"],
         "description": "info = worth knowing; attention = the user should look soon; urgent = the user must act now (raises a notification)."}
REPLY_TO = {"type": "string",
            "description": "Feed id of your earlier post or ask this one follows up (a status that moved, a result). "
                           "The UI shows it as a reply in that card's thread as well as in time order."}
REQUEST_ID = {"type": "string", "description": "Stable unique id. Reuse it on retry to avoid a duplicate."}

TOOLS = [
    {"name": "operator_poll",
     "description": "Read what happened in your project's other sessions since `since` (the `cursor` of your previous poll): "
                    "Observer events, plus mechanical state that needs no LLM — cflow gates waiting on a person, open "
                    "observer_ask questions, blocked sessions. `attention` lists what is waiting now whatever the cursor. "
                    "Keep the returned `cursor` for the next call; `more: true` means call again with it.",
     "inputSchema": {"type": "object", "properties": {"since": {"type": "string", "description": "cursor from the previous poll; omit on the first"}}}},
    {"name": "operator_post",
     "description": "Post one item to the user's Operator feed: a finding, a digest, a status the user should know. "
                    "This is your only output channel — terminal text is never shown to the user. Post only what "
                    "matters; routine progress and message logistics are noise.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Markdown, <=4000 chars. Lead with the point; cite session names, ids, hashes."},
         "level": LEVEL, "refs": REFS, "reply_to": REPLY_TO, "request_id": REQUEST_ID}, "required": ["text", "request_id"]}},
    {"name": "operator_ask",
     "description": "Ask the user for a decision in the Operator feed. type=approve shows approve/deny buttons, "
                    "type=choice shows `choices` as buttons, type=text shows a text box; every type also takes a note. "
                    "Returns at once; the answer arrives through operator_inbox (you are nudged). A missing answer "
                    "never means yes. This does not answer a cflow gate or another session's question — the user does that.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "The question with the facts needed to answer it, <=4000 chars."},
         "type": {"type": "string", "enum": ["approve", "choice", "text"]},
         "choices": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 10},
         "level": LEVEL, "refs": REFS, "reply_to": REPLY_TO, "request_id": REQUEST_ID}, "required": ["text", "type", "request_id"]}},
    {"name": "operator_inbox",
     "description": "Read the user's new input: messages typed in the Operator tab and answers to your asks. "
                    "Each item is returned once; call it whenever you are nudged.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "operator_dispatch",
     "description": "Type an instruction into another session of your project on the user's behalf. `on_behalf_of` "
                    "must be the id of the user message (or the answered, not denied, ask) that instructs it — you relay "
                    "the user's instruction, you do not originate work. The dispatch is recorded in the feed.",
     "inputSchema": {"type": "object", "properties": {
         "target": {"type": "string", "description": "the session to type into"},
         "text": {"type": "string", "description": "the instruction, faithful to what the user said"},
         "on_behalf_of": {"type": "string", "description": "id from operator_inbox"}},
         "required": ["target", "text", "on_behalf_of"]}},
]


class OperatorMcpError(Exception):
    pass


def call_tool(name, args):
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise OperatorMcpError("Operator tools require a managed CLAUNCH_SESSION")
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise OperatorMcpError(daemon_client.unreachable_reason(why))
    base = "/api/operator/agent/" + quote(session, safe="")
    if name == "operator_poll":
        since = args.get("since")
        return client.get(base + "/poll" + ("?since=" + quote(since, safe="") if since else ""))
    if name == "operator_inbox":
        return client.get(base + "/inbox")
    if name == "operator_post":
        return client.post(base + "/post", {k: args.get(k) for k in ("text", "level", "refs", "reply_to", "request_id") if k in args})
    if name == "operator_ask":
        return client.post(base + "/ask", {k: args.get(k) for k in ("text", "type", "choices", "level", "refs", "reply_to", "request_id") if k in args})
    if name == "operator_dispatch":
        return client.post(base + "/dispatch", {k: args.get(k) for k in ("target", "text", "on_behalf_of")})
    raise OperatorMcpError("unknown Operator tool")


SERVER = mcp_rpc.Server(name="claunch-operator", tools=tuple(TOOLS), dispatch=call_tool,
                        errors=(OperatorMcpError, daemon_client.DaemonClientError, OSError, ValueError))
