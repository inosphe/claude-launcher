"""``claunch search ...`` and ``claunch rag ...`` — semantic search from a shell.

``search`` asks the running daemon to rank the board (``--kind beads``, the
default), the fleet (``--kind sessions``) or the combined corpus
(``--kind unified``; its pre-rename name ``all`` is refused at the parser)
for a query: the same ``GET /api/search`` the dashboard's
search boxes call, so an
agent looking for "the issue about X" gets the answer the operator would.
``--mode active`` (only with ``--kind unified``) drops the documents of sessions
that are no longer live, which is what the dashboard's active-search box asks
for; the daemon enforces the same pairing, so this file refuses it first.
``rag status`` says whether the feature is configured and how much of each
corpus the index covers; ``rag reindex`` starts a sync (``--force`` re-embeds
everything).

The daemon does the work because the index lives in its directory and the
endpoint key in its config; this file only formats. Nothing here reads the
``rag:`` block itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

from . import daemon_client


def _client():
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        print(
            f"cannot search: {daemon_client.unreachable_reason(report)}",
            file=sys.stderr,
        )
        return None
    return client


def _cwd(args) -> str:
    return os.path.abspath(getattr(args, "cwd", None) or os.getcwd())


def _fmt_row(row: dict, kind: str) -> str:
    score = row.get("rerank_score")
    if score is None:
        score = row.get("score")
    try:
        score_s = f"{float(score):.3f}"
    except (TypeError, ValueError):
        score_s = "?"
    if kind == "sessions":
        head = row.get("name") or row.get("id") or "?"
        bits = [b for b in (row.get("status"), row.get("identity"), row.get("issue")) if b]
        line = row.get("one_line") or row.get("excerpt") or ""
    else:
        head = row.get("id") or "?"
        bits = [b for b in (row.get("status"), row.get("assignee")) if b]
        pri = row.get("priority")
        if pri is not None:
            bits.insert(0, f"P{pri}")
        line = row.get("title") or ""
    tag = "  [lexical]" if row.get("lexical") else ""
    meta = f" ({', '.join(bits)})" if bits else ""
    return f"{score_s}  {head}{meta}  {line}{tag}"


def _kind_of(args) -> str:
    """The corpus the caller named.

    The names are the daemon's (``daemon/rag.py`` ``KINDS``) and this parser's
    ``choices`` is the same list, so a name that reaches here is one the
    daemon answers. ``all`` — the combined corpus's name before the rename
    (claunch-7aujp.1) — is not among them and argparse refuses it.
    """
    return args.kind


def _cmd_search(args) -> int:
    kind = _kind_of(args)
    # The daemon answers 400 for this pairing; refusing it here means the
    # shell never reaches the network with a request that cannot succeed
    # (daemon/api.py ``_search_args``, daemon/rag.py ``search_stream``).
    if args.mode == "active" and kind != "unified":
        args.parser.error("--mode active is only valid with --kind unified")
    client = _client()
    if client is None:
        return 2
    query = " ".join(args.query).strip()
    if not query:
        print("search: a query is required", file=sys.stderr)
        return 2
    params = {
        "q": query,
        "kind": kind,
        "mode": args.mode,
        "limit": str(args.limit),
        "rerank": "0" if args.no_rerank else "1",
        "wait": str(args.wait),
    }
    if kind == "beads":
        params["cwd"] = _cwd(args)
    try:
        view = client.get("/api/search?" + urllib.parse.urlencode(params), timeout=180.0)
    except daemon_client.DaemonClientError as exc:
        print(f"search: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    index = view.get("index") or {}
    results = view.get("results") or []
    coverage = f"{index.get('indexed', 0)}/{index.get('total', 0)} indexed"
    if index.get("syncing"):
        coverage += ", sync in progress"
    if index.get("error"):
        coverage += f", last sync error: {index['error']}"
    print(f"{kind}: {len(results)} result(s) for {query!r} ({coverage}"
          f"{', reranked' if view.get('reranked') else ''})")
    for row in results:
        print("  " + _fmt_row(row, kind))
    return 0


def _cmd_rag_status(args) -> int:
    client = _client()
    if client is None:
        return 2
    try:
        view = client.get("/api/rag/status")
    except daemon_client.DaemonClientError as exc:
        print(f"rag: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    if not view.get("configured"):
        print("rag: not configured — fill the rag: block in ~/.claude-launcher/.claunch.yaml "
              "(base_url, api_key, embedding_model; rerank_model optional)")
        return 0
    print(f"rag: {view.get('host') or '?'}  embed={view.get('embedding_model')}"
          f"  rerank={view.get('rerank_model') or '(none)'}"
          f"  tls={'verify' if view.get('verify_tls') else 'NO VERIFY'}"
          f"  dims={view.get('dimensions') or 'model'}")
    indexes = view.get("indexes") or []
    if not indexes:
        print("  no index loaded yet (a first search or `claunch rag reindex` builds one)")
    for row in indexes:
        state = "syncing" if row.get("syncing") else "idle"
        line = (f"  {row.get('kind')}: {row.get('documents', 0)} documents, "
                f"{row.get('indexed', 0)}/{row.get('total', 0)} current, "
                f"{row.get('pending', 0)} pending, {state}")
        if row.get("error"):
            line += f", error: {row['error']}"
        print(line)
        print(f"    {row.get('path')}")
    return 0


def _cmd_rag_reindex(args) -> int:
    kind = _kind_of(args)
    client = _client()
    if client is None:
        return 2
    body = {"kind": kind, "force": bool(args.force)}
    if kind == "beads":
        body["cwd"] = _cwd(args)
    try:
        view = client.post("/api/rag/reindex", body)
    except daemon_client.DaemonClientError as exc:
        print(f"rag: {exc}", file=sys.stderr)
        return 1
    index = view.get("index") or {}
    print(f"rag: {kind} sync started — {index.get('indexed', 0)}/{index.get('total', 0)} "
          f"current, {index.get('pending', 0)} pending; `claunch rag status` follows it")
    if kind == "unified":
        print("rag: this sync rebuilds only the combined corpus; board buckets "
              "(kind beads) are separate and are not touched", file=sys.stderr)
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "search",
        help="semantic search over the board or the fleet (needs the rag: block)",
        description=(
            "Rank the repository board (default) or the daemon's sessions for a "
            "query, by embedding and, when configured, a reranker. The daemon "
            "keeps the index; a first search of a large board answers from what "
            "is indexed so far and says how much that is."
        ),
    )
    p.add_argument("query", nargs="+", help="the query, in any language the model reads")
    p.add_argument("--kind", choices=("beads", "sessions", "unified"),
                   default="beads",
                   help="what to search: this board, the fleet, or the combined corpus "
                        "(default beads)")
    p.add_argument("--mode", choices=("general", "active"), default="general",
                   help="general, or active to drop documents of sessions that are no "
                        "longer live (only with --kind unified)")
    p.add_argument("--limit", type=int, default=10, help="results to show (1..50, default 10)")
    p.add_argument("--no-rerank", action="store_true", help="vector ranking only")
    p.add_argument("--wait", type=int, default=2,
                   help="seconds to give a running index sync before answering (default 2)")
    p.add_argument("-C", "--cwd", help="a directory inside the repository whose board to search")
    p.add_argument("--json", action="store_true", help="print the daemon's answer as JSON")
    # The parser rides along so the kind/mode pairing is refused the way any
    # other bad argument is: usage on stderr, exit 2, no request sent.
    p.set_defaults(func=_cmd_search, parser=p)

    p_rag = sub.add_parser(
        "rag",
        help="the semantic-search index: status and reindex",
        description="Inspect or rebuild the embedding index behind `claunch search`.",
    )
    rsub = p_rag.add_subparsers(dest="rag_cmd", required=True)
    ps = rsub.add_parser("status", help="configuration and index coverage")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=_cmd_rag_status)
    pr = rsub.add_parser("reindex", help="start an index sync for a corpus")
    # The daemon's KINDS is the list, not this file: `unified` is the combined
    # corpus, and a CLI that refused it made the whole corpus unreachable from
    # a shell while `/api/rag/reindex` accepted it (claunch-i2i95.1). Its
    # pre-rename name `all` is gone with the rename (claunch-7aujp.1).
    pr.add_argument("--kind", choices=("beads", "sessions", "unified"),
                    default="beads",
                    help="which corpus (default beads; unified is the combined one)")
    pr.add_argument("--force", action="store_true", help="re-embed every document")
    pr.add_argument("-C", "--cwd", help="a directory inside the repository whose board to index")
    pr.set_defaults(func=_cmd_rag_reindex)
