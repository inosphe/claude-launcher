"""``claunch connections`` -- what the daemon is holding open right now.

A thin client over ``GET /api/connections`` (``daemon/connections.py``). The
command exists because the question it answers cannot be answered from a
browser: the page that is failing to connect is the one whose sockets are in
question, and opening a dashboard to count sockets adds one to the count.
This runs from a shell and adds nothing.

Exit codes: ``0`` read fine, ``2`` the daemon could not be asked -- the same
split ``claunch window`` uses, so a script can tell "the daemon said" from
"the daemon was not there".
"""

from __future__ import annotations

import json
import sys
import time

from . import daemon_client


def _client():
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        print(
            f"cannot ask the daemon: {daemon_client.unreachable_reason(report)}",
            file=sys.stderr,
        )
        return None
    return client


def _fmt_peer(row: dict) -> str:
    port = row.get("peer_port")
    return f"{row.get('peer_ip', '?')}:{port}" if port else str(row.get("peer_ip", "?"))


def _print(snapshot: dict, closed_limit: int) -> None:
    http = snapshot.get("http_connections")
    http_text = "unknown" if http is None else str(http)
    print(
        f"{snapshot.get('open_count', 0)} socket(s) open "
        f"(all daemon connections: {http_text})  at {snapshot.get('now', '?')}"
    )
    ports = snapshot.get("ports_by_agent") or {}
    if ports:
        # Every peer here is 127.0.0.1, so this is the line that separates a
        # browser's connections from a shell's. A browser at its own
        # per-server ceiling stops adding ports while its page says it cannot
        # connect.
        print(
            "  connections used recently: "
            + ", ".join(f"{agent} {n}" for agent, n in ports.items())
        )
    by_peer = snapshot.get("by_peer") or {}
    if by_peer:
        per = ", ".join(f"{ip} {n}" for ip, n in sorted(by_peer.items()))
        print(f"  per peer: {per}")
    for row in snapshot.get("open") or []:
        print(
            f"  #{row.get('id')} {row.get('kind')} {row.get('session')} "
            f"from {_fmt_peer(row)}  {row.get('age_s')}s"
        )
    refused = (snapshot.get("refused") or [])[:closed_limit]
    if refused:
        # The line that answers "why will this terminal not come up". An
        # upgrade refused here never became a socket, so it appears nowhere
        # above and nowhere in the daemon's close lines.
        print(f"  {snapshot.get('refused_count', len(refused))} refused, last {len(refused)}:")
        for row in refused:
            kind = "upgrade" if row.get("upgrade") else row.get("method", "?")
            credential = (
                "cookie" if row.get("had_cookie")
                else "bearer" if row.get("had_bearer") else "none"
            )
            print(
                f"    {row.get('at')} {kind} {row.get('path')} "
                f"from {_fmt_peer(row)} sent={credential} -- {row.get('reason')}"
            )
    closed = (snapshot.get("closed") or [])[:closed_limit]
    if closed:
        print(f"  last {len(closed)} closed:")
        for row in closed:
            error = row.get("error")
            reason = f" {error}" if error else ""
            print(
                f"    {row.get('closed_at')} {row.get('kind')} {row.get('session')} "
                f"from {_fmt_peer(row)} after {row.get('age_s')}s "
                f"code={row.get('code')}{reason}"
            )


def _cmd_connections(args) -> int:
    client = _client()
    if client is None:
        return 2
    while True:
        snapshot = client.get("/api/connections")
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        else:
            _print(snapshot, args.closed)
        if not args.watch:
            return 0
        time.sleep(args.watch)
        print()


def register(sub) -> None:
    p = sub.add_parser(
        "connections",
        aliases=["conns"],
        help="list the terminal sockets the daemon holds open, and the last "
             "ones that closed",
    )
    p.add_argument(
        "--watch", type=float, metavar="SECONDS", default=0.0,
        help="reprint every SECONDS instead of once (Ctrl-C to stop)",
    )
    p.add_argument(
        "--closed", type=int, default=10, metavar="N",
        help="how many recently closed sockets to show (default 10)",
    )
    p.add_argument("--json", action="store_true", help="print the raw reading")
    p.set_defaults(func=_cmd_connections)
