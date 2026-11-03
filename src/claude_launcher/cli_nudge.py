"""``claunch nudge`` -- the CLI half of the dashboard's nudge cards.

``nudge emergency "<message>"`` is the shell form of the ``emergency_nudge``
MCP tool (claunch-8myyt): a managed session raises a card without the run
waiting on anyone. ``nudge list`` reads the daemon's requests and ``nudge
resolve <id>`` is the person's answer that closes one. Resolving is refused
inside a managed session, the rule ``claunch window prioritize`` applies: an
agent that could close its own card would end the wait it raised.

Exit codes: ``0`` done, ``1`` refused by the daemon, ``2`` the daemon could
not be asked.
"""

from __future__ import annotations

import os
import sys

from . import daemon_client


def _client():
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        print(f"cannot ask the daemon: {daemon_client.unreachable_reason(report)}",
              file=sys.stderr)
    return client


def _cmd_emergency(args) -> int:
    session = os.environ.get("CLAUNCH_SESSION", "")
    if not session:
        print("refused: `claunch nudge emergency` runs inside a managed session "
              "($CLAUNCH_SESSION is not set)", file=sys.stderr)
        return 1
    if not args.cancel and not " ".join(args.message).strip():
        print("refused: give the message (the decision and your recommendation) or --cancel",
              file=sys.stderr)
        return 1
    client = _client()
    if client is None:
        return 2
    try:
        result = client.post("/api/flow-nudges", {
            "session": session, "cwd": os.getcwd(), "scope": session,
            "message": " ".join(args.message), "emergency": True, "cancel": args.cancel})
    except daemon_client.DaemonClientError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(result.get("note") or "done")
    return 0


def _cmd_list(args) -> int:
    client = _client()
    if client is None:
        return 2
    rows = (client.get("/api/flow-nudges") or {}).get("requests") or []
    for r in rows:
        if args.all or r.get("status") == "pending":
            print(f"{r.get('id')}  {r.get('status')}  {r.get('kind', 'flow')}  "
                  f"{r.get('session')}  {r.get('message')}")
    return 0


def _cmd_resolve(args) -> int:
    session = os.environ.get("CLAUNCH_SESSION")
    if session:
        print(f"refused: `claunch nudge resolve` is the person's answer and this shell is "
              f"managed session {session!r}. To withdraw your own emergency nudge use "
              f"`claunch nudge emergency --cancel`.", file=sys.stderr)
        return 1
    client = _client()
    if client is None:
        return 2
    try:
        client.post(f"/api/flow-nudges/{args.id}/resolve", {})
    except daemon_client.DaemonClientError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"resolved {args.id}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("nudge", help="dashboard nudge cards: emergency, list, resolve")
    nsub = p.add_subparsers(dest="nudge_command", required=True)

    e = nsub.add_parser("emergency", help="raise (or update) an emergency card for this "
                                          "session, whatever its run is doing")
    e.add_argument("message", nargs="*", help="the decision and your recommendation, "
                                              "one or two lines")
    e.add_argument("--cancel", action="store_true", help="withdraw this session's card")
    e.set_defaults(func=_cmd_emergency)

    ls = nsub.add_parser("list", help="the daemon's nudge requests (pending by default)")
    ls.add_argument("--all", action="store_true", help="settled and superseded too")
    ls.set_defaults(func=_cmd_list)

    r = nsub.add_parser("resolve", help="close a nudge card (the person's answer)")
    r.add_argument("id")
    r.set_defaults(func=_cmd_resolve)
