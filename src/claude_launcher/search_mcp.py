"""MCP access to the daemon's "Search anything": ``search`` and ``search_read``.

The dashboard's search box ranks one unified corpus -- every board the daemon
knows (issues *and* each comment as its own passage), every session's
identity/note/summary, its archived opening task and its Observer records
(briefings, reports, answers) -- and the same ranking already sits behind
``GET /api/search``. An agent at intake is exactly the reader who needs it:
"has this been done, attempted, or is somebody on it right now?" is a question
about issues *and* sessions at once, and ``claunch beads search`` only sees
the first half, by substring.

So this module is a thin, token-conscious front on that endpoint:

* ``search`` returns a compact row per hit -- what it is (issue, comment,
  session, briefing...), which issue and which sessions it belongs to with
  their *current* state, a short excerpt, and a ``read`` handle. The daemon's
  row carries hrefs, roots and full chunk text meant for a browser; those are
  dropped or cut here because every byte lands in the caller's context.
* ``search_read`` takes that ``read`` handle and returns the whole source:
  the issue with its comments, the archived record, or the session's
  definition. Only handles this module hands out are accepted -- it is a
  reader for search results, not a generic GET on the daemon's API.

The daemon owns the index and the endpoint key; nothing here reads the
``rag:`` block. When it is not configured the tool says so and names the
lexical fallback, instead of pretending the board is empty.
"""

from __future__ import annotations

import json
import os
import urllib.parse

from . import daemon_client, mcp_rpc

KINDS = ("all", "beads", "sessions")

#: Characters of a hit's passage kept on the row. Enough to judge relevance;
#: the rest is one ``search_read`` away.
EXCERPT_CHARS = 360

#: A ``search_read`` answer larger than this (as JSON) sheds its oldest
#: comments first: a long-running issue can carry a hundred of them, and the
#: newest ones are where its state is.
READ_CHARS = 40000

_SESSION_FIELDS = (
    "name", "status", "paused", "archived", "cwd", "role", "parent", "issue",
    "note", "task", "created_at", "exited_at", "last_activity_at",
)

TOOLS = [
    {
        "name": "search",
        "description": (
            "Search anything the claunch daemon has seen: beads issues and "
            "their comments on every known board, sessions (identity, note, "
            "summary, opening task) and their Observer records (briefings, "
            "reports). Ranked by embedding plus reranker, with exact-id/word "
            "hits pulled to the front. Use it at intake to find prior or "
            "in-flight work on the same subject before creating anything. "
            "Each row names the issue and the sessions it belongs to, with "
            "their current state, and a 'read' handle for search_read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "what to look for, in any language; an issue id or session name also works",
                },
                "kind": {
                    "type": "string",
                    "enum": list(KINDS),
                    "description": (
                        "'all' (default): the unified corpus. 'beads': issues of "
                        "this session's board only, one row per issue. "
                        "'sessions': one row per session."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "rows to return (default 10)",
                },
                "rerank": {
                    "type": "boolean",
                    "description": "false skips the reranker: faster, rougher order (default true)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_read",
        "description": (
            "Open one search result in full, by the 'read' handle a search "
            "row carried: an issue with its comments, an archived session "
            "record (briefing, report, opening task), or a session's "
            "definition and live state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "read": {
                    "type": "string",
                    "description": "the 'read' value of a search row",
                },
            },
            "required": ["read"],
        },
    },
]


class SearchMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise SearchMcpError(f"cannot search: {daemon_client.unreachable_reason(why)}")
    return client


def _clip(text, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _normalise(row: dict, corpus: str, root) -> dict:
    """Give a row of the per-kind corpora the shape of a unified-corpus row.

    The ``beads`` corpus keys a row by the issue id and names no kind or
    source; the ``sessions`` corpus keys it by the session name. The unified
    corpus already carries ``kind``, ``issue`` and ``source_url``.
    """
    row = dict(row)
    if corpus == "beads":
        row.setdefault("kind", "beads")
        row.setdefault("issue", row.get("id"))
        if not row.get("source_url") and row.get("issue"):
            query = "?cwd=" + urllib.parse.quote(str(root), safe="") if root else ""
            row["source_url"] = "api/beads/" + urllib.parse.quote(str(row["issue"]), safe="") + query
    elif corpus == "sessions":
        row.setdefault("kind", "session")
        row.setdefault("name", row.get("id"))
    return row


def _read_handle(row: dict) -> str:
    if row.get("source_url"):
        return row["source_url"]
    if row.get("kind") == "session" and row.get("name"):
        return "session:" + row["name"]
    return ""


def _session_label(entry: dict) -> str:
    state = entry.get("status") or "?"
    if entry.get("archived"):
        state += ", archived"
    elif entry.get("paused"):
        state += ", paused"
    via = entry.get("via")
    label = f"{entry.get('name')} ({state})"
    return label + (" via " + "/".join(via) if via else "")


def _compact(row: dict) -> dict:
    out = {"kind": row.get("kind")}
    for key in ("issue", "name", "status", "state", "assignee", "priority"):
        if row.get(key) not in (None, ""):
            out[key] = row[key]
    title = row.get("title")
    if title:
        out["title"] = title
    sessions = [_session_label(s) for s in row.get("sessions") or [] if s.get("name")]
    if sessions:
        out["sessions"] = sessions
    at = row.get("at") or row.get("updated_at")
    if at:
        out["at"] = at
    score = row.get("rerank_score", row.get("score"))
    if isinstance(score, (int, float)):
        out["score"] = round(float(score), 3)
    if row.get("lexical"):
        out["exact_match"] = True
    excerpt = row.get("excerpt") or row.get("one_line") or ""
    # The daemon's chunk repeats the title as its first line.
    if title and str(excerpt).startswith(title):
        excerpt = str(excerpt)[len(title):]
    if excerpt:
        out["excerpt"] = _clip(excerpt, EXCERPT_CHARS)
    handle = _read_handle(row)
    if handle:
        out["read"] = handle
    return out


def search(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise SearchMcpError("query is required")
    kind = str(args.get("kind") or "all")
    if kind not in KINDS:
        raise SearchMcpError(f"kind must be one of {', '.join(KINDS)}")
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        raise SearchMcpError("limit must be an integer 1..50")
    limit = max(1, min(50, limit))
    params = {
        "q": query, "kind": kind, "limit": str(limit),
        "rerank": "0" if args.get("rerank") is False else "1", "wait": "2",
    }
    if kind == "beads":
        params["cwd"] = os.getcwd()
    try:
        view = _client().get("/api/search?" + urllib.parse.urlencode(params), timeout=180.0)
    except daemon_client.DaemonClientError as exc:
        if "not configured" in str(exc):
            raise SearchMcpError(
                "semantic search is not configured on this daemon (the rag: block "
                "in ~/.claunch.yaml). Fall back to `claunch beads search \"<words>\" "
                "--json` for the board and `claunch sessions` for the fleet."
            ) from exc
        raise
    index = view.get("index") or {}
    coverage = f"{index.get('indexed', 0)}/{index.get('total', 0)} indexed"
    if index.get("syncing"):
        coverage += ", sync in progress"
    answer = {
        "query": query,
        "kind": kind,
        "coverage": coverage,
        "reranked": bool(view.get("reranked")),
        "results": [
            _compact(_normalise(r, kind, view.get("root"))) for r in view.get("results") or []
        ],
    }
    if view.get("warnings"):
        answer["warnings"] = view["warnings"]
    return answer


def _shed_comments(issue: dict) -> dict:
    comments = issue.get("comments")
    if not isinstance(comments, list):
        return issue
    dropped = 0
    while comments and len(json.dumps(issue, ensure_ascii=False)) > READ_CHARS:
        comments.pop(0)
        dropped += 1
    if dropped:
        issue["comments_omitted"] = (
            f"{dropped} oldest comment(s) left out to fit; "
            f"`claunch beads comments {issue.get('id')}` lists them all"
        )
    return issue


def read(args: dict) -> dict:
    handle = str(args.get("read") or "").strip().lstrip("/")
    if not handle:
        raise SearchMcpError("read is required: the 'read' value of a search row")
    if ".." in handle.split("?", 1)[0]:
        raise SearchMcpError(f"not a search handle: {handle!r}")
    client = _client()
    if handle.startswith("session:"):
        name = handle[len("session:"):]
        if not name:
            raise SearchMcpError("session: handle without a name")
        try:
            info = client.get("/api/sessions/" + urllib.parse.quote(name, safe=""))
        except daemon_client.DaemonClientError as exc:
            raise SearchMcpError(
                f"session {name!r} is no longer registered ({exc}); its archived "
                f"records stay searchable -- search for {name!r} with kind 'all'"
            ) from exc
        return {k: info.get(k) for k in _SESSION_FIELDS if info.get(k) not in (None, "")}
    if handle.startswith("api/search/records/"):
        return client.get("/" + handle)
    if handle.startswith("api/beads/"):
        view = client.get("/" + handle)
        if isinstance(view, dict) and isinstance(view.get("issue"), dict):
            view["issue"] = _shed_comments(view["issue"])
        elif isinstance(view, dict):
            view = _shed_comments(view)
        return view
    raise SearchMcpError(
        f"not a search handle: {handle!r} -- pass the 'read' value of a search row"
    )


def call_tool(name: str, args: dict) -> dict:
    if name == "search":
        return search(args or {})
    if name == "search_read":
        return read(args or {})
    raise SearchMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-search",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(SearchMcpError, daemon_client.DaemonClientError),
)
