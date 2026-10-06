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
    for c in payload.get("choices") or []:
        opts = ", ".join(o["name"] for o in c["options"])
        print(f"  choice {c['instance']} ({c['chooser']}): {opts}")
    for w in payload.get("waits") or []:
        print(f"  {w['kind']} {w['instance']}: {w.get('describe') or w.get('probe') or ''}")
    for d in payload.get("delegations") or []:
        print(f"  delegation {d['id']} {d['node']}: {d['executor']} {d['status']}")
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
    chart = _load(args.workflow)
    payload = runtime.start(chart, cwd=os.getcwd(), scope=args.scope,
                            params=_parse_params(args.param),
                            inputs=_parse_inputs(args.input, args.inputs_file),
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

    _refuse_own_session(args.scope, "abort")
    _print_status(runtime.abort(args.reason, cwd=os.getcwd(), scope=args.scope), False)
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "select")
    payload = runtime.select(args.node, args.option, args.reason, caller=runtime.CALLER_USER,
                             cwd=os.getcwd(), scope=args.scope)
    _print_status(payload, False)
    return 0


def _cmd_extend(args: argparse.Namespace) -> int:
    from .cflow2 import runtime

    _refuse_own_session(args.scope, "extend")
    if args.visits:
        payload = runtime.extend_visits(args.node, args.by, stop=args.stop, cwd=os.getcwd(),
                                        scope=args.scope)
    else:
        payload = runtime.extend_loop(args.node, args.by, stop=args.stop, cwd=os.getcwd(),
                                      scope=args.scope)
    _print_status(payload, False)
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
    q.add_argument("node", nargs="?", help="loop (or, with --visits, node) instance id "
                   "(default: the only one)")
    q.add_argument("--by", type=int, default=1)
    q.add_argument("--stop", action="store_true", help="end the loop with outcome fail")
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

    q = scoped(csub.add_parser("resume", help="resume a run a 'halt' monitor stopped "
                               "(waiting_approval, reason monitor_violation)"))
    q.add_argument("monitor", nargs="?", help="only this monitor's hold (default: all)")
    q.set_defaults(func=_guarded(_cmd_resume))
