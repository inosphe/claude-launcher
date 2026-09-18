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
    if args.close:
        result = client.post("/api/connections/close", {"ids": args.close})
        closed = result.get("closed") or []
        gone = result.get("already_gone") or []
        print(f"closed {len(closed)}: {closed}" if closed else "closed none")
        if gone:
            print(f"already gone: {gone}")
        return 0
    if args.wizard:
        from .wizard import WizardUnavailable

        try:
            return _wizard(client)
        except WizardUnavailable as exc:
            print(str(exc), file=sys.stderr)
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
    p.add_argument(
        "--close", type=int, nargs="+", metavar="ID", default=[],
        help="close these sockets by the id the listing gives them; the "
             "viewer's own retry brings it back, so this ends a connection "
             "rather than a session",
    )
    p.add_argument(
        "--wizard", action="store_true",
        help="pick sockets off the list and close them (needs a terminal)",
    )
    p.set_defaults(func=_cmd_connections)


# --------------------------------------------------------------------------- #
# --wizard: the list, with the ability to end what is on it
# --------------------------------------------------------------------------- #
# A page whose terminal will not come up is looking at a socket that either
# does not exist or is not the one it wants, and the reading alone cannot
# settle which. Ending a connection and watching what the page does next can:
# the viewer's own retry brings it back, the same path a daemon restart puts
# it on, so this is a safe lever rather than a destructive one.
#
# The screen reuses the new-session wizard's primitives (raw terminal, key
# decoding, alternate screen) so there is one interactive dialect in this
# product, not two.
_HELP = "↑/↓ move · space mark · x close marked · r refresh · q quit"


def _wizard_rows(snapshot: dict) -> list:
    return list(snapshot.get("open") or [])


def _wizard_frame(rows: list, cursor: int, marked: set, snapshot: dict, note: str) -> str:
    http = snapshot.get("http_connections")
    ports = snapshot.get("ports_by_agent") or {}
    lines = [
        "\x1b[2J\x1b[H",
        f"  claunch connections — {len(rows)} socket(s) open, "
        f"{'unknown' if http is None else http} daemon connection(s)",
    ]
    if ports:
        lines.append(
            "  connections used recently: "
            + ", ".join(f"{agent} {n}" for agent, n in ports.items())
        )
    lines.append("")
    if not rows:
        lines.append("  (nothing open)")
    for i, row in enumerate(rows):
        mark = "x" if row.get("id") in marked else " "
        point = ">" if i == cursor else " "
        lines.append(
            f" {point}[{mark}] #{row.get('id')} {row.get('kind')} "
            f"{row.get('session')} from {_fmt_peer(row)}  {row.get('age_s')}s"
        )
    lines.append("")
    if note:
        lines.append(f"  {note}")
    lines.append(f"  {_HELP}")
    return "\r\n".join(lines) + "\r\n"


def _wizard(client) -> int:
    import codecs

    from . import attach as attach_mod
    from .wizard import _ENTER, _LEAVE, _write, decode_keys, require_terminal

    require_terminal()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    snapshot = client.get("/api/connections")
    rows = _wizard_rows(snapshot)
    cursor, marked, note = 0, set(), ""
    _write(_ENTER)
    try:
        with attach_mod._RawTerminal():
            while True:
                cursor = max(0, min(cursor, max(0, len(rows) - 1)))
                _write(_wizard_frame(rows, cursor, marked, snapshot, note))
                data = attach_mod._read_stdin()
                if not data:
                    return 0
                for key in decode_keys(decoder.decode(data)):
                    if key in ("q", "escape"):
                        return 0
                    if key in ("up", "k"):
                        cursor -= 1
                    elif key in ("down", "j"):
                        cursor += 1
                    elif key == " " and rows:
                        socket_id = rows[cursor]["id"]
                        marked.symmetric_difference_update({socket_id})
                    elif key == "r":
                        snapshot = client.get("/api/connections")
                        rows = _wizard_rows(snapshot)
                        note = "refreshed"
                    elif key == "x":
                        wanted = sorted(marked) or ([rows[cursor]["id"]] if rows else [])
                        if not wanted:
                            note = "nothing to close"
                            continue
                        result = client.post("/api/connections/close", {"ids": wanted})
                        marked.clear()
                        snapshot = client.get("/api/connections")
                        rows = _wizard_rows(snapshot)
                        note = (
                            f"closed {len(result.get('closed') or [])}, "
                            f"already gone {len(result.get('already_gone') or [])}"
                        )
    finally:
        _write(_LEAVE)
