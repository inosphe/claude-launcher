"""``claunch shell`` -- a session's shell from a terminal.

Every session has its own shell (``daemon/clipty.py``'s ``SessionShell``,
``daemon/session_shells.py``), shown in that session's ``cli`` split pane. Its
agent may read it and propose input; only the user decides. This command is
the same surface from a terminal, over ``/api/sessions/<name>/shell/*``
(``daemon/shell_api.py``).

  claunch shell [--session NAME] status          running/exited, cursor, pending
  claunch shell [--session NAME] read [--offset N]   output since a cursor
  claunch shell [--session NAME] suggest TEXT --why ..
  claunch shell [--session NAME] suggestions     the proposal queue
  claunch shell [--session NAME] accept ID       the user's control: type it
  claunch shell [--session NAME] reject ID       the user's control: decline
  claunch shell [--session NAME] withdraw ID     take back a proposal

``--session`` addresses one session's shell and defaults to
``$CLAUNCH_SESSION``. Naming another session is how a person inspects a
shell they are not sitting in, and how they accept or reject there; the
verbs that make a proposal act as a session, so ``suggest``/``withdraw``
refuse a ``--session`` that disagrees with ``$CLAUNCH_SESSION`` — an agent
cannot propose into somebody else's shell by passing a flag.

``read`` writes the shell's bytes (ANSI included) to stdout so it can be
piped; ``--json`` returns the whole response instead. ``accept`` and
``reject`` act as the user (the route refuses a session-shaped call), which
is the boundary the MCP tools keep: an agent can read and propose, never
type.

Exit codes: ``0`` fine, ``1`` the daemon refused or no session was named,
``2`` the daemon could not be asked.
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


def _target(args) -> "str | None":
    """The session whose shell this call addresses.

    ``--session`` wins; without it the caller's own session. Named in the
    error when neither exists, because "which shell?" is the one thing the
    rest of the command cannot guess.
    """
    name = getattr(args, "session", None) or os.environ.get("CLAUNCH_SESSION")
    if not name:
        print(
            "error: no session to address -- pass --session <name> or run "
            "inside a session ($CLAUNCH_SESSION)",
            file=sys.stderr,
        )
        return None
    return name


def _base(session: str) -> str:
    return "/api/sessions/" + urllib.parse.quote(session, safe="") + "/shell"


def _owner(args) -> "str | None":
    """The session a proposal is attributed to.

    The two verbs that create state act as the session that owns the shell,
    so naming a different one is refused rather than honoured — this is the
    same rule the MCP tools cannot break because they have no such argument.
    """
    name = _target(args)
    if name is None:
        return None
    mine = os.environ.get("CLAUNCH_SESSION")
    if mine and mine != name:
        print(
            f"error: a proposal belongs to the session making it ({mine}); "
            f"--session {name} is a different session's shell",
            file=sys.stderr,
        )
        return None
    return name


def _cmd_status(args) -> int:
    session = _target(args)
    if session is None:
        return 1
    data, code = _call("GET", _base(session))
    if code:
        return code
    if args.json:
        _print_json(data)
        return 0
    state = "exited" if data.get("exited") else "running"
    print(f"shell {data.get('id', session)}: {state}"
          + (f" (pid {data['pid']})" if data.get("pid") else ""))
    print(f"  size: {data.get('cols')}x{data.get('rows')}  cursor: {data.get('offset')}"
          f"  pending suggestions: {data.get('pending', 0)}")
    return 0


def _cmd_read(args) -> int:
    session = _target(args)
    if session is None:
        return 1
    q = {}
    if args.offset is not None:
        q["offset"] = args.offset
    if args.limit is not None:
        q["limit"] = args.limit
    data, code = _call(
        "GET", _base(session) + "/output" + ("?" + urllib.parse.urlencode(q) if q else "")
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
    session = _owner(args)
    if session is None:
        return 1
    data, code = _call("POST", _base(session) + "/suggestions", {
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
        print(f"{row.get('id')}: proposed — the user accepts it in this "
              f"session's CLI pane")
    return 0


def _cmd_suggestions(args) -> int:
    session = _target(args)
    if session is None:
        return 1
    q = {"state": args.state} if args.state else {}
    data, code = _call(
        "GET", _base(session) + "/suggestions" + ("?" + urllib.parse.urlencode(q) if q else "")
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
    session = _target(args)
    if session is None:
        return 1
    sid = urllib.parse.quote(args.suggestion, safe="")
    data, code = _call("POST", f"{_base(session)}/suggestions/{sid}/{verb}", {})
    if code:
        return code
    row = data.get("suggestion") or {}
    if args.json:
        _print_json(row)
    else:
        print(f"{row.get('id')}: {row.get('state')}")
    return 0


def _cmd_withdraw(args) -> int:
    session = _owner(args)
    if session is None:
        return 1
    sid = urllib.parse.quote(args.suggestion, safe="")
    data, code = _call("POST", f"{_base(session)}/suggestions/{sid}/withdraw",
                       {"session": session})
    if code:
        return code
    row = data.get("suggestion") or {}
    if args.json:
        _print_json(row)
    else:
        print(f"{row.get('id')}: {row.get('state')}")
    return 0


def _session_option(parser, default=None) -> None:
    """``--session`` in both positions: before the verb, or after it.

    A subparser that does not receive it must not overwrite what the parent
    parsed, so its default is ``SUPPRESS``.
    """
    parser.add_argument(
        "--session", metavar="NAME", default=default,
        help="the session whose shell to address (default: $CLAUNCH_SESSION)",
    )


def register(sub) -> None:
    p = sub.add_parser(
        "shell",
        help="a session's shell (its CLI pane): read it, propose input to the user",
    )
    _session_option(p)
    ssub = p.add_subparsers(dest="shell_cmd", required=True)

    st = ssub.add_parser("status", help="whether the shell runs, its cursor and pending suggestions")
    _session_option(st, argparse.SUPPRESS)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=_cmd_status)

    rd = ssub.add_parser("read", help="print the shell's output (tail by default)")
    _session_option(rd, argparse.SUPPRESS)
    rd.add_argument("--offset", type=int, help="byte offset a previous read returned")
    rd.add_argument("--limit", type=int, help="maximum bytes (default 65536)")
    rd.add_argument("--json", action="store_true")
    rd.set_defaults(func=_cmd_read)

    sg = ssub.add_parser("suggest", help="propose input the user may accept and run")
    _session_option(sg, argparse.SUPPRESS)
    sg.add_argument("text", help="the exact input to run")
    sg.add_argument("--why", help="why you need it (shown to the user)")
    sg.add_argument("--json", action="store_true")
    sg.set_defaults(func=_cmd_suggest)

    sl = ssub.add_parser("suggestions", help="the proposal queue")
    _session_option(sl, argparse.SUPPRESS)
    sl.add_argument("--state", help="pending, accepted, rejected or withdrawn")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=_cmd_suggestions)

    ac = ssub.add_parser("accept", help="the user's control: type the suggestion and run it")
    _session_option(ac, argparse.SUPPRESS)
    ac.add_argument("suggestion", help="suggestion id (sh-xxxxxx)")
    ac.add_argument("--json", action="store_true")
    ac.set_defaults(func=lambda args: _decide(args, "accept"))

    rj = ssub.add_parser("reject", help="the user's control: decline the suggestion")
    _session_option(rj, argparse.SUPPRESS)
    rj.add_argument("suggestion", help="suggestion id (sh-xxxxxx)")
    rj.add_argument("--json", action="store_true")
    rj.set_defaults(func=lambda args: _decide(args, "reject"))

    wd = ssub.add_parser("withdraw", help="take back a proposal this session made")
    _session_option(wd, argparse.SUPPRESS)
    wd.add_argument("suggestion", help="suggestion id (sh-xxxxxx)")
    wd.add_argument("--json", action="store_true")
    wd.set_defaults(func=_cmd_withdraw)
