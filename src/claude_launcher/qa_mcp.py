"""MCP tools for session QA (claunch-l8vyi).

Thin clients of the daemon's ``/api/qa`` routes. The daemon keeps the
questions in SQLite and decides who may do what: a session asks, edits,
closes and deletes its own questions (every call carries
``$CLAUNCH_SESSION``); the user answers them in the session's QA pane.
Every change names the revision the caller last read, so a write made
against a question that moved is refused with the current question.
"""

from __future__ import annotations

import json
import os
import urllib.parse

from . import daemon_client, mcp_rpc

INSTRUCTIONS = """\
Session QA (qa_* tools): put a question to the user as a record in your QA
pane on the dashboard instead of asking in the terminal, where reminders and
messages bury it. qa_ask files it (title, optional body and options; the
pane opens itself), qa_update rewrites it, qa_close settles it once acted on,
qa_delete withdraws it, qa_list reads yours. Every change takes the
'revision' you last read; a refusal (conflict) carries the current question
-- often the user just answered it. When the user saves an answer, a
"[claunch qa]" block is typed into your terminal with the question and the
answer (and the answer it replaced, for a rewrite). Editing an answered
question marks its answer stale and asks the user again. The user may also
nudge you from the pane ("[claunch qa] ... nudge qn-..."): file what you
need to ask with qa_ask, or reply qa_nothing when you have nothing to ask.
A closed question is not final: the user can rewrite its answer or ask for
a direction check against it ("[claunch qa] ... direction check"), which
reopens it. Compare your current work with the answer, change course where
you drifted, and qa_close it again with a note -- the note is your reply.

A question that comes out of a thread conversation takes qa_ask thread=<id>,
so it links back to that thread; the user can also open a thread about any
question from its QA card (closed ones too), or by naming its id in a
thread's title. Each question's 'links' lists those threads, and
an answer's block names them -- carry the answer back into the thread there.
"""

_ID = {"id": {"type": "string", "description": "question id (qa-xxxxxxxx)"}}
_REV = {"revision": {"type": "integer", "description": "the question's revision as you last read it"}}
_OPTIONS = {
    "type": "array",
    "description": "choices offered to the user; each a label string or {label, description}. "
                   "The user may always add free text.",
    "items": {"anyOf": [
        {"type": "string"},
        {"type": "object", "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
         "required": ["label"]},
    ]},
}

TOOLS = [
    {"name": "qa_ask",
     "description": "Ask the user a question in this session's QA pane. Returns the question (id, revision). "
                    "Use for anything you need the user to decide or tell you.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "the question, one line"},
         "body": {"type": "string", "description": "context the user needs to answer (markdown text)"},
         "options": _OPTIONS,
         "multi": {"type": "boolean", "description": "the user may pick several options (default one)"},
         "thread": {"type": "string", "description": "the thread (th-xxxxxx) this question came out of; "
                                                     "it is linked both ways"},
     }, "required": ["title"]}},
    {"name": "qa_update",
     "description": "Rewrite one of your questions (only the fields given change). If it was answered, "
                    "the answer is kept but marked stale and the user is asked again.",
     "inputSchema": {"type": "object", "properties": {
         **_ID, **_REV,
         "title": {"type": "string"}, "body": {"type": "string"}, "options": _OPTIONS,
         "multi": {"type": "boolean"},
     }, "required": ["id", "revision"]}},
    {"name": "qa_close",
     "description": "Settle one of your questions: its answer has been acted on (or it no longer matters). "
                    "It stays readable but no longer waits on the user.",
     "inputSchema": {"type": "object", "properties": {
         **_ID, **_REV, "note": {"type": "string", "description": "what came of it (kept in the record; for a direction check, "
                                                    "whether you were on track and what you changed)"},
     }, "required": ["id", "revision"]}},
    {"name": "qa_delete",
     "description": "Withdraw one of your questions entirely (asked by mistake).",
     "inputSchema": {"type": "object", "properties": {**_ID, **_REV}, "required": ["id", "revision"]}},
    {"name": "qa_nothing",
     "description": "Reply to the user's nudge (\"[claunch qa] ... nudge qn-...\"): you have nothing to ask "
                    "right now. Filing a question with qa_ask is the other reply and needs no call here.",
     "inputSchema": {"type": "object", "properties": {
         "nudge": {"type": "string", "description": "the nudge id (qn-xxxxxxxx); omit for your waiting nudge"},
         "note": {"type": "string", "description": "one line on why there is nothing to ask"},
     }}},
    {"name": "qa_list",
     "description": "Your questions with their status (open, answered, stale, closed), revision and answer.",
     "inputSchema": {"type": "object", "properties": {
         "closed": {"type": "boolean", "description": "include closed questions (default false)"},
     }}},
]


class QaMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise QaMcpError(f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise QaMcpError("no $CLAUNCH_SESSION -- session QA only works inside a managed claunch session")
    return session


def _qid(args: dict) -> str:
    qid = str(args.get("id") or "")
    if not qid:
        raise QaMcpError("id is required")
    return urllib.parse.quote(qid, safe="")


def call_tool(name: str, args: dict) -> dict:
    me = _session()
    c = _client()
    try:
        return _call(c, me, name, args)
    except daemon_client.DaemonClientError as exc:
        if name in ("qa_update", "qa_close", "qa_delete") and "revision" in str(exc):
            # A refused CAS: hand back the question as it stands now, so the
            # next move is made on what the user actually sees.
            current = _current(c, me, str(args.get("id") or ""))
            if current is not None:
                raise QaMcpError(f"{exc}\ncurrent: {json.dumps(current, ensure_ascii=False)}") from exc
        raise


def _current(c, me: str, qid: str):
    try:
        doc = c.get("/api/qa?" + urllib.parse.urlencode({"session": me, "closed": "1"}))
    except daemon_client.DaemonClientError:
        return None
    return next((q for q in doc.get("questions") or [] if q.get("id") == qid), None)


def _call(c, me: str, name: str, args: dict) -> dict:
    if name == "qa_list":
        q = {"session": me, "closed": "1" if args.get("closed") else "0"}
        return c.get("/api/qa?" + urllib.parse.urlencode(q))
    if name == "qa_ask":
        body = {k: args[k] for k in ("title", "body", "options", "multi", "thread") if k in args}
        return c.post("/api/qa", {**body, "session": me})
    if name == "qa_update":
        body = {k: args[k] for k in ("revision", "title", "body", "options", "multi") if k in args}
        return c.post(f"/api/qa/{_qid(args)}", {**body, "session": me})
    if name == "qa_close":
        body = {k: args[k] for k in ("revision", "note") if k in args}
        return c.post(f"/api/qa/{_qid(args)}/close", {**body, "session": me})
    if name == "qa_delete":
        return c.post(f"/api/qa/{_qid(args)}/delete", {"revision": args.get("revision"), "session": me})
    if name == "qa_nothing":
        nid = urllib.parse.quote(str(args.get("nudge") or "latest"), safe="")
        body = {"note": args["note"]} if "note" in args else {}
        return c.post(f"/api/qa/nudge/{nid}/nothing", {**body, "session": me})
    raise QaMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-qa",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(QaMcpError, daemon_client.DaemonClientError),
    instructions=INSTRUCTIONS,
)
