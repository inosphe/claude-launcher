"""``claunch shell`` -- the shared user shell from a terminal.

The shell is the dashboard's CLI tab (``daemon/clipty.py``): one persistent,
unmanaged shell with the user's own privileges. It is shared with agent
sessions read-only; this command is the same surface from a terminal, over
``/api/shell*`` (``daemon/shell_api.py``).

  claunch shell status                 running/exited, cursor, pending count
  claunch shell read [--offset N]      output since a cursor (tail by default)
  claunch shell suggest TEXT --why ..  propose input (attributed to $CLAUNCH_SESSION)
  claunch shell suggestions            the proposal queue
  claunch shell accept ID              the user's control: type it and run it
  claunch shell reject ID              the user's control: decline it
  claunch shell withdraw ID            take back a proposal of this session

``read`` writes the shell's bytes (ANSI included) to stdout so it can be
piped; ``--json`` returns the whole response instead. ``accept`` and
``reject`` act as the user (the route refuses a session-shaped call), which
is the boundary the MCP tools keep: an agent can read and propose, never
type.

Exit codes: ``0`` fine, ``1`` the daemon refused, ``2`` the daemon could not
be asked.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

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


def _call(method: str, path: str, body=None):
    """``(response, 0)`` or ``(None, exit code)`` with the daemon's words."""
    client = _client()
    if client is None:
        return None, 2
    try:
        return client.request(method, path, body), 0
    except daemon_client.DaemonClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 1


def _print_json(value) -> None:
    print(json.dumps(value, indent=1, ensure_ascii=False))


def _cmd_status(args) -> int:
    data, code = _call("GET", "/api/shell")
    if code:
        return code
    if args.json:
        _print_json(data)
        return 0
    state = "exited" if data.get("exited") else "running"
    print(f"shell {data.get('id', 'cli')}: {state}"
          + (f" (pid {data['pid']})" if data.get("pid") else ""))
    print(f"  size: {data.get('cols')}x{data.get('rows')}  cursor: {data.get('offset')}"
          f"  pending suggestions: {data.get('pending', 0)}")
    return 0


def _cmd_read(args) -> int:
    q = {}
    if args.offset is not None:
        q["offset"] = args.offset
    if args.limit is not None:
        q["limit"] = args.limit
    data, code = _call(
        "GET", "/api/shell/output" + ("?" + urllib.parse.urlencode(q) if q else "")
    )
    if code:
        return code
    if args.json:
        _print_json(data)
    else:
        sys.stdout.write(data.get("text") or "")
        sys.stdout.flush()
    return 0


def _cmd_suggest(args) -> int:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        print("error: no $CLAUNCH_SESSION -- a suggestion is attributed to a session",
              file=sys.stderr)
        return 1
    data, code = _call("POST", "/api/shell/suggestions", {
        "session": session,
        "text": args.text,
        "why": args.why or "",
    })
    if code:
        return code
    row = data.get("suggestion") or {}
    if args.json:
        _print_json(row)
    else:
        print(f"{row.get('id')}: proposed — the user accepts it in the CLI tab")
    return 0


def _cmd_suggestions(args) -> int:
    q = {"state": args.state} if args.state else {}
    data, code = _call(
        "GET", "/api/shell/suggestions" + ("?" + urllib.parse.urlencode(q) if q else "")
    )
    if code:
        return code
    rows = data.get("suggestions") or []
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("no suggestions")
        return 0
    for row in rows:
        line = f"{row.get('id')}  {row.get('state'):9}  {row.get('session', '?')}"
        print(line)
        print(f"  {row.get('text', '')}")
        if row.get("why"):
            print(f"  why: {row['why']}")
    return 0


def _decide(args, verb: str) -> int:
    sid = urllib.parse.quote(args.suggestion, safe="")
    data, code = _call("POST", f"/api/shell/suggestions/{sid}/{verb}", {})
    if code:
        return code
    row = data.get("suggestion") or {}
    if args.json:
        _print_json(row)
    else:
        print(f"{row.get('id')}: {row.get('state')}")
    return 0


def _cmd_withdraw(args) -> int:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        print("error: no $CLAUNCH_SESSION -- only the proposing session withdraws",
              file=sys.stderr)
        return 1
    sid = urllib.parse.quote(args.suggestion, safe="")
    data, code = _call("POST", f"/api/shell/suggestions/{sid}/withdraw",
                       {"session": session})
    if code:
        return code
    row = data.get("suggestion") or {}
    if args.json:
        _print_json(row)
    else:
        print(f"{row.get('id')}: {row.get('state')}")
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "shell",
        help="the shared user shell (the CLI tab): read it, propose input to the user",
    )
    ssub = p.add_subparsers(dest="shell_cmd", required=True)

    st = ssub.add_parser("status", help="whether the shell runs, its cursor and pending suggestions")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=_cmd_status)

    rd = ssub.add_parser("read", help="print the shell's output (tail by default)")
    rd.add_argument("--offset", type=int, help="byte offset a previous read returned")
    rd.add_argument("--limit", type=int, help="maximum bytes (default 65536)")
    rd.add_argument("--json", action="store_true")
    rd.set_defaults(func=_cmd_read)

    sg = ssub.add_parser("suggest", help="propose input the user may accept and run")
    sg.add_argument("text", help="the exact input to run")
    sg.add_argument("--why", help="why you need it (shown to the user)")
    sg.add_argument("--json", action="store_true")
    sg.set_defaults(func=_cmd_suggest)

    sl = ssub.add_parser("suggestions", help="the proposal queue")
    sl.add_argument("--state", help="pending, accepted, rejected or withdrawn")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=_cmd_suggestions)

    ac = ssub.add_parser("accept", help="the user's control: type the suggestion and run it")
    ac.add_argument("suggestion", help="suggestion id (sh-xxxxxx)")
    ac.add_argument("--json", action="store_true")
    ac.set_defaults(func=lambda args: _decide(args, "accept"))

    rj = ssub.add_parser("reject", help="the user's control: decline the suggestion")
    rj.add_argument("suggestion", help="suggestion id (sh-xxxxxx)")
    rj.add_argument("--json", action="store_true")
    rj.set_defaults(func=lambda args: _decide(args, "reject"))

    wd = ssub.add_parser("withdraw", help="take back a proposal this session made")
    wd.add_argument("suggestion", help="suggestion id (sh-xxxxxx)")
    wd.add_argument("--json", action="store_true")
    wd.set_defaults(func=_cmd_withdraw)
