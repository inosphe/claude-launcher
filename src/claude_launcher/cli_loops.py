"""``claunch loops`` -- a session's open-loop ledger from the shell.

The ledger itself is the daemon's (``daemon/loops.py``); this is the thin
client the MCP ``loops`` / ``loop_add`` / ``loop_close`` tools share their
routes with, for a session (or a person at its ``!`` shell) that prefers a
command. Exit codes: ``0`` done, ``1`` the daemon said no (no such loop),
``2`` the daemon could not be asked.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote

from . import daemon_client
from .daemon import loops as loops_mod


def _client():
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        print(
            f"cannot reach the ledger: {daemon_client.unreachable_reason(report)}",
            file=sys.stderr,
        )
        return None
    return client


def _session(args) -> str:
    name = getattr(args, "session", None) or os.environ.get("CLAUNCH_SESSION")
    if not name:
        print(
            "error: no session: pass --session NAME, or run this inside a "
            "managed session (which sets $CLAUNCH_SESSION)",
            file=sys.stderr,
        )
    return name or ""


def _cmd_ls(args) -> int:
    name = _session(args)
    if not name:
        return 2
    client = _client()
    if client is None:
        return 2
    q = "?all=1" if args.all else ""
    doc = client.get(f"/api/sessions/{name}/loops{q}")
    rows = doc.get("all") if args.all else doc.get("open")
    rows = rows or []
    if not rows:
        print(f"{name}: no open loops")
        return 0
    stale = doc.get("stale") or 0
    print(f"{name}: {len(doc.get('open') or [])} open" + (f" ({stale} stale)" if stale else ""))
    for entry in rows:
        line = loops_mod.line(entry)
        if entry.get("closed_at"):
            line += f" [closed {entry['closed_at']}"
            if entry.get("closed_note"):
                line += f": {entry['closed_note']}"
            line += "]"
        print(line)
    return 0


def _cmd_add(args) -> int:
    name = _session(args)
    if not name:
        return 2
    client = _client()
    if client is None:
        return 2
    payload = {"what": args.what}
    if args.resume_when:
        payload["resume_when"] = args.resume_when
    if args.then:
        payload["then"] = args.then
    if args.key:
        payload["key"] = args.key
    if args.expires_in is not None:
        payload["expires_in"] = args.expires_in
    if args.ref:
        refs = {}
        for item in args.ref:
            k, _, v = item.partition("=")
            if not k or not v:
                print(f"error: --ref wants KEY=VALUE, got {item!r}", file=sys.stderr)
                return 2
            refs[k] = v
        payload["refs"] = refs
    doc = client.post(f"/api/sessions/{name}/loops", payload)
    print(loops_mod.line(doc.get("loop") or {}))
    return 0


def _cmd_close(args) -> int:
    name = _session(args)
    if not name:
        return 2
    client = _client()
    if client is None:
        return 2
    try:
        doc = client.post(
            f"/api/sessions/{name}/loops/{quote(args.id, safe='')}/close",
            {"note": args.note or ""},
        )
    except daemon_client.DaemonClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    entry = doc.get("loop") or {}
    print(f"closed {entry.get('id')}: {entry.get('what')}")
    return 0


def register(sub) -> None:
    p_loops = sub.add_parser(
        "loops",
        help="a session's open loops: what it is waiting on, kept by the "
             "daemon so a re-briefing hands it back after /compact or /clear",
    )
    lsub = p_loops.add_subparsers(dest="loops_command", required=True)

    p = lsub.add_parser("ls", help="list open loops (default: $CLAUNCH_SESSION)")
    p.add_argument("--session", help="whose ledger (default: $CLAUNCH_SESSION)")
    p.add_argument("--all", action="store_true", help="include closed entries")
    p.set_defaults(func=_cmd_ls)

    p = lsub.add_parser("add", help="record something you are waiting on")
    p.add_argument("what", help="what is waited on")
    p.add_argument("--resume-when", dest="resume_when", help="the condition that ends the wait")
    p.add_argument("--then", help="what to do once it ends")
    p.add_argument("--key", help="dedupe key: re-adding while open updates in place")
    p.add_argument(
        "--expires-in", dest="expires_in", type=float, metavar="SECS",
        help="seconds until the loop counts as stale (default 6h)",
    )
    p.add_argument(
        "--ref", action="append", metavar="KEY=VALUE",
        help="an id the wait is about (issue=..., message=..., session=...)",
    )
    p.add_argument("--session", help="whose ledger (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_add)

    p = lsub.add_parser("close", help="end one open loop")
    p.add_argument("id", help="the loop id")
    p.add_argument("--note", help="how it resolved")
    p.add_argument("--session", help="whose ledger (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_close)
