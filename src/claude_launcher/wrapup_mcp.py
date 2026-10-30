"""MCP tools for session wrap-up drafts (claunch-ar8jn).

Thin clients of the daemon's ``/api/wrapup`` routes. A session files a
glossary term, a category placement or a knowledge record as a draft; the
user edits, discards or confirms it in the session's wrap-up pane, and only
a confirm writes it into the workspace store. A session cannot confirm.
"""

from __future__ import annotations

import json
import os
import urllib.parse

from . import daemon_client, mcp_rpc

_ID = {"id": {"type": "string", "description": "draft id (wd-xxxxxxxx)"}}
_REV = {"revision": {"type": "integer", "description": "the draft's revision as you last read it"}}
_PAYLOAD = {
    "type": "object",
    "description": "the entry, shaped as the matching direct tool's arguments: "
                   "glossary = glossary_suggest {term, definition, aliases?, source?}; "
                   "knowledge = knowledge_record {kind, title, body, tags?, refs?, source?, supersedes?}; "
                   "category = category_assign {node_id | new | fold | split | group, items?, reason?} "
                   "(items default to this session and its issue)",
}

TOOLS = [
    {"name": "wrapup_draft",
     "description": "File a wrap-up draft for the user to confirm: a glossary term, a category placement or "
                    "a knowledge record. Nothing reaches the workspace store until the user confirms it in "
                    "your wrap-up pane; what the user does to it (edit, delete, confirm) arrives as a "
                    "\"[claunch wrapup]\" block. Returns the draft (id, revision).",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["glossary", "category", "knowledge"]},
         "payload": _PAYLOAD,
         "reason": {"type": "string", "description": "one line for the user: why this entry"},
     }, "required": ["kind", "payload"]}},
    {"name": "wrapup_update",
     "description": "Rewrite one of your drafts while it waits on the user (draft or failed). "
                    "Give the whole payload.",
     "inputSchema": {"type": "object", "properties": {
         **_ID, **_REV, "payload": _PAYLOAD, "reason": {"type": "string"},
     }, "required": ["id", "revision"]}},
    {"name": "wrapup_withdraw",
     "description": "Withdraw one of your drafts (filed by mistake, or no longer true).",
     "inputSchema": {"type": "object", "properties": {**_ID, **_REV}, "required": ["id", "revision"]}},
    {"name": "wrapup_list",
     "description": "Your wrap-up drafts with status (draft, applying, confirmed, failed, discarded), "
                    "revision and, for a failed one, why its store refused it.",
     "inputSchema": {"type": "object", "properties": {
         "settled": {"type": "boolean", "description": "include confirmed and discarded drafts (default false)"},
     }}},
]


class WrapupMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise WrapupMcpError(f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise WrapupMcpError("no $CLAUNCH_SESSION -- wrap-up drafts only work inside a managed claunch session")
    return session


def _did(args: dict) -> str:
    did = str(args.get("id") or "")
    if not did:
        raise WrapupMcpError("id is required")
    return urllib.parse.quote(did, safe="")


def call_tool(name: str, args: dict) -> dict:
    me = _session()
    c = _client()
    try:
        return _call(c, me, name, args)
    except daemon_client.DaemonClientError as exc:
        if name in ("wrapup_update", "wrapup_withdraw") and "revision" in str(exc):
            current = _current(c, me, str(args.get("id") or ""))
            if current is not None:
                raise WrapupMcpError(f"{exc}\ncurrent: {json.dumps(current, ensure_ascii=False)}") from exc
        raise


def _current(c, me: str, did: str):
    try:
        doc = c.get("/api/wrapup?" + urllib.parse.urlencode({"session": me, "settled": "1"}))
    except daemon_client.DaemonClientError:
        return None
    return next((d for d in doc.get("drafts") or [] if d.get("id") == did), None)


def _call(c, me: str, name: str, args: dict) -> dict:
    if name == "wrapup_list":
        q = {"session": me, "settled": "1" if args.get("settled") else "0"}
        return c.get("/api/wrapup?" + urllib.parse.urlencode(q))
    if name == "wrapup_draft":
        body = {k: args[k] for k in ("kind", "payload", "reason") if k in args}
        return c.post("/api/wrapup", {**body, "session": me})
    if name == "wrapup_update":
        body = {k: args[k] for k in ("revision", "payload", "reason") if k in args}
        return c.post(f"/api/wrapup/{_did(args)}", {**body, "session": me})
    if name == "wrapup_withdraw":
        return c.post(f"/api/wrapup/{_did(args)}/discard", {"revision": args.get("revision"), "session": me})
    raise WrapupMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-wrapup",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(WrapupMcpError, daemon_client.DaemonClientError),
)
