"""``claunch report ...`` — the round report a session leaves behind.

A session ending a round writes one HTML page and the daemon indexes it under
the session's name. This command is the whole interface to that: ``path`` says
where to write, ``save`` takes a page written elsewhere, ``ls`` shows what a
session has left, and ``check`` is the exit code a workflow's ``verify`` can
gate on. See :mod:`claude_launcher.reports` for the naming and location rules
and why they are what they are.

Every subcommand defaults ``--session`` to ``$CLAUNCH_SESSION``, so inside a
claunch session none of them needs an argument.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import reports


def _run_scope() -> str:
    """The session name a cflow run here is keyed to, if exactly one is.

    The last resort of :func:`_session`, and it exists for one concrete case:
    ``claunch report check`` is written to be a workflow's ``verify``. The
    engine runs a verify with the MCP server's environment, where
    ``CLAUNCH_SESSION`` is set — but a *human* advancing the same run from
    their own shell (``claunch cflow next``) runs that same command without
    it, and a verify that failed only because a person pressed the button
    would be a gate on the wrong thing. Resolution order matches
    ``claunch cflow``'s own (``cli_cflow._resolve_run``): flag, env, then the
    directory's single run. More than one run here is ambiguity, so it is
    declined rather than guessed.
    """
    try:
        from .cflow import state as cflow_state
    except Exception:
        return ""
    here = Path(cflow_state.resolve_cwd())
    for cwd in (here, *here.parents):
        scopes = cflow_state.scopes_in(str(cwd))
        if len(scopes) == 1:
            return scopes[0]
        if scopes:
            return ""
    return ""


def _session(args: argparse.Namespace) -> str:
    name = (
        getattr(args, "session", None)
        or os.environ.get("CLAUNCH_SESSION")
        or _run_scope()
        or ""
    ).strip()
    if not name:
        raise reports.ReportError(
            "no session: run this inside a claunch session (CLAUNCH_SESSION is "
            "set there) or name one with --session <name>"
        )
    return reports.check_session(name)


def _cmd_path(args: argparse.Namespace) -> int:
    path = reports.target(_session(args), args.issue, new=args.new)
    print(path)
    return 0


def _cmd_save(args: argparse.Namespace) -> int:
    dest = reports.save(_session(args), Path(args.file), args.issue, new=args.new)
    print(dest)
    return 0


def _cmd_ls(args: argparse.Namespace) -> int:
    session = _session(args)
    rows = reports.listing(session)
    if args.json:
        print(json.dumps({"session": session, "reports": rows}, indent=2))
        return 0
    if not rows:
        print(f"{session}: no report yet ({reports.dir_for(session)})")
        return 0
    for row in rows:
        issue = row["issue"] or "-"
        print(f"{row['at']}  {issue:<16}  {row['size']:>7}B  {row['path']}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Exit 0 only if this session has left a readable report.

    Written to be a ``verify``: the failure is printed with the command that
    fixes it, because the agent that trips this gate reads the output and
    nothing else. Note what it does *not* claim — the check is per session,
    not per run. That is exact for improv-worker (one session, one round) and
    would need a run id in the filename to be exact for a workflow that loops.
    """
    session = _session(args)
    rows = reports.listing(session)
    if args.issue:
        want = reports.issue_slug(args.issue)
        rows = [r for r in rows if reports.issue_slug(r["issue"]) == want]
    if rows:
        row = rows[0]
        print(f"report ok: {row['path']} ({row['size']}B, issue {row['issue'] or '-'})")
        return 0
    where = reports.dir_for(session)
    for_issue = f" for issue {args.issue}" if args.issue else ""
    print(
        f"no round report{for_issue} for {session} in {where}\n"
        f"  write one: claunch report path"
        + (f" --issue {args.issue}" if args.issue else "")
        + " prints the file to create,\n"
        f"  or hand over a page written elsewhere: claunch report save <file.html>\n"
        f"  it must be real HTML and at least {reports.MIN_BYTES} bytes — an empty "
        "file at the right path is not a report",
        file=sys.stderr,
    )
    return 1


def check_main(argv: Optional[List[str]] = None) -> int:
    """``report check`` without building the whole CLI — the gate's entry point.

    ``tools/report_check.py`` calls this rather than :func:`cli.main`, and the
    difference is not style. A gate runs under ``uv run --no-sync``, which is a
    promise never to populate the worktree's ``.venv``; in a worktree nobody
    synced by hand, the only packages that exist are the standard library and
    this checkout's own ``src``. :func:`cli.main` builds every subparser, and
    one of them imports :mod:`claude_launcher.harnesses`, which imports
    ``yaml`` — so routing the gate through it fails with
    ``ModuleNotFoundError: No module named 'yaml'`` (measured) before it ever
    reaches the check. Everything reached from here is standard library plus
    :mod:`claude_launcher.reports`, so the gate holds on a bare interpreter.

    Same flags and same exit codes as ``claunch report check``: 0 when a
    readable report exists, 1 when it does not, 2 when the session cannot be
    named.
    """
    parser = argparse.ArgumentParser(
        prog="report check",
        description="exit 0 only if this session has left a readable round report",
    )
    parser.add_argument(
        "--session", help="session name (default: $CLAUNCH_SESSION)"
    )
    parser.add_argument("--issue", help="require the report to be for this issue")
    args = parser.parse_args(argv)
    args.report_func = _cmd_check
    return _cmd(args)


def _cmd(args: argparse.Namespace) -> int:
    try:
        return args.report_func(args)
    except reports.ReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def register(sub) -> None:
    p = sub.add_parser(
        "report",
        help="a session's round report (HTML), where the daemon indexes it",
        description=(
            "One HTML page per round, written to "
            "<daemon dir>/reports/<session>/<UTC stamp>-<issue>.html and served "
            "by the daemon at /api/sessions/<session>/reports/<file>. Outside "
            "every repository, so writing one never dirties a working tree."
        ),
    )
    rsub = p.add_subparsers(dest="report_command", required=True)

    p_path = rsub.add_parser(
        "path",
        help="print the file this session's report should be written to",
        description=(
            "Idempotent: asking twice for the same session and issue returns "
            "the same file, so revising a report overwrites it instead of "
            "leaving two half-reports behind. --new forces a fresh one."
        ),
    )
    p_path.add_argument("--issue", help="board issue id this round is for")
    p_path.add_argument(
        "--new", action="store_true",
        help="mint a new file even if one already exists for this issue",
    )
    p_path.set_defaults(report_func=_cmd_path)

    p_save = rsub.add_parser(
        "save", help="copy an HTML file into the indexed location"
    )
    p_save.add_argument("file", help="the .html page to file as this round's report")
    p_save.add_argument("--issue", help="board issue id this round is for")
    p_save.add_argument("--new", action="store_true", help="do not reuse an existing file")
    p_save.set_defaults(report_func=_cmd_save)

    p_ls = rsub.add_parser("ls", aliases=["list"], help="list a session's reports")
    p_ls.add_argument("--json", action="store_true", help="machine-readable output")
    p_ls.set_defaults(report_func=_cmd_ls)

    p_check = rsub.add_parser(
        "check",
        help="exit 0 only if this session has left a readable report (for verify)",
    )
    p_check.add_argument("--issue", help="require the report to be for this issue")
    p_check.set_defaults(report_func=_cmd_check)

    for parser in (p_path, p_save, p_ls, p_check):
        parser.add_argument(
            "--session", help="session name (default: $CLAUNCH_SESSION)"
        )
        parser.set_defaults(func=_cmd)
