"""Delegated asks of cflow v2 runs: the mesh side (``docs/cflow-v2.md`` 2.2).

The runtime keeps an ask's state (``runtime._open_ask`` and friends); this
module does what needs the daemon, always outside the run lock:

- :func:`route` resolves the next ``from`` group of every ask that needs one
  against one reading of the mesh roster (v1's :func:`..cflow.responders.pool`
  and :meth:`~..cflow.responders.Pool.match`, so candidates, scopes,
  ``connect`` and the "never something the run spawned" rule are v1's),
  applies the result under the lock (:func:`.runtime.ask_apply_route`), then
  delivers the question as a mesh ``decide`` message -- the same door v1's
  :func:`..cflow.responders.deliver` uses -- and records the ask in a small
  index so the responder can answer by id alone;
- :func:`tick` is what the daemon clock (and the driver's MCP calls) run: ask
  timeouts (applied by the runtime's time tick) and routing;
- :func:`answer_by_id` is ``flow_answer``: find the run an ask id belongs to
  through the index, answer it, wake the asking session;
- :func:`pending_for` lists the open asks addressed to a session;
- :func:`who_answers` previews, without wiring anything, which ``from``
  groups of a task's ask would reach someone (``claunch cflow2 responders``).

``pool_fn`` / ``deliver_fn`` / ``nudge_fn`` / ``withdraw_fn`` are injectable so
tests (and a caller that must not call its own daemon synchronously) can
replace the daemon traffic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import runtime
from . import store as store_mod
from .store import RunStore

PoolFn = Callable[..., Any]
DeliverFn = Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]
NudgeFn = Callable[[str, str, str], Any]

NUDGE_ANSWERED = (
    "[cflow2] an ask of your run was answered. Call flow_status and continue from "
    "where it put the run."
)
NUDGE_FELL = (
    "[cflow2] an ask of your run could not be answered by any responder: {how}"
)


# -- the index: ask id -> run --------------------------------------------------


def index_dir() -> Path:
    from .. import config

    return config.launcher_home() / "cflow2" / "asks"


def _index_write(ask_id: str, cwd: str, scope: str, sessions: List[str]) -> None:
    try:
        d = index_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ask_id}.json").write_text(
            json.dumps({"ask": ask_id, "cwd": cwd, "scope": scope, "sessions": sessions}),
            encoding="utf-8")
    except OSError:
        pass  # the message still names cwd and scope


def _index_read(ask_id: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((index_dir() / f"{ask_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _index_drop(ask_id: str) -> None:
    try:
        (index_dir() / f"{ask_id}.json").unlink()
    except OSError:
        pass


# -- daemon traffic (defaults) -------------------------------------------------


def default_pool(*, session: str, mesh: str = "", cwd: Optional[str] = None) -> Any:
    from ..cflow import responders

    return responders.pool(session=session, mesh=mesh, cwd=cwd)


def question_text(rec: Dict[str, Any], *, workflow: str, sender: str, cwd: str,
                  scope: str) -> str:
    """The message a responder receives: the question, the answers, the tool."""
    opts = "\n".join(f"  - {o['name']}: {o.get('description') or ''}".rstrip()
                     for o in runtime._ask_choices(rec))
    lines = [
        f"{sender} needs a decision on cflow2 workflow {workflow!r}, task "
        f"{rec.get('label') or rec.get('node')!r}.",
        "",
        str(rec.get("prompt") or "").strip(),
        "",
        "Answer with one of:",
        opts,
        "  - abstain: you have no basis to decide -- passes it further up",
        "",
        f"Record it with the claunch 'flow_answer' tool: {{ask_id: {rec['id']!r}, "
        f"decision: <one of the above>, reason: <why>}}. Judge it yourself -- you were "
        f"asked because {sender} does not get to decide this one. (Run: {cwd}, scope "
        f"{scope}.)",
    ]
    if rec.get("deadline"):
        lines.append(f"If you do not answer within {float(rec.get('timeout') or 0):g}s, it "
                     f"moves on without you.")
    return "\n".join(lines)


def default_deliver(rec: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
    """Post the question to the responders through the mesh (v1's door:
    ``POST /api/mesh/<mesh>/messages``, type ``decide``). Returns a failure
    reason; best-effort like v1 -- the ask is recorded and answerable either
    way."""
    from .. import daemon_client
    from ..cflow import responders

    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        return f"the claunch {daemon_client.unreachable_reason(why)}, so nobody was notified"
    try:
        client.post(
            f"/api/mesh/{ctx['mesh']}/messages",
            {"from": ctx["sender"], "to": [e.get("handle") for e in rec.get("asked") or []],
             "body": question_text(rec, workflow=ctx.get("workflow") or "",
                                   sender=ctx["sender"], cwd=ctx["cwd"], scope=ctx["scope"]),
             "type": "decide",
             "ref": {"kind": "cflow2.ask", "id": rec["id"], "task": rec.get("label")
                     or rec.get("node")}},
            timeout=responders.CALL_TIMEOUT,
        )
    except daemon_client.DaemonClientError as exc:
        return f"the question could not be delivered: {exc}"
    return None


def _closed_notice(item: Dict[str, Any], *, sender: str, to: List[str]) -> Optional[str]:
    """An ``fyi`` that a question is closed without a person's answer (it
    timed out to the next group, its task was cancelled, another responder
    answered, or the whole group abstained)."""
    from .. import daemon_client
    from ..cflow import responders

    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        return f"the claunch {daemon_client.unreachable_reason(why)}, so nobody was told"
    body = (f"cflow2: the question I sent you ({item.get('ask')}, {item.get('workflow')}/"
            f"{item.get('label') or item.get('node')}) is closed: {item.get('decision')}. "
            f"There is nothing left to decide here; do not answer it.")
    try:
        client.post(f"/api/mesh/{item['mesh']}/messages",
                    {"from": sender, "to": to, "body": body, "type": "fyi",
                     "ref": {"kind": "cflow2.ask.withdrawn", "id": item.get("ask"),
                             "task": item.get("label") or item.get("node")}},
                    timeout=responders.CALL_TIMEOUT)
    except daemon_client.DaemonClientError as exc:
        return f"the withdrawal could not be delivered: {exc}"
    return None


def default_nudge(session: str, message: str, cwd: str) -> Any:
    from ..cflow import responders

    return responders.nudge(session, message, cwd=cwd)


def default_withdraw(answered: Dict[str, Any], *, sender: str) -> Optional[str]:
    """Tell the responders in ``answered['pending']`` the question is closed
    (answered, timed out to the next group, cancelled, or everyone of the
    group abstained); ``decision`` says which."""
    from ..cflow import responders

    to = [e.get("handle") for e in answered.get("pending") or [] if e.get("handle")]
    if not to or not answered.get("mesh"):
        return None
    if answered.get("reason_kind") == "closed":
        return _closed_notice(answered, sender=sender, to=to)
    return responders.withdraw(
        {"id": answered.get("ask"), "step": answered.get("label") or answered.get("node")},
        mesh=str(answered["mesh"]), sender=sender, workflow=str(answered.get("workflow") or ""),
        to=to, decision=str(answered.get("decision")))


# -- routing -------------------------------------------------------------------


def _session_of(scope: str) -> str:
    return "" if scope in ("", store_mod.DEFAULT_SCOPE) else store_mod.session_of(scope)


def _driver(st: RunStore, state: Optional[Dict[str, Any]] = None) -> str:
    """The session a run's asks speak for: the driver recorded at start
    (never the scope a caller named -- a scope can be any slot)."""
    try:
        state = state if state is not None else st.load_state()
    except store_mod.StoreError:
        return ""
    return runtime.driver_of(state)


def plan_route(rec: Dict[str, Any], pool: Any) -> Dict[str, Any]:
    """Which group of ``rec`` answers, read off one roster (``pool``)."""
    from ..cflow import model as v1

    cands = rec.get("from") or []
    group = int(rec.get("group") or 0)
    plan: Dict[str, Any] = {"ask": rec["id"], "from_group": group, "skipped": [],
                            "asked": [], "mesh": getattr(pool, "mesh", "")}
    if getattr(pool, "unreadable", False) and group < len(cands) \
            and int(rec.get("deferrals") or 0) < runtime.MAX_ROSTER_DEFERRALS:
        plan["defer"] = getattr(pool, "problem", "") or "the mesh roster could not be read"
        return plan
    while group < len(cands):
        c = cands[group]
        cand = v1.Candidate(role=c["role"], scope=c.get("scope") or v1.SCOPE_ANY,
                            connect=bool(c.get("connect")))
        found, reason = pool.match(cand, autowire=True)
        if found:
            plan["asked"] = [m.to_dict() for m in found]
            break
        plan["skipped"].append({"group": group, "candidate": cand.describe(),
                                "reason": reason or "nobody matched"})
        group += 1
    plan["group"] = group
    if getattr(pool, "wired", None):
        plan["wired"] = list(pool.wired)
    return plan


def who_answers(node_ref: str, *, role: str = "", cwd: Optional[str] = None,
                scope: Optional[str] = None,
                pool_fn: Optional[PoolFn] = None) -> Dict[str, Any]:
    """Who task ``node_ref``'s ``ask`` would go to right now, one entry per
    ``from`` group (read-only) -- v1's :func:`..cflow.engine.who_answers` for
    a v2 run (claunch-3va8h).

    A step reads it *before* the question is opened: a worker that finds no
    reviewer spawns its own before ``peer-review`` routes. So every group is
    reported, not only the first that answers, and ``role`` keeps only the
    groups naming that role ("is there a *reviewer*" must not be answered yes
    by the leader group below it). Nothing is wired: a ``connect: true`` group
    lists the answerable members it would wire when the question opens.
    """
    from ..cflow import model as v1

    st = RunStore(cwd, scope)
    if not st.exists():
        raise runtime.FlowError(f"no cflow2 run here for scope {st.scope!r}")
    snap = runtime.Run.load(st)
    node = snap.chart.by_label(node_ref)
    if node is None:
        raise runtime.FlowError(f"workflow {snap.chart.name!r} has no node {node_ref!r}")
    spec = runtime._ask_spec(node) if node.get("ask") else {"from": []}
    if not spec["from"]:
        raise runtime.FlowError(
            f"node {node_ref!r} asks nobody -- it declares no 'ask' with 'from' groups")
    pool = (pool_fn or default_pool)(session=_driver(st, snap.state), mesh="",
                                     cwd=str(st.cwd))
    wanted = role.strip().lower()
    groups = []
    for c in spec["from"]:
        if wanted and c["role"] != wanted:
            continue
        cand = v1.Candidate(role=c["role"], scope=c["scope"] or v1.SCOPE_ANY,
                            connect=c["connect"])
        _, reason = pool.match(cand)
        named = pool.eligible(cand)
        entry = {
            "candidate": cand.describe(),
            "asks": [m.handle for m in named
                     if m.answerable and (m.handle in pool.reachable or cand.connect)],
            "relations": {m.handle: pool.relation(m.handle) for m in named if m.answerable},
        }
        if reason and not entry["asks"]:
            entry["reason"] = reason
        groups.append(entry)
    return {
        "node": node.label or node.id, "role": wanted, "me": pool.me, "mesh": pool.mesh,
        "groups": groups, "resolves": any(g["asks"] for g in groups),
        "unreadable": bool(getattr(pool, "unreadable", False)),
        "problem": getattr(pool, "problem", ""),
    }


def route(*, cwd: Optional[str] = None, scope: Optional[str] = None,
          now: Optional[float] = None, pool_fn: Optional[PoolFn] = None,
          deliver_fn: Optional[DeliverFn] = None) -> List[Dict[str, Any]]:
    """Route every ask waiting for its next group. Returns the routed plans."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return []
    snap = runtime.Run.load(st, now)
    if snap.status not in runtime.LIVE_STATUSES:
        return []
    if "storage-recovery" in runtime._halted(snap):
        return []
    todo = runtime.asks_to_route(snap)
    if not todo:
        return []
    session = _driver(st, snap.state)
    pool = (pool_fn or default_pool)(session=session, mesh=str(todo[0].get("mesh") or ""),
                                     cwd=str(st.cwd))
    plans = [plan_route(rec, pool) for rec in todo]
    routed = runtime.ask_apply_route(plans, cwd=cwd, scope=scope, now=now)
    deliver = deliver_fn or default_deliver
    failures = []
    for plan in routed:
        rec = plan["record"]
        sessions = [e.get("session") for e in rec.get("asked") or [] if e.get("session")]
        _index_write(rec["id"], str(st.cwd), st.scope, sessions)
        ctx = {"mesh": rec.get("mesh") or plan.get("mesh") or "",
               "sender": getattr(pool, "me", "") or session, "workflow": plan.get("workflow"),
               "cwd": str(st.cwd), "scope": st.scope}
        why = deliver(rec, ctx)
        if why:
            failures.append((rec, why))
    for rec, why in failures:
        runtime.note("ask_undelivered", cwd=cwd, scope=scope, ask=rec["id"],
                     node=rec.get("node"), reason=why)
    return routed


def tick(*, cwd: Optional[str] = None, scope: Optional[str] = None,
         now: Optional[float] = None, pool_fn: Optional[PoolFn] = None,
         deliver_fn: Optional[DeliverFn] = None) -> List[str]:
    """Expire asks past their deadline and route the ones that need a group.

    Returns notices for the run's own session: asks that fell to a person or
    to the driver since the last look (each said once)."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return []
    runtime.status(cwd=cwd, scope=scope, now=now, tick=True)  # applies ask timeouts
    route(cwd=cwd, scope=scope, now=now, pool_fn=pool_fn, deliver_fn=deliver_fn)
    return _fell_notices(cwd, scope, now)


def _fell_notices(cwd: Optional[str], scope: Optional[str],
                  now: Optional[float]) -> List[str]:
    out: List[str] = []

    def op(r: runtime.Run) -> None:
        for rec in (r.state.get("asks") or {}).values():
            if rec.get("status") != "open" or rec.get("notified") == rec.get("resolution"):
                continue
            if rec.get("resolution") not in (runtime.RES_HUMAN, runtime.RES_SELF):
                continue
            rec["notified"] = rec["resolution"]
            if not rec.get("from"):
                continue  # never routed: the driver saw it in its own status
            out.append(NUDGE_FELL.format(how=runtime._ask_how(rec["instance"], rec)))

    runtime._mutate(cwd, scope, now, op)
    return out


def needs_routing(cwd: Optional[str] = None, scope: Optional[str] = None,
                  now: Optional[float] = None) -> bool:
    st = RunStore(cwd, scope)
    if not st.exists():
        return False
    try:
        snap = runtime.Run.load(st, now)
    except store_mod.StoreError:
        return False
    return snap.status in runtime.LIVE_STATUSES and bool(runtime.asks_to_route(snap))


# -- answering -----------------------------------------------------------------


def answer_by_id(ask_id: str, decision: str, reason: Optional[str] = None, *,
                 by_session: str, now: Optional[float] = None,
                 pool_fn: Optional[PoolFn] = None, deliver_fn: Optional[DeliverFn] = None,
                 nudge_fn: Optional[NudgeFn] = None) -> Dict[str, Any]:
    """``flow_answer``: answer an ask by id, wherever its run lives."""
    entry = _index_read(ask_id)
    if not entry or by_session not in (entry.get("sessions") or []):
        raise runtime.FlowError(
            f"no open request {ask_id!r} is waiting on you -- it was answered, it moved past "
            f"you, or its run left the task. Call flow_asks for what is actually open")
    cwd, scope = entry["cwd"], entry["scope"]
    receipt = runtime.answer(ask_id, decision, reason, by_session=by_session, cwd=cwd,
                             scope=scope, now=now)
    if receipt.get("decision") == runtime.ASK_ABSTAIN:
        if not receipt.get("moved"):
            return receipt  # the rest of the group still has it
        # the next group is asked now rather than on the clock's next tick
        route(cwd=cwd, scope=scope, now=now, pool_fn=pool_fn, deliver_fn=deliver_fn)
    else:
        _index_drop(ask_id)
    driver = _driver(RunStore(cwd, scope)) or scope
    woken = (nudge_fn or default_nudge)(driver, NUDGE_ANSWERED, cwd)
    receipt["nudged"] = woken or None
    return receipt


def pending_for(session: str) -> List[Dict[str, Any]]:
    """Open asks addressed to ``session`` on this machine (index + state)."""
    out: List[Dict[str, Any]] = []
    session = str(session or "").strip()
    if not session:
        return out
    try:
        files = sorted(index_dir().glob("ask2-*.json"))
    except OSError:
        return out
    for path in files:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if session not in (entry.get("sessions") or []):
            continue
        try:
            st = RunStore(entry["cwd"], entry["scope"])
            state = st.load_state()
        except (store_mod.StoreError, KeyError, OSError):
            _index_drop(path.stem)
            continue
        rec = (state.get("asks") or {}).get(entry.get("ask"))
        if not rec or rec.get("status") != "open" or rec.get("resolution") != runtime.RES_ASKED \
                or not any(e.get("session") == session for e in rec.get("asked") or []):
            if not rec or rec.get("status") != "open":
                _index_drop(path.stem)
            continue
        out.append({
            "ask_id": rec["id"], "kind": rec.get("kind"), "prompt": rec.get("prompt"),
            "options": [o["name"] for o in runtime._ask_choices(rec)] + [runtime.ASK_ABSTAIN],
            "deadline": rec.get("deadline"), "workflow": state.get("workflow"),
            "task": rec.get("label") or rec.get("node"),
            "from_session": runtime.driver_of(state) or entry["scope"],
            "cwd": entry["cwd"],
        })
    return out


def nudge_driver(message: str, *, cwd: Optional[str] = None,
                 scope: Optional[str] = None,
                 nudge_fn: Optional[NudgeFn] = None) -> Any:
    """Type ``message`` into the session driving the run at (cwd, scope):
    a person's command changed the run and the driver should read it now
    (claunch-2xyjw). A held driver is woken for it (``/deliver``). Best
    effort: the change is recorded either way; returns the targets that
    accepted it, or None."""
    try:
        st = RunStore(cwd, scope)
        driver = _driver(st) or _session_of(st.scope)
        if not driver:
            return None
        return (nudge_fn or default_nudge)(driver, message, str(st.cwd)) or None
    except Exception:  # noqa: BLE001
        return None


def human_approve(node: Optional[str] = None, *, decision: Optional[str] = None,
                  decline: bool = False, reason: Optional[str] = None,
                  by: Optional[str] = None, cwd: Optional[str] = None,
                  scope: Optional[str] = None, now: Optional[float] = None,
                  withdraw_fn: Optional[Callable[..., Any]] = None,
                  nudge_fn: Optional[NudgeFn] = None) -> Dict[str, Any]:
    """A person's answer (CLI, web UI): apply it, tell the responders that
    still had the question that it is closed, wake the run's session."""
    payload = runtime.approve(node, decision=decision, decline=decline, reason=reason,
                              caller=runtime.CALLER_USER, by=by, cwd=cwd, scope=scope, now=now)
    answered = payload.get("answered") or {}
    st = RunStore(cwd, scope)
    driver = _driver(st) or _session_of(st.scope)
    if answered.get("pending"):
        sender = driver
        try:
            (withdraw_fn or default_withdraw)(answered, sender=sender)
        except Exception:  # noqa: BLE001 - best effort, the answer is recorded
            pass
    if answered.get("ask"):
        _index_drop(answered["ask"])
    try:
        payload["nudged"] = (nudge_fn or default_nudge)(
            driver or _session_of(st.scope), "[cflow2] a person answered an ask of your run "
            f"({answered.get('decision')}). Call flow_status and continue.", str(st.cwd)) or None
    except Exception:  # noqa: BLE001
        payload["nudged"] = None
    return payload

