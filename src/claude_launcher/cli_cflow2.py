"""``claunch cflow2 ...`` -- cflow v2 definitions and runs (``docs/cflow-v2.md``).

``ls``/``show``/``compile``/``validate``/``caps`` read definitions;
``start``/``status``/``journal``/``abort`` operate the run in this directory;
``select``, ``extend`` and ``approve`` are the person's side of a run (a
``chooser: user`` choice, a loop that reached its ``max``, a task's ``ask``),
which the agent's tools refuse. ``collection push|set|remove`` change a
collection as the principal ``person`` (``ls`` only reads).

The person's commands (``approve``, ``select``, ``extend``, ``resume``,
``abort``, ``collection push|set|remove``, ``start --force`` over a live run) refuse to run in a shell whose
``$CLAUNCH_SESSION`` is the session that drives the run (or carries out its
nested delegation): that shell is the agent's. A person answers from another
terminal or the web UI. This is the check that holds; the harness deny rules
(``install.GATE_DENY_RULES``) only keep the agent from typing the commands in
the first place. Neither stops code that imports the runtime directly
(``python -c``) -- the gates are a guard against drift, not a security
boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .cflow2 import ir


def _errors() -> tuple:
    from .cflow2 import expr, frontend, runtime, store

    return (runtime.FlowError, store.StoreError, frontend.FrontendError, ir.IRError,
            expr.ExprError)


def _guarded(fn):
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except _errors() as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return run


# -- definitions --------------------------------------------------------------


def _cmd_ls(_args: argparse.Namespace) -> int:
    from .cflow2 import layers

    items = layers.list_definitions()
    if not items:
        dirs = ", ".join(str(d) for _, d in layers.search_dirs())
        print(f"no v2 workflows found (searched: {dirs})")
        return 0
    for item in items:
        if item.error:
            print(f"{item.name:<28} [{item.layer}] (invalid: {item.error})  [{item.path}]")
        else:
            print(f"{item.name:<28} [{item.layer}] {item.kind or ''}  {item.description}  "
                  f"[{item.path}]")
        for shadowed in item.shadows:
            print(f"{'':<28} overrides [{shadowed}]")
    return 0


def _load(ref: str) -> ir.Chart:
    from .cflow2 import runtime

    return runtime.load_definition(ref, os.getcwd())


def _tree_lines(chart: ir.Chart, nid: str, depth: int = 0, role: str = "") -> List[str]:
    node = chart.node(nid)
    pad = "  " * depth
    head = f"{role}: " if role else ""
    extra = ""
    if node.type == ir.T_TASK:
        desc = f"task {node.label}"
        if node.get("title") and node.get("title") != node.label:
            desc += f" -- {node.get('title')}"
    elif node.type == ir.T_LOOP:
        desc = f"{node.get('mode')} (max {node.get('max')})"
        if node.get("guard"):
            desc += f" {node.get('guard')}"
        if node.get("items"):
            desc += f" in {node.get('items')}" + (" par" if node.get("parallel") else "")
    elif node.type == ir.T_CONTROL:
        desc = f"{node.get('op')}" + (f" {node.get('outcome')}" if node.get("outcome") else "") \
            + (f" -> {node.get('target')}" if node.get("target") else "")
    elif node.type in (ir.T_PAR, ir.T_GROUP):
        desc = f"{node.type} settle={node.get('settle')}"
        if node.type == ir.T_GROUP:
            desc += f" limit={node.get('limit')}"
    elif node.type == ir.T_SCOPE:
        desc = f"call {node.get('proc')}"
    elif node.type == ir.T_WAIT:
        desc = f"wait {node.get('describe') or node.get('probe') or node.get('seconds')}"
    elif node.type == ir.T_TIMEOUT:
        desc = f"timeout {node.get('seconds')}s"
    else:
        desc = node.type
    if node.executor:
        extra += f" executor={node.executor}"
    if node.bind:
        extra += f" bind={node.bind}"
    if node.label and node.type != ir.T_TASK:
        extra += f" id={node.label}"
    out = [f"{pad}{head}{desc}{extra}"]
    t = node.type
    if t == ir.T_SEQ:
        for c in node.get("children") or []:
            out += _tree_lines(chart, c, depth + 1)
    elif t == ir.T_COND:
        for br in node.get("branches") or []:
            out += _tree_lines(chart, br["child"], depth + 1,
                               f"if {br['guard']}" if br.get("guard") else "else")
    elif t == ir.T_CHOOSE:
        for opt in node.get("options") or []:
            role_ = f"option {opt['name']}" + (f" when {opt['guard']}" if opt.get("guard") else "")
            out += _tree_lines(chart, opt["child"], depth + 1, role_)
    elif t == ir.T_PAR:
        for name, c in (node.get("branches") or {}).items():
            out += _tree_lines(chart, c, depth + 1, f"branch {name}")
    elif t == ir.T_LOOP:
        out += _tree_lines(chart, node.get("child"), depth + 1, "do")
        if node.get("between"):
            out += _tree_lines(chart, node.get("between"), depth + 1, "between")
    elif t == ir.T_TIMEOUT:
        out += _tree_lines(chart, node.get("child"), depth + 1, "do")
        if node.get("else_child"):
            out += _tree_lines(chart, node.get("else_child"), depth + 1, "else")
    elif t == ir.T_TRY:
        out += _tree_lines(chart, node.get("child"), depth + 1, "try")
        for oc, c in (node.get("catch") or {}).items():
            out += _tree_lines(chart, c, depth + 1, f"catch {oc}")
        if node.get("finally_child"):
            out += _tree_lines(chart, node.get("finally_child"), depth + 1, "finally")
    elif t == ir.T_GROUP:
        out += _tree_lines(chart, node.get("child"), depth + 1, "do")
        out += _tree_lines(chart, node.get("member"), depth + 1, "member")
    elif t == ir.T_SCOPE:
        out += _tree_lines(chart, node.get("child"), depth + 1)
    for oc, c in (node.on or {}).items():
        out += _tree_lines(chart, c, depth + 1, f"on {oc}")
    return out


def _cmd_show(args: argparse.Namespace) -> int:
    chart = _load(args.workflow)
    print(f"{chart.name} ({chart.kind}) -- {chart.description}  [{chart.source}]")
    if chart.params:
        print("params: " + ", ".join(chart.params))
    for line in _tree_lines(chart, chart.root):
        print("  " + line)
    for w in chart.warnings:
        print(f"warning: {w}")
    return 0


def _cmd_compile(args: argparse.Namespace) -> int:
    chart = _load(args.workflow)
    if args.json:
        print(chart.to_json())
        return 0
    tasks = chart.tasks()
    print(f"{chart.name}: {len(chart.nodes)} nodes, {len(tasks)} tasks, "
          f"{len(chart.delegation_points())} delegation points, root {chart.root}")
    for w in chart.warnings:
        print(f"warning: {w}")
    return 0


def _caps_for(profile: Optional[str], harness: Optional[str]):
    from .cflow2 import runtime

    caps = runtime.resolve_caps(profile, harness)
    if caps is None:
        from .cflow2 import caps as caps_mod
        caps = caps_mod.resolve(harness or "claude", profile=profile)
    return caps


def _cmd_validate(args: argparse.Namespace) -> int:
    from .cflow2 import caps as caps_mod, check, executors

    try:
        chart = _load(args.workflow)
    except _errors() as exc:
        print(f"compile error: {exc}")
        return 1
    print(f"{chart.name}: compiled ({len(chart.nodes)} nodes)  [{chart.source}]")
    for w in chart.warnings:
        print(f"warning: {w}")
    findings = check.check(chart)
    for f in findings:
        print(str(f))
    if not findings:
        print("checks: no findings")
    caps = _caps_for(args.profile, args.harness)
    for w in getattr(caps, "warnings", []) or []:
        print(f"caps warning: {w}")
    points = chart.delegation_points()
    who = ", ".join(x for x in (f"profile {args.profile}" if args.profile else "",
                                 f"harness {args.harness}" if args.harness else "") if x) \
        or "this session's profile"
    if points:
        print(f"delegation points ({who}):")
    for node in points:
        plan = caps_mod.plan_node(node, caps)
        print(f"  {node.id}: executor {plan.route} "
              f"(preferences {', '.join(node.executor)})")
        for part in ("returns", "readonly", "parallel", "budget", "carry"):
            p = getattr(plan, part, None)
            if p is not None:
                print(f"    {part}: {p.route}")
        for reason in plan.degraded:
            print(f"    fallback: {reason}")
        if plan.route in executors.DISPATCHED and not executors.registered():
            nxt = executors.InlineFallback.next_executor(node, plan.route, caps)
            print(f"    fallback: without a daemon dispatcher {plan.route} -> {nxt}")
    errs = [f for f in findings if f.level == check.ERROR]
    return 1 if errs else 0


def _cmd_caps(args: argparse.Namespace) -> int:
    from .cflow2 import caps as caps_mod

    caps = _caps_for(args.profile, args.harness)
    for cap_id in caps_mod.CAPABILITIES:
        c = caps.get(cap_id)
        on = getattr(c, "enabled", bool(c))
        src = getattr(c, "source", "")
        print(f"{cap_id:<28} {'yes' if on else 'no ':<4} {src}")
    for w in getattr(caps, "warnings", []) or []:
        print(f"warning: {w}")
    return 0


# -- runs ---------------------------------------------------------------------


def _refuse_own_session(scope: Optional[str], what: str) -> None:
    """A person's command run from the shell of the session that drives the
    run is the agent answering its own gate: refused."""
    from .cflow2 import runtime, store

    session = os.environ.get(store.SESSION_ENV, "").strip()
    if not session:
        return
    st = store.RunStore(os.getcwd(), scope)
    if not st.exists():
        return
    try:
        state = st.load_state()
    except store.StoreError:
        return
    if runtime.may_drive(state, session):
        raise runtime.FlowError(
            f"'claunch cflow2 {what}' is a person's answer, and this shell belongs to "
            f"session {session}, which drives this run. Run it from a terminal outside the "
            f"session, or use the web UI")


def _print_status(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if payload.get("status") == "idle":
        print(payload.get("note"))
        return
    print(f"{payload.get('workflow')} [{payload.get('run')}] {payload.get('status')}"
          + (f" ({payload['reason']})" if payload.get("reason") else ""))
    if payload.get("status") not in ("running", "waiting_approval"):
        print(f"outcome: {payload.get('outcome')}  value: "
              f"{json.dumps(payload.get('value'), ensure_ascii=False)}")
        return
    for t in payload.get("tasks") or []:
        print(f"  task {t['instance']}: {t.get('title') or t.get('label')}")
        res = t.get("call_result") or {}
        if res:
            print(f"    call {res.get('call')} {res.get('procedure')} "
                  f"returned {res.get('outcome')}")
        last = t.get("last_verify") or {}
        if last:
            tail = (last.get("tail") or "").strip().splitlines()
            print(f"    verify failed ({last.get('why')})" + (f": {tail[-1]}" if tail else ""))
    for c in payload.get("choices") or []:
        opts = ", ".join(o["name"] for o in c["options"])
        print(f"  choice {c['instance']} ({c['chooser']}): {opts}")
    for w in payload.get("waits") or []:
        print(f"  {w['kind']} {w['instance']}: {w.get('describe') or w.get('probe') or ''}")
    for d in payload.get("delegations") or []:
        print(f"  delegation {d['id']} {d['node']}: {d['executor']} {d['status']}")
    for c in payload.get("calls") or []:
        # dynamic calls (docs 2.12): the callee is its own run, at --scope <scope>
        done = f" -> {c['outcome']}" if c.get("outcome") else ""
        print(f"  call {c['id']} {c.get('label') or c['node']}: {c['procedure']} "
              f"{c['status']}{done} (scope {c['scope']})")
    for a in payload.get("asks") or []:
        print(f"  ask {a['instance']} [{a['ask']}] {a.get('resolution')}: "
              f"{(a.get('prompt') or '').strip().splitlines()[0] if a.get('prompt') else ''}")
        if a.get("question_text"):
            # a brief ask (docs 2.10): the whole question, then the answers
            for line in str(a["question_text"]).splitlines():
                print(f"    {line}".rstrip())
            print(f"    answers: {', '.join(o['name'] for o in a.get('options') or [])}")
        print(f"    {a.get('how')}")
    if payload.get("inputs") is not None:
        print(f"  inputs: {json.dumps(payload['inputs'], ensure_ascii=False)}")
    if payload.get("halted"):
        print(f"  HALTED by monitor {', '.join(payload['halted'])} (claunch cflow2 resume)")
    for v in payload.get("verifying") or []:
        print(f"  verifying {v['instance']}: {v.get('label') or v.get('node')}")
    acc = payload.get("acceptance") or {}
    if acc.get("status") == "failed":
        print("  acceptance failed: " + ", ".join(str(x.get("id")) for x in acc.get("failed")
                                                 or []) + " (claunch cflow2 approve acceptance)")
    for lim in payload.get("visit_limits") or []:
        print(f"  visit limit {lim['instance']}: {lim['visits']}/{lim['max']} "
              f"(claunch cflow2 extend --visits {lim['instance']} [--by N | --stop])")
    for lim in payload.get("loop_limits") or []:
        print(f"  loop limit {lim['instance']}: {lim['iterations']}/{lim['max']} "
              f"(claunch cflow2 extend {lim['instance']} [--by N | --stop])")


def _parse_params(items: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"error: --param needs k=v, got {item!r}")
        key, _, raw = item.partition("=")
        try:
            out[key.strip()] = json.loads(raw)
        except ValueError:
            out[key.strip()] = raw
    return out


def _parse_inputs(items: List[str], path: Optional[str]) -> Optional[Dict[str, Any]]:
    """``--inputs-file`` (a JSON object) overlaid by each ``--input k=v``
    (raw text; the definition's input types coerce it)."""
    out: Dict[str, Any] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"error: --inputs-file {path}: {exc}")
        if not isinstance(doc, dict):
            raise SystemExit(f"error: --inputs-file {path}: must hold a JSON object")
        out.update(doc)
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"error: --input needs name=value, got {item!r}")
        key, _, raw = item.partition("=")
        out[key.strip()] = raw
    return out if (out or path) else None


def _cmd_start(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    if args.force:
        _refuse_own_session(args.scope, "start --force")
    from .cflow2 import asks

    chart = _load(args.workflow)
    mesh = getattr(args, "mesh", None) or asks.home_mesh(
        runtime.default_driver(args.scope), cwd=os.getcwd())
    payload = runtime.start(chart, cwd=os.getcwd(), scope=args.scope,
                            params=_parse_params(args.param),
                            inputs=_parse_inputs(args.input, args.inputs_file),
                            caps=runtime.resolve_caps(args.profile), force=args.force,
                            mesh=mesh)
    _print_status(payload, args.json)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _print_status(runtime.status(cwd=os.getcwd(), scope=args.scope), args.json)
    return 0


def _cmd_journal(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    for ev in runtime.journal(cwd=os.getcwd(), scope=args.scope, limit=args.n):
        rest = {k: v for k, v in ev.items() if k not in ("ts", "event")}
        print(f"{ev.get('ts')} {ev.get('event'):<18} {json.dumps(rest, ensure_ascii=False)}")
    return 0


#: What a person's command types into the run's driver, so it picks the run
#: back up at once -- and a held driver is woken for it (claunch-2xyjw). The
#: dashboard's buttons send the same through the daemon (``daemon/api.py``).
NUDGE_SELECTED = ("[cflow2] a person took a choice of your run ({option}){note}. Call "
                  "flow_status and continue (its person_picks keeps the pick and the "
                  "whole note).")
NUDGE_MOVED = ("[cflow2] a person moved your run - what you were doing there is "
               "cancelled; call flow_status and continue from its tasks.")
NUDGE_MOVE_ANSWERED = ("[cflow2] a person answered your move request ({decision}). "
                       "Call flow_status and continue.")
NUDGE_REPORTED = ("[cflow2] a person reported your run's task - stop working on it; "
                  "call flow_status and continue.")
NUDGE_ABORTED = ("[cflow2] a person aborted your v2 workflow run - stop working on it; "
                 "flow_status shows it aborted.")
NUDGE_EXTENDED = ("[cflow2] a person answered your run's limit hold. Call flow_status "
                  "and continue.")
NUDGE_INPUT_SET = ("[cflow2] a person changed your run's start input {name} to {value}. "
                   "Steps read it from now on; call flow_status and continue.")


def _nudge_driver(scope: Optional[str], message: str) -> None:
    from .cflow2 import asks

    nudged = asks.nudge_driver(message, cwd=os.getcwd(), scope=scope)
    if nudged:
        print(f"nudge accepted for session(s): {', '.join(nudged)}")


def _cmd_abort(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "abort")
    _print_status(runtime.abort(args.reason, cwd=os.getcwd(), scope=args.scope), False)
    _nudge_driver(args.scope, NUDGE_ABORTED)
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    from .cflow2 import store

    if args.apply and os.environ.get(store.SESSION_ENV):
        raise store.StoreError("recovery --apply is an operator action; run it outside an agent "
                               "session after inspecting the checkpoint and external effects")
    result = store.RunStore(os.getcwd(), args.scope).recovery(
        apply=args.apply, expected=args.revision)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_slots(args: argparse.Namespace) -> int:
    """The plan for this directory's run slots written before the daemon
    qualifier (claunch-9jmdz): which daemon owns each and where it would move.
    Read-only: the move is the owning daemon's, when it starts."""
    from .cflow2 import slot_migration
    from .daemon import cflow2_slots, paths

    me = paths.instance_id()
    report = slot_migration.migrate([args.cwd or os.getcwd()], me,
                                    cflow2_slots.known_sessions(), apply=False)
    doc = report.as_dict()
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    if not report.entries:
        print("no run slots without a daemon here")
        return 0
    for e in report.entries:
        what = {slot_migration.MOVE: f"-> {e.target} (this daemon, @{me}, at its next start)",
                slot_migration.OTHER: f"-> {e.target} (daemon {e.owner}, at its next start)",
                slot_migration.LEAVE: "stays"}[e.action]
        print(f"{e.slot} {what}: {e.why}")
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    out = runtime.archive(cwd=os.getcwd(), scope=args.scope)
    print(f"archived {out['workflow']} [{out['run']}] -> {out['archived']}")
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "move")
    if args.approve or args.deny:
        # The answer to the driver's move request (flow_request_move).
        if args.approve and args.deny:
            raise runtime.FlowError("give --approve or --deny, not both")
        if args.node:
            raise runtime.FlowError("--approve/--deny answer the pending request; it names "
                                    "its node already (give a node without them to move "
                                    "somewhere else, which supersedes the request)")
        payload = runtime.resolve_move("approve" if args.approve else "deny",
                                       reason=args.reason, cwd=os.getcwd(), scope=args.scope)
        req = payload.get("move_request") or {}
        what = ("the report of" if req.get("kind") == runtime.MOVE_KIND_REPORT
                else "the move to")
        answered = NUDGE_MOVE_ANSWERED.format(decision=req.get("decision") or "")
        if req.get("decision") == "denied":
            print(f"refused {what} {req.get('label') or req.get('node')} "
                  f"({req.get('id')}); the driver reads the refusal on its next flow_status")
            _print_status(payload, False)
            _nudge_driver(args.scope, answered)
            return 0
        if req.get("kind") == runtime.MOVE_KIND_REPORT:
            print(f"reported {req.get('label') or req.get('node')} as {req.get('outcome')} "
                  f"({req.get('id')})")
            _print_status(_after_verify(payload, args.scope), False)
            _nudge_driver(args.scope, answered)
            return 0
    elif not args.node:
        raise runtime.FlowError("name the node to move to, or answer the driver's request "
                                "with --approve / --deny")
    else:
        payload = runtime.force_move(args.node, args.reason, cwd=os.getcwd(),
                                     scope=args.scope)
    moved = payload.get("forced") or {}
    print(f"moved to {moved.get('label') or moved.get('node')} (from {moved.get('at')}; "
          f"cancelled: {', '.join(moved.get('left') or []) or 'nothing open'})")
    _print_status(payload, False)
    _nudge_driver(args.scope, NUDGE_MOVED)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "report")
    value = json.loads(args.value) if args.value else None
    text = "forced by a person" + (f": {args.reason}" if args.reason else "")
    payload = runtime.report(args.node, args.outcome, value, text, cwd=os.getcwd(),
                             scope=args.scope, by=runtime.CALLER_USER)
    _print_status(_after_verify(payload, args.scope), False)
    _nudge_driver(args.scope, NUDGE_REPORTED)
    return 0


def _after_verify(payload: Dict[str, Any], scope: Optional[str]) -> Dict[str, Any]:
    """A report this command filed may have started the task's verify, which
    runs in a thread of THIS process: wait for it before exiting, or it dies
    with the process and the task sits in 'verifying' until the lost-verify
    grace passes (s214, claunch-vwmoj). Returns the status after it."""
    from .cflow2 import runtime

    job = payload.get("verifying")
    if not job:
        return payload
    print(f"verifying {job.get('task')} (up to {float(job.get('timeout') or 0):g}s) ...",
          flush=True)
    runtime.verify_join()
    return runtime.status(cwd=os.getcwd(), scope=scope, tick=False)


def _cmd_recur(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    if not args.cancel:
        payload = runtime.status(cwd=os.getcwd(), scope=args.scope, tick=False)
        nxt = payload.get("next_round")
        print(f"next round: {json.dumps(nxt, ensure_ascii=False)}" if nxt
              else "no next round is requested here")
        return 0
    _refuse_own_session(args.scope, "recur --cancel")
    _print_status(runtime.cancel_next_round(args.reason, cwd=os.getcwd(), scope=args.scope),
                  False)
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "select")
    payload = runtime.select(args.node, args.option, args.reason, caller=runtime.CALLER_USER,
                             cwd=os.getcwd(), scope=args.scope)
    _print_status(payload, False)
    note = runtime.person_pick_words(payload, args.option)["note"]
    _nudge_driver(args.scope, NUDGE_SELECTED.format(option=args.option, note=note))
    return 0


def _cmd_extend(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "extend")
    if args.visits:
        if args.reset:
            raise SystemExit("error: --reset answers a loop limit, not --visits")
        payload = runtime.extend_visits(args.node, args.by, stop=args.stop, cwd=os.getcwd(),
                                        scope=args.scope)
    else:
        payload = runtime.extend_loop(args.node, args.by, stop=args.stop, reset=args.reset,
                                      cwd=os.getcwd(), scope=args.scope)
    _print_status(payload, False)
    _nudge_driver(args.scope, NUDGE_EXTENDED)
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    from .cflow2 import asks

    _refuse_own_session(args.scope, "approve")
    payload = asks.human_approve(args.node, decision=args.option, decline=args.decline,
                                 reason=args.reason, cwd=os.getcwd(), scope=args.scope)
    ans = payload.get("answered") or {}
    if ans.get("acceptance"):
        print("accepted the run's finish over its failed acceptance checks")
        _print_status(payload, False)
        return 0
    print(f"answered {ans.get('ask')} on {ans.get('label') or ans.get('node')}: "
          f"{ans.get('decision')}")
    _print_status(payload, False)
    return 0


def _cmd_responders(args: argparse.Namespace) -> int:
    """Who a task's ask would go to right now, group by group (read-only).

    Exit 0 when some group reaches an answerable member, 1 when none does, 2
    when it cannot be told (no v2 run here, no such node, a node that asks
    nobody, a roster that did not answer, a session in several meshes with
    none named for the run) -- so a step can tell "nobody" from "could not
    look" (claunch-3va8h, claunch-5itbf).
    """
    from .cflow2 import asks

    try:
        payload = asks.who_answers(args.node, role=args.role or "", cwd=os.getcwd(),
                                   scope=args.scope, mesh=getattr(args, "mesh", "") or "")
    except _errors() as exc:
        print(f"responders: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        label = f"node {payload['node']!r}" + (
            f", role {payload['role']!r}" if payload["role"] else "")
        print(f"{label} as {payload['me'] or '?'} in mesh {payload['mesh'] or '?'}:")
        for group in payload["groups"]:
            who = ", ".join(f"{h} ({group['relations'].get(h, '?')})" for h in group["asks"])
            print(f"  {group['candidate']}: " + (who or f"nobody -- {group.get('reason')}"))
    if payload["unreadable"]:
        return 2
    if payload.get("ambiguous") and not payload["resolves"]:
        # not "nobody": nobody was looked for. Naming the mesh settles it.
        print(f"cannot tell: the session is in several meshes "
              f"({', '.join(payload['ambiguous'])}); name the run's with "
              f"'claunch cflow2 mesh <name>' (or pass --mesh to look in one)",
              file=sys.stderr)
        return 2
    return 0 if payload["resolves"] else 1


def _cmd_mesh(args: argparse.Namespace) -> int:
    """Show or name the mesh the run's asks are routed in (claunch-5itbf).
    Inside a session only the run's driver names it; a person's terminal
    (no session) may too."""
    from .cflow2 import runtime, store

    if args.name is None and not args.clear:
        payload = runtime.status(cwd=os.getcwd(), scope=args.scope, tick=False)
        print(payload.get("mesh") or "(none named -- the roster decides at each ask)")
        return 0
    session = os.environ.get(store.SESSION_ENV, "").strip()
    if session:
        runtime.authorize(cwd=os.getcwd(), scope=args.scope, session=session)
    payload = runtime.set_mesh("" if args.clear else args.name, session=session,
                               caller=runtime.CALLER_AGENT if session else runtime.CALLER_USER,
                               cwd=os.getcwd(), scope=args.scope)
    print(f"mesh = {payload.get('mesh') or '(none)'}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "resume")
    payload = runtime.release_halt(args.monitor, cwd=os.getcwd(), scope=args.scope)
    _print_status(payload, False)
    return 0


def _parse_fields(items: List[str]) -> Dict[str, Any]:
    """``FIELD=VALUE`` pairs; a value is JSON when it parses as JSON, else
    the text itself (``status=landed``, ``count=3``, ``note="a b"``)."""
    out: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"error: a field needs FIELD=VALUE, got {item!r}")
        key, _, raw = item.partition("=")
        try:
            out[key.strip()] = json.loads(raw)
        except ValueError:
            out[key.strip()] = raw
    return out


def _print_collections(view: Dict[str, Any], only: Optional[str], as_json: bool) -> None:
    cols = view.get("collections") or {}
    if only:
        if only not in cols:
            from .cflow2 import runtime

            raise runtime.FlowError(f"no collection {only!r} in this run "
                                    f"(declared: {', '.join(cols) or 'none'})")
        cols = {only: cols[only]}
    if as_json:
        print(json.dumps({**view, "collections": cols}, ensure_ascii=False, indent=2))
        return
    if view.get("status") == "idle":
        print("no cflow2 run here")
        return
    if not cols:
        print(f"{view.get('workflow')} [{view.get('run')}] declares no collections")
    for name, c in cols.items():
        extra = ""
        if c.get("statuses"):
            extra = "  " + ", ".join(f"{k}={v}" for k, v in c["statuses"].items())
        if c.get("kind") == "log":
            extra += f"  cursor={c.get('cursor')} fresh={c.get('fresh')}"
        print(f"{name} ({c.get('kind')}, {c.get('count')} records){extra}")
        for rec in c.get("items") or []:
            fields = {k: v for k, v in rec.items() if k not in ("seq", "at", "by")}
            print(f"  #{rec.get('seq')} {json.dumps(fields, ensure_ascii=False)}  "
                  f"by {rec.get('by')}")
    if view.get("pending_triggers"):
        print(f"pending triggers: {view['pending_triggers']}")


def _cmd_collection(args: argparse.Namespace) -> int:
    from .cflow2 import runtime, store

    op = args.collection_command
    if op == "ls":
        _print_collections(runtime.collection_view(os.getcwd(), args.scope), args.name,
                           args.json)
        return 0
    _refuse_own_session(args.scope, f"collection {op}")
    kw: Dict[str, Any] = {}
    if op == "push":
        try:
            item = json.loads(args.item)
        except ValueError as exc:
            raise SystemExit(f"error: ITEM must be a JSON object: {exc}")
        if not isinstance(item, dict):
            raise SystemExit("error: ITEM must be a JSON object")
        kw["item"] = item
    else:
        kw["key"] = args.key  # the runtime reads it as the key field's type
        if op == "set":
            kw["fields"] = _parse_fields(args.field)
            if not kw["fields"]:
                raise SystemExit("error: set needs at least one FIELD=VALUE")
    payload = runtime.collection_op(op, args.name, cwd=os.getcwd(), scope=args.scope,
                                    principal=runtime.PR_PERSON,
                                    session=os.environ.get(store.SESSION_ENV, "").strip(), **kw)
    ev = payload.get("applied") or {}
    print(f"{op} {args.name}" + (f" {ev.get('key')}" if ev.get("key") is not None else "")
          + f" (seq {ev.get('seq')})" * (op == "push"))
    _print_collections(runtime.collection_view(os.getcwd(), args.scope), args.name, False)
    return 0


def _cmd_input(args: argparse.Namespace) -> int:
    """Write a start input of the run here. Without ``--user``,
    ``flow_input``'s CLI door: an empty ``set_by: [agent]`` input, the run's
    driver only. With ``--user``, a person's change of a ``set_by: [user]``
    input, whatever it holds (claunch-umq1f) -- refused from the driver's
    own shell, like every person's command."""
    from .cflow2 import runtime, store

    value: Any = args.value
    if args.json_value:
        try:
            value = json.loads(args.value)
        except ValueError as exc:
            raise SystemExit(f"error: VALUE is not JSON: {exc}")
    if args.user:
        _refuse_own_session(args.scope, "input --user")
        payload = runtime.set_input(args.name, value, caller=runtime.CALLER_USER,
                                    cwd=os.getcwd(), scope=args.scope)
    else:
        session = os.environ.get(store.SESSION_ENV, "").strip()
        runtime.authorize(cwd=os.getcwd(), scope=args.scope, session=session)
        payload = runtime.set_input(args.name, value, session=session, cwd=os.getcwd(),
                                    scope=args.scope)
    got = json.dumps((payload.get("inputs") or {}).get(args.name), ensure_ascii=False)
    print(f"input {args.name} = {got}")
    if args.user:
        _nudge_driver(args.scope, NUDGE_INPUT_SET.format(name=args.name, value=got))
    return 0


#: ``pr-preflight`` exit codes: the facts a v2 worker's landing reads to
#: choose its medium (docs/cflow-v2.md 4.6, claunch-yq80p.5).
PR_READY = 0
PR_BLOCKED = 1


def _cmd_pr_preflight(args: argparse.Namespace) -> int:
    """Can this directory land through a pull request right now? The same
    facts the session panel's PR wizard checks (prflow.preview): a
    repository with a commit, a remote on a GitHub host, ``gh`` installed
    and signed in to that host. Prints one JSON object (``ready``,
    ``remote``, ``host``, ``slug``, ``repo`` = the ``-R`` value, ``base``,
    ``head``, ``gh``, ``account``, ``blockers``); exit 0 = ready, 1 = not.

    ``--base`` names the PR's base instead of the configured one: a stack
    child's base is its parent's ``<parent>-stack`` (claunch-yq80p.2). That
    branch must already be on the chosen remote -- ``git ls-remote`` says so,
    and ``base_sha`` carries what it points at -- or it is a blocker: a PR
    cannot target a branch the remote does not have."""
    from . import prflow

    cwd = os.path.abspath(args.cwd or os.getcwd())
    doc = prflow.preview(cwd, session=os.environ.get("CLAUNCH_SESSION", ""))
    row = next((r for r in doc.get("remotes") or [] if r["remote"] == doc.get("remote")), {})
    host, slug = row.get("host"), row.get("slug")
    if doc.get("repo") and doc.get("remote") and not host:
        doc["blockers"].append(f"the chosen remote {doc['remote']!r} is not on a GitHub host")
    auth = (doc.get("auth") or {}).get(host or "", {})
    base_sha = None
    if args.base:
        doc["base"] = args.base
        if not doc.get("remote"):
            doc["blockers"].append(f"no remote to look for the base {args.base!r} on")
        else:
            # never wait on a credential prompt: an unreachable remote is a fact
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            code, line, err = prflow._git(cwd, "ls-remote", "--exit-code", doc["remote"],
                                          f"refs/heads/{args.base}", env=env)
            if code == 0 and line:
                base_sha = line.split()[0]
            elif code == 2:
                doc["blockers"].append(
                    f"the base {doc['remote']}/{args.base} is not on the remote"
                    " -- a stack child's base is pushed by its parent")
            else:
                doc["blockers"].append(
                    f"cannot read the remote {doc['remote']!r} to find the base "
                    f"{args.base!r} (git ls-remote exit {code}: {err or 'no output'})")
    out = {
        "ready": not doc["blockers"],
        "remote": doc.get("remote") or None,
        "configured": bool(row.get("configured")),
        "host": host, "slug": slug,
        "repo": f"{host}/{slug}" if host and slug else None,
        "base": doc.get("base") or "master",
        "base_sha": base_sha,
        "branch": doc.get("branch") or None,
        "head": doc.get("head") or None,
        "gh": (doc.get("gh") or {}).get("version"),
        "account": auth.get("account"),
        "blockers": doc["blockers"],
    }
    from . import local_sync
    try:
        out["pr_only"] = local_sync.protected(cwd, out["remote"] or "", out["base"])
    except Exception as exc:
        out["pr_only"] = None
        out["ready"] = False
        out["blockers"].append("cannot determine integration policy: " + str(exc))
    print(json.dumps(out, ensure_ascii=False, indent=None if args.compact else 1))
    return PR_READY if out["ready"] else PR_BLOCKED


def register(sub) -> None:
    p = sub.add_parser(
        "cflow2",
        help="cflow v2 workflows (combinators compiled to a statechart): list, "
        "validate, inspect capabilities, operate runs",
    )
    csub = p.add_subparsers(dest="cflow2_command", required=True)
    # No option abbreviations: the deny rules match the spelled-out flags
    # (`--force`), and argparse would otherwise read `--forc` as one (G3).
    _add_parser = csub.add_parser
    csub.add_parser = lambda *a, **k: _add_parser(*a, **{"allow_abbrev": False, **k})

    def scoped(q):
        q.add_argument("--scope", "-t", help="run slot (default: $CLAUNCH_SESSION or 'default')")
        return q

    q = csub.add_parser("ls", help="list v2 definitions (project, global, package)")
    q.set_defaults(func=_guarded(_cmd_ls))

    q = csub.add_parser("show", help="print a v2 definition's combinator tree")
    q.add_argument("workflow")
    q.set_defaults(func=_guarded(_cmd_show))

    q = csub.add_parser("compile", help="compile a v2 definition to IR")
    q.add_argument("workflow")
    q.add_argument("--json", action="store_true", help="print the IR as JSON")
    q.set_defaults(func=_guarded(_cmd_compile))

    q = csub.add_parser("validate", help="compile, run static checks, and show each "
                        "delegation point's executor and fallbacks for a profile")
    q.add_argument("workflow")
    q.add_argument("--profile")
    q.add_argument("--harness")
    q.set_defaults(func=_guarded(_cmd_validate))

    q = csub.add_parser("caps", help="composed capabilities and the layer each came from")
    q.add_argument("--profile")
    q.add_argument("--harness")
    q.set_defaults(func=_guarded(_cmd_caps))

    q = scoped(csub.add_parser("start", help="start a v2 run in this directory"))
    q.add_argument("workflow")
    q.add_argument("--param", action="append", default=[], metavar="K=V",
                   help="a param value (JSON or plain string); repeatable")
    q.add_argument("--profile", help="resolve capabilities for this profile")
    q.add_argument("--input", action="append", default=[], metavar="NAME=VALUE",
                   help="a typed start input (the definition's 'inputs'); repeatable")
    q.add_argument("--inputs-file", metavar="PATH",
                   help="a JSON object of start inputs (--input overrides its keys)")
    q.add_argument("--force", action="store_true", help="abort and archive a live run first")
    q.add_argument("--mesh", help="the mesh the run's asks are routed in (default: the one "
                   "team mesh the driving session is in now)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_guarded(_cmd_start))

    q = scoped(csub.add_parser("status", help="the v2 run here"))
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_guarded(_cmd_status))

    q = scoped(csub.add_parser("recover", help="inspect the durable checkpoint; optionally "
                               "preserve damaged files, restore projections and halt the run"))
    q.add_argument("--apply", action="store_true", help="operator-only: apply the inspected revision")
    q.add_argument("--revision", type=int, help="revision returned by the inspection")
    q.set_defaults(func=_guarded(_cmd_recover))

    q = csub.add_parser("slots", help="read-only: which daemon owns each run slot here "
                        "written before the daemon qualifier, and where it moves at that "
                        "daemon's next start")
    q.add_argument("--cwd", default=None, help="directory (default: here)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_guarded(_cmd_slots))

    q = scoped(csub.add_parser("journal", help="the run's event log"))
    q.add_argument("-n", type=int, default=None, help="last N events")
    q.set_defaults(func=_guarded(_cmd_journal))

    q = scoped(csub.add_parser("abort", help="abort the v2 run here"))
    q.add_argument("--reason", default="", help="optional; journaled")
    q.set_defaults(func=_guarded(_cmd_abort))

    q = scoped(csub.add_parser("archive", help="move the finished v2 run here into the "
                               "archive (the slot becomes idle)"))
    q.set_defaults(func=_guarded(_cmd_archive))

    q = scoped(csub.add_parser("move", help="a person's forced move to a node the "
                               "definition marks force_target: true, or the answer to "
                               "the driver's move request (--approve/--deny)"))
    q.add_argument("node", nargs="?", help="the target's node id or label (omit with "
                   "--approve/--deny)")
    q.add_argument("--approve", action="store_true",
                   help="grant the driver's pending move request (flow_request_move)")
    q.add_argument("--deny", action="store_true",
                   help="refuse the driver's pending move request; --reason is what it reads")
    q.add_argument("--reason")
    q.set_defaults(func=_guarded(_cmd_move))

    q = scoped(csub.add_parser("report", help="a person forces an active task's outcome "
                               "(checked against its returns, journaled by: user)"))
    q.add_argument("node", help="task instance id or label")
    q.add_argument("outcome")
    q.add_argument("--value", help="the outcome's fields as JSON")
    q.add_argument("--reason")
    q.set_defaults(func=_guarded(_cmd_report))

    q = scoped(csub.add_parser("recur", help="a recurring run's next-round request: show it, "
                               "or --cancel it (stops the loop after the finished round)"))
    q.add_argument("--cancel", action="store_true", help="drop the pending next-round request")
    q.add_argument("--reason")
    q.set_defaults(func=_guarded(_cmd_recur))

    q = scoped(csub.add_parser("select", help="answer a choice (including chooser: user)"))
    q.add_argument("node", help="choice instance id or label")
    q.add_argument("option")
    q.add_argument("--reason")
    q.set_defaults(func=_guarded(_cmd_select))

    q = scoped(csub.add_parser("extend", help="answer a loop_limit: allow more iterations, "
                               "reset the count, or stop the loop"))
    q.add_argument("node", nargs="?", help="loop (or, with --visits, node) instance id "
                   "(default: the only one)")
    q.add_argument("--by", type=int, default=1)
    g = q.add_mutually_exclusive_group()
    g.add_argument("--stop", action="store_true", help="end the loop with outcome fail")
    g.add_argument("--reset", action="store_true",
                   help="reset the loop's count to 0: it gets its full max again and "
                   "earlier extensions are dropped (--by is ignored)")
    q.add_argument("--visits", action="store_true",
                   help="answer a visit_limit (a node past the run's max_visits) instead of "
                   "a loop's max: --by more visits, or --stop")
    q.set_defaults(func=_guarded(_cmd_extend))

    q = scoped(csub.add_parser("approve", help="answer a task's ask as a person: approve "
                               "(enter the task), --decline, or --option for an ask with options"))
    q.add_argument("node", nargs="?", help="the waiting task's instance id or name "
                   "(default: the only open ask), or 'acceptance' to accept a finish whose "
                   "acceptance checks failed")
    g = q.add_mutually_exclusive_group()
    g.add_argument("--decline", action="store_true", help="refuse: the task finishes with its "
                   "on_decline outcome (default 'declined')")
    g.add_argument("--option", help="the answer of an ask that declares options")
    q.add_argument("--reason")
    q.set_defaults(func=_guarded(_cmd_approve))

    q = scoped(csub.add_parser("responders", help="who a task's ask would go to right now, "
                               "one line per 'from' group (read-only, wires nothing): exit 0 "
                               "when some group reaches an answerable member, 1 when none "
                               "does, 2 when it cannot be told"))
    q.add_argument("node", help="the task whose ask to resolve (name or node id)")
    q.add_argument("--role", help="only the groups naming this role -- e.g. 'reviewer', so a "
                   "leader group does not answer 'is there a reviewer'")
    q.add_argument("--mesh", help="look in this mesh instead of the run's own (state.mesh) "
                   "-- for a session in several meshes")
    q.add_argument("--json", action="store_true", help="print raw JSON")
    q.set_defaults(func=_cmd_responders)

    q = scoped(csub.add_parser("mesh", help="show or name the mesh this run's asks are "
                               "routed in (needed when its session is in several meshes)"))
    q.add_argument("name", nargs="?", help="the mesh to route asks in; omit to show it")
    q.add_argument("--clear", action="store_true", help="name none: the roster decides")
    q.set_defaults(func=_guarded(_cmd_mesh))

    q = csub.add_parser("collection", help="the run's collections: list them, or change "
                        "one as a person (push, set, remove)")
    colsub = q.add_subparsers(dest="collection_command", required=True)
    _col_add = colsub.add_parser
    colsub.add_parser = lambda *a, **k: _col_add(*a, **{"allow_abbrev": False, **k})
    r = scoped(colsub.add_parser("ls", help="collections with their records"))
    r.add_argument("name", nargs="?", help="only this collection")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=_guarded(_cmd_collection))
    r = scoped(colsub.add_parser("push", help="add a record (a queue replaces the record "
                                 "with the same key)"))
    r.add_argument("name")
    r.add_argument("item", metavar="ITEM", help="the record as a JSON object")
    r.set_defaults(func=_guarded(_cmd_collection))
    r = scoped(colsub.add_parser("set", help="change fields (or status) of a queue record"))
    r.add_argument("name")
    r.add_argument("key")
    r.add_argument("field", nargs="+", metavar="FIELD=VALUE")
    r.set_defaults(func=_guarded(_cmd_collection))
    r = scoped(colsub.add_parser("remove", help="delete a queue record"))
    r.add_argument("name")
    r.add_argument("key")
    r.set_defaults(func=_guarded(_cmd_collection))

    q = scoped(csub.add_parser("input", help="write a start input of the run here: an empty "
                               "set_by: [agent] input (the run's driver only), or with --user "
                               "a person's change of a set_by: [user] input"))
    q.add_argument("name")
    q.add_argument("value")
    q.add_argument("--user", action="store_true",
                   help="a person's change: the input's set_by names user; overwrites what "
                        "it holds (an empty VALUE clears it). Not from the driver's own shell")
    q.add_argument("--json-value", action="store_true",
                   help="read VALUE as JSON (a list, a number) instead of text")
    q.set_defaults(func=_guarded(_cmd_input))

    q = csub.add_parser("pr-preflight", help="can this directory land through a pull "
                        "request now (remote, host, gh, sign-in)? JSON; exit 0 ready, 1 not")
    q.add_argument("--cwd", help="the repository directory (default: here)")
    q.add_argument("--base", help="the PR's base branch, which must be on the remote "
                   "(a stack child's <parent>-stack); default: the configured base")
    q.add_argument("--compact", action="store_true", help="one line of JSON")
    q.set_defaults(func=_cmd_pr_preflight)

    # Packaged workflows need a probe available outside this repository's
    # tools/ directory. This command only reads board and checkout evidence.
    from .cflow2.coordinator import command as coordinator_check

    q = csub.add_parser("coordinator-check", help="read-only coordinator evidence check")
    mode = q.add_mutually_exclusive_group()
    mode.add_argument("--exists", action="store_true", help="require an existing issue")
    mode.add_argument("--parent", action="store_true", help="check the parent issue in run inputs")
    mode.add_argument("--settled", action="store_true", help="recheck all tracked issues")
    mode.add_argument("--direct-clean", action="store_true", help="check the direct-work checkout")
    mode.add_argument("--direct-ready", action="store_true", help="check the repository before direct work")
    q.add_argument("--board", help="canonical absolute repository directory")
    q.add_argument("--issue", help="beads issue id (must be closed by default)")
    q.add_argument("--key", help="expected board#issue key")
    q.set_defaults(func=coordinator_check)

    q = scoped(csub.add_parser("resume", help="resume a run a 'halt' monitor stopped "
                               "(waiting_approval, reason monitor_violation)"))
    q.add_argument("monitor", nargs="?", help="only this monitor's hold (default: all)")
    q.set_defaults(func=_guarded(_cmd_resume))
