"""MCP tools for session threads (claunch-doj98).

Thin clients of the daemon's ``/api/threads`` routes. A thread is one
sub-topic of the conversation between the user and this session, held in
the session's threads pane on the dashboard; the daemon keeps the messages
in SQLite. Every call carries ``$CLAUNCH_SESSION``: a session reads and
writes only its own threads.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc

INSTRUCTIONS = """\
Session threads (thread_* tools): answer a "[claunch thread th-... ]" block
with thread_reply on that thread, in markdown. When the user must act now,
alert them in a thread (every message there is a dashboard notice). Full
guidance: prompt pack 'mcp-threads'.
"""

_TID = {"thread_id": {"type": "string", "description": "thread id (th-xxxxxx)"}}

TOOLS = [
    {"name": "thread_list",
     "description": "Your threads with the user: id, title, status, the last message, how many user "
                    "messages you have not read (agent_unread) and the QA questions each touches (links).",
     "inputSchema": {"type": "object", "properties": {
         "closed": {"type": "boolean", "description": "include closed threads (default false)"}}}},
    {"name": "thread_read",
     "description": "One thread's conversation, oldest first. Marks it read. Pass after=<seq> to get only "
                    "what came after a message you already have. A user message may carry images: each "
                    "entry of its 'images' has the file's 'path' on this machine -- open it to see the picture.",
     "inputSchema": {"type": "object", "properties": {
         **_TID, "after": {"type": "integer", "description": "only messages with a larger seq"},
         "limit": {"type": "integer", "description": "at most this many messages (default 500)"}},
         "required": ["thread_id"]}},
    {"name": "thread_reply",
     "description": "Write a message in one of your threads. The text is GitHub-flavored markdown, "
                    "rendered in the threads pane (lists, code fences, tables, links). Replying to a "
                    "closed thread reopens it. Only for the thread's own topic: when the text is about "
                    "another subject than the thread's title, use thread_open for a new thread instead.",
     "inputSchema": {"type": "object", "properties": {
         **_TID, "text": {"type": "string", "description": "the message, in markdown"}},
         "required": ["thread_id", "text"]}},
    {"name": "thread_open",
     "description": "Start a new thread with the user on a sub-topic, optionally with its first message. "
                    "Open one whenever what you have to say has diverged from an existing thread's "
                    "topic. Returns the thread (id).",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "the topic, one line"},
         "text": {"type": "string", "description": "first message, in markdown"},
         "qa": {"type": "string", "description": "the QA question (qa-xxxxxxxx) this thread discusses; "
                                                 "it is linked both ways and shown on its QA card. Omitted, "
                                                 "a single qa-id of yours in the title links it"}},
         "required": ["title"]}},
    {"name": "thread_close",
     "description": "Close one of your threads once its topic is settled (kept for the record; a new "
                    "message from either side reopens it).",
     "inputSchema": {"type": "object", "properties": _TID, "required": ["thread_id"]}},
]


class ThreadMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise ThreadMcpError(f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise ThreadMcpError("no $CLAUNCH_SESSION -- threads only work inside a managed claunch session")
    return session


def _tid(args: dict) -> str:
    tid = str(args.get("thread_id") or "")
    if not tid:
        raise ThreadMcpError("thread_id is required")
    return urllib.parse.quote(tid, safe="")


def call_tool(name: str, args: dict) -> dict:
    me = _session()
    c = _client()
    if name == "thread_list":
        q = {"session": me, "closed": "1" if args.get("closed") else "0"}
        return c.get("/api/threads?" + urllib.parse.urlencode(q))
    if name == "thread_read":
        q = {"reader": me}
        for k in ("after", "limit"):
            if args.get(k) is not None:
                q[k] = int(args[k])
        return c.get(f"/api/threads/{_tid(args)}?" + urllib.parse.urlencode(q))
    if name == "thread_reply":
        return c.post(f"/api/threads/{_tid(args)}/messages", {"session": me, "text": args.get("text") or ""})
    if name == "thread_open":
        body = {"session": me, "title": args.get("title") or ""}
        for k in ("text", "qa"):
            if args.get(k):
                body[k] = args[k]
        return c.post("/api/threads", body)
    if name == "thread_close":
        return c.post(f"/api/threads/{_tid(args)}/close", {"session": me})
    raise ThreadMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-threads",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(ThreadMcpError, daemon_client.DaemonClientError),
    instructions=INSTRUCTIONS,
)
