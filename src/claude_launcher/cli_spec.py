"""``claunch spec ...`` — check, plan and import a work spec.

``check``, ``preview`` and ``plan`` have no side effects: they print the
validator's errors (with the YAML line each is about), the sessions and work
the spec describes, or the ordered actions an import would take. ``import`` runs them
(:mod:`claude_launcher.workspec_import`). The format is ``docs/work-spec.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import workspec, workspec_import


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise workspec.SpecError(f"cannot read {path}: {exc}") from None


def _load(path: str) -> dict:
    return workspec.load(_read(path))


def _cmd_check(args: argparse.Namespace) -> int:
    """Each error as ``<file>:<line>: <error>`` (the line the error is
    about, :func:`workspec.locate`), so an editor or an agent jumps to it."""
    text = ""
    try:
        text = _read(args.file)
        errs = workspec.validate(workspec.load(text))
    except workspec.SpecError as exc:
        errs = [str(exc)]
    for e, line in zip(errs, workspec.locate(text, errs) if text else
                       [None] * len(errs)):
        print(f"{args.file}:{line}: {e}" if line else e)
    if not errs:
        print("ok")
    return 1 if errs else 0


def _cmd_preview(args: argparse.Namespace) -> int:
    """The sessions and the work a valid spec describes (no writes, no
    leader choice): :func:`workspec.preview`."""
    try:
        doc = _load(args.file)
    except workspec.SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    errs = workspec.validate(doc)
    if errs:
        print(f"error: {len(errs)} problem(s); run claunch spec check "
              f"{args.file}", file=sys.stderr)
        return 2
    view = workspec.preview(doc)
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    print("sessions:")
    for s in view["sessions"]:
        role = " + ".join([s["role"], *s["subroles"]])
        extra = (f" queue={','.join(s['queue'])}" if s["queue"] else "") + \
                (f" reviews={','.join(s['reviews'])}" if s["reviews"] else "")
        name = s["id"] + (f" ({s['mode']})" if s["kind"] == "leader" else "")
        print(f"  {'  ' * s['depth']}{name:24} {s['kind']:9} {role:16} "
              f"{s['workflow'] or '-'}{extra}")
    print("tasks (wave = start order by blocked_by):")
    for t in sorted(view["tasks"], key=lambda t: (t["wave"], t["priority"])):
        extra = (f" after={','.join(t['blocked_by'])}" if t["blocked_by"]
                 else "") + \
                (f" waits={','.join(t['gated_by'])}" if t["gated_by"] else "")
        print(f"  {t['wave']}  {t['key']:16} {t['type']:7} P{t['priority']} "
              f"{t['assignee'] or '-':12} {t['title']}{extra}")
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        actions = workspec.plan(_load(args.file), args.leader or "")
    except workspec.SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(actions, ensure_ascii=False, indent=2))
    else:
        for act in actions:
            print(workspec_import.describe(act))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from .daemon_client import DaemonClientError, ensure_running
    try:
        doc = _load(args.file)
        workspec.plan(doc, args.leader or "")  # refuse before connecting
        base = workspec_import.board_dir(doc)
    except workspec.SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    backend = workspec_import.DaemonBackend(ensure_running(), base)
    code = 0
    try:
        log = workspec_import.execute(
            doc, backend, leader_choice=args.leader or "",
            profile=args.profile or "", base_dir=base)
    except (workspec_import.SpecImportError, DaemonClientError) as exc:
        log = getattr(exc, "log", [])
        print(f"error: {exc}", file=sys.stderr)
        print("what ran before it is kept; rerun the same command to "
              "continue", file=sys.stderr)
        code = 1
    if args.json:
        print(json.dumps(log, ensure_ascii=False, indent=2))
    else:
        for row in log:
            print(f"{row['do']:12} {row['what']:20} {row['result']:8} "
                  f"{row['detail']}".rstrip())
    return code


def register(sub) -> None:
    p = sub.add_parser(
        "spec",
        help="check, plan or import a work spec (docs/work-spec.md)",
    )
    ssub = p.add_subparsers(dest="spec_cmd", required=True)

    c = ssub.add_parser("check", help="validate a work spec; exit 1 on errors")
    c.add_argument("file")
    c.set_defaults(func=_cmd_check)

    v = ssub.add_parser(
        "preview", help="show the sessions and work a valid spec describes")
    v.add_argument("file")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=_cmd_preview)

    for name, func, helptext in (
        ("plan", _cmd_plan, "print the actions an import would take (no writes)"),
        ("import", _cmd_import, "create the issues, mesh and sessions a spec "
                                "describes; rerunning continues it"),
    ):
        s = ssub.add_parser(name, help=helptext)
        s.add_argument("file")
        s.add_argument(
            "--leader", default="",
            help="own | shared:<session> — overrides the spec's leader.mode "
                 "(required when it is 'ask')")
        s.add_argument("--json", action="store_true")
        if name == "import":
            s.add_argument(
                "--profile", default="",
                help="profile for an own leader (a new top-level session)")
        s.set_defaults(func=func)
