"""The Operator bot's tools, exposed by the shared claunch MCP server.

Every tool acts as the calling session ($CLAUNCH_SESSION), and the daemon
refuses all of them unless that session is the bound operator of its project
(see :mod:`claude_launcher.daemon.operator_bot`). Output to the user goes
through ``operator_post``/``operator_ask``; input from the user is read with
``operator_inbox`` — the operator's terminal is not where it talks to them.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
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
                    "Keep the returned `cursor` for the next call; `more: true` means call again with it. "
                    "`restart` is a daemon restart the daemon recorded (once); `recovered` says earlier polls failed "
                    "and this one answered (first failure, recovery time, count); `degraded` names parts not read in time.",
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
    {"name": "operator_transcripts",
     "description": "Your observation mode, and in `transcript` mode the conversations themselves: each running session's "
                    "new transcript records since the last call (the daemon keeps the cursor per session; a session "
                    "seen first starts from its last few records). A record is `{seq, role, ts, text}` — prose clipped "
                    "at 1500 chars, tool calls and results as one clipped line, thinking omitted. `more: true` means "
                    "call again; these records replace operator_poll's routine `events`, while its `attention`, `progress` "
                    "and restart fields still count. In `events` mode it returns only `mode`. "
                    "The user switches the mode from the Operator tab; you are nudged when they do.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class OperatorMcpError(Exception):
    pass


#: Where a failed poll is remembered until one succeeds, in the session's own
#: scratch directory. The daemon cannot record this: a poll that never reached
#: it, or timed out on the way back, left no trace on its side.
OUTAGE_FILE = "operator-poll-outage.json"


def _outage_path():
    scratch = os.environ.get("CLAUNCH_SCRATCH")
    return Path(scratch) / OUTAGE_FILE if scratch else None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def note_poll_failure(error):
    """Count one failed poll: the first failure's time stays, the last error
    and the count move."""
    path = _outage_path()
    if path is None:
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict) or not record.get("first_failed_at"):
        record = {"first_failed_at": _now(), "failures": 0}
    record["failures"] = int(record.get("failures") or 0) + 1
    record["last_failed_at"] = _now()
    record["last_error"] = str(error)[:300]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def take_recovery():
    """The outage the previous polls recorded, closed now that one got an
    answer, or None when there was none."""
    path = _outage_path()
    if path is None or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(record, dict) or not record.get("first_failed_at"):
        return None
    return {**record, "recovered_at": _now()}


def call_tool(name, args):
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise OperatorMcpError("Operator tools require a managed CLAUNCH_SESSION")
    if name == "operator_poll":
        return _poll(session, args.get("since"))
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise OperatorMcpError(daemon_client.unreachable_reason(why))
    base = "/api/operator/agent/" + quote(session, safe="")
    if name == "operator_inbox":
        return client.get(base + "/inbox")
    if name == "operator_post":
        return client.post(base + "/post", {k: args.get(k) for k in ("text", "level", "refs", "reply_to", "request_id") if k in args})
    if name == "operator_ask":
        return client.post(base + "/ask", {k: args.get(k) for k in ("text", "type", "choices", "level", "refs", "reply_to", "request_id") if k in args})
    if name == "operator_dispatch":
        return client.post(base + "/dispatch", {k: args.get(k) for k in ("target", "text", "on_behalf_of")})
    if name == "operator_transcripts":
        return client.get(base + "/transcripts")
    raise OperatorMcpError("unknown Operator tool")


def _poll(session, since):
    """One poll. A failure is remembered; the first poll that answers after
    it carries ``recovered`` (first failure, recovery time, count, last
    error) so the operator can say the outage ended."""
    try:
        client, why = daemon_client.connect_with_diagnosis()
        if client is None:
            raise OperatorMcpError(daemon_client.unreachable_reason(why))
        result = client.get("/api/operator/agent/" + quote(session, safe="") + "/poll"
                            + ("?since=" + quote(since, safe="") if since else ""))
    except (OperatorMcpError, daemon_client.DaemonClientError, OSError) as exc:
        note_poll_failure(exc)
        raise
    recovered = take_recovery()
    if recovered and isinstance(result, dict):
        result = {**result, "recovered": recovered}
    return result


SERVER = mcp_rpc.Server(name="claunch-operator", tools=tuple(TOOLS), dispatch=call_tool,
                        errors=(OperatorMcpError, daemon_client.DaemonClientError, OSError, ValueError))
