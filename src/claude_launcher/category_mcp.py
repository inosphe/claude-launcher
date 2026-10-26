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
     "description}, reason} creates a new category and places the items in it at once; pass "
     "proposal: true to file it for review on the Category page instead (until then the items "
     "wait in parent_id). An existing label or alias is refused with the node that owns it -- "
     "assign to that node. "
     "MERGE at registration: {fold: {label, nodes: [id...], parent_id?, aliases?, description?}, "
     "reason} absorbs the named nodes into one new category (their items and child categories "
     "move there, their labels become aliases) and places the items in it -- use it when this "
     "work belongs to subjects already split across several flat nodes. "
     "SPLIT at registration: {split: {parent_id?, parts: [{label, aliases?, description?, "
     "items?: [ref...]}]}, reason} makes several children of one parent and puts the items "
     "across them; a part with no items takes whatever the earlier parts left. "
     "Without 'items' it places the session this job is about (and its issue). A request "
     "started from the session pane's 'Assign to tree' follows that pane's '제안으로 등록' "
     "checkbox instead of this argument.",
     "inputSchema": {"type": "object", "properties": {
         "node_id": {"type": "string"},
         "new": {"type": "object", "properties": {
             "parent_id": {"type": "string", "description": "existing node id; omit for a top-level category"},
             "label": {"type": "string"},
             "aliases": {"type": "array", "items": {"type": "string"}},
             "description": {"type": "string"}}, "required": ["label"]},
         "fold": {"type": "object", "description":
                  "merge existing categories into one new one at registration; the new category "
                  "lands under this work's closest common ancestor unless parent_id says otherwise",
                  "properties": {
                      "parent_id": {"type": "string", "description": "existing node id; omit to use the lowest common ancestor of the folded nodes"},
                      "label": {"type": "string"},
                      "aliases": {"type": "array", "items": {"type": "string"}},
                      "description": {"type": "string"},
                      "nodes": {"type": "array", "items": {"type": "string"},
                                "description": "ids of the existing categories to absorb"}},
                  "required": ["label", "nodes"]},
         "split": {"type": "object", "description":
                   "make several subcategories in one call and put this work's items across them",
                   "properties": {
                       "parent_id": {"type": "string", "description": "existing node id; omit for top-level children"},
                       "parts": {"type": "array", "items": {"type": "object", "properties": {
                           "label": {"type": "string"},
                           "aliases": {"type": "array", "items": {"type": "string"}},
                           "description": {"type": "string"},
                           "items": {"type": "array", "items": {"type": "string"},
                                     "description": "refs (session name or issue id) from this call's items; omit to take the rest"}},
                           "required": ["label"]}}},
                  "required": ["parts"]},
         "proposal": {"type": "boolean", "description":
                      "file a new category for a person to review instead of creating it now "
                      "(default false: create now)"},
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
        body = {k: args[k] for k in ("node_id", "new", "fold", "split", "proposal", "reason", "items")
                if args.get(k)}
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
