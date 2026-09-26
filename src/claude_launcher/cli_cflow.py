"""``claunch cflow ...`` subcommands.

The human/orchestrator side of cflow: list and inspect workflows, watch a
run, and operate the controls the agent deliberately does not have —
``approve`` (gates), ``select`` (confirming user-chooser branches) and
``goto`` (forcing the position, or answering with ``--approve``/``--deny`` an
agent's request to leave the route its workflow declares). Also
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
    # --run: the command is about one of the session's SUB runs. Installed
    # as the ambient run for the rest of this (short-lived) process rather
    # than threaded through every engine call below — the engine's ops read
    # the ambient run exactly as they read the ambient scope, and the slot
    # search here stays about the scope (a sub run lives inside it).
    sub = getattr(args, "run", None)
    if sub:
        state_mod.push_run(str(sub))

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
    if wf.recur:
        recur = (
            "    recur: yes (auto — the daemon starts each next round)"
            if wf.recur_auto
            else "    recur: yes (each finished round requests the next)"
        )
    else:
        recur = ""
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
    def step_line(s):
        """Return the stable, single-step part of the tree display."""
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
        if s.timer:
            # A timed wait: the daemon moves the run on this schedule (see
            # the model's "Timed wait" section).
            flags.append(
                f"timer: every {s.timer.every:g}s x{s.timer.max} "
                f"-> {s.timer.then}, then {s.timer.after}"
            )
        if s.triggers:
            # Daemon side effects. On a graph review they belong beside the
            # other declared properties: nothing else shows them, and a
            # reader who cannot see them reads a step that quietly costs the
            # driver context (`checks`) or an LLM call (`briefing`) as one
            # that does neither.
            flags.append(
                "triggers: "
                + ", ".join(f"{t.do} at {t.at}" for t in s.triggers)
            )
        suffix = f"  ({'; '.join(flags)})" if flags else ""
        if s.select:
            chooser = s.select.chooser
            if s.select.delegate:
                chooser = s.select.delegate.describe()
            return f"{s.id} [select, chooser={chooser}]{suffix}"
        else:
            title = f": {s.title}" if s.title else ""
            return f"{s.id}{title}{suffix}  -> {s.next or 'end'}"

    # A flat list made a branch look like a backward arrow: readers had to
    # scan the whole workflow to discover which option reached which step.
    # Render the reachable graph as a rooted tree instead.  A workflow is a
    # graph, so a merge or a cycle is printed as a reference after its first
    # occurrence; this keeps the output finite while preserving the edge.
    seen = set()

    def children(s):
        if s.select:
            return [(name, opt.next) for name, opt in s.select.options.items()]
        return [(None, s.next)]

    def render(step_id, prefix="", branch="", edge=None):
        if not step_id or step_id not in wf.steps:
            return
        s = wf.steps[step_id]
        connector = branch
        edge_text = f"{edge}: " if edge else ""
        if step_id in seen:
            print(f"{prefix}{connector}{edge_text}↪ {step_id} (위에서 표시됨)")
            return
        seen.add(step_id)
        label = step_line(s)
        print(f"{prefix}{connector}{edge_text}{label}")
        outgoing = children(s)
        # Option descriptions belong to the edge, so they remain adjacent to
        # the branch they describe instead of becoming detached list items.
        if s.select:
            outgoing = [
                (f"{name}: {opt.description}"
                 + (f"  [at most every {opt.interval:g}s]" if opt.interval else ""),
                 opt.next)
                for name, opt in s.select.options.items()
            ]
        for i, (edge_label, target) in enumerate(outgoing):
            last = i == len(outgoing) - 1
            render(target, prefix + ("│  " if not last else "   "),
                   "└─ " if last else "├─ ", edge_label)

    render(wf.start)
    # Keep malformed/disconnected steps visible after the rooted tree. The
    # parser accepts unreachable nodes for authoring diagnostics.
    for step_id in wf.steps:
        if step_id not in seen:
            render(step_id)
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


def _cmd_checklist(args: argparse.Namespace) -> int:
    """Show the current checklist gate, optionally re-measuring it first.

    ``--recheck`` is the door that keeps a checklist honest without a daemon:
    the clock is what normally measures and moves, and a run with no daemon
    behind it would otherwise sit at a gate nothing ever samples. It is not an
    override — it runs the same items under the same two conditions, so a red
    item stays red.
    """
    scope, cwd = _resolve_run(args, required=False)
    if args.recheck:
        moved = engine.check_checklist(cwd=cwd, scope=scope)
        if moved and moved.get("moved_to"):
            print(
                f"checklist {'expired' if moved.get('expired') else 'passed'}: "
                f"{moved['step']} -> {moved['moved_to']} "
                f"({moved['passed']}/{moved['total']} items true)"
            )
    payload = engine.status(cwd=cwd, scope=scope)
    if args.json:
        _print_payload(payload.get("checklist") or {})
        return 0
    if payload.get("status") != "waiting_checklist":
        print(
            f"this run is not at a checklist gate "
            f"(status: {payload.get('status')})"
        )
        return 0
    print(f"step:     {payload.get('step_id')}")
    _print_checklist(payload.get("checklist") or {})
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
    if payload.get("sub"):
        print(f"sub run:  {payload['sub']}  (of {payload.get('parent_run')})")
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
    for sub in payload.get("subs") or []:
        where = f" (step {sub.get('step_id')})" if sub.get("step_id") else ""
        print(
            f"sub:      {sub.get('sub')}  {sub.get('workflow')}  "
            f"{sub.get('status')}{where}  (--run {sub.get('sub')})"
        )
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
    if status == "waiting_checklist":
        _print_checklist(payload.get("checklist") or {})
    _print_state(payload.get("state"))
    _print_landing(payload)
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


#: What a checklist item's three states look like on a terminal. ``?`` is not
#: a decoration: an item nobody could measure is a different fact from one
#: that measured false, and a reader chasing a stuck gate needs to tell them
#: apart before deciding where to look.
_MARKS = {True: "[x]", False: "[ ]", None: "[?]"}


def _print_checklist(checklist: dict) -> None:
    """The gate as a list a person can read: what is true, and what is not.

    This is the whole point of the ``checklist:`` gate reaching the CLI. The
    same decisions used to be carried by a step's prose, where the only way
    to learn which conditions held was to ask the agent — and its account is
    exactly what a mechanical gate exists to stop relying on.
    """
    if not checklist:
        return
    if checklist.get("prompt"):
        _print_block("gate", checklist["prompt"])
    print(
        f"{'checklist:':<{_LABEL}}{checklist.get('passed')}/"
        f"{checklist.get('total')} true"
        + (f"  (measured {checklist['checked_at']})" if checklist.get("checked_at") else "")
    )
    pad = " " * _LABEL
    for item in checklist.get("items") or []:
        code = item.get("exit_code")
        if item.get("by"):
            # Ticked, not measured: there is no exit code to report, and "could
            # not measure" would read as a broken command.
            who = "/".join(item["by"])
            detail = item.get("output") if item.get("ok") else (
                f"waits for {who} to tick it: claunch cflow set {item.get('path')} true"
                if "user" in item["by"] and item.get("path") else f"waits for {who}"
            )
        else:
            detail = "not measured yet" if item.get("measured_at") is None else (
                f"exit {code}" if code is not None else "could not measure"
            )
        print(f"{pad}{_MARKS.get(item.get('ok'), '[?]')} {item.get('id')}: "
              f"{item.get('describe')} ({detail})")
    then = checklist.get("then")
    if checklist.get("all_true") and not checklist.get("report_filed"):
        print(f"{'held by:':<{_LABEL}}the step's report has not been filed — "
              f"every item is true and the move is waiting on it")
    elif checklist.get("all_true"):
        print(f"{'moves to:':<{_LABEL}}{then} — the daemon performs it")
    else:
        print(f"{'moves to:':<{_LABEL}}{then}, once every item is true "
              f"(the daemon measures; nobody has to advance it)")
    otherwise = checklist.get("otherwise") or {}
    if otherwise and not checklist.get("all_true"):
        when = otherwise.get("expires_at") or f"{otherwise.get('after')}s after presentation"
        print(f"{'or else:':<{_LABEL}}{otherwise.get('then')} at {when}, "
              f"if the list is still not all true")
    print(f"{'recheck:':<{_LABEL}}claunch cflow checklist --recheck")


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
        print(f"{action}; nudge accepted for session(s): {', '.join(nudged)}")
    else:
        print(f"{action}; nudge the agent to continue")


def _cmd_set(args: argparse.Namespace) -> int:
    """Write one of the run's declared state paths as a person.

    The person's door to ``editable:``; the agent's is the ``set_state`` MCP
    tool. The driving session is nudged so it reads the new value from its
    payload's ``state`` rather than learning of it at some later step.
    """
    scope, cwd = _resolve_run(args)
    payload = engine.set_state(
        args.path, args.value, by="user", scope=scope, cwd=cwd
    )
    print(
        f"{payload['path']}: {payload['was']!r} -> {payload['value']!r}"
        + ("" if payload["changed"] else "  (unchanged)")
    )
    print(f"applies:  {payload['applies']}")
    _report_unblock(
        f"set {payload['path']!r}",
        engine.nudge_for_state_write(payload["path"], payload["value"]),
        scope,
        cwd,
    )
    return 0


def _print_state(entries) -> None:
    """The run's writable state: one line per declared path."""
    for entry in entries or []:
        who = "/".join(entry.get("by") or [])
        line = f"{entry.get('path')} = {entry.get('value')!r}  [{who}]"
        if entry.get("set_by"):
            line += f"  (set by {entry['set_by']} at {entry.get('set_at')})"
        print(f"{'state:':<{_LABEL}}{line}")


def _print_landing(payload: dict) -> None:
    """The run's landing queue (``landing_queue:``): one line per entry."""
    if "landing_queue" not in payload:
        return
    queue = payload.get("landing_queue") or []
    if not queue:
        print(f"{'landing:':<{_LABEL}}queue empty")
    for entry in queue:
        tip = str(entry.get("tip") or "")[:8]
        line = f"{entry.get('issue')} {entry.get('status')}  {entry.get('branch') or '?'} @ {tip}"
        line += f"  (by {entry.get('requested_by')})"
        if entry.get("note"):
            line += f"  -- {entry['note']}"
        print(f"{'landing:':<{_LABEL}}{line}")


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
    """Force the position, or answer the agent's request for one.

    One command for both because they are one question to the person typing
    it — where should this run be — and because a reader who has just been
    handed "approve with 'claunch cflow goto --approve'" should not have to
    learn a second verb to say no, or to send the run somewhere third.
    """
    scope, cwd = _resolve_run(args)
    if args.approve or args.deny:
        if args.step:
            print(
                "give a step or --approve/--deny, not both: --approve grants "
                "the step the agent asked for, while naming a step forces "
                "that one instead (which also answers the request)",
                file=sys.stderr,
            )
            return 2
        decision = "approve" if args.approve else "deny"
        payload = engine.resolve_goto(
            decision, by="user", reason=args.reason, scope=scope, cwd=cwd
        )
        asked = (payload.get("goto_request") or {}).get("step")
        if decision == "deny":
            _report_unblock(
                f"refused the request to move to {asked!r}",
                engine.NUDGE_GOTO_DENIED,
                scope,
                cwd,
            )
            return 0
        if payload.get("status") in ("done", "aborted"):
            print(f"granted; workflow is {payload['status']}")
            return 0
        _report_unblock(
            f"granted the request to move to {asked!r} "
            f"(visit {payload.get('visit')})",
            engine.nudge_for_state(str(asked)),
            scope,
            cwd,
        )
        return 0
    if not args.step:
        print(
            "give a step to force the run to, or --approve/--deny to answer "
            "the agent's pending request",
            file=sys.stderr,
        )
        return 2
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


def _cmd_sub_done(args: argparse.Namespace) -> int:
    """Answer 'has sub run NAME finished?' with an exit code.

    Three answers, and keeping the third apart is the point: a sub run that
    was never started (or was already archived) is not "not done yet" — a
    checklist waiting on it would wait forever, so it exits 2 and says so.
    """
    scope, cwd = _resolve_run(args, required=False)
    if getattr(args, "all", False):
        # Every sub run of the scope finished (or none stands): the shape a
        # step's `awaits: {sub: all}` and a wrap-up gate ask. Aborted counts
        # as finished here — the question is "is anything still going",
        # and an aborted side track is not.
        main = engine.status(cwd=cwd, scope=scope)
        running = [
            s for s in (main.get("subs") or [])
            if s.get("status") not in ("done", "aborted")
        ]
        if running:
            for s in running:
                print(f"sub run {s['sub']!r}: {s.get('status')} (step {s.get('step_id')})  run: {s.get('run')}")
            return 1
        print(f"sub runs: none active ({len(main.get('subs') or [])} finished)")
        return 0
    if not args.name:
        print("sub-done: give a sub run NAME, or --all")
        return 2
    try:
        payload = engine.status(cwd=cwd, scope=scope, run=args.name)
    except state_mod.StateError as exc:
        print(f"sub run {args.name!r}: {exc}")
        return 2
    status = payload.get("status")
    if status == "idle":
        print(f"sub run {args.name!r}: no such sub run in this scope")
        return 2
    step = payload.get("step_id")
    print(
        f"sub run {args.name!r}: {status}"
        + (f" (step {step})" if step else "")
        + f"  run: {payload.get('run')}"
    )
    return 0 if status == "done" else 1


def _cmd_published(args: argparse.Namespace) -> int:
    """Answer 'has SOURCE published MILESTONE since STEP consumed it?'.

    The probe behind ``awaits: {sub, at}`` / ``awaits: {main}``. The asking
    run is ``--run``, else ``$CLAUNCH_CFLOW_RUN`` (the daemon sets it when the
    probe is a sub run's), else the main run. Exit 0 = a publication the step
    has not consumed, 1 = none yet, 2 = the source run does not stand.
    """
    if not getattr(args, "run", None) and os.environ.get(state_mod.RUN_ENV):
        args.run = os.environ[state_mod.RUN_ENV]
    scope, cwd = _resolve_run(args, required=False)
    try:
        view = engine.published(
            args.source, args.milestone, step=args.step, cwd=cwd, scope=scope
        )
    except (engine.CflowError, state_mod.StateError) as exc:
        print(f"published: {exc}")
        return 2
    if not view["stands"]:
        print(f"run {view['from']!r}: not running in this scope")
        return 2
    print(
        f"{view['from']} {view['milestone']}: published {view['count']}x, "
        f"step {view['step']} consumed {view['consumed']} — "
        + ("new" if view["new"] else "nothing new")
    )
    return 0 if view["new"] else 1


def _cmd_responders(args: argparse.Namespace) -> int:
    """Who a delegated step of this run would ask right now, group by group.

    Exit 0 when some group would reach an answerable member, 1 when none
    would, 2 when it cannot be told (no run, no such step, the roster did not
    answer). The check a step runs before a question is opened — a worker that
    finds no reviewer spawns its own before ``peer-review`` resolves.
    """
    scope, cwd = _resolve_run(args)
    try:
        payload = engine.who_answers(
            args.step, role=args.role or "", cwd=cwd, scope=scope
        )
    except (engine.CflowError, state_mod.StateError) as exc:
        print(f"responders: {exc}")
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        label = f"step {payload['step']!r}" + (
            f", role {payload['role']!r}" if payload["role"] else ""
        )
        print(f"{label} as {payload['me'] or '?'} in mesh {payload['mesh'] or '?'}:")
        for group in payload["groups"]:
            asks = ", ".join(
                f"{h} ({group['relations'].get(h, '?')})" for h in group["asks"]
            )
            print(f"  {group['candidate']}: " + (asks or f"nobody — {group.get('reason')}"))
    if payload["unreadable"]:
        return 2
    return 0 if payload["resolves"] else 1


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
    _report_unblock("archived", engine.NUDGE_ARCHIVED, scope, cwd)
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    engine.reset(cwd=cwd, scope=scope)
    print("cleared cflow run state (journal kept)")
    return 0


def _cmd_journal(args: argparse.Namespace) -> int:
    scope, cwd = _resolve_run(args)
    entries = state_mod.read_journal(cwd, scope=scope, events=args.event)
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
        parser.add_argument(
            "--run",
            metavar="SUB",
            help="act on the session's SUB run of this name instead of its "
            "main run ('claunch cflow status' lists them under 'subs')",
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
        "checklist",
        help="show the current checklist gate; --recheck re-measures it now",
    ))
    q.add_argument(
        "--recheck",
        action="store_true",
        help="run every item now instead of waiting for the daemon's poll "
        "(the gate's two conditions are unchanged — this is not an override)",
    )
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_checklist)

    q = _scoped(csub.add_parser(
        "set",
        help="write one of the run's declared state paths (the workflow's "
        "'editable:'), as a person; auto-nudges the run's session",
    ))
    q.add_argument(
        "path",
        help="steps.<id>.skip, steps.<id>.checklist.<item>, or a declared name",
    )
    q.add_argument("value", help="true/false for a bool path; text otherwise")
    q.set_defaults(func=_cmd_set)

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
        help="force the run's current step, or answer the agent's request for "
        "one with --approve/--deny (human override; 'end' finishes); "
        "auto-nudges the run's session",
    ))
    q.add_argument(
        "step",
        nargs="?",
        help="step id to force the run to ('end' finishes it); omit when "
        "answering a request with --approve/--deny",
    )
    q.add_argument(
        "--approve",
        action="store_true",
        help="grant the step-change the agent asked for (see 'status')",
    )
    q.add_argument(
        "--deny",
        action="store_true",
        help="refuse it; the run stays where it is and the agent is told",
    )
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

    q = csub.add_parser(
        "sub-done",
        help="exit 0 when the session's sub run NAME has finished (done), 1 "
        "while it is still running or was aborted, 2 when no such sub run "
        "stands — the check a main run's checklist item or verify uses to "
        "wait on a side track",
    )
    q.add_argument("name", nargs="?", help="the sub run's name")
    q.add_argument(
        "--all",
        action="store_true",
        help="exit 0 when NO sub run of the session is still running (none, "
        "or all finished/aborted), 1 while any is — the shape a step's "
        "'awaits: {sub: all}' measures",
    )
    q.add_argument(
        "-t",
        "--session",
        help="whose sub run (default: $CLAUNCH_SESSION, or the nearest run)",
    )
    q.set_defaults(func=_cmd_sub_done)

    q = csub.add_parser(
        "published",
        help="exit 0 when run SOURCE ('main' or a sub run's name) has "
        "published MILESTONE since the asking step last consumed it, 1 when "
        "not, 2 when SOURCE does not stand — the probe of 'awaits: {sub, at}' "
        "and 'awaits: {main}'",
    )
    q.add_argument("source", help="'main', or the sub run's name")
    q.add_argument("milestone", help="the milestone a step 'publishes:'")
    q.add_argument(
        "--step",
        help="the asking run's step whose consumption counts (default: its "
        "current step)",
    )
    q.add_argument(
        "--run",
        help="the asking run: a sub run's name (default: $CLAUNCH_CFLOW_RUN, "
        "else the main run)",
    )
    q.add_argument(
        "-t",
        "--session",
        help="whose runs (default: $CLAUNCH_SESSION, or the nearest run)",
    )
    q.set_defaults(func=_cmd_published)

    q = csub.add_parser(
        "responders",
        help="who a delegated step of this run would ask right now, one line "
        "per candidate group (read-only, wires nothing): exit 0 when some "
        "group reaches an answerable member, 1 when none does, 2 when it "
        "cannot be told",
    )
    q.add_argument("step", help="the step whose ask/chooser to resolve")
    q.add_argument(
        "--role",
        help="only the groups naming this role — e.g. 'reviewer', so a "
        "leader fallback does not answer 'is there a reviewer'",
    )
    q.add_argument(
        "-t",
        "--session",
        help="whose run (default: $CLAUNCH_SESSION, or the nearest run)",
    )
    q.add_argument("--json", action="store_true", help="print raw JSON")
    q.set_defaults(func=_cmd_responders)

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
    q.add_argument("-e", "--event", action="append", help="filter by event name (repeatable)")
    q.set_defaults(func=_cmd_journal)

    q = csub.add_parser(
        "checkout",
        help="who else is standing in the directory this run works in; "
        "prints and never blocks (always exits 0)",
    )
    q.add_argument(
        "--session",
        help="whose neighbours to look for (default: $CLAUNCH_SESSION)",
    )
    q.set_defaults(func=_cmd_checkout)

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
