"""``claunch cflow2 ...`` -- cflow v2 definitions and runs (``docs/cflow-v2.md``).

``ls``/``show``/``compile``/``validate``/``caps`` read definitions;
``start``/``status``/``journal``/``abort`` operate the run in this directory;
``select`` and ``extend`` are the person's side of a run (a ``chooser: user``
choice, a loop that reached its ``max``), which the agent's tools refuse.
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
    from .cflow2 import layers

    return layers.load(ref, os.getcwd())


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
    for c in payload.get("choices") or []:
        opts = ", ".join(o["name"] for o in c["options"])
        print(f"  choice {c['instance']} ({c['chooser']}): {opts}")
    for w in payload.get("waits") or []:
        print(f"  {w['kind']} {w['instance']}: {w.get('describe') or w.get('probe') or ''}")
    for d in payload.get("delegations") or []:
        print(f"  delegation {d['id']} {d['node']}: {d['executor']} {d['status']}")
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


def _cmd_start(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    chart = _load(args.workflow)
    payload = runtime.start(chart, cwd=os.getcwd(), scope=args.scope,
                            params=_parse_params(args.param),
                            caps=runtime.resolve_caps(args.profile), force=args.force)
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


def _cmd_abort(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _print_status(runtime.abort(args.reason, cwd=os.getcwd(), scope=args.scope), False)
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    payload = runtime.select(args.node, args.option, args.reason, caller=runtime.CALLER_USER,
                             cwd=os.getcwd(), scope=args.scope)
    _print_status(payload, False)
    return 0


def _cmd_extend(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    payload = runtime.extend_loop(args.node, args.by, stop=args.stop, cwd=os.getcwd(),
                                  scope=args.scope)
    _print_status(payload, False)
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "cflow2",
        help="cflow v2 workflows (combinators compiled to a statechart): list, "
        "validate, inspect capabilities, operate runs",
    )
    csub = p.add_subparsers(dest="cflow2_command", required=True)

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
    q.add_argument("--force", action="store_true", help="abort and archive a live run first")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_guarded(_cmd_start))

    q = scoped(csub.add_parser("status", help="the v2 run here"))
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_guarded(_cmd_status))

    q = scoped(csub.add_parser("journal", help="the run's event log"))
    q.add_argument("-n", type=int, default=None, help="last N events")
    q.set_defaults(func=_guarded(_cmd_journal))

    q = scoped(csub.add_parser("abort", help="abort the v2 run here"))
    q.add_argument("--reason", required=True)
    q.set_defaults(func=_guarded(_cmd_abort))

    q = scoped(csub.add_parser("select", help="answer a choice (including chooser: user)"))
    q.add_argument("node", help="choice instance id or label")
    q.add_argument("option")
    q.add_argument("--reason")
    q.set_defaults(func=_guarded(_cmd_select))

    q = scoped(csub.add_parser("extend", help="answer a loop_limit: allow more iterations "
                               "or stop the loop"))
    q.add_argument("node", nargs="?", help="loop instance id (default: the only one)")
    q.add_argument("--by", type=int, default=1)
    q.add_argument("--stop", action="store_true", help="end the loop with outcome fail")
    q.set_defaults(func=_guarded(_cmd_extend))
