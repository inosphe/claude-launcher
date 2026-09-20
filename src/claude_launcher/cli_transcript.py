"""``claunch transcript ...`` — find a session's conversation and read it.

The terminal cannot answer "what did that session say an hour ago". A claude
session repaints the alternate screen instead of scrolling it, so its history
never reaches any scrollback; the readable record is the harness's own jsonl,
and until now the only door to it was the dashboard's transcript page. This
module is the shell's door to the same file: ``ls`` says which sessions have
one and where it is, ``show`` reads a page of it, and ``search`` says where in
it something was said.

The daemon does the reading. It owns the session records that say which
conversation belongs to which session, it holds the byte-offset index that
makes paging and searching cheap (:mod:`~claude_launcher.daemon.transcript_view`),
and it is where the files are addressed from. This file only formats.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from typing import List, Optional

from . import daemon_client

#: How many sessions a fleet-wide search reads before it stops and says so.
#: A search with no hits reads every record of every transcript it is given,
#: and the fleet holds hundreds — so the default is a recent slice, not the
#: whole history, and the footer names the flag that widens it.
MAX_SESSIONS = 25


def _client():
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        print(
            f"cannot read transcripts: {daemon_client.unreachable_reason(report)}",
            file=sys.stderr,
        )
        return None
    return client


def _get(client, path: str, params: dict, *, timeout: float = 180.0):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return client.get(f"{path}?{query}" if query else path, timeout=timeout)


def _size(value) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return "?"


def _one_line(text: str, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line[:limit] + "..." if len(line) > limit else line


# --------------------------------------------------------------------------- #
# ls / path
# --------------------------------------------------------------------------- #
def _cmd_ls(args) -> int:
    client = _client()
    if client is None:
        return 2
    try:
        view = _get(client, "/api/transcripts", {
            "state": args.state,
            "all": "1" if args.all else None,
            "deep": "1" if args.deep else None,
        })
    except daemon_client.DaemonClientError as exc:
        print(f"transcript ls: {exc}", file=sys.stderr)
        return 1
    rows = view.get("sessions") or []
    if args.limit:
        rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no session has a transcript on disk "
              "(`--state all --all` lists the sessions with none)")
        return 0
    print(f"{len(rows)} session(s) with a transcript, newest written first")
    for row in rows:
        records = row.get("records")
        counted = f"{records} records" if records is not None else "not counted"
        print(f"  {row.get('session'):<8} {row.get('status', '?'):<9} "
              f"{row.get('harness', '?'):<7} {_size(row.get('size')):>8}  "
              f"{row.get('modified_at') or '?':<21} {counted}")
        print(f"      {row.get('source') or '(no transcript)'}")
    return 0


def _cmd_path(args) -> int:
    client = _client()
    if client is None:
        return 2
    try:
        row = _get(client, f"/api/sessions/{args.session}/transcript/info", {})
    except daemon_client.DaemonClientError as exc:
        print(f"transcript path: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0
    source = row.get("source")
    if not source:
        cid = row.get("conversation_id")
        why = "the session holds no conversation id" if not cid else (
            f"no file for conversation {cid}")
        print(f"{args.session}: no transcript on disk — {why}", file=sys.stderr)
        return 1
    print(source)
    return 0


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #
def _render(record: dict, *, full: bool, prose: bool) -> List[str]:
    """One record as a reader sees it: a header line, then its blocks."""
    lines = [f"[{record.get('seq')}] {record.get('role', '?')} "
             f"{record.get('ts') or ''}"
             f"{'  (sidechain)' if record.get('sidechain') else ''}"]
    for block in record.get("blocks") or []:
        kind = str(block.get("type") or "")
        if prose and kind in ("tool_use", "tool_result"):
            continue
        text = str(block.get("text") or "")
        tail = ""
        if block.get("clipped"):
            tail = f"  ... +{int(block.get('full') or 0) - len(text)} more chars"
        if kind == "tool_use":
            head = f"  → {block.get('name') or '?'}"
        elif kind == "tool_result":
            head = "  ← result" + (" (error)" if block.get("error") else "")
        elif kind == "thinking":
            head = "  (thinking)"
        else:
            head = None
        if head is not None and not full:
            lines.append(f"{head}  {_one_line(text, 200)}{tail}")
            continue
        if head is not None:
            lines.append(head + tail)
        for line in text.splitlines() or [""]:
            lines.append(f"  {line}")
    return lines


def _cmd_show(args) -> int:
    client = _client()
    if client is None:
        return 2
    try:
        view = _get(client, f"/api/sessions/{args.session}/transcript", {
            "limit": str(args.limit),
            "before": None if args.before is None else str(args.before),
        })
    except daemon_client.DaemonClientError as exc:
        print(f"transcript show: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    if not view.get("source"):
        print(f"{args.session}: no transcript on disk", file=sys.stderr)
        return 1
    records = view.get("records") or []
    if args.role:
        records = [r for r in records if str(r.get("role") or "") in set(args.role)]
    print(f"{args.session}: {view.get('source')}")
    print(f"{view.get('total', 0)} records; showing "
          f"{len(records)} ending at {view.get('cursor', 0) + len(view.get('records') or [])}"
          + ("; older records above (`--before "
             f"{view.get('cursor', 0)}`)" if view.get("has_more") else ""))
    for record in records:
        print()
        for line in _render(record, full=args.full, prose=args.prose):
            print(line)
    return 0


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def _search_one(client, session: str, args, limit: int) -> Optional[dict]:
    try:
        return _get(client, f"/api/sessions/{session}/transcript/search", {
            "q": args.pattern,
            "limit": str(limit),
            "regex": "1" if args.regex else None,
            "case": "1" if args.case else None,
            "prose": "1" if args.prose else None,
            "role": ",".join(args.role) if args.role else None,
        })
    except daemon_client.DaemonClientError as exc:
        print(f"  {session}: {exc}", file=sys.stderr)
        return None


def _targets(client, args) -> Optional[List[str]]:
    """The sessions a search reads, newest conversation first."""
    if args.session:
        return list(args.session)
    try:
        view = _get(client, "/api/transcripts", {"state": args.state}, timeout=120.0)
    except daemon_client.DaemonClientError as exc:
        print(f"transcript search: {exc}", file=sys.stderr)
        return None
    return [row["session"] for row in (view.get("sessions") or []) if row.get("session")]


def _cmd_search(args) -> int:
    client = _client()
    if client is None:
        return 2
    targets = _targets(client, args)
    if targets is None:
        return 1
    capped = targets
    if not args.session and args.max_sessions:
        capped = targets[: args.max_sessions]

    found = 0
    read = 0
    rows = []
    for session in capped:
        if found >= args.limit:
            break
        view = _search_one(client, session, args, args.limit - found)
        read += 1
        if view is None:
            continue
        for match in view.get("matches") or []:
            found += 1
            rows.append((session, match, view.get("truncated")))
        if args.json:
            continue

    if args.json:
        print(json.dumps(
            [{"session": s, **m} for s, m, _ in rows], ensure_ascii=False, indent=2))
        return 0

    for session, match, _ in rows:
        print(f"{session}:{match.get('seq')} {match.get('role', '?')} "
              f"{match.get('ts') or ''} [{match.get('block')}]"
              f"{' x' + str(match['matches']) if match.get('matches', 1) > 1 else ''}")
        print(f"    {match.get('excerpt') or ''}")

    note = f"{found} match(es) in {read} session(s) read"
    if len(capped) < len(targets):
        note += (f"; {len(targets) - len(capped)} older transcript(s) not read "
                 f"(--max-sessions {len(targets)} reads them)")
    if found >= args.limit:
        note += "; stopped at --limit, older matches may exist"
    print(("\n" if rows else "") + note)
    print("read a hit in context with "
          "`claunch transcript show <session> --before <seq+1>`")
    return 0 if found else 1


def register(sub) -> None:
    p = sub.add_parser(
        "transcript",
        help="find, read and search a session's conversation on disk",
        description=(
            "A session's conversation is kept by its harness as an append-only "
            "jsonl, and it is the only readable record of what the session said: "
            "the terminal repaints its screen rather than scrolling it, so "
            "nothing older than the last frame is there to scroll back to. These "
            "commands find that file, read pages of it, and search it. The "
            "daemon holds the session records and the index, so it must be "
            "running."
        ),
    )
    tsub = p.add_subparsers(dest="transcript_cmd", required=True)

    p_ls = tsub.add_parser(
        "ls", help="sessions that have a transcript, newest written first")
    p_ls.add_argument("--state", default="all",
                      choices=("all", "active", "killed", "paused", "archived"),
                      help="which sessions to list (default all)")
    p_ls.add_argument("--all", action="store_true",
                      help="keep the sessions with no transcript on disk")
    p_ls.add_argument("--deep", action="store_true",
                      help="search the whole config dir for a file the direct "
                           "address misses (slow: a directory walk per session)")
    p_ls.add_argument("--limit", type=int, default=0, help="show at most N rows")
    p_ls.add_argument("--json", action="store_true")
    p_ls.set_defaults(func=_cmd_ls)

    p_path = tsub.add_parser(
        "path", help="print one session's transcript file path")
    p_path.add_argument("session", help="session name (as `claunch sessions` lists it)")
    p_path.add_argument("--json", action="store_true",
                        help="the whole row: path, size, mtime, record count")
    p_path.set_defaults(func=_cmd_path)

    p_show = tsub.add_parser(
        "show", help="read a page of the conversation (the tail by default)",
        description=(
            "Records are printed oldest-last, as the conversation ran. `--before "
            "<seq>` walks backwards: the footer of each page prints the cursor "
            "for the page above it."
        ),
    )
    p_show.add_argument("session")
    p_show.add_argument("--limit", type=int, default=40,
                        help="records per page (1..200, default 40)")
    p_show.add_argument("--before", type=int,
                        help="end the page just before this record's seq")
    p_show.add_argument("--role", action="append",
                        help="keep only these roles (repeatable: user, assistant)")
    p_show.add_argument("--prose", action="store_true",
                        help="drop tool calls and their results")
    p_show.add_argument("--full", action="store_true",
                        help="print tool calls and results whole, as the daemon "
                             "sends them (it clips each at 2000 characters)")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_find = tsub.add_parser(
        "search", help="where in a conversation something was said",
        description=(
            "Matches are reported newest first, one line each, with the record's "
            "seq — which `claunch transcript show --before <seq+1>` reads in "
            "context. With no --session the search reads the most recently "
            "written transcripts in the fleet and says how many it left."
        ),
    )
    p_find.add_argument("pattern", help="a literal string, or a regex with --regex")
    p_find.add_argument("--session", action="append",
                        help="search this session only (repeatable)")
    p_find.add_argument("--state", default="all",
                        choices=("all", "active", "killed", "paused", "archived"),
                        help="which sessions the fleet search covers (default all)")
    p_find.add_argument("--regex", action="store_true",
                        help="read the pattern as a Python regular expression")
    p_find.add_argument("--case", action="store_true",
                        help="match case (the default ignores it)")
    p_find.add_argument("--prose", action="store_true",
                        help="search what was said only, not tool calls or results")
    p_find.add_argument("--role", action="append",
                        help="keep only these roles (repeatable: user, assistant)")
    p_find.add_argument("--limit", type=int, default=20,
                        help="stop after this many matches (default 20)")
    p_find.add_argument("--max-sessions", type=int, default=MAX_SESSIONS,
                        help=f"sessions a fleet search reads (default {MAX_SESSIONS}; "
                             "0 reads every one)")
    p_find.add_argument("--json", action="store_true")
    p_find.set_defaults(func=_cmd_search)
