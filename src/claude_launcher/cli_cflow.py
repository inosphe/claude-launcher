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
import textwrap
from pathlib import Path
from typing import Optional

from . import daemon_client
from .cflow import checkout, engine, install, model, responders, state as state_mod


def _note_redirect(cwd, scope: str) -> None:
    print(f"note: using the cflow run at {cwd} (session {scope})")


def _no_run_message(want) -> str:
    here = state_mod.resolve_cwd()
    what = (
        f"no cflow run for session {want!r}" if want else "no active cflow run"
    )
    runs = state_mod.known_runs()
    if runs:
        listing = ", ".join(f"{c} [{s}]" for c, s in runs)
        return (
            f"{what} in {here} or any parent directory; known runs: {listing} "
            f"(pick one with -t <session>, or run this from its directory)"
        )
    return (
        f"{what} in {here} or any parent directory (start one with the "
        f"cflow 'start' tool or see 'claunch cflow ls')"
    )


def _resolve_run(args: argparse.Namespace, *, required: bool = True):
    """Which run to operate on, as a ``(scope, cwd)`` pair for the engine.

    The scope: -t/--session > $CLAUNCH_SESSION > the directory's only run.
    The directory: this one — unless it holds no matching run. The shell a
    human types these commands into often stands somewhere other than the
    run's directory (the classic case: a chat session's ``!`` shell pinned
    inside a git worktree under the project root, while the run is keyed to
    the root), so before giving up the run is looked for where it could
    actually live: the nearest ancestor directory holding one, then — when a
    session was named (flag or env), which identifies the run machine-wide —
    the run registry. A redirect is printed, never silent; ambiguity is an
    error, not a guess.

    ``required=False`` (status, request): a miss falls back to (scope, here)
    so "idle here" stays an answer, and a request may create a fresh slot.
    """
    explicit = getattr(args, "session", None)
    want = str(explicit) if explicit else os.environ.get(state_mod.SESSION_ENV)
    here = Path(state_mod.resolve_cwd())

    if want:
        for cwd in (here, *here.parents):
            if want in state_mod.scopes_in(str(cwd)):
                if cwd == here:
                    return want, None
                _note_redirect(cwd, want)
                return want, str(cwd)
        hits = sorted({c for c, s in state_mod.known_runs() if s == want})
        if len(hits) == 1:
            _note_redirect(hits[0], want)
            return want, hits[0]
        if len(hits) > 1:
            raise engine.CflowError(
                f"session {want!r} has cflow runs in several directories "
                f"({', '.join(hits)}); run this command from the right one"
            )
        if required:
            raise engine.CflowError(_no_run_message(want))
        return want, None

    for cwd in (here, *here.parents):
        scopes = state_mod.scopes_in(str(cwd))
        if not scopes:
            continue
        if len(scopes) > 1:
            raise engine.CflowError(
                f"multiple cflow runs in {cwd} ({', '.join(scopes)}); "
                "pick one with -t/--session"
            )
        if cwd == here:
            return scopes[0], None
        _note_redirect(cwd, scopes[0])
        return scopes[0], str(cwd)
    if required:
        raise engine.CflowError(_no_run_message(None))
    return None, None


def _cmd_ls(_args: argparse.Namespace) -> int:
    flows = state_mod.resolved_workflows()
    if not flows:
        dirs = ", ".join(str(d) for d in state_mod.search_dirs())
        print(f"no workflows found (searched: {dirs})")
        print("write one, or scaffold an example: claunch cflow example")
        return 0
    for wf_ref in flows:
        try:
            composed = state_mod.compose_located(wf_ref)
            wf = composed.workflow
            desc = wf.description or ""
            count = wf.step_count()
            print(f"{wf_ref.name:<24} {count:>3} steps  {desc}  [{wf_ref.path}]")
            # A layer over something is not the whole workflow it looks like:
            # the step count and the description are mostly the base's, and
            # the reader should know which file to open to change them.
            for base in composed.bases:
                print(f"{'':<24} extends [{base}]")
        except model.WorkflowError as exc:
            print(f"{wf_ref.name:<24} (invalid: {exc})  [{wf_ref.path}]")
        # Which layer answered, and — the part worth the extra line — what it
        # kept from answering. Two copies of one workflow drift; a listing
        # that shows only the winner is how nobody notices.
        for shadowed in wf_ref.shadows:
            print(f"{'':<24} {wf_ref.origin} copy overrides [{shadowed}]")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    composed = state_mod.load_workflow(args.workflow)
    path = composed.path
    wf = composed.workflow
    print(f"{wf.name} — {wf.description}  [{path}]")
    for base in composed.bases:
        print(f"extends: {base}")
    recur = "    recur: yes (each finished round requests the next)" if wf.recur else ""
    print(f"start: {wf.start}    max_visits: {wf.max_visits}{recur}")
    if wf.filter_roles:
        print(
            f"filter_roles: {wf.filter_roles.describe()} — which mesh roles "
            f"may drive a run"
        )
    if wf.default_role:
        prio = f"    priority: {wf.priority}" if wf.priority else ""
        print(
            f"default_role: {wf.default_role} — pickers select this workflow "
            f"when that role is chosen{prio}"
        )
    elif wf.priority:
        print(f"priority: {wf.priority} — rank among a picker's candidates")
    if wf.default_child_cflow:
        print(
            f"default_child_cflow: {wf.default_child_cflow} — a child spawned "
            f"by a session driving this workflow starts on that one"
        )
    for s in wf.steps.values():
        flags = []
        if s.gate:
            flags.append("gate")
        if s.ask:
            flags.append(f"ask: {s.ask.delegate.describe()}")
            if s.ask.on_decline:
                flags.append(f"decline -> {s.ask.on_decline}")
        if s.verify:
            flags.append(f"verify: {s.verify.command}")
        if s.done_when:
            # Multi-line prose collapsed to one line: show is a graph review,
            # and `status` serves the full text to the run that needs it.
            done_when = " ".join(s.done_when.split())
            if len(done_when) > 72:
                done_when = done_when[:72] + "…"
            flags.append(f"done_when: {done_when}")
        if s.awaits:
            # What the step WAITS for, and how often the daemon re-measures
            # it. Shown next to verify because the two are read together: an
            # `awaits: verify` is only legible beside the command it names.
            flags.append(
                f"awaits: {s.awaits.command(s)} (every {s.awaits.poll:g}s)"
            )
        suffix = f"  ({'; '.join(flags)})" if flags else ""
        if s.select:
            chooser = s.select.chooser
            if s.select.delegate:
                chooser = s.select.delegate.describe()
            print(f"- {s.id} [select, chooser={chooser}]{suffix}")
            for name, opt in s.select.options.items():
                # A paced option: the agent's take of it is held until this
                # long has passed since the last one (see model "Cadence").
                pace = f"  [at most every {opt.interval:g}s]" if opt.interval else ""
                print(f"    {name}: {opt.description}  -> {opt.next or 'end'}{pace}")
        else:
            title = f": {s.title}" if s.title else ""
            print(f"- {s.id}{title}{suffix}  -> {s.next or 'end'}")
    for warning in wf.warnings:
        print(f"warning: {warning}")
    # Advice to whoever is WRITING this file, which is why it lives here and
    # not in front of every run (see `Workflow.deprecations`).
    for note in wf.deprecations:
        print(f"deprecated: {note}")
    for note in wf.advice:
        print(f"advice: {note}")
    return 0


def _print_payload(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_asks(args: argparse.Namespace) -> int:
    """Decisions other sessions' runs are waiting on someone for.

    Deliberately not a way to answer one: the CLI is the HUMAN channel, and a
    human settles a delegated question through the approval and selection
    doors that already exist (which is also how an override gets recorded as
    an override). This is the read — most useful for seeing why a run has
    gone quiet, and for checking that a responder was actually asked.
    """
    session = args.session or state_mod.current_scope()
    waiting = engine.open_asks(session)
    if args.json:
        _print_payload({"session": session, "waiting_on": waiting})
        return 0
    if not waiting:
        print(f"nothing is waiting on {session!r}")
        return 0
    for entry in waiting:
        options = "|".join(o["name"] for o in entry.get("options") or [])
        print(
            f"{entry['ask']}  {entry['workflow']}/{entry['step']}  "
            f"from {entry['from_session']}  [{options}|abstain]"
        )
        print(f"  {(entry.get('prompt') or '').strip().splitlines()[0]}")
        if entry.get("deadline"):
            print(f"  moves on after {entry['deadline']}")
        print(f"  {entry['cwd']}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args, required=False)
    payload = engine.status(cwd=cwd, scope=scope)
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
        others = [s for s in state_mod.scopes_in(cwd) if s != here]
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
        _print_block("gate", payload.get("gate"))
        print("unblock:  claunch cflow approve")
    if status == "waiting_answer":
        # Delegated, and until now invisible from here: the CLI printed the
        # status word and stopped, so the one channel a person can actually
        # answer through showed neither the question nor the way in. It is
        # still not the reader's move — the wording says who holds it before
        # it says what they could do — but the door is named, because a
        # person's answer lands over a responder's and only they can decide
        # this run has waited long enough.
        ask = payload.get("ask") or {}
        kind = ask.get("kind") or payload.get("reason")
        _print_block(
            "decision" if kind == "branch" else "gate",
            ask.get("prompt") or payload.get("prompt"),
        )
        _print_options(ask.get("options") or payload.get("options") or [])
        holders = ", ".join(
            str(e.get("handle") or e.get("kind") or "?")
            for e in ask.get("asked") or []
        )
        print(f"with:     {holders or 'nobody — nothing will answer it but you'}")
        if ask.get("deadline"):
            print(f"          moves on after {ask['deadline']}")
        for entry in ask.get("skipped") or []:
            print(f"skipped:  {entry.get('candidate')} — {entry.get('reason')}")
        door = payload.get("user_door") or {}
        if door.get("command"):
            print(f"yours:    {door['command']}")
            print(
                "          you can answer it now — yours lands over theirs"
                if holders
                else "          it is yours: nobody else was asked"
            )
    if status == "waiting_selection":
        # Everything a person needs to answer this, in the order they need
        # it: the question, what each answer means, and only then what the
        # agent would do. Its proposal read first for a while, which is the
        # wrong way round — the recommendation is the part being checked.
        _print_block("decision", payload.get("prompt"))
        _print_options(payload.get("options", []))
        proposal = payload.get("proposal") or {}
        _print_block(
            "proposed",
            f"{proposal.get('option')!r} — "
            f"{proposal.get('reason') or 'no reasoning recorded'}",
        )
        names = ", ".join(o["name"] for o in payload.get("options", []))
        print(f"confirm:  claunch cflow select <{names}>")
        print("          any of them — the proposal is a recommendation only")
    if status == "select":
        _print_block("decision", payload.get("prompt"))
        _print_options(payload.get("options", []))
        print(f"pending:  {payload.get('chooser')} decides this one")
    if status == "waiting_window":
        # The agent chose; the workflow paces that option. Nobody is asked
        # anything — but a person CAN take it now: a confirm from here is not
        # paced, and is journaled as the override it is.
        _print_block(
            "held",
            f"{payload.get('option')!r} — this option runs at most every "
            f"{payload.get('interval'):g}s; its window opens at "
            f"{payload.get('opens_at')} (~{payload.get('remaining')}s). The "
            f"daemon releases it then (or the agent's next 'next' after that "
            f"moment does)",
        )
        print(f"override: claunch cflow select {payload.get('option')}   "
              f"(takes it now, unpaced)")
    if pending:
        _print_pending(pending)
    return 0


#: Width of the ``label:`` column this report lines its values up on.
_LABEL = 10

#: Wrap width. Narrow enough for a half-screen terminal, which is where a
#: dashboard-watching human usually keeps this.
_WIDTH = 78


def _print_block(label: str, text) -> None:
    """A label and a value that may run to several lines.

    Workflow prompts are written as paragraphs — the question a person is
    being asked rarely fits in one line, and printing only its first would
    hide the half that says what the choice costs.
    """
    body = (text or "").strip()
    if not body:
        return
    head = f"{label + ':':<{_LABEL}}"
    pad = " " * _LABEL
    first = True
    for para in body.splitlines():
        for line in textwrap.wrap(para.strip(), _WIDTH - _LABEL) or [""]:
            print(f"{head if first else pad}{line}")
            first = False


def _print_options(options: list) -> None:
    """Each option with the description its workflow gave it.

    The names alone are what this printed before, and a name is not a
    decision: ``<request, hold>`` says nothing about what either one does to
    the run. The descriptions were already in the payload — the person
    confirming simply never got shown them, and had to reconstruct the
    choice from whatever the agent happened to say in its terminal.
    """
    if not options:
        return
    print("options:")
    width = max(len(o["name"]) for o in options)
    for opt in options:
        desc = " ".join((opt.get("description") or "").split())
        name = f"  {opt['name']:<{width}}"
        if not desc:
            print(name.rstrip())
            continue
        indent = " " * (len(name) + 3)
        for i, line in enumerate(textwrap.wrap(desc, _WIDTH - len(indent))):
            print(f"{name} — {line}" if i == 0 else f"{indent}{line}")


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
    scope, cwd = _resolve_run(args, required=False)
    if args.cancel:
        payload = engine.cancel_request(by="user", scope=scope, cwd=cwd)
        print(f"withdrew the pending start of {payload['request'].get('workflow')!r}")
        return 0
    if not args.workflow:
        raise engine.CflowError("a workflow name is required (or pass --cancel)")
    payload = engine.request_start(
        args.workflow, args.context, by="user", scope=scope, cwd=cwd
    )
    request = payload["request"]
    _report_unblock(
        f"requested a start of {request['name']!r}",
        engine.nudge_for_request(request["workflow"]),
        scope,
        cwd,
    )
    return 0


def _nudge_via_daemon(message: str, scope, cwd) -> list:
    """Type a resume nudge into the run's own session (scope == session name).

    The cwd half of the pair is the run's own directory — where
    :func:`_resolve_run` found it, which is not necessarily where this shell
    stands; see :func:`responders.nudge` for the rest.
    """
    try:
        return responders.nudge(
            scope or state_mod.current_scope(), message, cwd=cwd or str(Path.cwd())
        )
    except Exception:
        return []  # a nudge is a convenience; never fail a CLI action on it


def _report_unblock(action: str, message: str, scope, cwd) -> None:
    nudged = _nudge_via_daemon(message, scope, cwd)
    if nudged:
        print(f"{action}; nudged session(s): {', '.join(nudged)}")
    else:
        print(f"{action}; nudge the agent to continue")


def _cmd_approve(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    payload = engine.approve(by="user", scope=scope, cwd=cwd)
    _report_unblock(
        f"approved gate at step {payload.get('step_id')!r}",
        engine.NUDGE_APPROVED,
        scope,
        cwd,
    )
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    payload = engine.select(args.option, args.reason, by="user", scope=scope, cwd=cwd)
    if payload.get("status") in ("done", "aborted"):
        print(f"selected {args.option!r}; workflow is {payload['status']}")
    else:
        _report_unblock(f"selected {args.option!r}", engine.NUDGE_SELECTED, scope, cwd)
    return 0


def _cmd_goto(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    payload = engine.goto(args.step, by="user", reason=args.reason, scope=scope, cwd=cwd)
    if payload.get("status") in ("done", "aborted"):
        print(f"workflow forced to {payload['status']}")
        return 0
    _report_unblock(
        f"current step forced to {args.step!r} (visit {payload.get('visit')})",
        engine.nudge_for_state(args.step),
        scope,
        cwd,
    )
    return 0


def _cmd_abort(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    payload = engine.abort(by="user", scope=scope, cwd=cwd)
    print(f"aborted run {payload.get('run')}")
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    payload = engine.archive(by="user", scope=scope, cwd=cwd)
    print(f"archived run {payload.get('run')} -> {payload.get('archived_to')}")
    if payload.get("was") not in ("done", "aborted"):
        print("note: the run was still active; it was aborted before archiving")
    print("the slot is free — a new run can be started here")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    engine.reset(cwd=cwd, scope=scope)
    print("cleared cflow run state (journal kept)")
    return 0


def _cmd_journal(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    entries = state_mod.read_journal(cwd, scope=scope)
    for entry in entries[-args.tail :] if args.tail else entries:
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def _cmd_checkout(args: argparse.Namespace) -> int:
    """Who else is standing in the directory this run works in.

    Prints, never gates: the exit code stays 0 even with neighbours, because
    the answer this command gives is one a human decides on. A checkout
    shared with sessions the decider cannot move (another subtree's, say)
    must not become a wall that stops every integration — the value here is
    that nobody passes through *unknowingly*, not that nobody passes.
    """
    session = args.session or os.environ.get(state_mod.SESSION_ENV) or ""
    occ = checkout.inspect(session=session, cwd=None)
    print(f"cwd     : {occ.run_cwd}")
    if occ.problem:
        # Not an error: "could not ask" is a different answer from "nobody is
        # there", and saying which one it is keeps the reader from reading
        # silence as an all-clear.
        print(f"unknown : {occ.problem}")
        return 0
    print(f"session : {occ.session_cwd}")
    print(f"peers   : {', '.join(occ.peers) if occ.peers else '(none)'}")
    note = checkout.warning(occ)
    if note:
        print(f"warning : {note}")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    from .cli import run_install

    print("note: 'cflow install' is now 'claunch install'; installing every "
          "skill and the merged MCP server")
    return run_install(args.profile, args.project, args.global_, args.all_)


def _cmd_example(args: argparse.Namespace) -> int:
    target = Path(".") / state_mod.PROJECT_WORKFLOWS / f"{args.name}.yaml"
    if target.exists():
        print(f"error: {target} already exists", file=sys.stderr)
        return 1
    install.install_workflow(install.example_workflow(), target)
    print(f"wrote example workflow: {target}")
    print(f"run it with: /cflow {args.name} <task description>")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    """Put a workflow file into a layer, having first read it.

    The reason this exists rather than a documented ``cp``: the destination
    layer is the one nobody looks at. A broken YAML copied into a project is
    found the next time somebody runs it; copied into the global layer it sits
    there until an unrelated project's picker breaks on it. So the file is
    parsed before it is installed, and a failure is refused here, once, where
    the person who can fix it is standing.
    """
    if args.name and len(args.workflow) > 1:
        print(
            "error: --name renames one workflow; you gave several",
            file=sys.stderr,
        )
        return 1
    if args.project is not None:
        dest_dir = Path(args.project) / state_mod.PROJECT_WORKFLOWS
        layer = state_mod.LAYER_PROJECT
    else:
        dest_dir = state_mod.global_workflows_dir()
        layer = state_mod.LAYER_GLOBAL

    failed = False
    for ref in args.workflow:
        # A name, not just a path: promoting a project's workflow to the
        # global layer is the common case, and it should not require knowing
        # where either layer keeps its files.
        try:
            located = state_mod.locate(ref)
            src = located.path
            wf = state_mod.compose_located(located).workflow
        except model.WorkflowError as exc:
            print(f"error: {ref}: {exc}", file=sys.stderr)
            failed = True
            continue
        dest = dest_dir / f"{args.name or src.stem}.yaml"
        if dest.resolve() == src.resolve():
            print(f"error: {src} is already the {layer} copy", file=sys.stderr)
            failed = True
            continue
        if args.overlay:
            if not _write_overlay(
                src, dest, ref, layer, args.project, force=args.force
            ):
                failed = True
            continue
        outcome = install.install_workflow(src, dest, force=args.force)
        if outcome == install.KEPT:
            print(
                f"error: {dest} already exists and differs; pass --force to "
                f"replace it",
                file=sys.stderr,
            )
            failed = True
            continue
        verb = "already there" if outcome == install.UNCHANGED else "added"
        print(f"{verb}: {dest}  ({wf.step_count()} steps, {layer})")
        _report_shadowing(dest.stem, layer)
    return 1 if failed else 0


def _write_overlay(
    src: Path,
    dest: Path,
    ref: str,
    layer: str,
    project: Optional[str],
    *,
    force: bool,
) -> bool:
    """Write a layer over ``src`` at ``dest``, and prove it composes.

    The stub is deliberately almost empty. What makes a layer worth having is
    that it holds ONLY what this repository changes — the moment it holds a
    copy of a step's prose it is the old arrangement again, drifting from the
    base with nobody to notice.

    It is written and then loaded back: a stub whose ``extends`` cannot be
    resolved from where it now sits (promoting a project overlay into the
    global layer is the way to get one) is removed again rather than left as a
    file that only fails when somebody tries to run it.
    """
    if dest.exists() and not force:
        print(
            f"error: {dest} already exists; pass --force to replace it",
            file=sys.stderr,
        )
        return False
    base_ref = ref if not ref.endswith((".yaml", ".yml")) else src.stem
    stub = (
        f"# A layer over {src}.\n"
        f"#\n"
        f"# Only what THIS project changes belongs here — every property not\n"
        f"# named below is inherited from the base, one property at a time, so\n"
        f"# the base stays the single place the workflow is written. An\n"
        f"# explicit `null` deletes an inherited property; omitting it inherits.\n"
        f"extends: {base_ref}\n"
        f"\n"
        f"# steps:\n"
        f"#   <step id>:\n"
        f"#     verify: <the command THIS project checks that step with>\n"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(stub, encoding="utf-8")
    try:
        composed = state_mod.compose_located(
            state_mod.Located(dest.stem, dest, layer), project
        )
    except model.WorkflowError as exc:
        dest.unlink(missing_ok=True)
        print(f"error: the layer would not compose from {dest}: {exc}", file=sys.stderr)
        return False
    print(
        f"wrote layer: {dest}  (extends {base_ref} -> {composed.bases[0]}, "
        f"{composed.workflow.step_count()} steps inherited)"
    )
    print(f"  add only what this project changes; run it with: /cflow {dest.stem}")
    return True


def _report_shadowing(name: str, layer: str) -> None:
    """Say if what was just added is not what would run here.

    Adding to the global layer from inside a project that declares the same
    name is a real thing to do (you are publishing it for *other* projects),
    so this is a note, not an error — but a silent one would look like the
    add had no effect.
    """
    try:
        winner = state_mod.locate(name)
    except model.WorkflowError:
        return
    if winner.origin != layer:
        print(f"  note: here, {winner.origin} wins — {winner.path}")


def _cmd_update(args: argparse.Namespace) -> int:
    from . import worktree

    # The isatty guard, shared with worktree pruning: a real terminal gets a
    # prompt for edited files, a script or an agent session
    # (``$CLAUNCH_SESSION`` set) cannot and must say --force instead.
    can_ask = worktree.interactive()
    outcomes = install.update_global_workflows(
        list(args.name),
        force=args.force,
        can_ask=can_ask,
    )
    failed = False
    for name, outcome, applied, detail in outcomes:
        if applied:
            print(f"{name}: {detail}")
        else:
            # refused (an edited/unknown file without --force, or a "no")
            print(f"{name}: kept — {detail}", file=sys.stderr)
            failed = True
    if failed and not can_ask:
        print(
            "run with --force to replace edited files (each kept as .bak)",
            file=sys.stderr,
        )
    return 1 if failed else 0


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
            help="target this session's run, wherever on this machine it "
            "lives (default: $CLAUNCH_SESSION, or the nearest run in or "
            "above this directory)",
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

    q = csub.add_parser(
        "asks",
        help="decisions other runs are waiting on a session for (read-only)",
    )
    q.add_argument(
        "--session",
        help="whose decisions to list (default: this session)",
    )
    q.add_argument("--json", action="store_true", help="print raw JSON")
    q.set_defaults(func=_cmd_asks)

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
        help="alias for 'claunch install' (one MCP server + every skill)",
    )
    from .cli import add_install_scope_args

    add_install_scope_args(q)
    q.set_defaults(func=_cmd_install)

    q = csub.add_parser("example", help="scaffold an example workflow in this project")
    q.add_argument("name", nargs="?", default="feature-dev")
    q.set_defaults(func=_cmd_example)

    q = csub.add_parser(
        "add",
        help="install a workflow into the global layer, so every project "
        "can run it (--project to install into one project instead)",
    )
    q.add_argument(
        "workflow",
        nargs="+",
        help="a .yaml path, or the name of a workflow findable from here "
        "(which promotes this project's copy to the global layer)",
    )
    q.add_argument("--name", help="install under this name instead of the file's")
    layer = q.add_mutually_exclusive_group()
    layer.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="install into the global layer — the default",
    )
    layer.add_argument(
        "--project",
        nargs="?",
        const=".",
        default=None,
        metavar="DIR",
        help=f"install into DIR/{state_mod.PROJECT_WORKFLOWS.as_posix()}/ "
        f"instead (DIR defaults to the current directory)",
    )
    q.add_argument(
        "--overlay",
        action="store_true",
        help="write a LAYER over the workflow instead of copying it: a stub "
        "declaring 'extends: <name>', where only the properties this "
        "layer changes (a repo-specific verify, say) are written and "
        "everything else is inherited",
    )
    q.add_argument(
        "--force", action="store_true", help="replace a different file already there"
    )
    q.set_defaults(func=_cmd_add)

    q = csub.add_parser(
        "update",
        help="bring stale global workflows up to the packaged copies "
        "(edited ones are kept unless --force)",
    )
    q.add_argument(
        "name",
        nargs="*",
        help="workflow(s) to update; without names, every packaged workflow",
    )
    q.add_argument(
        "--force",
        action="store_true",
        help="replace edited/unknown files without asking (each kept as one .bak)",
    )
    q.set_defaults(func=_cmd_update)

    q = csub.add_parser("mcp", help="run the stdio MCP server (spawned by claude)")
    q.set_defaults(func=_cmd_mcp)
