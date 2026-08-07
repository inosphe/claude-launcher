"""``claunch cflow ...`` subcommands.

The human/orchestrator side of cflow: list and inspect workflows, watch a
run, and operate the controls the agent deliberately does not have —
``approve`` (gates) and ``select`` (confirming user-chooser branches). Also
hosts ``cflow mcp``, the stdio server Claude Code spawns, and ``install``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import daemon_client, profile as profile_mod
from .cflow import engine, install, model, state as state_mod


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
        if s.verify:
            flags.append(f"verify: {s.verify.command}")
        suffix = f"  ({'; '.join(flags)})" if flags else ""
        if s.select:
            print(f"- {s.id} [select, chooser={s.select.chooser}]{suffix}")
            for name, opt in s.select.options.items():
                print(f"    {name}: {opt.description}  -> {opt.next or 'end'}")
        else:
            title = f": {s.title}" if s.title else ""
            print(f"- {s.id}{title}{suffix}  -> {s.next or 'end'}")
    for warning in wf.warnings:
        print(f"warning: {warning}")
    return 0


def _print_payload(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_status(args: argparse.Namespace) -> int:
    payload = engine.status()
    if args.json:
        _print_payload(payload)
        return 0
    status = payload.get("status")
    if status == "idle":
        print("no active cflow run in this directory")
        return 0
    print(f"workflow: {payload.get('workflow')}  run: {payload.get('run')}")
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
    return 0


def _nudge_via_daemon(message: str) -> list:
    """Best-effort: type a resume nudge into this directory's managed
    sessions (needs the daemon; silently a no-op when it isn't running)."""
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
    nudged = []
    for s in sessions:
        try:
            same = Path(str(s.get("cwd") or "")).resolve() == here
        except OSError:
            same = False
        if not same or s.get("status") == "exited":
            continue
        try:
            client.post(f"/api/sessions/{s['name']}/keys", {"keys": [message, "Enter"]})
        except daemon_client.DaemonClientError:
            continue
        nudged.append(str(s["name"]))
    return nudged


def _report_unblock(action: str, message: str) -> None:
    nudged = _nudge_via_daemon(message)
    if nudged:
        print(f"{action}; nudged session(s): {', '.join(nudged)}")
    else:
        print(f"{action}; nudge the agent to continue")


def _cmd_approve(_args: argparse.Namespace) -> int:
    payload = engine.approve(by="user")
    _report_unblock(
        f"approved gate at step {payload.get('step_id')!r}", engine.NUDGE_APPROVED
    )
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    payload = engine.select(args.option, args.reason, by="user")
    if payload.get("status") in ("done", "aborted"):
        print(f"selected {args.option!r}; workflow is {payload['status']}")
    else:
        _report_unblock(f"selected {args.option!r}", engine.NUDGE_SELECTED)
    return 0


def _cmd_abort(_args: argparse.Namespace) -> int:
    payload = engine.abort(by="user")
    print(f"aborted run {payload.get('run')}")
    return 0


def _cmd_reset(_args: argparse.Namespace) -> int:
    engine.reset()
    print("cleared cflow run state (journal kept)")
    return 0


def _cmd_journal(args: argparse.Namespace) -> int:
    entries = state_mod.read_journal()
    for entry in entries[-args.tail :] if args.tail else entries:
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    if args.profile and args.project is not None:
        print("error: choose --profile or --project, not both", file=sys.stderr)
        return 1
    if args.profile:
        p = profile_mod.require(args.profile)
        done = install.install_into_profile(p)
    else:
        target = Path(args.project or ".").resolve()
        done = install.install_into_project(target)
    for line in done:
        print(f"installed: {line}")
    print("note: restart claude for the MCP server to be picked up")
    return 0


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

    q = csub.add_parser("status", help="show the active run in this directory")
    q.add_argument("--json", action="store_true", help="raw JSON payload")
    q.set_defaults(func=_cmd_status)

    q = csub.add_parser(
        "approve", help="approve the current human gate (the agent cannot)"
    )
    q.set_defaults(func=_cmd_approve)

    q = csub.add_parser(
        "select", help="confirm a branch choice at a user-chooser decision point"
    )
    q.add_argument("option")
    q.add_argument("--reason", help="recorded in the journal")
    q.set_defaults(func=_cmd_select)

    q = csub.add_parser("abort", help="abort the active run")
    q.set_defaults(func=_cmd_abort)

    q = csub.add_parser("reset", help="clear run state (keeps the journal)")
    q.set_defaults(func=_cmd_reset)

    q = csub.add_parser("journal", help="print the run journal (JSONL)")
    q.add_argument("-n", "--tail", type=int, default=0, help="only the last N entries")
    q.set_defaults(func=_cmd_journal)

    q = csub.add_parser(
        "install",
        help="register the cflow MCP server + /cflow skill "
        "(--profile NAME, or --project [DIR])",
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
