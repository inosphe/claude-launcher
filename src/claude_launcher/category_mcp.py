"""Agents read the workspace category tree and place sessions in it."""
from __future__ import annotations

import os
from urllib.parse import quote, urlencode

from . import daemon_client, mcp_rpc


class CategoryMcpError(Exception):
    pass


_WORKSPACE = {"type": "string", "description": "registered workspace name; default: the current session's workspace"}
_ITEMS = {"type": "array", "description": "items to place; default: the session this job is about and its issue",
          "items": {"type": "object", "properties": {
              "kind": {"type": "string", "enum": ["session", "issue"]}, "ref": {"type": "string"}},
              "required": ["kind", "ref"]}}
TOOLS = [
    {"name": "category_tree", "description":
     "The workspace's category tree: a keyword index of what sessions and issues are about "
     "(separate from the glossary). Returns every node (id, parent_id, label, aliases, path), "
     "the items placed in them (kind session|issue, ref, node_id) and pending proposals for "
     "new categories. The labels and aliases are the tree's vocabulary: reuse them.",
     "inputSchema": {"type": "object", "properties": {"workspace": _WORKSPACE}}},
    {"name": "category_search", "description":
     "Find category nodes by keyword: normalized spelling, aliases, substrings and character "
     "similarity (no embeddings). Use it before proposing a new category.",
     "inputSchema": {"type": "object", "properties": {
         "workspace": _WORKSPACE, "query": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["query"]}},
    {"name": "category_items", "description":
     "The sessions and issues placed in one category node (and, by default, the nodes below it).",
     "inputSchema": {"type": "object", "properties": {
         "workspace": _WORKSPACE, "node_id": {"type": "string"},
         "descendants": {"type": "boolean"}}, "required": ["node_id"]}},
    {"name": "category_assign", "description":
     "Place items in the category tree: {node_id} puts them in an existing node (moving them "
     "from wherever they were; an item sits in one node). {new: {parent_id, label, aliases, "
     "description}, reason} proposes a new category instead: a person accepts or rejects it on "
     "the Category page, and until then the items wait in parent_id. An existing label or alias "
     "is refused with the node that owns it -- assign to that node. Without 'items' it places "
     "the session this job is about (and its issue).",
     "inputSchema": {"type": "object", "properties": {
         "node_id": {"type": "string"},
         "new": {"type": "object", "properties": {
             "parent_id": {"type": "string", "description": "existing node id; omit for a top-level category"},
             "label": {"type": "string"},
             "aliases": {"type": "array", "items": {"type": "string"}},
             "description": {"type": "string"}}, "required": ["label"]},
         "reason": {"type": "string", "description": "why this category (required for a proposal)"},
         "items": _ITEMS}}},
]


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise CategoryMcpError(daemon_client.unreachable_reason(why))
    return client


def call_tool(name, args):
    if name not in {tool["name"] for tool in TOOLS}:
        raise CategoryMcpError(f"unknown tool {name!r}")
    args = args or {}
    client = _client()
    session = os.environ.get("CLAUNCH_SESSION")
    if name == "category_assign":
        if not session:
            raise CategoryMcpError("category_assign needs a claunch session (CLAUNCH_SESSION)")
        body = {k: args[k] for k in ("node_id", "new", "reason", "items") if args.get(k)}
        return client.post("/api/category/agent-assign", {**body, "session": session})
    selection = {"workspace": args["workspace"]} if args.get("workspace") else {"cwd": os.getcwd()}
    # The registered session's cwd is authoritative for the default workspace.
    if session and not args.get("workspace"):
        selection = {"cwd": client.get("/api/sessions/" + quote(session, safe=""))["cwd"]}
    if name == "category_tree":
        return client.get("/api/category?" + urlencode(selection))
    if name == "category_search":
        return client.get("/api/category/search?" + urlencode({
            **selection, "q": args.get("query", ""), "limit": args.get("limit", 20)}))
    return client.get("/api/category/nodes/" + quote(str(args.get("node_id", "")), safe="")
                      + "/items?" + urlencode({**selection, "descendants": "0" if args.get("descendants") is False else "1"}))


SERVER = mcp_rpc.Server(name="claunch-category", tools=tuple(TOOLS), dispatch=call_tool,
                        errors=(CategoryMcpError, daemon_client.DaemonClientError))
