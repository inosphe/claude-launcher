"""``claunch cflow ...`` subcommands.

The human/orchestrator side of cflow: list and inspect workflows, watch a
run, and operate the controls the agent deliberately does not have —
``approve`` (gates) and ``select`` (confirming user-chooser branches). Also
hosts ``cflow mcp``, the stdio server Claude Code spawns, and ``install``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import daemon_client
from .cflow import engine, install, model, state as state_mod


def _resolve_scope(args: argparse.Namespace):
    """Which run to operate on: -t/--session > $CLAUNCH_SESSION > the only
    run in this directory. Ambiguity is an error, not a guess."""
    explicit = getattr(args, "session", None)
    if explicit:
        return str(explicit)
    if os.environ.get(state_mod.SESSION_ENV):
        return None  # the ambient session scope applies
    scopes = state_mod.scopes_in()
    if len(scopes) > 1:
        raise engine.CflowError(
            f"multiple cflow runs in this directory ({', '.join(scopes)}); "
            "pick one with -t/--session"
        )
    return scopes[0] if scopes else None


def _cmd_ls(_args: argparse.Namespace) -> int:
    flows = state_mod.list_workflows()
    if not flows:
        dirs = ", ".join(str(d) for d in state_mod.search_dirs())
        print(f"no workflows found (searched: {dirs})")
        print("write one, or scaffold an example: claunch cflow example")
        return 0
    for name, path in flows:
        try:
            wf = model.load(path)
            desc = wf.description or ""
            count = wf.step_count()
            print(f"{name:<24} {count:>3} steps  {desc}  [{path}]")
        except model.WorkflowError as exc:
            print(f"{name:<24} (invalid: {exc})  [{path}]")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    path = state_mod.find_workflow(args.workflow)
    wf = model.load(path)
    print(f"{wf.name} — {wf.description}  [{path}]")
    print(f"start: {wf.start}    max_visits: {wf.max_visits}")
    for s in wf.steps.values():
        flags = []
        if s.gate:
            flags.append("gate")
        if s.ask:
            who = " > ".join(c.describe() for c in s.ask.delegate.candidates)
            flags.append(f"ask: {who}")
            if s.ask.on_decline:
                flags.append(f"decline -> {s.ask.on_decline}")
        if s.verify:
            flags.append(f"verify: {s.verify.command}")
        suffix = f"  ({'; '.join(flags)})" if flags else ""
        if s.select:
            chooser = s.select.chooser
            if s.select.delegate:
                chooser = " > ".join(
                    c.describe() for c in s.select.delegate.candidates
                )
            print(f"- {s.id} [select, chooser={chooser}]{suffix}")
            for name, opt in s.select.options.items():
                print(f"    {name}: {opt.description}  -> {opt.next or 'end'}")
        else:
            title = f": {s.title}" if s.title else ""
            print(f"- {s.id}{title}{suffix}  -> {s.next or 'end'}")
    for warning in wf.warnings:
        print(f"warning: {warning}")
    # Advice to whoever is WRITING this file, which is why it lives here and
    # not in front of every run (see `Workflow.deprecations`).
    for note in wf.deprecations:
        print(f"deprecated: {note}")
    return 0


def _print_payload(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_status(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args)
    payload = engine.status(scope=scope)
    if args.json:
        _print_payload(payload)
        return 0
    status = payload.get("status")
    pending = payload.get("pending_start")
    if status == "idle":
        print("no active cflow run in this directory")
        if pending:
            _print_pending(pending)
        # ...but not the one just reported on: a slot holding only a start
        # request counts as a scope now, and "runs exist for: default" while
        # standing in default is noise.
        here = scope or state_mod.current_scope()
        others = [s for s in state_mod.scopes_in() if s != here]
        if others:
            print(f"runs exist for: {', '.join(others)} (use -t <session>)")
        return 0
    shown = scope or state_mod.current_scope()
    print(f"workflow: {payload.get('workflow')}  run: {payload.get('run')}  session: {shown}")
    print(f"status:   {status}")
    if payload.get("step_id"):
        visit = payload.get("visit")
        note = f"  (visit {visit})" if visit and visit > 1 else ""
        print(f"step:     {payload['step_id']}{note}")
    report = payload.get("report")
    if report:
        print(f"report:   {report.get('summary')}")
    print(f"steps completed: {payload.get('steps_completed')}")
    revisited = {
        s: n for s, n in (payload.get("visits") or {}).items() if n > 1
    }
    if revisited:
        pairs = ", ".join(f"{s}x{n}" for s, n in sorted(revisited.items()))
        print(f"loops:    {pairs}")
    if status == "waiting_approval":
        print(f"gate:     {payload.get('gate')}")
        print("unblock:  claunch cflow approve")
    if status == "waiting_selection":
        proposal = payload.get("proposal") or {}
        print(
            f"agent proposes: {proposal.get('option')!r} — {proposal.get('reason')}"
        )
        options = ", ".join(o["name"] for o in payload.get("options", []))
        print(f"confirm:  claunch cflow select <{options}>")
    if status == "select":
        options = ", ".join(o["name"] for o in payload.get("options", []))
        print(f"decision pending ({payload.get('chooser')}): {options}")
    if pending:
        _print_pending(pending)
    return 0


def _print_pending(pending: dict) -> None:
    ctx = pending.get("context")
    print(
        f"start requested: {pending.get('workflow')!r} "
        f"(by {pending.get('by')}, {pending.get('at')})"
    )
    if ctx:
        print(f"  context: {ctx}")
    print(
        "  the session's agent starts it itself; "
        "withdraw: claunch cflow request --cancel"
    )


def _cmd_request(args: argparse.Namespace) -> int:
    """Ask the scope's agent to start a workflow (it performs the start)."""
    scope = _resolve_scope(args)
    if args.cancel:
        payload = engine.cancel_request(by="user", scope=scope)
        print(f"withdrew the pending start of {payload['request'].get('workflow')!r}")
        return 0
    if not args.workflow:
        raise engine.CflowError("a workflow name is required (or pass --cancel)")
    payload = engine.request_start(
        args.workflow, args.context, by="user", scope=scope
    )
    request = payload["request"]
    _report_unblock(
        f"requested a start of {request['name']!r}",
        engine.nudge_for_request(request["workflow"]),
        scope,
    )
    return 0


def _nudge_via_daemon(message: str, scope) -> list:
    """Best-effort: type a resume nudge into the run's own session (scope ==
    session name, 1:1). Needs the daemon; silently a no-op without it, and
    default-scope runs belong to no session.

    Goes through the daemon's ``/deliver`` — the same door in-process senders
    use — rather than typing keys, so how a message gets submitted stays a
    single decision made in one place."""
    target = scope or state_mod.current_scope()
    if target == state_mod.DEFAULT_SCOPE:
        return []
    try:
        client = daemon_client.connect()
    except Exception:
        return []
    if client is None:
        return []
    here = Path.cwd().resolve()
    try:
        sessions = (client.get("/api/sessions") or {}).get("sessions") or []
    except daemon_client.DaemonClientError:
        return []
    for s in sessions:
        try:
            same = Path(str(s.get("cwd") or "")).resolve() == here
        except OSError:
            same = False
        if s.get("name") != target or not same or s.get("status") == "exited":
            continue
        try:
            client.post(f"/api/sessions/{s['name']}/deliver", {"text": message})
        except daemon_client.DaemonClientError:
            return []
        return [target]
    return []


def _report_unblock(action: str, message: str, scope) -> None:
    nudged = _nudge_via_daemon(message, scope)
    if nudged:
        print(f"{action}; nudged session(s): {', '.join(nudged)}")
    else:
        print(f"{action}; nudge the agent to continue")


def _cmd_approve(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args)
    payload = engine.approve(by="user", scope=scope)
    _report_unblock(
        f"approved gate at step {payload.get('step_id')!r}",
        engine.NUDGE_APPROVED,
        scope,
    )
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args)
    payload = engine.select(args.option, args.reason, by="user", scope=scope)
    if payload.get("status") in ("done", "aborted"):
        print(f"selected {args.option!r}; workflow is {payload['status']}")
    else:
        _report_unblock(f"selected {args.option!r}", engine.NUDGE_SELECTED, scope)
    return 0


def _cmd_goto(args: argparse.Namespace) -> int:
    scope = _resolve_scope(args)
    payload = engine.goto(args.step, by="user", reason=args.reason, scope=scope)
    if payload.get("status") in ("done", "aborted"):
        print(f"workflow forced to {payload['status']}")
        return 0
    _report_unblock(
        f"current step forced to {args.step!r} (visit {payload.get('visit')})",
        engine.nudge_for_state(args.step),
        scope,
    )
    return 0


def _cmd_abort(args: argparse.Namespace) -> int:
    payload = engine.abort(by="user", scope=_resolve_scope(args))
    print(f"aborted run {payload.get('run')}")
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    payload = engine.archive(by="user", scope=_resolve_scope(args))
    print(f"archived run {payload.get('run')} -> {payload.get('archived_to')}")
    if payload.get("was") not in ("done", "aborted"):
        print("note: the run was still active; it was aborted before archiving")
    print("the slot is free — a new run can be started here")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    engine.reset(scope=_resolve_scope(args))
    print("cleared cflow run state (journal kept)")
    return 0


def _cmd_journal(args: argparse.Namespace) -> int:
    entries = state_mod.read_journal(scope=_resolve_scope(args))
    for entry in entries[-args.tail :] if args.tail else entries:
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    from .cli import run_install

    print("note: 'cflow install' is now 'claunch install'; installing both "
          "skills and the merged MCP server")
    return run_install(args.profile, args.project)


def _cmd_example(args: argparse.Namespace) -> int:
    target = Path(".") / state_mod.PROJECT_WORKFLOWS / f"{args.name}.yaml"
    if target.exists():
        print(f"error: {target} already exists", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(install.EXAMPLE_WORKFLOW, encoding="utf-8")
    print(f"wrote example workflow: {target}")
    print(f"run it with: /cflow {args.name} <task description>")
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from .cflow import mcp

    return mcp.serve()


def register(sub) -> None:
    p = sub.add_parser(
        "cflow",
        help="declarative agent workflows: list/inspect, approve gates, "
        "confirm selections, run the MCP server",
    )
    csub = p.add_subparsers(dest="cflow_command", required=True)

    q = csub.add_parser("ls", help="list available workflows (project + global)")
    q.set_defaults(func=_cmd_ls)

    q = csub.add_parser("show", help="print a workflow's step tree")
    q.add_argument("workflow")
    q.set_defaults(func=_cmd_show)

    def _scoped(parser):
        parser.add_argument(
            "-t",
            "--session",
            help="target this session's run (default: $CLAUNCH_SESSION, or "
            "the directory's only run)",
        )
        return parser

    q = _scoped(csub.add_parser("status", help="show the active run in this directory"))
    q.add_argument("--json", action="store_true", help="raw JSON payload")
    q.set_defaults(func=_cmd_status)

    q = _scoped(csub.add_parser(
        "request",
        help="ask this session's agent to start a workflow (the agent runs "
        "the start itself)",
    ))
    q.add_argument("workflow", nargs="?", help="workflow name or .yaml path")
    q.add_argument("-c", "--context", help="task context carried into the run")
    q.add_argument(
        "--cancel", action="store_true", help="withdraw the pending request"
    )
    q.set_defaults(func=_cmd_request)

    q = _scoped(csub.add_parser(
        "approve", help="approve the current human gate (the agent cannot)"
    ))
    q.set_defaults(func=_cmd_approve)

    q = _scoped(csub.add_parser(
        "select", help="confirm a branch choice at a user-chooser decision point"
    ))
    q.add_argument("option")
    q.add_argument("--reason", help="recorded in the journal")
    q.set_defaults(func=_cmd_select)

    q = _scoped(csub.add_parser(
        "goto",
        help="force the run's current step (human override; 'end' finishes); "
        "auto-nudges the run's session",
    ))
    q.add_argument("step")
    q.add_argument("--reason", help="recorded in the journal")
    q.set_defaults(func=_cmd_goto)

    q = _scoped(csub.add_parser("abort", help="abort the active run"))
    q.set_defaults(func=_cmd_abort)

    q = _scoped(csub.add_parser(
        "archive",
        help="retire the run (finished or not) into .cflow/.../archive/, "
        "freeing the slot for a new start",
    ))
    q.set_defaults(func=_cmd_archive)

    q = _scoped(csub.add_parser("reset", help="clear run state (keeps the journal)"))
    q.set_defaults(func=_cmd_reset)

    q = _scoped(csub.add_parser("journal", help="print the run journal (JSONL)"))
    q.add_argument("-n", "--tail", type=int, default=0, help="only the last N entries")
    q.set_defaults(func=_cmd_journal)

    q = csub.add_parser(
        "install",
        help="alias for 'claunch install' (one MCP server + both skills)",
    )
    q.add_argument("--profile", help="install into this claunch profile")
    q.add_argument(
        "--project",
        nargs="?",
        const=".",
        help="install into a project directory (default: current)",
    )
    q.set_defaults(func=_cmd_install)

    q = csub.add_parser("example", help="scaffold an example workflow in this project")
    q.add_argument("name", nargs="?", default="feature-dev")
    q.set_defaults(func=_cmd_example)

    q = csub.add_parser("mcp", help="run the stdio MCP server (spawned by claude)")
    q.set_defaults(func=_cmd_mcp)
