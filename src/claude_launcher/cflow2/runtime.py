"""The cflow v2 interpreter (``docs/cflow-v2.md`` section 4).

A run is the chart snapshot (``chart.json``) plus a state document
(``state.json``) holding *instances*: one record per entered node that has not
finished yet. The same IR node can have several live instances at once (a
parallel ``foreach``, ``group`` members), so instances are named
``<node id>@<n>`` and carry their own environment (``item``, ``prev``, the
``params`` of the procedure they run in).

Instances that wait on the outside are the *active configuration*:

=============  ==============================================================
await          completed by
=============  ==============================================================
``task``       :func:`report`
``choose``     :func:`select`
``wait``       :func:`wait_tick` (time or probe)
``delegation`` :func:`delegate_submit` (or a dispatcher, later)
``pause``      :func:`wait_tick` (a loop's ``every`` between iterations)
``limit``      :func:`extend_loop` (a loop at its ``max``; the run is
               ``waiting_approval`` with reason ``loop_limit``; extend it
               or reset its count) or
               :func:`extend_visits` (a node entered more than the chart's
               ``max_visits`` times; reason ``visit_limit``)
``approval``   an ask's answer (:func:`approve`, :func:`answer`)
``verifying``  a background ``verify`` of an ``ok`` report
=============  ==============================================================

What holds what (``docs/cflow-v2.md`` 4.2): a halting monitor holds the WHOLE
run -- no report, select, fork, delegation result, answer or time-based move
applies until a person resumes it (:func:`release_halt`); a result a daemon
executor brings in meanwhile is kept and applied on resume. An ask holds only
its own task instance, a loop or visit limit only that loop or node instance;
the rest of the run moves on.

Everything else is completed by the engine from its children (``advance``).

Semantics this module fixes where the spec leaves room (also listed in the
report that introduced it):

- Outcome propagation: ``ok`` continues a ``seq``; any other outcome stops it
  and becomes the seq's outcome, so it travels up to the nearest ``try`` whose
  ``catch`` names it (``*`` catches anything). ``raise X`` finishes the
  control node with outcome ``X``. ``fail`` is always an acceptable reported
  outcome, declared or not.
- ``loop`` while checks its guard before each iteration, ``until`` after each
  (the body runs at least once). ``max`` counts iterations of one entry of the
  loop; reaching it with the guard still asking for more puts the run in
  ``waiting_approval`` (``loop_limit``). ``retry`` does not stop for approval:
  exhausting ``max`` attempts finishes the retry with the last attempt's
  outcome (``fail`` if the attempt was ``ok`` but ``until`` did not hold). A
  ``foreach`` over more items than ``max`` stops for approval at item max+1
  (parallel: before starting any).
- ``between`` runs only when another iteration follows; ``every`` pauses after
  ``between``. Both apply to every loop mode.
- A ``cond`` with no true guard and no ``else`` finishes with ``fail``.
- ``bind`` stores the node's own value whatever its outcome -- the value
  before ``spec.ensures`` is applied (a failed ensures changes the outcome to
  ``fail`` and the value the parent sees to ``{reason, original}``; the bound
  variable keeps the node's own return shape).
- ``on`` handlers run as a child of the node; the handler's outcome replaces
  the node's. ``spec.ensures`` is checked on the node's own outcome first.
- par ``all`` fails fast with the failing branch's outcome/value; ``any`` with
  no ok branch and ``quorum:K`` that cannot be reached finish ``fail``.
  Branches still running when a par decides are cancelled (journaled).
- ``group``: members may be forked only while ``do`` runs; the group settles
  (by its ``settle`` mode over the members) once ``do`` finished ok and every
  member finished. A non-ok ``do`` cancels the members.
- Control signals (``continue``/``break``/``return``/``end``) leave every
  instance between the control and its target; a ``try`` with ``finally`` on
  the way runs the finally first and then resumes the signal.
- The run is ``done`` when the root finishes ``ok`` or with an outcome the
  chart's ``returns`` declares, ``failed`` otherwise.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import executors, ir
from . import store as store_mod
from .store import RunStore, StoreError

try:  # sibling modules; the runtime degrades without them
    from . import caps as caps_mod
except ImportError:  # pragma: no cover
    caps_mod = None  # type: ignore
from . import expr as expr_mod
from . import collections as col_mod
from . import question as q_mod

ST_RUNNING = "running"
ST_WAITING = "waiting_approval"
ST_DONE = "done"
ST_ABORTED = "aborted"
ST_FAILED = "failed"
LIVE_STATUSES = (ST_RUNNING, ST_WAITING)
FINISHED_STATUSES = (ST_DONE, ST_ABORTED, ST_FAILED)

AW_TASK = "task"
AW_CHOOSE = "choose"
AW_WAIT = "wait"
AW_DELEGATION = "delegation"
AW_PAUSE = "pause"
AW_LIMIT = "limit"
#: A task whose ``ask`` has not been answered yet: entering it needs an
#: approval (or, for an ask with ``options``, the decision is its result).
AW_APPROVAL = "approval"
#: A task whose ``ok`` report waits on its ``verify`` running in the background.
AW_VERIFY = "verifying"
#: A task that called a procedure (``flow_call``, doc 2.12): it is active
#: again, with the call's result, when the callee's nested run finishes.
AW_CALL = "calling"

#: Hold kinds (``state["approvals"]``). ``monitor_violation`` holds the whole
#: run; the others hold one instance.
HOLD_HALT = "monitor_violation"
HOLD_LOOP = "loop_limit"
HOLD_VISIT = "visit_limit"
#: An ``ok`` finish whose acceptance probes did not all pass.
HOLD_ACCEPTANCE = "acceptance"
#: How often a failed acceptance check is run again (flow_status, the clock).
ACCEPTANCE_RECHECK = 30.0
#: A verify that has not come back this long after its own timeout is
#: treated as lost (the process running it went away).
VERIFY_LOST_GRACE = 120.0

#: v1's default for a verify command (``cflow.model.DEFAULT_VERIFY_TIMEOUT``).
DEFAULT_VERIFY_TIMEOUT = 900.0
#: How much of a failed verify's output a refusal carries.
VERIFY_TAIL_LINES = 40
VERIFY_TAIL_CHARS = 4000

ASK_APPROVE = "approve"
ASK_DECLINE = "decline"
ASK_ABSTAIN = "abstain"
ASK_DECLINED = "declined"
ASK_OTHERWISE_HUMAN = "human"
ASK_OTHERWISE_SELF = "self"
#: Where an open ask's resolution stands.
RES_ROUTING = "routing"   # a candidate group has to be resolved (asks.route)
RES_ASKED = "asked"       # with responder sessions, until ``deadline``
RES_HUMAN = "human"       # every group ran out; a person answers
RES_SELF = "self"         # every group ran out; the driver may decide
#: v1's patience with an unreadable roster (``cflow.engine.ROSTER_RETRY`` and
#: ``MAX_ROSTER_DEFERRALS``): a silent daemon defers routing instead of
#: spending the candidate groups.
ROSTER_RETRY = 30.0
MAX_ROSTER_DEFERRALS = 4

CALLER_AGENT = "agent"
CALLER_USER = "user"

#: State key of the driver's move request (:func:`request_move`): one per run,
#: pending until a person answers; a refusal stays until the driver files
#: another request or the run moves.
MOVE_REQUEST = "move_request"

#: State key of the choices a person took for the driver (``select`` with
#: ``caller: user``), newest last, with the note they typed. The nudge that
#: tells the driver about a pick can be missed or compacted away; this is
#: where flow_status keeps it (claunch-e437w).
PERSON_PICKS = "person_picks"
PERSON_PICKS_KEPT = 5
#: How much of a person's note a nudge carries; the whole note is in
#: ``person_picks``.
NUDGE_NOTE_LIMIT = 1500
#: The two things a driver may ask a person for (``request_move``): a move
#: to a ``force_target`` node, or a report of one of its active tasks.
MOVE_KIND_MOVE = "move"
MOVE_KIND_REPORT = "report"

#: Probe poll interval when a wait declares none.
DEFAULT_POLL = 60.0

ProbeRunner = Callable[[Any, str], Any]


class FlowError(Exception):
    """A refused operation; the message is written for the agent/user."""


# ---------------------------------------------------------------------------
# the run object


class Run:
    """One loaded run: chart + state + pending journal events."""

    def __init__(self, st: RunStore, chart: ir.Chart, state: Dict[str, Any],
                 now: Optional[float] = None) -> None:
        self.store = st
        self.chart = chart
        self.state = state
        self.now = time.time() if now is None else float(now)
        self.events: List[Dict[str, Any]] = []
        self.deferred_error: Optional[str] = None
        self._caps: Optional[Dict[str, Any]] = None
        #: Questions closed without an answer (timeout hop, cancellation,
        #: abstention): their responders are told after the lock is released.
        self.withdrawals: List[Dict[str, Any]] = []
        #: Work that must happen after this run's lock is released (verify
        #: jobs, a nested run's result for its parent).
        self.after: List[Callable[[], Any]] = []
        #: Nested delegation runs to abort with this run's save.
        self.nested_aborts: List[Dict[str, Any]] = []

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, st: RunStore, now: Optional[float] = None) -> "Run":
        return cls(st, st.load_chart(), st.load_state(), now)

    def save(self) -> None:
        if any(ev.get("event") in PEER_VISIBLE for ev in self.events) and _has_peers(self):
            cwd, scope, now = str(self.store.cwd), self.store.scope, self.now
            self.after.append(lambda: poke_peers(cwd, scope, now))
        _sync_waiting(self)
        self.state["active"] = sorted(
            iid for iid, inst in self.state["inst"].items() if inst.get("await")
        )
        # "updated" moves only with a change: a pass of the clock that changes
        # nothing writes no new checkpoint revision.
        self.store.commit(self.state, self.events, touch="updated")
        self.events = []
        for link in self.nested_aborts:
            _abort_nested(link)
        self.nested_aborts = []
        if self.state.get("parent") and self.state.get("status") in FINISHED_STATUSES \
                and not self.state.get("delivered"):
            self.after.append(lambda: deliver_nested(str(self.store.cwd), self.store.scope))
        if self.withdrawals:
            pending, self.withdrawals = self.withdrawals, []
            sender = driver_of(self.state)
            self.after.append(lambda: _send_withdrawals(pending, sender))

    def run_after(self) -> None:
        """Run the work queued for after the lock (best effort)."""
        todo, self.after = self.after, []
        for fn in todo:
            try:
                fn()
            except Exception:  # noqa: BLE001 - recorded state is already safe
                pass

    def emit(self, event: str, **fields: Any) -> None:
        rec = {"ts": store_mod.utcnow(), "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        self.events.append(rec)

    # -- accessors -------------------------------------------------------
    @property
    def inst(self) -> Dict[str, Dict[str, Any]]:
        return self.state["inst"]

    @property
    def status(self) -> str:
        return self.state["status"]

    @property
    def caps(self) -> Dict[str, Any]:
        if self._caps is None:
            self._caps = _caps_from_json(self.state.get("caps"))
        return self._caps

    def node_of(self, iid: str) -> ir.Node:
        return self.chart.node(self.inst[iid]["node"])

    def alive(self, iid: Optional[str]) -> bool:
        return bool(iid) and iid in self.inst

    def children_of(self, iid: str) -> List[str]:
        return [k for k, v in self.inst.items() if v.get("parent") == iid]

    def new_id(self, prefix: str) -> str:
        self.state["seq"] = int(self.state.get("seq") or 0) + 1
        return f"{prefix}@{self.state['seq']}"

    def env(self, iid: Optional[str]) -> Dict[str, Any]:
        """The expression environment seen by an instance."""
        local = self.inst[iid]["env"] if iid and iid in self.inst else {}
        out: Dict[str, Any] = {}
        out.update(local.get("vars") or {})
        out.update({
            "params": local["params"] if "params" in local else self.state.get("params", {}),
            "data": self.state.get("data", {}),
            "prev": local.get("prev"),
            "item": local.get("item"),
            "result": _scoped_results(self, iid),
            "visits": self.state.get("visits", {}),
            "caps": {k: _cap_on(v) for k, v in self.caps.items()},
            "probe": self.state.get("probe", {}),
        })
        if _col_specs(self):
            # ``${col.<name>}`` (prompts)
            out["col"] = {name: col_mod.items(cs)
                          for name, cs in (self.state.get("collections") or {}).items()}
        # items()/count()/fresh()/exists() and side() (doc 2.7, 2.9)
        out[expr_mod.COLLECTIONS_KEY] = _ColResolver(self)
        qtypes = (self.chart.meta or {}).get("question_types")
        if qtypes:
            out[expr_mod.QUESTIONS_KEY] = qtypes  # guide(name), docs 2.10
        return out


def _sync_waiting(r: "Run") -> None:
    """``status``/``reason`` from the open approvals: the run is
    ``waiting_approval`` while something only a person can release holds it
    (a loop or visit limit, a halting monitor, an ask that fell to
    ``human``). Every path that adds or removes a hold -- including every
    cancellation -- goes through here on save, so status and reason never
    outlive the hold that set them. A halt names the reason whenever present."""
    if r.state.get("status") not in LIVE_STATUSES:
        return
    holds = [a for a in r.state.get("approvals") or []
             if a.get("kind") != "ask" or a.get("resolution") == RES_HUMAN]
    if holds:
        kinds = [a.get("kind") for a in holds]
        r.state["status"] = ST_WAITING
        if HOLD_HALT in kinds:
            r.state["reason"] = HOLD_HALT
        elif r.state.get("reason") not in kinds:
            r.state["reason"] = kinds[0]
    else:
        r.state["status"] = ST_RUNNING
        r.state["reason"] = None


def _halted(r: "Run") -> List[str]:
    """Names of the halting monitors that hold the run (empty: not halted)."""
    return [str(a.get("monitor")) for a in r.state.get("approvals") or []
            if a.get("kind") == HOLD_HALT]


def driver_of(state: Dict[str, Any]) -> str:
    """The session that drives a run (recorded at start; older runs: the
    scope when it names a session)."""
    if "driver" in state:
        return str(state.get("driver") or "")
    scope = str(state.get("scope") or "")
    return "" if scope in ("", store_mod.DEFAULT_SCOPE) else store_mod.session_of(scope)


def _cap_on(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("enabled"))
    if hasattr(value, "enabled"):
        return bool(value.enabled)
    return bool(value)


def _caps_to_json(caps: Any) -> Optional[Dict[str, Any]]:
    if caps is None:
        return None
    out: Dict[str, Any] = {}
    for key, value in dict(caps).items():
        if hasattr(value, "to_dict"):
            out[key] = value.to_dict()
        elif isinstance(value, dict):
            out[key] = dict(value)
        else:
            out[key] = {"enabled": bool(value), "args": [], "source": "given"}
    return out


def _caps_from_json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if caps_mod is not None and isinstance(value, dict):
            out[key] = caps_mod.Capability(
                enabled=bool(value.get("enabled")), args=list(value.get("args") or []),
                source=str(value.get("source") or caps_mod.SOURCE_DEFAULT),
            )
        else:
            out[key] = _cap_on(value)
    return out


def node_key(node: ir.Node) -> str:
    """The name a node's result and visits are kept under."""
    return node.label or node.id


def _result_scopes(r: "Run", iid: Optional[str]) -> List[str]:
    """``iid`` and its ancestors, innermost first, that run beside siblings of
    the same chart: a group member, a parallel foreach item, a par branch.

    Their nodes share labels (:func:`node_key`), so the run-wide ``result``
    table holds whichever sibling reported last; each of these instances also
    keeps the results of the nodes finished under it (claunch-vmv24)."""
    out: List[str] = []
    cur = iid
    while cur and cur in r.inst:
        role = str(r.inst[cur].get("role") or "")
        if role == "member" or role.startswith(("item:", "branch:")):
            out.append(cur)
        cur = r.inst[cur].get("parent")
    return out


def _scoped_results(r: "Run", iid: Optional[str]) -> Dict[str, Any]:
    """``$result`` as ``iid`` sees it: the run-wide table, with each enclosing
    sibling instance's own results over it (the innermost last)."""
    table = r.state.get("result", {})
    scopes = _result_scopes(r, iid)
    if not scopes:
        return table
    out = dict(table)
    for sid in reversed(scopes):
        out.update(r.inst[sid].get("results") or {})
    return out


def shown_label(chart: ir.Chart, node: ir.Node) -> Optional[str]:
    """The label a reader is shown for ``node`` (status, briefs, asks).

    A node inlined from a ``call`` keeps its own name as its label -- the
    procedure reads its results under it (:func:`node_key`) -- so a chart
    that calls one procedure from several sites has several nodes under one
    label, and every one of them read the same: improv-worker-v2's ask-user
    sites all showed ``ask-pose`` (claunch-tm5ki). Such a label is shown
    with the author ids on its path in front
    (``round/resolve/issue-decision/ask-pose``): each segment of the node's
    id stands for the authored node it was generated under
    (``issue-decision.call`` for ``issue-decision``), and a segment with no
    authored node (``body.0``) adds nothing. A label no other node shares is
    shown as it is. :func:`_find` accepts a shown label as a reference.
    """
    if not node.label:
        return None
    if sum(1 for n in chart.nodes.values() if n.label == node.label) < 2:
        return node.label
    segs = node.id.split("/")[:-1]
    names = [_authored(chart, "/".join(segs[:i]), seg) for i, seg in enumerate(segs)]
    return "/".join([n for n in names if n] + [node.label])


def _authored(chart: ir.Chart, prefix: str, seg: str) -> Optional[str]:
    """The author id an id segment stands for: the segment's own node when
    it has one, else the node its generated suffixes were added to
    (``issue-decision.call`` -> ``issue-decision``); None for a path with no
    authored node (``body.0``)."""
    head = seg
    while True:
        cand = chart.nodes.get(f"{prefix}/{head}" if prefix else head)
        if cand is not None and cand.label:
            return cand.label
        if "." not in head:
            return None
        head = head.rsplit(".", 1)[0]


# ---------------------------------------------------------------------------
# expressions, values, rendering


def _eval(r: Run, iid: Optional[str], text: Any) -> Any:
    return expr_mod.evaluate(text, r.env(iid))


def _guard(r: Run, iid: Optional[str], text: Optional[str]) -> bool:
    if text is None:
        return True
    return expr_mod.truthy(_eval(r, iid, text))


def _values(r: Run, iid: Optional[str], spec: Any) -> Dict[str, Any]:
    """Evaluate a ``with_``/return ``value`` map (``{k: {expr}|{lit}}``)."""
    out: Dict[str, Any] = {}
    for key, item in (spec or {}).items():
        if isinstance(item, dict) and "expr" in item:
            out[key] = _eval(r, iid, item["expr"])
        elif isinstance(item, dict) and "lit" in item:
            out[key] = item["lit"]
        else:
            out[key] = item
    return out


_REF = re.compile(r"\$\{([^}]*)\}")
_CALL_START = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(")


def render(text: Optional[str], env: Dict[str, Any]) -> str:
    """Substitute ``${...}`` value references. ``${frag:...}`` (already
    expanded by the compiler) and references that evaluate to null or fail
    are left as written, so the reader sees what is unresolved."""
    if not text:
        return text or ""

    def repl(m: "re.Match[str]") -> str:
        inner = m.group(1).strip()
        if not inner or inner.startswith("frag:"):
            return m.group(0)
        # ``${guide($params.format)}``: a function call keeps its spelling
        src = inner if inner.startswith("$") or _CALL_START.match(inner) else "$" + inner
        try:
            value = expr_mod.evaluate(src, env)
            if value is None:
                return m.group(0)
            if isinstance(value, str):
                return value
            return json.dumps(value, ensure_ascii=False)
        except (expr_mod.ExprError, TypeError, ValueError, RecursionError, OverflowError):
            return m.group(0)

    return _REF.sub(repl, text)


# -- return schemas ----------------------------------------------------------

_TYPES = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "any": lambda v: True,
}


def fill_optional(returns: Dict[str, Any], outcome: str, value: Any) -> Any:
    """A ``return``'s value with the declared optional fields (``str?``…) it
    left out set to null (docs 2.2, returns): a record need not spell out
    ``field: null`` for each one. Other values pass through."""
    fields = (returns or {}).get(outcome)
    if not isinstance(fields, dict) or not (value is None or isinstance(value, dict)):
        return value
    missing = [f for f, t in fields.items()
               if isinstance(t, str) and t.endswith("?") and f not in (value or {})]
    if not missing:
        return value
    out = dict(value or {})
    for f in missing:
        out[f] = None
    return out


def returns_of(node: ir.Node) -> Dict[str, Dict[str, str]]:
    """The schema a node's result is validated against (``{ok: {}}`` default)."""
    schema = node.get("returns")
    if isinstance(schema, dict) and schema:
        return schema
    return {ir.OUTCOME_OK: {}}


def validate_result(schema: Dict[str, Any], outcome: str, value: Any, *,
                    qtypes: Optional[Dict[str, Any]] = None,
                    params: Optional[Dict[str, Any]] = None) -> List[str]:
    """Problems with ``(outcome, value)`` against ``schema``; empty when fine.

    ``fail`` is always accepted (the engine's own failure outcome); a declared
    ``fail`` still has its fields checked. Fields are required unless the type
    ends in ``?``; extra fields are allowed. A ``question:<type>`` field
    (docs 2.10) is checked against that question type from ``qtypes``;
    ``question:$params.<p>`` names the type through ``params``. Without
    ``qtypes`` such a field only has to be an object.
    """
    if not isinstance(outcome, str) or not outcome:
        return ["outcome must be a non-empty string"]
    if outcome not in schema:
        if outcome == ir.OUTCOME_FAIL:
            return []
        return [f"unknown outcome {outcome!r} (declared: {', '.join(schema) or 'ok'}, or fail)"]
    fields = schema.get(outcome) or {}
    if not fields:
        return []
    if value is None:
        value = {}
    if not isinstance(value, dict):
        return [f"value for outcome {outcome!r} must be an object with "
                f"{', '.join(fields)}; got {type(value).__name__}"]
    errors: List[str] = []
    for name, ftype in fields.items():
        ftype = str(ftype)
        optional = ftype.endswith("?")
        base = ftype[:-1] if optional else ftype
        if name not in value or value[name] is None:
            if not optional:
                errors.append(f"missing field {name!r} ({ftype})")
            continue
        qname = q_mod.return_type(ftype)
        if qname is not None:
            errors.extend(_question_field(name, qname, value[name], qtypes, params))
            continue
        check = _TYPES.get(base)
        if check is not None and not check(value[name]):
            errors.append(
                f"field {name!r} must be {base}, got {type(value[name]).__name__}"
            )
    return errors


def question_type(qname: str, qtypes: Optional[Dict[str, Any]],
                  params: Optional[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(declaration, name)`` for a question type named literally or as
    ``$params.<p>``; the declaration is None when it cannot be found."""
    if qname.startswith("$params."):
        qname = str((params or {}).get(qname[len("$params."):]) or "")
    return (qtypes or {}).get(qname), qname


def _question_field(name: str, qname: str, v: Any, qtypes: Optional[Dict[str, Any]],
                    params: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(v, dict):
        return [f"field {name!r} must be an object (question {qname}), got {type(v).__name__}"]
    if qtypes is None:
        return []
    decl, resolved = question_type(qname, qtypes, params)
    if decl is None:
        return [f"field {name!r}: no question type {resolved or qname!r}"]
    _norm, errs = q_mod.validate(v, decl)
    return [f"field {name!r} ({resolved}): {e}" for e in errs]


# ---------------------------------------------------------------------------
# entering nodes


def _splice_env(r: Run, parent: Optional[str], splice: Dict[str, Any],
                base: Dict[str, Any]) -> Dict[str, Any]:
    """A spliced block sees the environment of the call it was written at
    (``$params``, a foreach item, waterfall ``$prev``), not the procedure's:
    the env the call's scope instance saved before binding its params. A
    block written in the workflow body sees the run's params."""
    call = splice.get("call")
    cur = parent
    while cur is not None and cur in r.inst:
        if r.inst[cur]["node"] == call:
            saved = r.inst[cur]["frame"].get("caller_env")
            return dict(saved) if saved is not None else base
        cur = r.inst[cur].get("parent")
    return base


def _enter(r: Run, node_id: str, parent: Optional[str], role: str,
           env: Optional[Dict[str, Any]] = None) -> str:
    node = r.chart.node(node_id)
    iid = _new_instance(r, node, parent, role, env)
    key = node_key(node)
    limit = _max_visits(r, key)
    if limit is not None and r.state["visits"][key] > limit:
        _visit_limit(r, iid, node)
        return iid
    _enter_body(r, iid, node)
    return iid


def _new_instance(r: Run, node: ir.Node, parent: Optional[str], role: str,
                  env: Optional[Dict[str, Any]] = None) -> str:
    """The instance record of an entered node, its visit counted and its
    ``entered`` journaled -- everything of :func:`_enter` but the body."""
    node_id = node.id
    iid = r.new_id(node_id)
    base = dict(r.inst[parent]["env"]) if parent and parent in r.inst else {}
    if (node.get("splice") or {}).get("call"):
        # the procedure's own bindings (its foreach item, waterfall prev) are
        # not the block's: only the call site's environment
        base = _splice_env(r, parent, node.get("splice") or {}, base)
    elif env:
        base.update(env)
    r.inst[iid] = {
        "id": iid, "node": node_id, "parent": parent, "role": role,
        "env": base, "frame": {}, "await": None,
        "entered": store_mod.utcnow(), "entered_at": r.now,
    }
    key = node_key(node)
    visits = r.state.setdefault("visits", {})
    visits[key] = int(visits.get(key) or 0) + 1
    r.emit("entered", node=node_id, instance=iid, type=node.type, label=node.label or None)
    return iid


def _enter_body(r: Run, iid: str, node: ir.Node) -> None:
    # doc 4.5: do (at enter) -> triggers (at enter) -> the body
    err = _apply_effects(r, iid, node, AT_ENTER)
    if err:
        _complete(r, iid, ir.OUTCOME_FAIL, {"reason": err})
        return
    _queue_triggers(r, iid, node, AT_ENTER)
    if node.type == ir.T_SCOPE and not _scope_params(r, iid, node):
        return
    if node.executor and node.executor != [ir.EXEC_INLINE] and _delegate(r, iid):
        return
    _ENTER[node.type](r, iid, node)


# -- the run-wide visit limit (chart meta ``max_visits``) ----------------------


def _max_visits(r: Run, key: str) -> Optional[int]:
    """The visits ``key`` may have; ``None`` when the chart sets
    ``max_visits: 0`` (no limit, doc 2.1)."""
    base = (r.chart.meta or {}).get("max_visits")
    try:
        base = ir.DEFAULT_MAX_VISITS if base is None else int(base)
    except (TypeError, ValueError):
        base = ir.DEFAULT_MAX_VISITS
    if base == 0:
        return None
    return base + int((r.state.get("visit_extra") or {}).get(key) or 0)


def _visit_limit(r: Run, iid: str, node: ir.Node) -> None:
    """Entering ``node`` once more would pass the run's ``max_visits``: the
    instance is parked before its body starts and the run holds for a person
    (``claunch cflow2 extend --visits``). Only this instance waits."""
    key = node_key(node)
    inst = r.inst[iid]
    inst["await"] = AW_LIMIT
    inst["frame"]["visit_limit"] = True
    visits = int(r.state["visits"].get(key) or 0)
    approval = {"kind": HOLD_VISIT, "instance": iid, "node": node.id,
                "label": shown_label(r.chart, node), "visits": visits, "max": _max_visits(r, key)}
    r.state.setdefault("approvals", []).append(approval)
    r.emit("waiting", node=node.id, instance=iid, kind=HOLD_VISIT, visits=visits,
           max=approval["max"])


def _enter_child(r: Run, parent: str, node_id: Optional[str], role: str,
                 env: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if not node_id:
        return None
    return _enter(r, node_id, parent, role, env)


def _en_task(r: Run, iid: str, node: ir.Node) -> None:
    if node.get("ask"):
        _open_ask(r, iid, node)
        return
    r.inst[iid]["await"] = AW_TASK


def _en_wait(r: Run, iid: str, node: ir.Node) -> None:
    inst = r.inst[iid]
    inst["await"] = AW_WAIT
    if node.get("seconds") is not None:
        inst["frame"]["due"] = r.now + float(node.get("seconds"))
    inst["frame"]["last_poll"] = None
    r.emit("waiting", node=node.id, instance=iid, kind="wait",
           describe=node.get("describe"), probe=node.get("probe"),
           seconds=node.get("seconds"))


def _en_seq(r: Run, iid: str, node: ir.Node) -> None:
    children = node.get("children") or []
    r.inst[iid]["frame"]["idx"] = 0
    if not children:
        _complete(r, iid, ir.OUTCOME_OK, None)
        return
    _enter_child(r, iid, children[0], "seq")


def _en_cond(r: Run, iid: str, node: ir.Node) -> None:
    for i, br in enumerate(node.get("branches") or []):
        try:
            hit = _guard(r, iid, br.get("guard"))
        except expr_mod.ExprError as exc:
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"cond guard error: {exc}"})
            return
        if hit:
            r.inst[iid]["frame"]["branch"] = i
            _enter_child(r, iid, br.get("child"), "branch")
            return
    _complete(r, iid, ir.OUTCOME_FAIL, {"reason": "no cond branch matched and no else"})


def _en_choose(r: Run, iid: str, node: ir.Node) -> None:
    r.inst[iid]["await"] = AW_CHOOSE
    r.emit("waiting", node=node.id, instance=iid, kind="choose",
           chooser=node.get("chooser") or ir.CHOOSER_AGENT)


def _en_par(r: Run, iid: str, node: ir.Node) -> None:
    frame = r.inst[iid]["frame"]
    frame["results"] = {}
    frame["order"] = []
    for name, child in (node.get("branches") or {}).items():
        if not r.alive(iid) or frame.get("decided"):
            return
        _enter_child(r, iid, child, f"branch:{name}")


def _en_timeout(r: Run, iid: str, node: ir.Node) -> None:
    frame = r.inst[iid]["frame"]
    frame["deadline"] = r.now + float(node.get("seconds") or 0)
    frame["phase"] = "body"
    _enter_child(r, iid, node.get("child"), "body")


def _en_try(r: Run, iid: str, node: ir.Node) -> None:
    r.inst[iid]["frame"]["phase"] = "body"
    _enter_child(r, iid, node.get("child"), "body")


def _en_group(r: Run, iid: str, node: ir.Node) -> None:
    frame = r.inst[iid]["frame"]
    frame.update({"phase": "do", "members": [], "results": {}, "order": [], "do": None})
    _enter_child(r, iid, node.get("child"), "do")


def _scope_params(r: Run, iid: str, node: ir.Node) -> bool:
    """Bind a scope's ``params`` (its ``with_`` evaluated in the caller's
    environment; a group member also gets the fork's ``with``)."""
    inst = r.inst[iid]
    # the call site's environment: a spliced block runs in it (docs 2.2)
    inst["frame"]["caller_env"] = dict(inst["env"])
    params: Dict[str, Any] = {}
    if inst["role"] == "member":
        params.update(inst["env"].get("params") or {})
    try:
        params.update(_values(r, iid, node.get("with_")))
    except expr_mod.ExprError as exc:
        _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"call arguments: {exc}"})
        return False
    for name, decl in (node.get("params") or {}).items():
        if params.get(name) is not None:
            continue  # null is no value: the default applies
        decl = decl or {}
        # A required param without a value never gets here: a call site is
        # checked by the compiler, a group member by :func:`fork`.
        params[name] = json.loads(json.dumps(decl["default"])) if "default" in decl else None
    inst["env"]["params"] = params
    return True


def _en_scope(r: Run, iid: str, node: ir.Node) -> None:
    _enter_child(r, iid, node.get("child"), "body")


def _en_control(r: Run, iid: str, node: ir.Node) -> None:
    op = node.get("op")
    if op == ir.CTL_RAISE:
        _complete(r, iid, str(node.get("outcome")), None)
        return
    sig: Dict[str, Any] = {"op": op, "target": node.get("target"), "from": node.id}
    if op == ir.CTL_RETURN:
        try:
            sig["value"] = _values(r, iid, node.get("value"))
        except expr_mod.ExprError as exc:
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"return value: {exc}"})
            return
        sig["outcome"] = node.get("outcome") or ir.OUTCOME_OK
    _propagate(r, iid, sig)


# -- loops -----------------------------------------------------------------


def _loop_cap(r: Run, iid: str) -> int:
    """The limit a person sees: the loop's ``max`` plus what was extended
    since the count was last reset."""
    node = r.node_of(iid)
    return int(node.get("max") or 1) + int(r.inst[iid]["frame"].get("extra") or 0)


def _loop_count(frame: Dict[str, Any]) -> int:
    """Iterations run since the count was last reset (``extend_loop(reset=True)``).
    ``iter`` itself keeps counting: it indexes a foreach's items and is a
    retry's ``attempt``."""
    return int(frame.get("iter") or 0) - int(frame.get("base") or 0)


def _loop_max(r: Run, iid: str) -> int:
    """The bound ``iter`` is compared against."""
    return _loop_cap(r, iid) + int(r.inst[iid]["frame"].get("base") or 0)


def _en_loop(r: Run, iid: str, node: ir.Node) -> None:
    frame = r.inst[iid]["frame"]
    frame.update({"iter": 0, "base": 0, "values": [], "extra": 0, "phase": "body",
                  "last": None})
    mode = node.get("mode")
    if mode == ir.LOOP_FOREACH:
        try:
            items = _eval(r, iid, node.get("items"))
        except expr_mod.ExprError as exc:
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"foreach.in: {exc}"})
            return
        if items is None:
            items = []
        if isinstance(items, dict):
            items = list(items)
        if not isinstance(items, list):
            _complete(r, iid, ir.OUTCOME_FAIL,
                      {"reason": f"foreach.in must be a list, got {type(items).__name__}"})
            return
        frame["items"] = items
        if not items:
            _complete(r, iid, ir.OUTCOME_OK, [])
            return
        if node.get("parallel"):
            frame["results"] = {}
            if len(items) > _loop_max(r, iid):
                frame["phase"] = "limit_par"
                _loop_limit(r, iid)
                return
            _foreach_par_start(r, iid)
            return
        _loop_iterate(r, iid)
        return
    if mode == ir.LOOP_WHILE:
        try:
            go = _guard(r, iid, node.get("guard"))
        except expr_mod.ExprError as exc:
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"loop guard: {exc}"})
            return
        if not go:
            _complete(r, iid, ir.OUTCOME_OK, None)
            return
    _loop_iterate(r, iid)


def _item_env(node: ir.Node, item: Any) -> Dict[str, Any]:
    env: Dict[str, Any] = {"item": item}
    as_name = node.get("as_name")
    if as_name and as_name != "item":
        env["vars"] = {as_name: item}
    return env


def _loop_iterate(r: Run, iid: str) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if frame["iter"] >= _loop_max(r, iid):
        _loop_limit(r, iid)
        return
    frame["iter"] += 1
    frame["phase"] = "body"
    env: Dict[str, Any] = {}
    if node.get("mode") == ir.LOOP_FOREACH:
        env = _item_env(node, frame["items"][frame["iter"] - 1])
    if node.get("mode") == ir.LOOP_RETRY:
        env["vars"] = dict(env.get("vars") or {})
        env["vars"]["attempt"] = frame["iter"]
        env["prev"] = frame.get("last")
    _enter_child(r, iid, node.get("child"), "body", env)


def _foreach_par_start(r: Run, iid: str) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    frame["phase"] = "body"
    frame["iter"] = len(frame["items"])
    for i, item in enumerate(list(frame["items"])):
        if not r.alive(iid) or frame.get("decided"):
            return
        _enter_child(r, iid, node.get("child"), f"item:{i}", _item_env(node, item))


def _loop_limit(r: Run, iid: str) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if frame.get("phase") != "limit_par":
        frame["phase"] = "limit"
    r.inst[iid]["await"] = AW_LIMIT
    approval = {
        "kind": "loop_limit", "instance": iid, "node": node.id,
        "label": shown_label(r.chart, node), "iterations": _loop_count(frame),
        "max": _loop_cap(r, iid), "mode": node.get("mode"),
    }
    r.state.setdefault("approvals", []).append(approval)
    r.state["status"] = ST_WAITING
    r.state["reason"] = "loop_limit"
    r.emit("waiting", node=node.id, instance=iid, kind="loop_limit",
           iterations=frame.get("iter"), max=approval["max"])


def _loop_body_done(r: Run, iid: str, outcome: str, value: Any) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    mode = node.get("mode")
    frame["last"] = value
    if mode == ir.LOOP_RETRY:
        ok = outcome == ir.OUTCOME_OK
        if ok and node.get("guard") is not None:
            try:
                ok = _guard(r, iid, node.get("guard"))
            except expr_mod.ExprError as exc:
                _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"retry.until: {exc}"})
                return
        if ok:
            _complete(r, iid, ir.OUTCOME_OK, value)
            return
        if frame["iter"] >= _loop_max(r, iid):
            if outcome == ir.OUTCOME_OK or outcome == "continue":
                _complete(r, iid, ir.OUTCOME_FAIL,
                          {"reason": "retry attempts exhausted", "last": value})
            else:
                _complete(r, iid, outcome, value)
            return
        _loop_next(r, iid)
        return
    if outcome != ir.OUTCOME_OK:
        _complete(r, iid, outcome, value)
        return
    frame["values"].append(value)
    if mode == ir.LOOP_FOREACH:
        if frame["iter"] < len(frame["items"]):
            _loop_next(r, iid)
        else:
            _complete(r, iid, ir.OUTCOME_OK, frame["values"])
        return
    try:
        go = _guard(r, iid, node.get("guard"))
    except expr_mod.ExprError as exc:
        _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"loop guard: {exc}"})
        return
    if mode == ir.LOOP_UNTIL:
        go = not go
    if not go:
        _complete(r, iid, ir.OUTCOME_OK, value)
        return
    _loop_next(r, iid)


def _loop_next(r: Run, iid: str) -> None:
    """Between iterations: the limit, then ``between``, then ``every``."""
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if node.get("mode") != ir.LOOP_RETRY and frame["iter"] >= _loop_max(r, iid):
        _loop_limit(r, iid)
        return
    _loop_between(r, iid)


def _loop_between(r: Run, iid: str) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if node.get("between"):
        frame["phase"] = "between"
        _enter_child(r, iid, node.get("between"), "between", {"prev": frame.get("last")})
        return
    _loop_pause(r, iid)


def _loop_pause(r: Run, iid: str) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    every = node.get("every")
    if every:
        frame["phase"] = "pause"
        frame["resume_at"] = r.now + float(every)
        r.inst[iid]["await"] = AW_PAUSE
        r.emit("waiting", node=node.id, instance=iid, kind="pause", seconds=every)
        return
    _loop_iterate(r, iid)


def _loop_child_done(r: Run, iid: str, role: str, outcome: str, value: Any) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if role == "between":
        if outcome != ir.OUTCOME_OK:
            _complete(r, iid, outcome, value)
            return
        _loop_pause(r, iid)
        return
    if role.startswith("item:"):  # parallel foreach
        idx = int(role.split(":", 1)[1])
        frame["results"][str(idx)] = {"outcome": outcome, "value": value}
        if outcome != ir.OUTCOME_OK:
            frame["decided"] = True
            _cancel_children(r, iid, "foreach item failed")
            _complete(r, iid, outcome, value)
            return
        if len(frame["results"]) == len(frame["items"]):
            frame["decided"] = True
            values = [frame["results"][str(i)]["value"] for i in range(len(frame["items"]))]
            _complete(r, iid, ir.OUTCOME_OK, values)
        return
    _loop_body_done(r, iid, outcome, value)


# -- par / group settle ------------------------------------------------------


def _settle(mode: str, names: List[str], results: Dict[str, Dict[str, Any]],
            latest: Optional[str]) -> Optional[Tuple[str, Any]]:
    """Whether a parallel region is decided; ``(outcome, value)`` if so."""
    base, _, k = (mode or ir.SETTLE_ALL).partition(":")
    done = [n for n in names if n in results]
    oks = [n for n in done if results[n]["outcome"] == ir.OUTCOME_OK]
    every = len(done) == len(names)
    all_res = {n: results[n] for n in done}
    if base == ir.SETTLE_ALL:
        if latest is not None and results[latest]["outcome"] != ir.OUTCOME_OK:
            return results[latest]["outcome"], results[latest]["value"]
        if every:
            return ir.OUTCOME_OK, {n: results[n]["value"] for n in names}
        return None
    if base == ir.SETTLE_ALL_SETTLED:
        return (ir.OUTCOME_OK, all_res) if every else None
    if base == ir.SETTLE_RACE:
        if latest is not None:
            return results[latest]["outcome"], results[latest]["value"]
        return (ir.OUTCOME_OK, None) if every else None
    if base == ir.SETTLE_ANY:
        if latest is not None and results[latest]["outcome"] == ir.OUTCOME_OK:
            return ir.OUTCOME_OK, results[latest]["value"]
        if every:
            return (ir.OUTCOME_FAIL, {"reason": "no branch succeeded", "results": all_res}) \
                if names else (ir.OUTCOME_OK, None)
        return None
    if base == ir.SETTLE_QUORUM:
        need = int(k or 1)
        if len(oks) >= need:
            return ir.OUTCOME_OK, {n: results[n]["value"] for n in oks}
        if len(oks) + (len(names) - len(done)) < need:
            return ir.OUTCOME_FAIL, {"reason": f"quorum {need} unreachable", "results": all_res}
        return None
    return (ir.OUTCOME_OK, all_res) if every else None


def _settle_final(mode: str, names: List[str], results: Dict[str, Dict[str, Any]],
                  order: List[str]) -> str:
    """A group's outcome once every member finished (``order`` = finishing order)."""
    if not names:
        return ir.OUTCOME_OK
    base, _, k = (mode or ir.SETTLE_ALL_SETTLED).partition(":")
    seq = [n for n in order if n in names]
    oks = [n for n in seq if results[n]["outcome"] == ir.OUTCOME_OK]
    if base == ir.SETTLE_ALL:
        bad = [n for n in seq if results[n]["outcome"] != ir.OUTCOME_OK]
        return results[bad[0]]["outcome"] if bad else ir.OUTCOME_OK
    if base == ir.SETTLE_RACE:
        return results[seq[0]]["outcome"]
    if base == ir.SETTLE_ANY:
        return ir.OUTCOME_OK if oks else ir.OUTCOME_FAIL
    if base == ir.SETTLE_QUORUM:
        return ir.OUTCOME_OK if len(oks) >= int(k or 1) else ir.OUTCOME_FAIL
    return ir.OUTCOME_OK


def _par_child_done(r: Run, iid: str, role: str, outcome: str, value: Any) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    name = role.split(":", 1)[1]
    frame["results"][name] = {"outcome": outcome, "value": value}
    frame["order"].append(name)
    names = list((node.get("branches") or {}).keys())
    decided = _settle(str(node.get("settle") or ir.SETTLE_ALL), names, frame["results"], name)
    if decided is None:
        return
    frame["decided"] = True
    _cancel_children(r, iid, f"par settled ({node.get('settle')})")
    _complete(r, iid, decided[0], decided[1])


def _group_child_done(r: Run, iid: str, child: str, role: str, outcome: str, value: Any) -> None:
    frame = r.inst[iid]["frame"]
    if role == "do":
        frame["phase"] = "join"
        frame["do"] = {"outcome": outcome, "value": value}
        if outcome != ir.OUTCOME_OK:
            _cancel_children(r, iid, "group do failed")
            _complete(r, iid, outcome, value)
            return
    elif role == "member":
        frame["results"][child] = {"outcome": outcome, "value": value}
        frame["order"].append(child)
    _group_try_settle(r, iid)


def _group_try_settle(r: Run, iid: str) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if frame.get("do") is None:
        return
    members = [m["instance"] for m in frame["members"]]
    if any(m not in frame["results"] for m in members):
        return
    settle = str(node.get("settle") or ir.SETTLE_ALL_SETTLED)
    outcome = _settle_final(settle, members, frame["results"], frame["order"])
    value = {
        "do": frame["do"]["value"],
        "members": [{"instance": m["instance"], "with": m["with"],
                     **frame["results"][m["instance"]]} for m in frame["members"]],
    }
    r.emit("group_settled", node=node.id, instance=iid, outcome=outcome,
           members=len(members), settle=settle)
    _complete(r, iid, outcome, value)


# ---------------------------------------------------------------------------
# finishing nodes (advance)


def _ensures(r: Run, iid: str, node: ir.Node, outcome: str, value: Any) -> Tuple[str, Any]:
    ens = (r.chart.spec or {}).get("ensures") or {}
    for key, text in ens.items():
        name, _, out = str(key).rpartition(".")
        if out != outcome or name not in (node.label, node.id):
            continue
        try:
            held = expr_mod.truthy(_eval(r, iid, text))
            error = None
        except expr_mod.ExprError as exc:
            held, error = False, str(exc)
        if not held:
            r.emit("monitor_violation", kind="ensures", key=key, node=node.id,
                   instance=iid, expr=text, error=error)
            reason = f"ensures {key} did not hold: {text}"
            return ir.OUTCOME_FAIL, {"reason": reason, "original": {"outcome": outcome,
                                                                    "value": value}}
    return outcome, value


def _complete(r: Run, iid: str, outcome: str, value: Any, handled: bool = False) -> None:
    """A node instance finished with ``(outcome, value)``: record, then advance."""
    if iid not in r.inst:
        return
    inst = r.inst[iid]
    node = r.chart.node(inst["node"])
    inst["await"] = None
    if not handled:
        key = node_key(node)
        r.state.setdefault("result", {})[key] = {"outcome": outcome, "value": value}
        own = value
        outcome, value = _ensures(r, iid, node, outcome, value)
        r.state["result"][key] = {"outcome": outcome, "value": value}
        for sid in _result_scopes(r, inst.get("parent")):
            r.inst[sid].setdefault("results", {})[key] = {"outcome": outcome, "value": value}
        if node.bind:
            # the node's own value, before ensures (doc 4.2): the variable
            # keeps the shape the node returns
            r.state.setdefault("data", {})[node.bind] = own
            r.emit("bound", node=node.id, instance=iid, var=node.bind)
        # doc 4.5: the outcome is final -> do (at leave). A failing effect
        # turns it to 'fail', which the node's own 'on' handles like any other
        outcome, value = _leave_effects(r, iid, node, outcome, value)
        handler = node.on.get(outcome) if node.on else None
        if handler:
            inst["frame"]["on"] = outcome
            _enter_child(r, iid, handler, "on")
            return
    # doc 4.5: ... -> triggers (at leave) -> lifecycle.reset_at -> the parent
    _queue_triggers(r, iid, node, AT_LEAVE, outcome)
    _reset_at(r, node)
    r.emit("left", node=node.id, instance=iid, outcome=outcome)
    parent = inst.get("parent")
    role = inst.get("role")
    _forget(r, iid)
    if parent is None:
        _finish_run(r, outcome, value)
        return
    if parent in r.inst:
        _child_done(r, parent, iid, role or "", outcome, value)


def _child_done(r: Run, iid: str, child: str, role: str, outcome: str, value: Any) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if role == "on":
        _complete(r, iid, outcome, value, handled=True)
        return
    t = node.type
    if t == ir.T_SEQ:
        if outcome != ir.OUTCOME_OK:
            _complete(r, iid, outcome, value)
            return
        children = node.get("children") or []
        frame["idx"] += 1
        if frame["idx"] >= len(children):
            _complete(r, iid, ir.OUTCOME_OK, value)
            return
        env = {"prev": value} if node.get("pass_prev") else None
        _enter_child(r, iid, children[frame["idx"]], "seq", env)
    elif t in (ir.T_COND, ir.T_CHOOSE, ir.T_SCOPE):
        _complete(r, iid, outcome, value)
    elif t == ir.T_LOOP:
        _loop_child_done(r, iid, role, outcome, value)
    elif t == ir.T_PAR:
        _par_child_done(r, iid, role, outcome, value)
    elif t == ir.T_TIMEOUT:
        _complete(r, iid, outcome, value)
    elif t == ir.T_TRY:
        _try_child_done(r, iid, role, outcome, value)
    elif t == ir.T_GROUP:
        _group_child_done(r, iid, child, role, outcome, value)
    else:  # an atomic node never has children except an ``on`` handler
        _complete(r, iid, outcome, value)


def _try_child_done(r: Run, iid: str, role: str, outcome: str, value: Any) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    catch = node.get("catch") or {}
    if role == "body":
        handler = None
        if outcome != ir.OUTCOME_OK:
            handler = catch.get(outcome) or catch.get("*")
        if handler:
            frame["phase"] = "catch"
            frame["caught"] = outcome
            _enter_child(r, iid, handler, "catch", {"prev": value})
            return
        _try_finally(r, iid, outcome, value)
        return
    if role == "catch":
        _try_finally(r, iid, outcome, value)
        return
    if role == "finally":
        pending = frame.get("pending") or {}
        sig = frame.get("signal")
        if outcome != ir.OUTCOME_OK:
            _complete(r, iid, outcome, value)
            return
        if sig:
            _propagate(r, iid, sig)
            return
        _complete(r, iid, pending.get("outcome", ir.OUTCOME_OK), pending.get("value"))


def _try_finally(r: Run, iid: str, outcome: str, value: Any) -> None:
    node = r.node_of(iid)
    frame = r.inst[iid]["frame"]
    if node.get("finally_child"):
        frame["phase"] = "finally"
        frame["pending"] = {"outcome": outcome, "value": value}
        _enter_child(r, iid, node.get("finally_child"), "finally")
        return
    _complete(r, iid, outcome, value)


# -- control signals -------------------------------------------------------


def _propagate(r: Run, start: str, sig: Dict[str, Any]) -> None:
    """Carry a control signal from instance ``start`` up to its target."""
    op = sig["op"]
    below = start
    cur = r.inst[start].get("parent") if start in r.inst else None
    while cur is not None and cur in r.inst:
        node = r.node_of(cur)
        frame = r.inst[cur]["frame"]
        if node.type == ir.T_LOOP and op in (ir.CTL_CONTINUE, ir.CTL_BREAK) \
                and (sig.get("target") in (None, node.id)):
            role = r.inst[below]["role"] if below in r.inst else ""
            if op == ir.CTL_BREAK:
                _cancel_children(r, cur, "break")
                frame["decided"] = True
                if node.get("mode") == ir.LOOP_RETRY:
                    vals = frame.get("last")
                elif node.get("parallel") and frame.get("results") is not None:
                    # a parallel foreach: the items that finished ok before
                    # the break, in item order
                    res = frame.get("results") or {}
                    vals = [res[k]["value"] for k in sorted(res, key=int)
                            if res[k].get("outcome") == ir.OUTCOME_OK]
                else:
                    vals = frame.get("values")
                _complete(r, cur, ir.OUTCOME_OK, vals)
                return
            if role.startswith("item:"):
                _drop(r, below, "continue")
                _loop_child_done(r, cur, role, ir.OUTCOME_OK, None)
                return
            _cancel_children(r, cur, "continue")
            if role == "between":
                _loop_pause(r, cur)
            elif node.get("mode") == ir.LOOP_RETRY:
                _loop_body_done(r, cur, "continue", None)
            else:
                _loop_body_done(r, cur, ir.OUTCOME_OK, None)
            return
        if node.type == ir.T_TRY and frame.get("phase") in ("body", "catch") \
                and node.get("finally_child"):
            _cancel_children(r, cur, op)
            frame["phase"] = "finally"
            frame["signal"] = sig
            _enter_child(r, cur, node.get("finally_child"), "finally")
            return
        if node.type == ir.T_SCOPE and op == ir.CTL_RETURN \
                and sig.get("target") in (None, node.id):
            _cancel_children(r, cur, "return")
            outcome = sig.get("outcome") or ir.OUTCOME_OK
            _complete(r, cur, outcome,
                      fill_optional(node.get("returns") or {}, outcome, sig.get("value")))
            return
        below = cur
        cur = r.inst[cur].get("parent")
    # nothing caught it: the run ends
    _drop(r, start, op)
    if op == ir.CTL_END:
        _finish_run(r, ir.OUTCOME_OK, None, how="end")
    elif op == ir.CTL_RETURN:
        outcome = sig.get("outcome") or ir.OUTCOME_OK
        _finish_run(r, outcome, fill_optional(r.chart.returns or {}, outcome, sig.get("value")),
                    how="return")
    else:
        _finish_run(r, ir.OUTCOME_FAIL, {"reason": f"{op} found no enclosing loop"})


# -- removal -----------------------------------------------------------------


def _forget(r: Run, iid: str) -> None:
    """Remove an instance with everything it held: its open delegation (a
    nested delegation run is aborted with the save), its hold, its open ask
    (responders that still had the question are told). Status and reason are
    recomputed from what holds remain."""
    r.inst.pop(iid, None)
    _release_holds(r, iid)


def _release_holds(r: Run, iid: str) -> None:
    """Cancel a frame's external work and approvals, retaining the frame itself."""
    for d in r.state.get("delegations", {}).values():
        if d.get("instance") == iid and d.get("status") in OPEN_DELEGATION + ("verifying",):
            d["status"] = "cancelled"
            d["finished"] = store_mod.utcnow()
            if d.get("nested"):
                r.nested_aborts.append(dict(d["nested"]))
    for c in r.state.get("calls", {}).values():
        if c.get("instance") == iid and c.get("status") == CALL_OPEN:
            c["status"] = "cancelled"
            c["finished"] = store_mod.utcnow()
            r.nested_aborts.append(dict(c["nested"]))
    r.state["approvals"] = [a for a in r.state.get("approvals", []) if a.get("instance") != iid]
    for rec in (r.state.get("asks") or {}).values():
        if rec.get("instance") == iid and rec.get("status") == "open":
            rec["status"] = "cancelled"
            rec["closed"] = store_mod.utcnow()
            _withdraw(r, rec, "cancelled")
    _sync_waiting(r)


def _drop(r: Run, iid: str, why: str) -> None:
    """Cancel an instance and everything below it (journaled)."""
    for child in r.children_of(iid):
        _drop(r, child, why)
    if iid not in r.inst:
        return
    r.emit("left", node=r.inst[iid]["node"], instance=iid, outcome="cancelled", why=why)
    _forget(r, iid)


def _cancel_children(r: Run, iid: str, why: str) -> None:
    for child in r.children_of(iid):
        _drop(r, child, why)


def _finish_run(r: Run, outcome: str, value: Any, how: Optional[str] = None) -> None:
    for iid in list(r.inst):
        if iid in r.inst and r.inst[iid].get("parent") is None:
            _drop(r, iid, how or "run finished")
    declared = r.chart.returns or {}
    ok = outcome == ir.OUTCOME_OK or outcome in declared
    if ok and _acceptance_probes(r) and not (r.state.get("acceptance") or {}).get("passed"):
        _acceptance_begin(r, outcome, value, how)
        return
    _finish_final(r, outcome, value, how)


def _finish_final(r: Run, outcome: str, value: Any, how: Optional[str] = None) -> None:
    declared = r.chart.returns or {}
    ok = outcome == ir.OUTCOME_OK or outcome in declared
    r.state["approvals"] = [a for a in r.state.get("approvals") or []
                            if a.get("kind") != HOLD_ACCEPTANCE]
    r.state["status"] = ST_DONE if ok else ST_FAILED
    r.state["reason"] = None
    # instance holds went with their instances; a run-wide hold (a halt) is
    # kept on record -- progress is refused while it stands, so a run cannot
    # finish past one, and a person can still see and release it
    r.state["approvals"] = [a for a in r.state.get("approvals") or []
                            if a.get("instance") is None]
    r.state["outcome"] = outcome
    r.state["value"] = value
    r.state["finished"] = store_mod.utcnow()
    r.emit("done", outcome=outcome, value=value, how=how, status=r.state["status"])
    _reset_at(r, None)
    _request_round(r)


# -- recur (docs 2.1, recur) -----------------------------------------------------

#: Who filed a next-round request.
ROUND_BY_RECUR = "recur"
#: Seconds between a round's end and the auto start of the next (``recur.min_gap``).
RECUR_MIN_GAP_DEFAULT = ir.RECUR_MIN_GAP_DEFAULT


def _request_round(r: Run) -> None:
    """A recurring run that finished with outcome ``ok`` files its own next
    round; with ``auto`` the daemon's clock starts it (:func:`start_next_round`).
    A failed or aborted run files nothing (the loop stops where a person must
    look), and neither does a run ending with another declared outcome -- that
    is how a definition ends the loop itself (a leader's wind-down)."""
    recur = (r.chart.meta or {}).get("recur")
    if not recur or r.state.get("parent") or r.state.get("status") != ST_DONE \
            or r.state.get("outcome") != ir.OUTCOME_OK:
        return
    nxt = int(r.state.get("round") or 1) + 1
    gap = float(recur.get("min_gap", RECUR_MIN_GAP_DEFAULT))
    r.state["next_round"] = {"by": ROUND_BY_RECUR, "auto": bool(recur.get("auto")),
                             "round": nxt, "requested": store_mod.utcnow(),
                             "not_before": r.now + gap}
    r.emit("round_requested", round=nxt, auto=bool(recur.get("auto")), min_gap=gap)


def _next_round_chart(st: RunStore, state: Dict[str, Any]) -> Tuple[ir.Chart, bool]:
    """The definition the next round runs: reloaded by name (or by its
    generated path), so an updated definition takes effect at a round
    boundary; the snapshot when it cannot be loaded. ``(chart, reloaded)``."""
    cwd = str(st.cwd)
    refs = [str(state.get("workflow") or "")]
    source = str(state.get("source") or "")
    if source and source not in refs:
        refs.append(source)
    for ref in refs:
        if not ref:
            continue
        try:
            return load_definition(ref, cwd), True
        except Exception:  # noqa: BLE001 - try the next form, then the snapshot
            continue
    return st.load_chart(), False


def start_next_round(cwd: Optional[str] = None, scope: Optional[str] = None,
                     now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Start the slot's next round when its finished run filed an ``auto``
    request. Daemon-driven (the cflow2 clock). ``None`` when there is nothing
    to start: no request, a plain ``recur: true`` request (the driver starts
    that one), a live run in the slot, ``recur.min_gap`` seconds not yet past
    since the round ended, or a request cancelled meanwhile -- the start
    re-checks the request under the slot lock, so a cancel that lands while the
    next definition loads wins.

    The request is single use: the start archives the finished run, so a
    second call finds nothing."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return None
    try:
        state = st.load_state()
    except StoreError:
        return None
    nxt = state.get("next_round") or {}
    if store_mod.recovery_held(state):
        return None
    if state.get("status") not in FINISHED_STATUSES or not nxt.get("auto"):
        return None
    t = time.time() if now is None else now
    if t < float(nxt.get("not_before") or 0):
        return None
    chart, reloaded = _next_round_chart(st, state)
    inputs = (state.get("data") or {}).get("inputs") if reloaded else None
    try:
        payload = start(chart, cwd=str(st.cwd), scope=st.scope, params=state.get("params"),
                        caps=state.get("caps"), now=now, driver=driver_of(state),
                        caller=CALLER_USER, inputs=inputs,
                        next_round_of=str(state.get("run_id") or ""))
    except FlowError:
        return None  # a start won the race, or the slot changed meanwhile
    note("round_auto_started", cwd=str(st.cwd), scope=st.scope, round=nxt.get("round"),
         previous=state.get("run_id"), reloaded=reloaded)
    return {"workflow": payload.get("workflow"), "run": payload.get("run"),
            "round": nxt.get("round"), "driver": driver_of(state) or None,
            "previous": state.get("run_id")}


def cancel_next_round(reason: Optional[str] = None, *, cwd: Optional[str] = None,
                      scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """A person stops a recurring run: the pending next-round request is
    dropped (``round_cancelled``). The run itself is not touched."""
    def op(r: Run) -> None:
        if not r.state.get("next_round"):
            raise FlowError("no next round is requested in this slot")
        dropped = r.state.pop("next_round")
        r.emit("round_cancelled", round=dropped.get("round"), reason=reason, by=CALLER_USER)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


# -- acceptance (typed start inputs, doc 2.6) ---------------------------------------

#: ``probe_runner`` for acceptance checks; ``None`` is :func:`default_probe_runner`.
ACCEPTANCE_RUNNER: Optional[ProbeRunner] = None


def _acceptance_probes(r: Run) -> List[Dict[str, Any]]:
    """The acceptance items with a probe (``spec.acceptance``, written by
    :func:`.inputs.apply`); text-only items are the agent's own check."""
    if r.state.get("parent"):
        return []  # a nested delegation run: the delegating run checks at its end
    items = (r.chart.spec or {}).get("acceptance") or []
    return [dict(i) for i in items if isinstance(i, dict) and i.get("probe")]


def _acceptance_begin(r: Run, outcome: str, value: Any, how: Optional[str]) -> None:
    """Hold an ``ok`` finish until the acceptance probes ran (outside the
    lock, in the background)."""
    token = secrets.token_hex(4)
    r.state["acceptance"] = {"status": "checking", "outcome": outcome, "value": value,
                             "how": how, "token": token, "checked_at": r.now, "failed": []}
    r.emit("acceptance_checking", items=[i.get("id") for i in _acceptance_probes(r)])
    st, now = r.store, r.now
    r.after.append(lambda: _launch(_acceptance_job, str(st.cwd), st.scope, token, now))


def _acceptance_job(cwd: str, scope: str, token: str, now: Optional[float]) -> None:
    st = RunStore(cwd, scope)
    try:
        snap = Run.load(st, now)
    except StoreError:
        return
    acc = snap.state.get("acceptance") or {}
    if acc.get("token") != token or snap.status not in LIVE_STATUSES:
        return
    runner = ACCEPTANCE_RUNNER or default_probe_runner
    results = []
    for item in _acceptance_probes(snap):
        verdict, output, code = _probe_verdict(_run_probe(snap, None, runner, item["probe"]))
        results.append({"id": item.get("id"), "text": item.get("text"), "probe": item["probe"],
                        "met": verdict == PROBE_HOLDS, "exit": code,
                        "output": _tail(str(output or ""))[-1000:]})

    def op(r: Run) -> None:
        acc = r.state.get("acceptance") or {}
        if acc.get("token") != token or r.status not in LIVE_STATUSES:
            return
        failed = [x for x in results if not x["met"]]
        acc["checked_at"] = r.now
        if not failed:
            acc.update(status="passed", passed=True, failed=[])
            r.emit("acceptance_passed", items=[x["id"] for x in results])
            _finish_final(r, acc["outcome"], acc.get("value"), acc.get("how"))
            return
        acc.update(status="failed", failed=failed)
        r.state["approvals"] = [a for a in r.state.get("approvals") or []
                                if a.get("kind") != HOLD_ACCEPTANCE]
        r.state["approvals"].append({"kind": HOLD_ACCEPTANCE, "instance": None,
                                     "failed": [x["id"] for x in failed]})
        r.emit("acceptance_failed", failed=[{k: x[k] for k in ("id", "text", "exit")}
                                            for x in failed])
    try:
        _mutate(cwd, scope, now, op)
    except (FlowError, StoreError):
        pass


def _acceptance_recheck_due(r: Run) -> Optional[str]:
    acc = r.state.get("acceptance") or {}
    if acc.get("status") != "failed" or r.status not in LIVE_STATUSES:
        return None
    if r.now - float(acc.get("checked_at") or 0) < ACCEPTANCE_RECHECK:
        return None
    return acc.get("token")


def accept_anyway(reason: Optional[str] = None, *, by: str = CALLER_USER,
                  cwd: Optional[str] = None, scope: Optional[str] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """A person accepts a finish whose acceptance probes failed: the run
    finishes with the outcome it reached (``acceptance_waived``)."""
    def op(r: Run) -> None:
        _require_progress(r)
        acc = r.state.get("acceptance") or {}
        if acc.get("status") != "failed":
            raise FlowError("no failed acceptance check holds this run")
        acc.update(status="waived", passed=True)
        r.emit("acceptance_waived", by=by, reason=reason,
               failed=[x.get("id") for x in acc.get("failed") or []])
        _finish_final(r, acc["outcome"], acc.get("value"), acc.get("how"))
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


# ---------------------------------------------------------------------------
# delegation


VIA_INTERNAL = "internal"
VIA_MCP = "mcp"
VIA_NESTED = "nested"


def _subtree(chart: ir.Chart, node_id: str) -> List[ir.Node]:
    """The nodes a delegation of ``node_id`` runs (without the node's own
    ``on`` handlers, which run in the delegating run)."""
    return list(ir.subtree_nodes(chart, node_id))


def _subtree_flat(chart: ir.Chart, node_id: str) -> bool:
    """Only task/seq nodes, no ``on``/``bind``/``ensures`` below the root
    (``ir.subtree_is_flat``)."""
    return bool(ir.subtree_is_flat(chart, node_id))


def _user_gates(chart: ir.Chart, node_id: str) -> List[str]:
    """A person's gates inside a subtree: a ``chooser: user`` choice, a task
    with ``ask``. A delegate has no person to put them to."""
    out = []
    for n in _subtree(chart, node_id):
        if n.type == ir.T_CHOOSE and (n.get("chooser") or ir.CHOOSER_AGENT) == ir.CHOOSER_USER:
            out.append(f"choose {n.label or n.id} (chooser: user)")
        elif n.type == ir.T_TASK and n.get("ask"):
            out.append(f"task {n.label or n.id} (ask)")
    return out


def _plan(node: ir.Node, caps: Any, flat: bool) -> Any:
    return caps_mod.plan_node(node, caps, flat=flat)


def _next_undispatched(node: ir.Node, current: str, caps: Any, flat: bool) -> str:
    return executors.InlineFallback.next_executor(node, current, caps, flat=flat)


def _delegate(r: Run, iid: str) -> bool:
    """Enter a delegation point. True when the subtree is NOT expanded here.

    A subtree with a person's gate in it (``chooser: user``, ``ask``) is never
    delegated: it runs inline (``fallback``; the static check reports it as an
    error). A flat subtree (task/seq only) goes out as one brief and comes
    back as one result. Any other subtree, for a ``subagent`` or ``session``
    executor, becomes a nested delegation run the delegate drives with the
    flow_* tools (doc 7)."""
    node = r.node_of(iid)
    if caps_mod is None:
        r.emit("fallback", node=node.id, cause="caps module unavailable", route="inline")
        return False
    gates = _user_gates(r.chart, node.id)
    if gates:
        r.emit("fallback", node=node.id, instance=iid, decision="executor",
               cause="a person's gate inside the delegated subtree: " + ", ".join(gates),
               route=f"{', '.join(node.executor)} -> inline")
        return False
    flat = _subtree_flat(r.chart, node.id)
    caps = r.caps
    plan = _plan(node, caps, flat)
    for ex, cap in plan.executor.skipped:
        r.emit("fallback", node=node.id, instance=iid, cause=cap,
               route=f"{ex} -> next preference", decision="executor")
    for part_name in ("returns", "readonly", "parallel", "budget", "carry"):
        part = getattr(plan, part_name, None)
        if part is not None and part.degraded:
            r.emit("fallback", node=node.id, instance=iid,
                   cause=part.capability or "; ".join(part.degraded),
                   route=part.route, decision=part_name, detail=list(part.degraded))
    executor = plan.route
    if executor in executors.DISPATCHED and not executors.available(str(r.store.cwd)):
        nxt = _next_undispatched(node, executor, caps, flat)
        r.emit("fallback", node=node.id, instance=iid,
               cause=f"no {executor} dispatcher available (no daemon clock watching "
                     f"this directory)",
               route=f"{executor} -> {nxt}", decision="executor")
        executor = nxt
        if executor != plan.route:
            plan = _plan(_with_executor(node, executor), caps, flat)
    if executor == ir.EXEC_INLINE:
        return False

    dels = r.state.setdefault("delegations", {})
    did = f"d{len(dels) + 1}"
    rec: Dict[str, Any] = {
        "id": did, "instance": iid, "node": node.id, "label": shown_label(r.chart, node),
        "executor": executor, "status": "pending" if executor == ir.EXEC_SUBAGENT else "queued",
        "route": plan.to_dict(), "receipt": None, "started": store_mod.utcnow(),
        "started_at": r.now, "finished": None, "attempts": 0, "errors": [],
        "returns": returns_of(node), "flat": flat,
    }
    dels[did] = rec
    r.inst[iid]["await"] = AW_DELEGATION
    r.inst[iid]["frame"]["delegation"] = did
    r.emit("delegate_started", node=node.id, instance=iid, delegation=did,
           executor=executor, route=plan.route, nested=None if flat else True)
    if not flat:
        _open_nested(r, iid, rec)
        if rec["status"] not in OPEN_DELEGATION:
            return True  # the nested run finished on entry; already applied
    if executor in executors.DISPATCHED:
        executors.dispatch(rec, r)
    return True


def _with_executor(node: ir.Node, executor: str) -> ir.Node:
    clone = ir.Node.from_dict(node.to_dict())
    clone.executor = [executor]
    return clone


# -- nested delegation runs ----------------------------------------------------


def nested_scope(scope: str, did: str) -> str:
    return f"{scope}{store_mod.NESTED_SEP}{did}"


def _nested_chart(r: Run, node: ir.Node) -> ir.Chart:
    """The delegation node's subtree as a chart of its own: the root keeps
    its body but not its executor, ``on``, ``bind`` or ensures (the
    delegating run applies those when the result comes back). Nor does it
    keep the root's ``do:`` and ``triggers`` (the delegating run applies them
    when it enters and leaves the node, doc 2.7/2.8) or the collections:
    they live in the delegating run only."""
    nodes: Dict[str, ir.Node] = {}
    for n in _subtree(r.chart, node.id):
        nodes[n.id] = ir.Node.from_dict(n.to_dict())
    root = nodes[node.id]
    root.parent = None
    root.executor = []
    root.on = {}
    root.bind = None
    root.props.pop("do", None)
    root.props.pop("triggers", None)
    labels = {x for n in nodes.values() if n.id != node.id for x in (n.id, n.label) if x}
    spec = json.loads(json.dumps(r.chart.spec or {}))
    spec.pop("monitors", None)  # run-wide: the delegating run watches them
    spec["ensures"] = {k: v for k, v in (spec.get("ensures") or {}).items()
                       if str(k).rsplit(".", 1)[0] in labels}
    return ir.Chart(
        name=f"{r.chart.name}~{node.label or node.id}", nodes=nodes, root=node.id,
        kind=ir.KIND_WORKFLOW, description=f"nested delegation run of {node.label or node.id}",
        meta={k: v for k, v in (r.chart.meta or {}).items() if k != "collections"},
        params={}, returns=returns_of(node), data={},
        rules=dict(r.chart.rules or {}), spec=spec, source=r.chart.source,
    )


def _open_nested(r: Run, iid: str, rec: Dict[str, Any]) -> None:
    """Start the nested run of a delegation whose subtree is not flat, in the
    same directory under scope ``<scope>~<delegation id>``, with the
    delegation's params and a snapshot of the run data. Its driver is the
    delegate: the delegating run's driver for ``subagent`` (a sub-agent calls
    the tools from the driver's session), the assigned child session for
    ``session``."""
    node = r.node_of(iid)
    scope = nested_scope(r.store.scope, rec["id"])
    link = {"cwd": str(r.store.cwd), "scope": r.store.scope, "delegation": rec["id"],
            "run": r.state.get("run_id")}
    rec["nested"] = {"cwd": str(r.store.cwd), "scope": scope}
    chart = _nested_chart(r, node)
    params = json.loads(json.dumps(r.env(iid).get("params") or {}, default=str))
    data = json.loads(json.dumps(r.state.get("data") or {}, default=str))
    st = RunStore(str(r.store.cwd), scope)
    driver = driver_of(r.state) if rec["executor"] == ir.EXEC_SUBAGENT else ""
    with st.lock():
        if st.exists():
            _abort_nested({"cwd": str(st.cwd), "scope": scope}, locked=True)
            st.archive()
        elif st.chart_path.exists():
            st.chart_path.unlink()
        child = _new_run(st, chart, params=params, data=data, caps=r.state.get("caps"),
                         driver=driver, now=r.now, parent=link)
        done = child.state.get("status") in FINISHED_STATUSES
        if done:
            child.state["delivered"] = True  # applied below, in this lock
        child.save()
        child.after = []  # nothing of the child's may wait on this run's lock
        outcome, value = child.state.get("outcome"), child.state.get("value")
    r.emit("nested_started", delegation=rec["id"], node=node.id, instance=iid, scope=scope)
    if done:
        _nested_result(r, rec, child.state.get("status"), outcome, value)


def _abort_nested(link: Dict[str, Any], locked: bool = False, _depth: int = 0) -> None:
    """Abort a nested run (its delegation or call was cancelled) and, below
    it, the nested runs it had open itself. Marked delivered: nothing goes
    back to the parent."""
    below: List[Dict[str, Any]] = []
    try:
        st = RunStore(link["cwd"], link["scope"])
        if not st.exists():
            return

        def go() -> None:
            child = Run.load(st)
            if child.status in LIVE_STATUSES:
                _abort(child, "its delegation or call was cancelled in the parent run")
            child.state["delivered"] = True
            child.store.commit(child.state, child.events)
            below.extend(child.nested_aborts)

        if locked:
            go()
        else:
            with st.lock():
                go()
    except (StoreError, OSError, KeyError):
        pass
    if _depth < 16:
        for sub in below:
            _abort_nested(sub, _depth=_depth + 1)


def _nested_result(r: Run, rec: Dict[str, Any], status_: Optional[str], outcome: Any,
                   value: Any) -> None:
    """A nested run finished: its root outcome/value is the delegation's
    result, validated against ``returns`` like any submission."""
    if rec["status"] not in OPEN_DELEGATION:
        return
    if status_ == ST_ABORTED:
        _fail_delegation(r, rec, "the nested delegation run was aborted")
        return
    outcome = str(outcome or ir.OUTCOME_FAIL)
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    errors = validate_result(rec.get("returns") or {ir.OUTCOME_OK: {}}, outcome, value)
    if errors:
        # the nested run cannot be re-run: an invalid result fails at once
        rec["attempts"] = max(int(rec["attempts"]), 2)
        _reject(r, rec, errors)
        return
    _accept(r, rec, outcome, value, f"nested run {rec['nested']['scope']}")


def deliver_nested(cwd: str, scope: str) -> bool:
    """Hand a finished nested run's result to its delegating run (called
    after the nested run's lock is released, and by the parent's status
    reconcile). True when something was delivered."""
    st = RunStore(cwd, scope)
    try:
        state = st.load_state()
    except StoreError:
        return False
    link = state.get("parent")
    if not link or state.get("delivered") or state.get("status") not in FINISHED_STATUSES:
        return False

    def op(r: Run) -> None:
        if link.get("call"):
            rec = (r.state.get("calls") or {}).get(link["call"])
        else:
            rec = (r.state.get("delegations") or {}).get(link.get("delegation"))
        if r.status not in LIVE_STATUSES or rec is None \
                or (rec.get("nested") or {}).get("scope") != scope:
            return
        if link.get("call"):
            _call_result(r, rec, state.get("status"), state.get("outcome"), state.get("value"))
            return
        _nested_result(r, rec, state.get("status"), state.get("outcome"), state.get("value"))
    try:
        _mutate(link["cwd"], link["scope"], None, op)
    except (FlowError, StoreError):
        return False
    with st.lock():
        cur = st.load_state()
        cur["delivered"] = True
        st.save_state(cur)
    return True


def _reconcile_nested(st: RunStore) -> None:
    """Deliver results of nested runs that finished without handing them on
    (the process finishing them went away first)."""
    try:
        state = st.load_state()
    except StoreError:
        return
    for rec in (state.get("delegations") or {}).values():
        nested = rec.get("nested")
        if nested and rec.get("status") in OPEN_DELEGATION:
            deliver_nested(nested["cwd"], nested["scope"])
    for rec in (state.get("calls") or {}).values():
        if rec.get("status") == CALL_OPEN:
            deliver_nested(rec["nested"]["cwd"], rec["nested"]["scope"])


# -- accepting a delegation result ----------------------------------------------


def _accept(r: Run, rec: Dict[str, Any], outcome: str, value: Any, source: str) -> None:
    """Apply a valid delegation result. While a monitor halts the run the
    result is kept (``held``) and applied when a person resumes it."""
    did = rec["id"]
    if _halted(r):
        rec["held"] = {"outcome": outcome, "value": value, "source": source}
        r.emit("delegate_held", delegation=did, node=rec["node"], outcome=outcome,
               reason="the run is halted by a monitor; applied on resume")
        return
    receipt = r.store.write_receipt(did, {
        "delegation": did, "node": rec["node"], "outcome": outcome,
        "value": value, "source": source, "submitted": store_mod.utcnow(),
    })
    rec.pop("held", None)
    rec.update({"status": "returned", "receipt": str(receipt),
                "finished": store_mod.utcnow(), "outcome": outcome})
    r.emit("delegate_returned", delegation=did, node=rec["node"],
           outcome=outcome, receipt=str(receipt))
    _complete(r, rec["instance"], outcome, value)


def _fail_delegation(r: Run, rec: Dict[str, Any], reason: str,
                     outcome: str = ir.OUTCOME_FAIL) -> None:
    rec["status"] = "timeout" if outcome == ir.OUTCOME_TIMEOUT else "failed"
    rec["finished"] = store_mod.utcnow()
    rec.setdefault("errors", []).append([reason])
    r.emit("delegate_failed", delegation=rec["id"], node=rec["node"], reason=reason,
           outcome=outcome)
    _complete(r, rec["instance"], outcome, {"reason": reason})


def _delegation_verifies(r: Run, rec: Dict[str, Any]) -> List[ir.Node]:
    """Tasks of a flat delegated subtree that declare ``verify``."""
    return [n for n in _subtree(r.chart, rec["node"])
            if n.type == ir.T_TASK and verify_spec(n)]


def brief(r: Run, did: str) -> Dict[str, Any]:
    """What a delegate gets: rendered prompts of the subtree's tasks in order,
    the return schema, the rules, the route plan -- and, for a subtree with
    control structure, the nested run it drives."""
    rec = r.state["delegations"][did]
    iid = rec["instance"]
    node = r.chart.node(rec["node"])
    env = r.env(iid) if iid in r.inst else r.env(None)
    tasks = []
    rule_ids: List[str] = []
    for n in _subtree(r.chart, node.id):
        if n.type != ir.T_TASK:
            continue
        tasks.append({
            "node": n.id, "label": n.label or None, "title": n.get("title"),
            "prompt": render(n.get("prompt"), env), "done_when": n.get("done_when"),
            "verify": n.get("verify"), "returns": returns_of(n),
            "effects": n.get("effects") or [],
        })
        for rid in n.get("rules") or []:
            if rid not in rule_ids:
                rule_ids.append(rid)
    rules = [{"id": rid, "text": (r.chart.rules.get(rid) or {}).get("text")} for rid in rule_ids]
    receipt = str(r.store.receipt_path(did))
    out = {
        "delegation": did, "node": node.id, "label": shown_label(r.chart, node),
        "type": node.type, "executor": rec["executor"], "status": rec["status"],
        "with": env.get("params") if node.type == ir.T_SCOPE else None,
        "tasks": tasks, "returns": rec.get("returns") or returns_of(node),
        "rules": rules, "route": rec.get("route"),
        "receipt_path": receipt,
    }
    nested = rec.get("nested")
    if nested:
        out["nested"] = dict(nested)
        out["tasks_outline_only"] = True
        out["submit"] = (
            f"this subtree has control structure, so the engine drives it as a nested run "
            f"in {nested['cwd']} under scope {nested['scope']!r}. Drive it like any run, "
            f"passing scope={nested['scope']!r} (and cwd={nested['cwd']!r} when you stand "
            f"in another directory) to every flow_* call: flow_status for what is active, "
            f"flow_report for each task, flow_select for each choice, until it is done. "
            f"Its final outcome and value become this delegation's result on their own -- "
            f"do not submit with flow_delegate. The task list above is an outline; the "
            f"nested run's flow_status is what to follow."
        )
    else:
        verifies = [t["label"] or t["node"] for t in tasks if t.get("verify")]
        out["submit"] = (
            "run the tasks above in order in a fresh context, then submit ONE result "
            "for the whole delegation with flow_delegate(delegation, outcome, value) "
            "matching 'returns'. Without MCP access the delegate writes "
            f'{{"outcome": ..., "value": {{...}}}} to {receipt} and the driver '
            "submits that path as receipt_path."
            + (f" An ok result is accepted only after the verify of "
               f"{', '.join(verifies)} passes (the engine runs it)." if verifies else "")
        )
    return out


# ---------------------------------------------------------------------------
# public operations


def _mutate(cwd: Optional[str], scope: Optional[str], now: Optional[float],
            fn: Callable[[Run], Any], lock_timeout: Optional[float] = None) -> Any:
    """Load, apply ``fn`` under the run lock, save. Time-based moves that are
    due (an ask's deadline, a timeout, a wait's seconds) apply first, so an
    operation never lands on a state the clock has already moved past."""
    st = RunStore(cwd, scope)
    with (st.lock() if lock_timeout is None else st.lock(lock_timeout)):
        r = Run.load(st, now)
        if r.status in LIVE_STATUSES:
            _tick_time(r)
        out = fn(r)  # a refusal raises before save: nothing is written
        if r.status in LIVE_STATUSES:
            _tick_until(r)  # a change may have made a 'wait: until' hold
        r.save()
    r.run_after()
    if r.deferred_error:
        raise FlowError(r.deferred_error)
    return out


def _require_live(r: Run) -> None:
    if r.status not in LIVE_STATUSES:
        raise FlowError(
            f"run {r.state.get('run_id')} is {r.status}; start a new one with flow_start"
        )


def _require_progress(r: Run) -> None:
    """Live and not halted: a halting monitor stops every move of the run."""
    _require_live(r)
    halted = _halted(r)
    if halted:
        raise FlowError(
            f"run {r.state.get('run_id')} is halted by monitor {', '.join(halted)}: nothing "
            f"moves until a person resumes it ('claunch cflow2 resume' or the web UI). Tell "
            f"the user what the monitor found; do not work around it")


def _absent(value: Any) -> bool:
    return value is None


def _validate_params(chart: ir.Chart, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    params = dict(params or {})
    decls = chart.params or {}
    if decls:
        unknown = sorted(set(params) - set(decls))
        if unknown:
            raise FlowError(f"unknown params {', '.join(unknown)} (declared: {', '.join(decls)})")
    out: Dict[str, Any] = {}
    for name, decl in decls.items():
        decl = decl or {}
        if not _absent(params.get(name)):
            out[name] = params[name]
        elif "default" in decl:
            out[name] = json.loads(json.dumps(decl["default"]))
        elif decl.get("required"):
            raise FlowError(f"missing required param {name!r} (null is no value)")
        else:
            out[name] = None
    for name, value in params.items():
        out.setdefault(name, value)
    return out


# -- values in shell commands (verify) -------------------------------------------

#: The only value references a ``verify`` command string may hold: a param or a
#: top-level data name. Each becomes a reference to an environment variable
#: (``CFLOW_IN_<NAME>`` / ``CFLOW_DATA_<NAME>``); the value itself never
#: becomes shell text.
_SHELL_REF = re.compile(r"^\$?(params|data)\.([A-Za-z_][A-Za-z0-9_-]*)$")


def _data_env_name(name: str) -> str:
    return "CFLOW_DATA_" + re.sub(r"[^A-Za-z0-9]", "_", str(name)).upper()


#: Root names of the run's expression environment; ``${name...}`` with one of
#: these (or with a ``.``/``[`` path) is a cflow value reference. Anything
#: else -- ``${HOME}``, ``${CFLOW_IN_X}``, ``${VAR:-x}`` -- is the shell's own.
_VALUE_ROOTS = ("params", "data", "prev", "item", "result", "visits", "caps", "probe",
                "inputs", "col")


def _is_value_ref(inner: str) -> bool:
    inner = inner.strip()
    if not inner or inner.startswith("frag:"):
        return False
    if inner.startswith("$"):
        return True
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(.*)$", inner, re.S)
    if not m:
        return False
    root, rest = m.group(1), m.group(2)
    return root in _VALUE_ROOTS or rest[:1] in (".", "[")


def _shell_refs(text: str) -> List[Tuple[int, int, str]]:
    """``(start, end, quoting)`` of every cflow value reference in ``text``
    where quoting is ``none``/``double``/``single`` -- the shell quoting
    state the reference sits in. Shell parameter expansions are not
    references (:func:`_is_value_ref`)."""
    out: List[Tuple[int, int, str]] = []
    state = "none"
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "$" and text.startswith("${", i):
            end = text.find("}", i + 2)
            if end != -1:
                if _is_value_ref(text[i + 2:end]):
                    out.append((i, end + 1, state))
                i = end + 1
                continue
        if state == "single":
            if ch == "'":
                state = "none"
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "'" and state == "none":
            state = "single"
        elif ch == '"':
            state = "double" if state == "none" else "none"
        i += 1
    return out


def shell_ref_problems(text: Any, what: str) -> List[str]:
    """Why ``text`` (a shell command) cannot be run with its value references
    turned into environment variables; empty when it can."""
    if isinstance(text, (list, tuple)):
        return [f"{what}: value reference in argv item {x!r} -- read the value from the "
                f"environment (CFLOW_IN_NAME / CFLOW_DATA_NAME) instead"
                for x in text if "${" in str(x)]
    if not isinstance(text, str):
        return []
    problems = []
    for a, b, quoting in _shell_refs(text):
        ref = text[a + 2:b - 1].strip()
        if not _SHELL_REF.match(ref):
            problems.append(f"{what}: value reference ${{{ref}}} -- a shell command may only "
                            f"name ${{params.NAME}} or ${{data.NAME}} (passed as environment "
                            f"variables CFLOW_IN_NAME / CFLOW_DATA_NAME)")
        elif quoting == "single":
            problems.append(f"{what}: ${{{ref}}} sits inside single quotes, where the shell "
                            f"does not expand the variable it becomes")
    return problems


def _shell_command(text: str) -> str:
    """``text`` with each ``${params.X}``/``${data.X}`` replaced by a quoted
    reference to its environment variable (``"${CFLOW_IN_X}"``, bare inside
    double quotes). Callers checked :func:`shell_ref_problems` first."""
    out, last = [], 0
    for a, b, quoting in _shell_refs(text):
        m = _SHELL_REF.match(text[a + 2:b - 1].strip())
        if not m:
            raise FlowError(f"verify {text!r}: unsupported value reference "
                            f"{text[a:b]!r}")
        var = _env_name(m.group(2)) if m.group(1) == "params" else _data_env_name(m.group(2))
        out.append(text[last:a])
        out.append(f"${{{var}}}" if quoting == "double" else f'"${{{var}}}"')
        last = b
    out.append(text[last:])
    return "".join(out)


def check_shell_refs(chart: ir.Chart) -> List[str]:
    """Every ``verify`` string and every probe (wait and monitor) of ``chart``
    checked against the shell value rule (probes follow the same rule:
    ``${params.X}``/``${data.X}`` become environment variable references)."""
    problems: List[str] = []
    for n in chart.nodes.values():
        if n.type == ir.T_TASK:
            spec = verify_spec(n)
            if spec:
                problems += shell_ref_problems(spec["run"], f"task {n.label or n.id} verify")
            for plat in ("windows", "linux"):
                cmd = (n.get("restart") or {}).get(plat)
                if cmd:
                    problems += shell_ref_problems(cmd, f"task {n.label or n.id} restart.{plat}")
        if n.type == ir.T_WAIT:
            problems += shell_ref_problems(n.get("probe"), f"wait {n.label or n.id} probe")
            for c in n.get("checks") or []:
                problems += shell_ref_problems(c.get("probe"),
                                               f"wait {n.label or n.id} check {c.get('id')}")
    for name, body in ((chart.spec or {}).get("monitors") or {}).items():
        for pname, cmd in _monitor_probes(dict(body or {}), str(name)).items():
            problems += shell_ref_problems(cmd, f"monitor {name} probe {pname}")
    return problems


# -- starting ----------------------------------------------------------------------


def _new_run(st: RunStore, chart: ir.Chart, *, params: Dict[str, Any], data: Any,
             caps: Any, driver: str, now: Optional[float],
             parent: Optional[Dict[str, Any]] = None,
             inputs: Optional[Dict[str, Any]] = None,
             previous: Optional[Dict[str, Any]] = None,
             side: Optional[Dict[str, Any]] = None,
             mesh: Optional[str] = None) -> Run:
    """A fresh run in ``st`` (lock held, slot empty): snapshot, state, root
    entered. The caller saves."""
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else None)) \
        + "-" + uuid.uuid4().hex[:6]
    state: Dict[str, Any] = {
        "version": 1, "run_id": run_id, "workflow": chart.name, "source": chart.source,
        "status": ST_RUNNING, "reason": None, "started": store_mod.utcnow(),
        "updated": None, "finished": None, "params": params,
        "data": json.loads(json.dumps(data or {})), "result": {}, "visits": {},
        "inst": {}, "active": [], "delegations": {}, "approvals": [], "seq": 0,
        "caps": caps if isinstance(caps, dict) or caps is None else _caps_to_json(caps),
        "probe": {}, "outcome": None, "value": None,
        "scope": st.scope, "cwd": str(st.cwd), "driver": driver or "",
        "mesh": str(mesh or "").strip(),
        store_mod.DAEMON_KEY: store_mod.daemon_of(st.scope),
    }
    if parent:
        state["parent"] = parent
    else:
        # rounds and choice windows belong to the slot (docs 2.1 recur, 2.3
        # interval): ``previous`` is the slot's last run of this definition
        prev = previous or {}
        state["round"] = int(prev.get("round") or 1) + 1 if prev.get("next_round") else 1
        if prev.get("windows"):
            state["windows"] = dict(prev["windows"])
    if side:
        state["side"] = side
    st.write_chart(chart)
    r = Run(st, chart, state, now)
    r.emit("started", run=run_id, workflow=chart.name, source=chart.source or None,
           params=params, driver=driver or None, parent=parent, inputs=inputs, side=side,
           mesh=str(mesh or "").strip() or None)
    _init_collections(r, previous)
    _enter(r, chart.root, None, "root")
    _tick_time(r)
    return r


def _apply_inputs(chart: ir.Chart, raw: Optional[Dict[str, Any]]
                  ) -> Tuple[ir.Chart, Optional[Dict[str, Any]]]:
    """Fold typed start inputs into the chart before it is snapshotted
    (:mod:`.inputs`): ``data.inputs``, invariant rules and monitors,
    acceptance checks. ``(chart, values)``; values ``None`` when the chart
    declares no inputs and none were given."""
    schema = (chart.meta or {}).get("inputs") or {}
    if (chart.meta or {}).get("inputs_applied"):
        if raw:
            raise FlowError(f"{chart.name}: this chart was already started with inputs")
        return chart, (chart.data or {}).get("inputs")
    if not schema and not raw:
        return chart, None
    try:
        from . import inputs as inputs_mod
    except ImportError:  # pragma: no cover - a runtime without typed inputs
        raise FlowError("typed start inputs are not available in this version")
    if raw is not None and not isinstance(raw, dict):
        raise FlowError("inputs must be an object of name -> value")
    values, errors = inputs_mod.normalize(schema, raw or {},
                                          (chart.meta or {}).get("input_constraints"))
    if errors:
        raise FlowError("invalid inputs: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    try:
        return inputs_mod.apply(chart, values), values
    except ValueError as exc:
        raise FlowError(str(exc))


def _gate_waits(state: Dict[str, Any], chart: Optional[ir.Chart],
                _depth: int = 0) -> List[str]:
    """What only a person may release in a run: a user choice, an open ask,
    a halt, a loop or visit limit."""
    out = []
    for a in state.get("approvals") or []:
        kind = a.get("kind")
        out.append(f"monitor {a.get('monitor')}" if kind == HOLD_HALT
                   else f"{kind} on {a.get('label') or a.get('node') or a.get('instance')}")
    if chart is not None:
        for iid, inst in (state.get("inst") or {}).items():
            if inst.get("await") != AW_CHOOSE:
                continue
            try:
                node = chart.node(inst["node"])
            except ir.IRError:
                continue
            if (node.get("chooser") or ir.CHOOSER_AGENT) == ir.CHOOSER_USER:
                out.append(f"user choice {node.label or iid}")
    # A nested delegation run that waits on a person is the parent's wait
    # too: replacing the parent would abort it (G2).
    if _depth < 4:
        for rec in [*(state.get("delegations") or {}).values(),
                    *(state.get("calls") or {}).values()]:
            nested = rec.get("nested")
            if not nested or rec.get("status") not in OPEN_DELEGATION + (CALL_OPEN,):
                continue
            try:
                child = Run.load(RunStore(nested["cwd"], nested["scope"]))
            except (StoreError, KeyError, ir.IRError):
                continue
            if child.state.get("status") not in LIVE_STATUSES:
                continue
            out.extend(f"{w} (nested run {nested['scope']})"
                       for w in _gate_waits(child.state, child.chart, _depth + 1))
    return out


def default_driver(scope: Optional[str] = None) -> str:
    """The session a run started now belongs to: the starter's session
    (``$CLAUNCH_SESSION``), else the session the scope names.

    For a starter that is a session (its MCP server, its CLI). A process that
    starts runs for others -- the daemon -- passes :func:`scope_driver`
    instead: its environment names no caller (claunch-2yiij)."""
    env = os.environ.get(store_mod.SESSION_ENV, "").strip()
    if env:
        return env
    return scope_driver(scope)


def scope_driver(scope: Optional[str]) -> str:
    """The session ``scope`` names, or ``""`` for the default slot."""
    scope = (scope or "").strip()
    return "" if scope in ("", store_mod.DEFAULT_SCOPE) else store_mod.session_of(scope)


def _carry_checks(chart: ir.Chart, raw: Optional[Dict[str, Any]], st: RunStore
                  ) -> Optional[Dict[str, Any]]:
    """An agent's forced start keeps the invariants and acceptance items of
    the live run it replaces (H1): an agent may add checks on itself, never
    drop the ones a person gave. Items are merged per input name, the old
    ones first, duplicates once."""
    try:
        from .inputs import CHECK_TYPES
    except ImportError:  # pragma: no cover
        return raw
    try:
        old = st.load_state() if st.exists() else {}
    except StoreError:
        return raw
    if old.get("status") not in LIVE_STATUSES:
        return raw
    before = ((old.get("data") or {}).get("inputs") or {})
    schema = (chart.meta or {}).get("inputs") or {}
    out = dict(raw or {})
    for name, spec in schema.items():
        if (spec or {}).get("type") not in CHECK_TYPES:
            continue
        kept = [i for i in before.get(name) or [] if isinstance(i, dict)]
        if not kept:
            continue
        merged: List[Any] = []
        for item in kept + list(out.get(name) or []):
            if item not in merged:
                merged.append(item)
        out[name] = merged
    return out


def start(chart: ir.Chart, *, cwd: Optional[str] = None, scope: Optional[str] = None,
          params: Optional[Dict[str, Any]] = None, caps: Any = None,
          force: bool = False, now: Optional[float] = None,
          driver: Optional[str] = None, caller: str = CALLER_USER,
          inputs: Optional[Dict[str, Any]] = None,
          side: Optional[Dict[str, Any]] = None,
          next_round_of: Optional[str] = None,
          mesh: Optional[str] = None) -> Dict[str, Any]:
    """Start a run of ``chart`` in the (cwd, scope) slot; returns :func:`status`.

    ``mesh`` is the mesh the run's asks are routed in (``state.mesh``,
    claunch-5itbf). The callers that know the session pin it here: the daemon
    from the mesh the session was enrolled in, the MCP and CLI starts from
    the one team mesh the driver is in (:func:`.asks.home_mesh`). Empty
    leaves it to the roster at each ask, which is ambiguous once the driver
    is in several meshes; :func:`set_mesh` names it later.

    ``next_round_of`` (:func:`start_next_round`): the run whose auto
    next-round request this start fulfils; under the lock the slot must still
    hold that run with the request, or the start is refused.

    ``driver`` is the session that drives the run -- its identity for asks
    and for the MCP tools' authority (default :func:`default_driver`). A
    finished run in the slot is archived first. A live one refuses the start
    unless ``force`` (it is then aborted and archived); an agent's force
    (``caller`` agent) is refused while that run waits on a person.
    """
    if chart.kind != ir.KIND_WORKFLOW:
        raise FlowError(f"{chart.name} is a {chart.kind}; only a workflow can be started")
    ir.validate(chart)
    if store_mod.side_of(store_mod.normalize_scope(scope)) and side is None:
        raise FlowError(f"scope {scope!r} is a side run's slot; a side run is started "
                        f"from its session's main run (start_side, flow_start side)")
    if force and caller != CALLER_USER:
        inputs = _carry_checks(chart, inputs, RunStore(cwd, scope))
    chart, input_values = _apply_inputs(chart, inputs)
    problems = check_shell_refs(chart)
    if problems:
        raise FlowError("refused: " + "; ".join(problems))
    values = _validate_params(chart, params)
    st = RunStore(cwd, scope)
    who = default_driver(st.scope) if driver is None else str(driver or "")
    previous: Optional[Dict[str, Any]] = None
    with st.lock():
        if st.exists():
            try:
                old = st.load_state()
            except StoreError:
                old = {}
            previous = old
            if next_round_of is not None and (
                    old.get("run_id") != next_round_of
                    or not (old.get("next_round") or {}).get("auto")
                    or store_mod.recovery_held(old)):
                raise FlowError("the next-round request was withdrawn or the slot changed")
            if old.get("status") in LIVE_STATUSES:
                if not force:
                    raise FlowError(
                        f"a cflow2 run ({old.get('workflow')}, {old.get('run_id')}) is still "
                        f"{old.get('status')} here; continue it, or abort it first"
                    )
                old_run = Run.load(st, now)
                gates = _gate_waits(old_run.state, old_run.chart)
                if caller != CALLER_USER and gates:
                    raise FlowError(
                        f"refused: the live run waits on a person ({'; '.join(gates)}); a "
                        f"forced start would discard that. Ask the user to answer it, or to "
                        f"abort the run themselves ('claunch cflow2 abort')")
                _abort(old_run, "replaced by a forced start")
                old_run.save()
                previous = old_run.state
            st.archive(old.get("run_id"))
        elif st.chart_path.exists():
            st.chart_path.unlink()
        r = _new_run(st, chart, params=values, data=chart.data, caps=_caps_to_json(caps),
                     driver=who, now=now, inputs=input_values,
                     previous=previous if (previous or {}).get("workflow") == chart.name
                     else None, side=side, mesh=mesh)
        r.save()
    r.run_after()
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def _find(r: Run, ref: Optional[str], kinds: Tuple[str, ...], what: str,
          pred: Optional[Callable[[str], bool]] = None) -> str:
    cands = [iid for iid, inst in r.inst.items() if inst.get("await") in kinds
             and (pred is None or pred(iid))]
    if ref:
        if ref in cands:
            return ref
        hits = [iid for iid in cands
                if r.inst[iid]["node"] == ref or r.node_of(iid).label == ref]
        if not hits:
            # a label as status shows it (claunch-tm5ki); tried only after the
            # node id and the own label, so a reference that named one node
            # before still does
            hits = [iid for iid in cands if shown_label(r.chart, r.node_of(iid)) == ref]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise FlowError(f"{ref!r} names several active {what}s ({', '.join(sorted(hits))}); "
                            f"pass the instance id")
        raise FlowError(f"no active {what} {ref!r} (active: {', '.join(sorted(cands)) or 'none'})")
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise FlowError(f"no {what} is active right now")
    raise FlowError(f"several {what}s are active ({', '.join(sorted(cands))}); name one")


# -- authority (the MCP tools) ---------------------------------------------------


def _assignee(state: Dict[str, Any], rec: Dict[str, Any]) -> str:
    """Who carries out a delegation: the driver for a sub-agent, the child
    session for a session delegation (``assignee``, else ``child``)."""
    if rec.get("executor") == ir.EXEC_SUBAGENT:
        return driver_of(state)
    if rec.get("executor") == ir.EXEC_SESSION:
        return str(rec.get("assignee") or rec.get("child") or "")
    return ""


def acting_session(state: Dict[str, Any]) -> str:
    """The session a run's triggers act on (doc 2.7, principal ``driver``):
    the run's driver, or -- a nested delegation run -- the session that
    carries out that delegation (read now: a session delegation's child may
    be spawned after the nested run opened). ``""`` when there is none."""
    drv = driver_of(state)
    if drv:
        return drv
    link = state.get("parent")
    if not link:
        return ""
    try:
        pstate = RunStore(link["cwd"], link["scope"]).load_state()
    except (StoreError, KeyError, TypeError):
        return ""
    rec = (pstate.get("delegations") or {}).get(link.get("delegation"))
    return _assignee(pstate, rec) if isinstance(rec, dict) else ""


def may_drive(state: Dict[str, Any], session: str) -> bool:
    """Whether ``session`` may operate the run ``state``: it is the run's
    driver, or the run is a nested delegation run and ``session`` is the
    assignee of that delegation."""
    session = str(session or "").strip()
    drv = driver_of(state)
    if drv:
        if session == drv:
            return True
    elif not session and not state.get("parent"):
        return True  # an unmanaged run operated from an unmanaged shell
    link = state.get("parent")
    if link and session:
        try:
            pstate = RunStore(link["cwd"], link["scope"]).load_state()
        except (StoreError, KeyError):
            return False
        rec = (pstate.get("delegations") or {}).get(link.get("delegation"))
        if rec is not None and rec.get("status") in OPEN_DELEGATION \
                and _assignee(pstate, rec) == session:
            return True
    return False


def authorize(*, cwd: Optional[str], scope: Optional[str], session: str) -> None:
    """Refuse an agent's call on a run it does not drive (no run: nothing to
    refuse; starting is checked by the caller)."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return
    try:
        state = st.load_state()
    except StoreError:
        return
    if may_drive(state, session):
        return
    drv = driver_of(state) or "no session"
    raise FlowError(
        f"the cflow2 run in {st.cwd} scope {st.scope!r} is driven by {drv}; "
        f"{session or 'an unmanaged caller'} may not operate it. Work your own run "
        f"(omit scope and cwd)")


def _check_delegation_authority(r: Run, rec: Dict[str, Any], session: str,
                                what: str) -> None:
    session = str(session or "").strip()
    ex = rec.get("executor")
    if rec.get("nested") and what == "submit":
        raise FlowError(
            f"delegation {rec['id']} runs as the nested run {rec['nested']['scope']!r}: "
            f"drive it with the flow_* tools under that scope; its result comes back on its "
            f"own")
    if ex == ir.EXEC_SUBAGENT:
        if may_drive(r.state, session):
            return
    elif ex == ir.EXEC_SESSION:
        assignee = _assignee(r.state, rec)
        if session and session == assignee:
            return
        if what == "brief" and may_drive(r.state, session):
            return
        raise FlowError(f"delegation {rec['id']} is carried out by session "
                        f"{assignee or '(not spawned yet)'}; only it may {what} here")
    raise FlowError(f"delegation {rec['id']} belongs to the run driven by "
                    f"{driver_of(r.state) or 'no session'}; {session or 'this caller'} may "
                    f"not {what} it")


def _receipt_ok(r: Run, path: Path) -> bool:
    """A receipt is read only from the run's receipts directory or the
    caller's ``$CLAUNCH_SCRATCH`` (real paths: no link leads out)."""
    try:
        real = Path(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return False
    roots = [r.store.receipts_dir]
    scratch = os.environ.get("CLAUNCH_SCRATCH", "").strip()
    if scratch:
        roots.append(Path(scratch))
    for root in roots:
        try:
            base = Path(os.path.realpath(str(root)))
        except (OSError, ValueError):
            continue
        if real == base or base in real.parents:
            return True
    return False


# -- verify ------------------------------------------------------------------

#: ``(command, cwd, env, timeout) -> (exit code, output[, timed_out])``
VerifyRunner = Callable[[Any, str, Dict[str, str], float], Tuple[Any, ...]]

_VERIFY_THREADS: List[threading.Thread] = []
_VERIFY_GUARD = threading.Lock()


def verify_spec(node: ir.Node) -> Optional[Dict[str, Any]]:
    """A task's ``verify`` as ``{run, timeout}`` (older charts stored the
    command string or argv list as written)."""
    raw = node.get("verify")
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, dict):
        run = raw.get("run", raw.get("command"))
        timeout = raw.get("timeout")
    else:
        run, timeout = raw, None
    if not run:
        return None
    return {"run": run, "timeout": float(timeout) if timeout else DEFAULT_VERIFY_TIMEOUT}


def _env_name(name: str) -> str:
    return "CFLOW_IN_" + re.sub(r"[^A-Za-z0-9]", "_", str(name)).upper()


def _env_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return ""


def verify_env(r: Run, iid: str, node: Optional[ir.Node] = None,
               data_names: Tuple[str, ...] = ()) -> Dict[str, str]:
    """The environment a verify command runs with: the process environment
    plus ``CFLOW2_RUN``, ``CFLOW2_NODE``, ``CFLOW2_INSTANCE``, ``CFLOW2_SCOPE``,
    every param as ``CFLOW_IN_<NAME>`` (the run's params, overlaid by the
    params of the procedure the task runs in), and the top-level run data as
    ``CFLOW_DATA_<NAME>``: scalars as text, the rest as JSON text up to 64KB,
    as a probe gets them (:func:`_value_env`; a scalar is passed whatever its
    size, as before). A name in ``data_names``
    (a ``${data.NAME}`` the runtime rewrote) is passed whatever its size.

    Non-scalars used to be left out, which left a verify no way to read a
    result a node had bound -- improv-leader-v2's sweep gate needs the
    repository its integrate step merged into (claunch-6xg).

    ``CLAUNCH_SESSION`` is the run's driver (else the session its scope
    names; blank for an unmanaged run), never the process's own. The daemon
    runs a verify for a person's report, a resolved move and a delegation's
    submit, and its environment names nobody (claunch-2yiij; before that, the
    terminal that started it): the gates under ``tools/`` that resolve "my
    checkout" and "my report" from that name answered about no session, or
    the wrong one (claunch-2yiij.2). A probe has the same rule
    (:func:`_probe_session`)."""
    node = node or r.node_of(iid)
    env = dict(os.environ)
    env.update({
        "CFLOW2_RUN": str(r.state.get("run_id") or ""),
        "CFLOW2_NODE": node.label or node.id,
        "CFLOW2_INSTANCE": iid,
        "CFLOW2_SCOPE": str(r.state.get("scope") or ""),
        store_mod.SESSION_ENV: (str(r.state.get("driver") or "").strip()
                                or scope_driver(r.state.get("scope"))),
    })
    params: Dict[str, Any] = dict(r.state.get("params") or {})
    params.update((r.env(iid) if iid in r.inst else {}).get("params") or {})
    for name, value in params.items():
        env[_env_name(name)] = _env_text(value)
    for name, value in (r.state.get("data") or {}).items():
        text = _env_text(value)
        scalar = value is None or isinstance(value, (str, int, float, bool))
        if scalar or len(text) <= 64 * 1024 or name in data_names:
            env[_data_env_name(name)] = text
    return env


#: ``$CFLOW_<NAME>`` inside an argv item (docs 2.3, 명령 안의 값 참조).
_ARGV_REF = re.compile(r"\$(CFLOW_[A-Za-z0-9_]*)")


def expand_argv(command: Any, env: Dict[str, str]) -> Any:
    """An argv command with each ``$CFLOW_<NAME>`` replaced by that
    variable of ``env`` (empty when unset), as a shell would for the string
    form. An argv runs without a shell, so nothing else would expand it --
    and on Windows the string form goes to cmd.exe, which does not read
    ``$VAR`` either. Each value stays inside its one argument; it is never
    parsed as a command. A string command is returned unchanged."""
    if not isinstance(command, list):
        return command
    return [_ARGV_REF.sub(lambda m: str(env.get(m.group(1), "")), str(part))
            for part in command]


def default_verify_runner(command: Any, cwd: str, env: Dict[str, str],
                          timeout: float) -> Tuple[int, str, bool]:
    """Run a verify command: ``(exit code, output, timed_out)``. Output goes
    to a file, never a pipe, so a descendant that outlives the command cannot
    keep the reader waiting. Past ``timeout`` the whole process group is
    killed (exit 124)."""
    import tempfile

    shell = not isinstance(command, list)
    command = expand_argv(command, env)
    kwargs: Dict[str, Any] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(command, shell=shell, cwd=cwd, env=env, stdout=out,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    **kwargs)
        except OSError as exc:
            return 127, f"verify could not start: {exc}", False
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if os.name == "posix":
            # the command's whole group goes, also when it finished but left
            # children behind
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        if timed_out:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        out.seek(0)
        text = out.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
    if timed_out:
        return 124, text + f"\nverify timed out after {timeout:g}s", True
    return proc.returncode, text, False


# -- restart (docs 2.2, restart) ---------------------------------------------------

RESTART_PENDING = "pending"
RESTART_RUNNING = "running"
RESTART_SUCCEEDED = "succeeded"
RESTART_FAILED = "failed"
RESTART_INTERRUPTED = "interrupted"
RESTART_UNSUPPORTED = "unsupported"
RESTART_FINAL = (RESTART_SUCCEEDED, RESTART_FAILED, RESTART_INTERRUPTED, RESTART_UNSUPPORTED)
RESTART_OUTPUT_CHARS = 4000


def restart_command(spec: Optional[Dict[str, Any]], system: str) -> Optional[str]:
    """The command of a task's ``restart`` for host ``system`` (v1's
    ``Restart.command_for``): ``win*`` -> windows, ``linux``/``wsl`` -> linux."""
    spec = spec or {}
    system = (system or "").lower()
    if system.startswith("win"):
        return spec.get("windows")
    if system in ("linux", "wsl"):
        return spec.get("linux")
    return None


def _restart_view(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    rec = rec or {}
    return {"status": rec.get("status") or RESTART_PENDING,
            **{k: rec.get(k) for k in ("requested_at", "completed_at", "exit_code", "output",
                                       "platform") if rec.get(k) is not None}}


def _require_restart_done(r: Run, iid: str, node: ir.Node) -> None:
    if not node.get("restart"):
        return
    rec = r.inst[iid]["frame"].get("restart") or {}
    st = rec.get("status") or RESTART_PENDING
    if st not in RESTART_FINAL:
        raise FlowError(
            f"task {node.label or node.id}: its restart is {st} -- the daemon runs it (an agent "
            f"never runs a restart itself) and types its result into this session; report "
            f"after that. Without a running daemon a person aborts the run")


def claim_restarts(*, platform: str, boot: str, cwd: Optional[str] = None,
                   scope: Optional[str] = None, now: Optional[float] = None
                   ) -> List[Dict[str, Any]]:
    """Claim every active task instance's restart that has not run in this
    visit (each instance is one visit). Daemon-only: the caller runs each
    ``kind: run`` action outside the lock and records it with
    :func:`complete_restart`. A ``running`` record of another daemon boot is
    marked ``interrupted`` and never run again -- the command may have
    restarted the daemon itself."""
    def op(r: Run) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if r.status not in LIVE_STATUSES or _halted(r) or r.state.get("parent"):
            return out
        base = {"run": r.state.get("run_id"), "workflow": r.state.get("workflow"),
                "driver": driver_of(r.state) or None}
        for iid in sorted(r.inst, key=lambda k: int(k.rsplit("@", 1)[1])):
            inst = r.inst[iid]
            node = r.node_of(iid)
            spec = node.get("restart")
            if inst.get("await") != AW_TASK or not spec:
                continue
            frame = inst["frame"]
            rec = frame.get("restart")
            item = {**base, "instance": iid, "node": node.id,
                    "label": shown_label(r.chart, node) or node.id}
            if rec:
                if rec.get("status") == RESTART_RUNNING and rec.get("boot") != boot:
                    rec.update(status=RESTART_INTERRUPTED, completed_at=store_mod.utcnow())
                    r.emit("restart_interrupted", node=node.id, instance=iid,
                           boot=rec.get("boot"))
                    out.append({**item, "kind": "interrupted"})
                continue
            command = restart_command(spec, platform)
            refused = ir.restart_refusal(command) if command else None
            if refused:
                # a chart compiled before the allowlist, or built by hand: never run it
                frame["restart"] = {"status": RESTART_FAILED, "platform": platform,
                                    "requested_at": store_mod.utcnow(),
                                    "completed_at": store_mod.utcnow(), "boot": boot,
                                    "output": f"refused: restart command {refused}"}
                r.emit("restart_refused", node=node.id, instance=iid, platform=platform,
                       command=command, reason=refused)
                out.append({**item, "kind": "refused", "platform": platform,
                            "reason": refused})
                continue
            if not command:
                frame["restart"] = {"status": RESTART_UNSUPPORTED, "platform": platform,
                                    "requested_at": store_mod.utcnow(), "boot": boot}
                r.emit("restart_unsupported", node=node.id, instance=iid, platform=platform)
                out.append({**item, "kind": "unsupported", "platform": platform})
                continue
            token = secrets.token_hex(4)
            frame["restart"] = {"status": RESTART_RUNNING, "command": command,
                                "platform": platform, "boot": boot, "token": token,
                                "requested_at": store_mod.utcnow()}
            r.emit("restart_requested", node=node.id, instance=iid, platform=platform,
                   command=command)
            out.append({**item, "kind": "run", "command": command, "token": token,
                        "timeout": float(spec.get("timeout") or 120.0)})
        return out
    st = RunStore(cwd, scope)
    if not st.exists():
        return []
    return _mutate(cwd, scope, now, op)


def complete_restart(instance: str, token: str, exit_code: Optional[int], output: str, *,
                     cwd: Optional[str] = None, scope: Optional[str] = None,
                     now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Record a claimed restart's result; ``None`` when the instance left or
    the record is not the claimed one any more (nothing written)."""
    def op(r: Run) -> Optional[Dict[str, Any]]:
        inst = r.inst.get(instance)
        rec = (inst or {}).get("frame", {}).get("restart") or {}
        if not inst or rec.get("token") != token or rec.get("status") != RESTART_RUNNING:
            return None
        tail = (output or "")[-RESTART_OUTPUT_CHARS:]
        rec.update(status=RESTART_SUCCEEDED if exit_code == 0 else RESTART_FAILED,
                   exit_code=exit_code, output=tail, completed_at=store_mod.utcnow())
        node = r.node_of(instance)
        r.emit("restart_completed", node=node.id, instance=instance, exit_code=exit_code,
               output=tail)
        return {"run": r.state.get("run_id"), "workflow": r.state.get("workflow"),
                "instance": instance, "node": node.id,
                "label": shown_label(r.chart, node) or node.id,
                "exit_code": exit_code, "status": rec["status"],
                "driver": driver_of(r.state) or None}
    return _mutate(cwd, scope, now, op)


def _tail(text: str) -> str:
    lines = (text or "").rstrip().splitlines()[-VERIFY_TAIL_LINES:]
    out = "\n".join(lines)
    return out[-VERIFY_TAIL_CHARS:]


def _verify_prepare(r: Run, iid: str, node: ir.Node,
                    spec: Dict[str, Any]) -> Tuple[Any, Dict[str, str]]:
    """The command to run and its environment. A command string's value
    references become environment variable references (E1: no value is ever
    spliced into shell text); an argv list is used as written (a ``${...}``
    in it is refused, as the compiler does)."""
    run = spec["run"]
    if isinstance(run, str):
        problems = shell_ref_problems(run, f"task {node.label or node.id} verify")
        if problems:
            raise FlowError("; ".join(problems))
        names = tuple(m.group(2) for a, b, _q in _shell_refs(run)
                      for m in [_SHELL_REF.match(run[a + 2:b - 1].strip())]
                      if m and m.group(1) == "data")
        return _shell_command(run), verify_env(r, iid, node, names)
    problems = shell_ref_problems(run, f"task {node.label or node.id} verify")
    if problems:
        raise FlowError("; ".join(problems))
    return [str(x) for x in run], verify_env(r, iid, node)


def _run_verify(runner: Optional[VerifyRunner], command: Any, cwd: str,
                env: Dict[str, str], timeout: float) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        res = tuple((runner or default_verify_runner)(command, cwd, env, timeout))
    except Exception as exc:  # noqa: BLE001 - a runner failure is a failed verify
        res = (127, f"verify could not run: {exc}", False)
    return {"code": res[0], "output": res[1] if len(res) > 1 else "",
            "timed_out": bool(res[2]) if len(res) > 2 else False,
            "seconds": round(time.monotonic() - started, 3)}


def _launch(fn: Callable[..., Any], *args: Any) -> None:
    """Run a verify job in the background (the MCP call that started it has
    already returned)."""
    t = threading.Thread(target=fn, args=args, daemon=True, name="cflow2-verify")
    with _VERIFY_GUARD:
        _VERIFY_THREADS[:] = [x for x in _VERIFY_THREADS if x.is_alive()]
        _VERIFY_THREADS.append(t)
    t.start()


def verify_join(timeout: Optional[float] = None) -> None:
    """Wait for the background verify jobs of this process (tests, shutdown)."""
    with _VERIFY_GUARD:
        threads = list(_VERIFY_THREADS)
    for t in threads:
        t.join(timeout)


def _verify_task_job(cwd: str, scope: str, iid: str, token: str,
                     runner: Optional[VerifyRunner], now: Optional[float]) -> None:
    st = RunStore(cwd, scope)
    try:
        snap = Run.load(st, now)
        inst = snap.inst.get(iid)
        if not inst or inst.get("await") != AW_VERIFY \
                or (inst["frame"].get("verify") or {}).get("token") != token:
            return
        node = snap.node_of(iid)
        spec = verify_spec(node) or {"run": "", "timeout": DEFAULT_VERIFY_TIMEOUT}
        command, env = _verify_prepare(snap, iid, node, spec)
        res = _run_verify(runner, command, str(st.cwd), env, spec["timeout"])
    except (FlowError, StoreError) as exc:
        command, res = None, {"code": 127, "output": str(exc), "timed_out": False, "seconds": 0}

    def op(r: Run) -> None:
        inst = r.inst.get(iid)
        if not inst or inst.get("await") != AW_VERIFY \
                or (inst["frame"].get("verify") or {}).get("token") != token:
            return
        _verify_apply(r, iid, command, res)
    try:
        _mutate(cwd, scope, now, op)
    except (FlowError, StoreError):
        pass


def _verify_apply(r: Run, iid: str, command: Any, res: Dict[str, Any]) -> None:
    inst = r.inst[iid]
    n = r.node_of(iid)
    v = inst["frame"]["verify"]
    tail = _tail(res["output"])
    fields = dict(node=n.id, instance=iid, command=command, exit=res["code"],
                  seconds=res["seconds"])
    if res["code"] != 0:
        inst["frame"].pop("verify", None)
        inst["await"] = AW_TASK
        why = (f"timed out after {v.get('timeout') or 0:g}s" if res["timed_out"]
               else f"exit {res['code']}")
        inst["frame"]["last_verify"] = {"exit": res["code"], "why": why, "tail": tail,
                                        "command": command}
        r.emit("verify_failed", tail=tail, timed_out=res["timed_out"] or None, **fields)
        return
    r.emit("verify_passed", **fields)
    if _halted(r):
        v["passed"] = True  # applied when the halt is released
        return
    inst["frame"].pop("verify", None)
    inst["frame"].pop("last_verify", None)
    r.emit("reported", node=n.id, instance=iid, outcome=v["outcome"], value=v.get("value"),
           text=v.get("text"), by=v.get("by"))
    _complete(r, iid, v["outcome"], v.get("value"))


def _task_for_report(r: Run, node: Optional[str]) -> str:
    """The task instance a report names; an instance still waiting for its
    ``ask`` (or its verify) is refused with what it waits on."""
    try:
        return _find(r, node, (AW_TASK,), "task")
    except FlowError:
        held = [iid for iid, inst in r.inst.items()
                if inst.get("await") in (AW_APPROVAL, AW_VERIFY)
                and (node in (None, "", iid, inst["node"], r.node_of(iid).label))]
        if len(held) == 1:
            if r.inst[held[0]].get("await") == AW_VERIFY:
                raise FlowError(f"task {r.node_of(held[0]).label or held[0]} was reported and "
                                f"its verify is running; call flow_status later") from None
            rec = (r.state.get("asks") or {}).get(r.inst[held[0]]["frame"].get("ask") or "")
            how = _ask_how(held[0], rec) if rec else ""
            raise FlowError(f"task {r.node_of(held[0]).label or held[0]} is not entered yet: "
                            f"it waits for its ask ({how})") from None
        raise


def report(node: Optional[str], outcome: str = ir.OUTCOME_OK, value: Any = None,
           text: Optional[str] = None, *, cwd: Optional[str] = None,
           scope: Optional[str] = None, now: Optional[float] = None,
           verify_runner: Optional[VerifyRunner] = None,
           by: Optional[str] = None) -> Dict[str, Any]:
    """Report an active task's result and advance.

    ``by`` names a reporter other than the driver -- ``user`` for a person
    forcing the task's outcome from the dashboard or the CLI (docs 2.11). It
    is journaled on ``reported`` (and ``verify_started``); the result is
    checked, verified and routed exactly as the driver's would be.

    An ``ok`` report of a task with ``verify`` does not apply at once: the
    task goes to ``verifying`` (journal ``verify_started``) and the command
    runs in the background, outside the run lock. This call returns at once
    with ``verifying``. When the command exits 0 the report applies
    (``verify_passed``, ``reported``); otherwise the task is active again with
    the output's tail in ``last_verify`` (``verify_failed``)."""
    job: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_progress(r)
        if by is None:
            _require_no_move_request(r)
        iid = _task_for_report(r, node)
        _report_task(r, iid, outcome, value, text, by, verify_runner, now, job)
        if by is not None:
            # A person's report answers a pending request to report this
            # same task: nothing is left for that request to grant.
            pending = pending_move(r.state)
            if pending and pending.get("kind") == MOVE_KIND_REPORT \
                    and pending.get("instance") == iid:
                r.emit("move_superseded", request=pending.get("id"), node=pending.get("node"),
                       reported=iid, by=by)
                r.state.pop(MOVE_REQUEST, None)
    _mutate(cwd, scope, now, op)
    return _with_verify_note(status(cwd=cwd, scope=scope, now=now, tick=False), job)


def _check_report(r: Run, iid: str, outcome: str, value: Any) -> ir.Node:
    """The task ``iid`` may be reported with ``outcome``/``value`` now:
    its restart (if any) is done and the result matches its ``returns``."""
    n = r.node_of(iid)
    _require_restart_done(r, iid, n)
    errors = validate_result(returns_of(n), outcome, value,
                             qtypes=(r.chart.meta or {}).get("question_types") or {},
                             params=r.env(iid).get("params"))
    if errors:
        raise FlowError(f"report for {n.label or n.id} refused: " + "; ".join(errors)
                        + f". Declared returns: {json.dumps(returns_of(n))}")
    return n


def _report_task(r: Run, iid: str, outcome: str, value: Any, text: Optional[str],
                 by: Optional[str], verify_runner: Optional[VerifyRunner],
                 now: Optional[float], job: Dict[str, Any]) -> None:
    """Apply a report of the active task ``iid`` inside a mutation: an ``ok``
    of a task with ``verify`` starts the verify (``job`` is filled), any other
    report completes the task at once."""
    n = _check_report(r, iid, outcome, value)
    spec = verify_spec(n) if outcome == ir.OUTCOME_OK else None
    if spec:
        token = secrets.token_hex(4)
        r.inst[iid]["await"] = AW_VERIFY
        r.inst[iid]["frame"]["verify"] = {
            "token": token, "started_at": r.now, "timeout": spec["timeout"],
            "outcome": outcome, "value": value, "text": text, "by": by,
        }
        r.emit("verify_started", node=n.id, instance=iid, timeout=spec["timeout"], by=by)
        job.update(instance=iid, task=n.label or n.id, timeout=spec["timeout"])
        st = r.store
        r.after.append(lambda: _launch(_verify_task_job, str(st.cwd), st.scope, iid, token,
                                       verify_runner, now))
        return
    r.emit("reported", node=n.id, instance=iid, outcome=outcome, value=value, text=text,
           by=by)
    _complete(r, iid, outcome, value)


def _with_verify_note(payload: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    if job:
        payload["verifying"] = job
        payload["note"] = (f"task {job['task']} is verifying (up to {job['timeout']:g}s, in the "
                           f"background); the report applies when it passes. Call flow_status "
                           f"later: a failure puts the task back with the output")
    return payload


def select(node: Optional[str], option: str, reason: Optional[str] = None, *,
           caller: str = CALLER_AGENT, cwd: Optional[str] = None,
           scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Pick an option of an active ``choose``."""
    def op(r: Run) -> None:
        _require_progress(r)
        if caller != CALLER_USER:
            _require_no_move_request(r)
        iid = _find(r, node, (AW_CHOOSE,), "choice")
        n = r.node_of(iid)
        chooser = n.get("chooser") or ir.CHOOSER_AGENT
        if chooser == ir.CHOOSER_USER and caller != CALLER_USER:
            raise FlowError(
                f"choice {n.label or n.id} is the user's (chooser: user). Present the "
                f"options and your recommendation to the user; they pick with "
                f"'claunch cflow2 select {iid} <option>'"
            )
        opts = {o["name"]: o for o in n.get("options") or []}
        if option not in opts:
            raise FlowError(f"no option {option!r} (options: {', '.join(opts)})")
        opt = opts[option]
        try:
            allowed = _guard(r, iid, opt.get("guard"))
        except expr_mod.ExprError as exc:
            raise FlowError(f"option {option!r} guard could not be evaluated: {exc}")
        if not allowed:
            raise FlowError(f"option {option!r} is not available now (when: {opt.get('guard')})")
        # a person may always answer without a reason (docs 2.11); the
        # requirement is the agent's
        if (n.get("require_reason") and caller != CALLER_USER
                and not (reason or "").strip()):
            raise FlowError(f"choice {n.label or n.id} requires a reason")
        frame = r.inst[iid]["frame"]
        held = frame.get("held")
        opens = _window_opens(r, n, opt)
        if caller != CALLER_USER and opens is not None and r.now < opens:
            if held and held.get("option") == option:
                held["reason"] = reason
                held["updated"] = r.now
                r.emit("select_held", node=n.id, instance=iid, option=option, reason=reason,
                       opens_at=opens, updated=True)
            else:
                if held:
                    r.emit("hold_cancelled", node=n.id, instance=iid, option=held.get("option"),
                           why=f"{caller} chose {option}")
                frame["held"] = {"option": option, "reason": reason, "opens_at": opens,
                                 "since": r.now, "by": caller}
                r.emit("select_held", node=n.id, instance=iid, option=option, reason=reason,
                       opens_at=opens)
            out["held"] = dict(frame["held"])
            return
        if held:
            frame.pop("held", None)
            r.emit("hold_cancelled", node=n.id, instance=iid, option=held.get("option"),
                   why=f"{caller} chose {option}")
        override = caller == CALLER_USER and opens is not None and r.now < opens
        _take_option(r, iid, n, opt, reason, caller, override=override)
    out: Dict[str, Any] = {}
    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    if out.get("held"):
        h = out["held"]
        payload["held"] = h
        payload["note"] = (f"option {h['option']} is paced (interval): held until the window "
                           f"opens at {_iso(h['opens_at'])}. End your turn -- the daemon applies "
                           f"it then and wakes you. Choosing it again only updates the reason; "
                           f"choosing another option cancels the hold")
    return payload


def _iso(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts)


def _window_key(node: ir.Node, option: str) -> str:
    return f"{node_key(node)}:{option}"


def _window_opens(r: Run, node: ir.Node, opt: Dict[str, Any]) -> Optional[float]:
    """When ``opt`` may be taken again (its ``interval`` after the slot's last
    take), or ``None`` for an unpaced option or one never taken."""
    interval = opt.get("interval")
    if not interval:
        return None
    last = (r.state.get("windows") or {}).get(_window_key(node, opt["name"]))
    if last is None:
        return None
    return float(last) + float(interval)


def _take_option(r: Run, iid: str, n: ir.Node, opt: Dict[str, Any], reason: Optional[str],
                 by: str, override: bool = False) -> None:
    option = opt["name"]
    r.inst[iid]["await"] = None
    r.inst[iid]["frame"]["option"] = option
    if opt.get("interval"):
        r.state.setdefault("windows", {})[_window_key(n, option)] = r.now
    r.emit("selected", node=n.id, instance=iid, option=option, reason=reason, by=by,
           override=True if override else None)
    if by == CALLER_USER:
        picks = r.state.setdefault(PERSON_PICKS, [])
        picks.append({"instance": iid, "node": n.id, "label": n.label or n.id,
                      "option": option, "reason": reason, "at": _iso(r.now)})
        del picks[:-PERSON_PICKS_KEPT]
    if opt.get("child"):
        _enter_child(r, iid, opt["child"], "option")
    else:
        _complete(r, iid, ir.OUTCOME_OK, None)


def person_pick_words(payload: Dict[str, Any], option: str) -> Dict[str, str]:
    """What a nudge says about the pick a person just made, read from the
    status ``payload`` that :func:`select` returned: ``label`` (the choice)
    and ``note`` (`` -- their note: ...`` or empty). The note is cut at
    :data:`NUDGE_NOTE_LIMIT`; flow_status keeps it whole."""
    picks = payload.get(PERSON_PICKS) or []
    last = picks[-1] if picks and picks[-1].get("option") == option else {}
    reason = (last.get("reason") or "").strip()
    if len(reason) > NUDGE_NOTE_LIMIT:
        reason = reason[:NUDGE_NOTE_LIMIT] + f" ... (cut; whole note in {PERSON_PICKS})"
    return {"label": str(last.get("label") or "the open choice"),
            "note": f" -- their note: {reason}" if reason else ""}


def _release_hold(r: Run, iid: str, node: ir.Node) -> bool:
    """A held choice whose window opened: take it with the latest reason
    (``by: window``), or drop the hold when its option is no longer
    available. True when something changed."""
    frame = r.inst[iid]["frame"]
    held = frame.get("held") or {}
    if r.now < float(held.get("opens_at") or 0):
        return False
    frame.pop("held", None)
    opt = next((o for o in node.get("options") or [] if o["name"] == held.get("option")), None)
    why = None
    if opt is None:
        why = "the option no longer exists"
    else:
        try:
            if not _guard(r, iid, opt.get("guard")):
                why = f"its guard is false now (when: {opt.get('guard')})"
        except expr_mod.ExprError as exc:
            why = f"its guard could not be evaluated: {exc}"
    if why:
        r.emit("hold_cancelled", node=node.id, instance=iid, option=held.get("option"), why=why)
        return True
    _take_option(r, iid, node, opt, held.get("reason"), "window")
    return True


def fork(group: Optional[str], with_: Optional[Dict[str, Any]] = None, *,
         cwd: Optional[str] = None, scope: Optional[str] = None,
         now: Optional[float] = None) -> Dict[str, Any]:
    """Add a member to a running ``group`` (its ``do`` must still be running).
    ``with`` may name only the procedure's declared params (none declared:
    no key at all); a required param without a default needs a non-null
    value."""
    out: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_progress(r)
        _require_no_move_request(r)
        groups = [iid for iid, inst in r.inst.items()
                  if r.chart.node(inst["node"]).type == ir.T_GROUP]
        if group and group in groups:
            gid = group
        else:
            hits = [g for g in groups if group in (None, "", r.inst[g]["node"],
                                                   r.node_of(g).label, r.node_of(g).get("name"))]
            if len(hits) != 1:
                raise FlowError(
                    f"no single running group {group!r} (running: {', '.join(groups) or 'none'})")
            gid = hits[0]
        n = r.node_of(gid)
        frame = r.inst[gid]["frame"]
        if frame.get("phase") != "do":
            raise FlowError(f"group {n.get('name')} is joining; members can be added only while "
                            f"its do-part runs")
        limit = int(n.get("limit") or 0)
        if limit and len(frame["members"]) >= limit:
            raise FlowError(f"group {n.get('name')} is at its limit of {limit} members")
        if with_ is not None and not isinstance(with_, dict):
            raise FlowError("'with' must be an object of parameter -> value")
        args = dict(with_ or {})
        decls = r.chart.node(n.get("member")).get("params") or {}
        unknown = sorted(set(args) - set(decls))
        if unknown:
            raise FlowError(f"group {n.get('name')}: fork refused -- unknown param(s) "
                            f"{', '.join(unknown)} (declared: {', '.join(decls) or 'none'})")
        missing = [k for k, d in decls.items()
                   if (d or {}).get("required") and _absent(args.get(k))
                   and "default" not in (d or {})]
        if missing:
            raise FlowError(f"group {n.get('name')}: fork refused -- missing required "
                            f"param(s) {', '.join(missing)} of procedure "
                            f"{r.chart.node(n.get('member')).get('proc')}; pass them in 'with' "
                            f"(null is no value)")
        # registered before entering: a member may finish synchronously
        member = f"{n.get('member')}@{int(r.state.get('seq') or 0) + 1}"
        frame["members"].append({"instance": member, "with": args})
        r.emit("group_fork", node=n.id, instance=gid, member=member, args=args)
        mid = _enter(r, n.get("member"), gid, "member", {"params": args})
        out.update({"group": gid, "member": mid})
    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["forked"] = out
    return payload


# -- dynamic calls (doc 2.12) ---------------------------------------------------

#: A call whose callee run has not finished.
CALL_OPEN = "running"
#: Calls deep: a callee may call again, up to this many frames below the
#: run that started the chain.
CALL_DEPTH_MAX = 4


def call_scope(scope: str, cid: str) -> str:
    return f"{scope}{store_mod.NESTED_SEP}{cid}"


def _call_chain(state: Dict[str, Any], _depth: int = 0) -> List[str]:
    """The procedures called above this run (outermost first). A callee's
    link carries its chain; a nested delegation run inherits the chain of
    the run that delegated it, so recursion and depth are counted across
    delegations too."""
    link = state.get("parent") or {}
    if link.get("call"):
        return list(link.get("chain") or [])
    if not link or _depth >= 16:
        return []
    try:
        return _call_chain(RunStore(link["cwd"], link["scope"]).load_state(), _depth + 1)
    except (StoreError, KeyError, TypeError):
        return []


def call(procedure: str, node: Optional[str] = None, with_: Optional[Dict[str, Any]] = None,
         *, cwd: Optional[str] = None, scope: Optional[str] = None,
         now: Optional[float] = None) -> Dict[str, Any]:
    """An active task calls ``procedure`` (doc 2.12): the definition is
    resolved by name from the run's layers now, compiled, and started as a
    nested run ``<scope>~c<n>`` that the driver drives with the flow_* tools.
    The task waits (``calling``) until the callee finishes, then is active
    again with ``call_result`` (outcome, value) in its status entry; it
    reports as usual. The task must list the procedure in ``calls``
    (default deny); one open call per task; no procedure twice in a chain
    and at most :data:`CALL_DEPTH_MAX` frames."""
    procedure = str(procedure or "").strip()
    from . import layers
    if not layers.is_name(procedure):
        raise FlowError(f"call {procedure!r}: give a procedure name, not a path")
    if with_ is not None and not isinstance(with_, dict):
        raise FlowError("'with' must be an object of parameter -> value")
    out: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_progress(r)
        _require_no_move_request(r)
        iid = _find(r, node, (AW_TASK,), "task")
        tnode = r.node_of(iid)
        allowed = tnode.get("calls") or []
        if procedure not in allowed:
            raise FlowError(
                f"refused: task {shown_label(r.chart, tnode)} may not call {procedure!r} "
                f"(its calls: {', '.join(allowed) or 'none declared'})")
        chain = _call_chain(r.state)
        if procedure in chain:
            raise FlowError(f"refused: recursive call: {' -> '.join(chain + [procedure])}")
        if len(chain) >= CALL_DEPTH_MAX:
            raise FlowError(f"refused: call depth {CALL_DEPTH_MAX} reached "
                            f"({' -> '.join(chain)})")
        try:
            chart = layers.load(procedure, str(r.store.cwd))
        except Exception as exc:  # noqa: BLE001 - compile/lookup errors refuse the call
            raise FlowError(f"call {procedure!r}: {exc}") from None
        if chart.kind != ir.KIND_PROCEDURE:
            raise FlowError(f"call {procedure!r}: {chart.name} is a {chart.kind}, not a "
                            f"procedure (kind: procedure)")
        blocks = [k for k, d in (chart.params or {}).items()
                  if (d or {}).get("type") == "block"]
        if blocks:
            raise FlowError(f"call {procedure!r}: block parameter(s) {', '.join(blocks)} "
                            f"need a call site; a dynamic call has none")
        params = _validate_params(chart, with_)
        # the session that drives the calling run drives the callee: the
        # driver, or in a session-delegated nested run its assigned child
        driver = acting_session(r.state)
        if not driver and r.state.get("parent"):
            raise FlowError(f"call {procedure!r}: no session drives this nested run yet, "
                            f"so nobody could drive the callee")
        calls = r.state.setdefault("calls", {})
        cid = f"c{len(calls) + 1}"
        sub = call_scope(r.store.scope, cid)
        returns = json.loads(json.dumps(chart.returns or {ir.OUTCOME_OK: {}}))
        rec: Dict[str, Any] = {
            "id": cid, "instance": iid, "node": tnode.id,
            "label": shown_label(r.chart, tnode), "procedure": procedure,
            "with": params, "status": CALL_OPEN, "started": store_mod.utcnow(),
            "finished": None, "nested": {"cwd": str(r.store.cwd), "scope": sub},
            "returns": returns, "source": chart.source,
        }
        calls[cid] = rec
        frame = r.inst[iid]["frame"]
        frame.pop("call_result", None)
        frame["call"] = cid
        r.inst[iid]["await"] = AW_CALL
        r.emit("call_started", node=tnode.id, instance=iid, call=cid, procedure=procedure,
               scope=sub, params=params)
        link = {"cwd": str(r.store.cwd), "scope": r.store.scope, "call": cid,
                "run": r.state.get("run_id"), "chain": chain + [procedure]}
        st = RunStore(str(r.store.cwd), sub)
        data = json.loads(json.dumps(r.state.get("data") or {}, default=str))
        with st.lock():
            if st.exists():
                _abort_nested({"cwd": str(st.cwd), "scope": sub}, locked=True)
                st.archive()
            elif st.chart_path.exists():
                st.chart_path.unlink()
            child = _new_run(st, chart, params=params, data=data, caps=r.state.get("caps"),
                             driver=driver, now=r.now, parent=link)
            done = child.state.get("status") in FINISHED_STATUSES
            if done:
                child.state["delivered"] = True  # applied below, in this lock
            child.save()
            child.after = []
            result = (child.state.get("status"), child.state.get("outcome"),
                      child.state.get("value"))
        out.update({"call": cid, "instance": iid, "procedure": procedure, "scope": sub})
        if done:
            _call_result(r, rec, *result)
    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["called"] = out
    return payload


def _call_result(r: Run, rec: Dict[str, Any], status_: Optional[str], outcome: Any,
                 value: Any) -> None:
    """A callee run finished: its root outcome/value, checked against the
    procedure's ``returns``, goes back to the calling task, which is active
    again. A result that does not match, or an aborted callee, comes back
    as ``fail`` with the reason; the task decides what to report."""
    if rec.get("status") != CALL_OPEN:
        return
    if status_ == ST_ABORTED:
        outcome, value = ir.OUTCOME_FAIL, {"reason": "the callee run was aborted"}
    else:
        outcome = str(outcome or ir.OUTCOME_FAIL)
        errors = validate_result(rec.get("returns") or {ir.OUTCOME_OK: {}}, outcome, value)
        if errors:
            outcome, value = ir.OUTCOME_FAIL, {
                "reason": f"procedure {rec['procedure']} returned a result that does not "
                          f"match its returns: {'; '.join(errors)}"}
    rec.update({"status": "returned" if status_ == ST_DONE else "failed",
                "finished": store_mod.utcnow(), "outcome": outcome, "value": value})
    r.emit("call_returned", call=rec["id"], node=rec["node"], instance=rec["instance"],
           procedure=rec["procedure"], outcome=outcome)
    inst = r.inst.get(rec["instance"])
    if inst is None or inst.get("await") != AW_CALL or inst["frame"].get("call") != rec["id"]:
        return
    inst["frame"].pop("call", None)
    inst["frame"]["call_result"] = {"call": rec["id"], "procedure": rec["procedure"],
                                    "outcome": outcome, "value": value}
    inst["await"] = AW_TASK


OPEN_DELEGATION = ("pending", "queued", "running")


def delegate_submit(delegation: str, outcome: Optional[str] = None, value: Any = None,
                    receipt_path: Optional[str] = None, *, cwd: Optional[str] = None,
                    scope: Optional[str] = None, now: Optional[float] = None,
                    session: Optional[str] = None, via: str = VIA_INTERNAL,
                    verify_runner: Optional[VerifyRunner] = None) -> Dict[str, Any]:
    """Submit a delegate's result (directly, or as a receipt file to read).

    ``via`` is who calls: ``internal`` (the daemon's executors) or ``mcp``
    (an agent; ``session`` is its identity). An agent may submit a
    ``subagent`` delegation only as the run's driver and a ``session``
    delegation only as its assigned child session; a nested run's result
    only from the nested run. A receipt is read only from the run's receipts directory or
    ``$CLAUNCH_SCRATCH``.

    An invalid result is refused once with the problems listed (the delegate
    is re-run with them); a second invalid one fails the delegation and the
    node finishes ``fail`` (``delegate_failed``). An ``ok`` result of a flat
    subtree whose tasks declare ``verify`` is accepted only after those
    commands pass; they run in the background (``verifying``) and a failure
    counts as an invalid result.
    """
    job: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_live(r)
        if via != VIA_INTERNAL:
            _require_progress(r)
        dels = r.state.get("delegations", {})
        rec = dels.get(delegation)
        if rec is None:
            raise FlowError(f"no delegation {delegation!r} (known: {', '.join(dels) or 'none'})")
        if rec.get("nested"):
            # Only the nested run's own finish (deliver_nested) settles it;
            # a receipt on any door would skip its gates and verify (G1).
            raise FlowError(
                f"delegation {delegation} runs as the nested run {rec['nested']['scope']!r}; "
                f"its result comes only from that run finishing")
        if via == VIA_MCP:
            _check_delegation_authority(r, rec, session or "", "submit")
        if rec["status"] not in OPEN_DELEGATION:
            raise FlowError(f"delegation {delegation} is {rec['status']}; nothing to submit")
        if rec.get("held"):
            raise FlowError(f"delegation {delegation} already has a result, kept until the "
                            f"run's halt is released")
        res_outcome, res_value = outcome, value
        source = "direct"
        if receipt_path:
            path = Path(receipt_path)
            if not path.is_absolute():
                path = r.store.cwd / path
            if not _receipt_ok(r, path):
                raise FlowError(f"receipt {path} refused: a receipt is read only from "
                                f"{r.store.receipts_dir} or this session's $CLAUNCH_SCRATCH")
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FlowError(f"cannot read receipt {path}: {exc}")
            if isinstance(doc, dict) and "outcome" in doc:
                res_outcome = res_outcome or doc.get("outcome")
                if res_value is None:
                    res_value = doc.get("value")
            elif res_value is None:
                res_value = doc
            source = str(path)
        res_outcome = res_outcome or ir.OUTCOME_OK
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        errors = validate_result(rec.get("returns") or {ir.OUTCOME_OK: {}}, res_outcome, res_value)
        if errors:
            if _reject(r, rec, errors):
                r.deferred_error = (
                    f"delegation {delegation} result refused: {'; '.join(errors)}. Re-run the "
                    f"delegate once with these problems and submit again; a second invalid "
                    f"result fails the node. Declared returns: {json.dumps(rec.get('returns'))}"
                )
            return
        verifies = _delegation_verifies(r, rec) \
            if res_outcome == ir.OUTCOME_OK and not rec.get("nested") else []
        if verifies:
            token = secrets.token_hex(4)
            total = sum((verify_spec(n) or {}).get("timeout") or 0 for n in verifies)
            rec["verify"] = {"token": token, "started_at": r.now, "timeout": total,
                             "outcome": res_outcome, "value": res_value, "source": source,
                             "before": rec["status"]}
            rec["status"] = "verifying"
            r.emit("verify_started", delegation=delegation, node=rec["node"],
                   tasks=[n.label or n.id for n in verifies], timeout=total)
            job.update(delegation=delegation, tasks=[n.label or n.id for n in verifies])
            st = r.store
            r.after.append(lambda: _launch(_verify_delegation_job, str(st.cwd), st.scope,
                                           delegation, token, verify_runner, now))
            return
        _accept(r, rec, res_outcome, res_value, source)
    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    if job:
        payload["verifying"] = job
        payload["note"] = (f"delegation {delegation}: the verify of {', '.join(job['tasks'])} "
                           f"runs in the background; the result is accepted when it passes. "
                           f"Call flow_status later")
    return payload


def executor_submit(delegation: str, outcome: Optional[str] = None, value: Any = None, *,
                    receipt_path: Optional[str] = None, cwd: Optional[str] = None,
                    scope: Optional[str] = None, now: Optional[float] = None,
                    verify_runner: Optional[VerifyRunner] = None) -> Dict[str, Any]:
    """The daemon's door for a delegation result (a session child's receipt
    picked up by the clock). Never offered through
    MCP: an agent's submission goes through :func:`delegate_submit` with
    ``via='mcp'`` and its authority checks. A result that arrives while a
    monitor halts the run is kept and applied on resume."""
    return delegate_submit(delegation, outcome, value, receipt_path, cwd=cwd, scope=scope,
                           now=now, via=VIA_INTERNAL, verify_runner=verify_runner)


def _verify_delegation_job(cwd: str, scope: str, did: str, token: str,
                           runner: Optional[VerifyRunner], now: Optional[float]) -> None:
    st = RunStore(cwd, scope)
    results: List[Tuple[ir.Node, Any, Dict[str, Any]]] = []
    try:
        snap = Run.load(st, now)
        rec = (snap.state.get("delegations") or {}).get(did)
        if not rec or rec.get("status") != "verifying" \
                or (rec.get("verify") or {}).get("token") != token:
            return
        iid = rec["instance"]
        for n in _delegation_verifies(snap, rec):
            spec = verify_spec(n) or {"run": "", "timeout": DEFAULT_VERIFY_TIMEOUT}
            try:
                command, env = _verify_prepare(snap, iid, n, spec)
            except FlowError as exc:
                results.append((n, None, {"code": 127, "output": str(exc), "timed_out": False,
                                          "seconds": 0}))
                break
            res = _run_verify(runner, command, str(st.cwd), env, spec["timeout"])
            results.append((n, command, res))
            if res["code"] != 0:
                break
    except StoreError:
        return

    def op(r: Run) -> None:
        rec = (r.state.get("delegations") or {}).get(did)
        if not rec or rec.get("status") != "verifying" \
                or (rec.get("verify") or {}).get("token") != token:
            return
        v = rec.pop("verify")
        rec["status"] = v.get("before") or "pending"
        errors = []
        for n, command, res in results:
            fields = dict(node=n.id, delegation=did, command=command, exit=res["code"],
                          seconds=res["seconds"])
            if res["code"] != 0:
                tail = _tail(res["output"])
                r.emit("verify_failed", tail=tail, timed_out=res["timed_out"] or None, **fields)
                errors.append(f"verify of task {n.label or n.id} failed "
                              f"({'timed out' if res['timed_out'] else 'exit ' + str(res['code'])}"
                              f"): {tail[-1500:]}")
            else:
                r.emit("verify_passed", **fields)
        if errors:
            _reject(r, rec, errors)
            return
        _accept(r, rec, v["outcome"], v.get("value"), v.get("source") or "direct")
    try:
        _mutate(cwd, scope, now, op)
    except (FlowError, StoreError):
        pass


def _reject(r: Run, rec: Dict[str, Any], errors: List[str]) -> bool:
    """Record an invalid attempt (``rec['attempts']`` already counts it).

    The first one is refused for a re-run (True); the second fails the
    delegation and the node finishes ``fail`` (False)."""
    did = rec["id"]
    rec.setdefault("errors", []).append(errors)
    if int(rec.get("attempts") or 0) < 2:
        r.emit("delegate_failed", delegation=did, node=rec["node"], errors=errors, retry=True)
        return True
    rec["status"] = "failed"
    rec["finished"] = store_mod.utcnow()
    r.emit("delegate_failed", delegation=did, node=rec["node"], errors=errors)
    _complete(r, rec["instance"], ir.OUTCOME_FAIL, {"reason": "delegate result invalid twice",
                                                    "errors": errors})
    return False


def _open_delegation(r: Run, delegation: str) -> Dict[str, Any]:
    dels = r.state.get("delegations", {})
    rec = dels.get(delegation)
    if rec is None:
        raise FlowError(f"no delegation {delegation!r} (known: {', '.join(dels) or 'none'})")
    if rec["status"] not in OPEN_DELEGATION:
        raise FlowError(f"delegation {delegation} is {rec['status']}; nothing to do")
    return rec


def delegate_reject(delegation: str, errors: List[str], *, cwd: Optional[str] = None,
                    scope: Optional[str] = None, now: Optional[float] = None) -> bool:
    """An executor's attempt produced no valid result (nothing extractable, or
    a value that fails ``returns``). Counted exactly like an invalid
    :func:`delegate_submit`: True means "re-run once with these errors",
    False means the delegation failed and the node finished ``fail``."""
    out: Dict[str, bool] = {}

    def op(r: Run) -> None:
        _require_live(r)
        rec = _open_delegation(r, delegation)
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        out["retry"] = _reject(r, rec, list(errors) or ["no result"])
    _mutate(cwd, scope, now, op)
    return out.get("retry", False)


def delegate_fail(delegation: str, reason: str, *, outcome: str = ir.OUTCOME_FAIL,
                  detail: Optional[Dict[str, Any]] = None, cwd: Optional[str] = None,
                  scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """End an open delegation without a result: ``timeout`` (budget.seconds
    ran out) or ``fail`` (the executor is gone). Journaled as
    ``delegate_failed`` with the reason; the node finishes with ``outcome``."""
    def op(r: Run) -> None:
        _require_live(r)
        rec = _open_delegation(r, delegation)
        rec["status"] = "timeout" if outcome == ir.OUTCOME_TIMEOUT else "failed"
        rec["finished"] = store_mod.utcnow()
        rec.setdefault("errors", []).append([reason])
        r.emit("delegate_failed", delegation=delegation, node=rec["node"], reason=reason,
               outcome=outcome)
        value = {"reason": reason}
        value.update(detail or {})
        if rec.get("nested"):
            r.nested_aborts.append(dict(rec["nested"]))
        _complete(r, rec["instance"], outcome, value)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def delegate_update(delegation: str, *, expect: Optional[Tuple[str, ...]] = None,
                    event: Optional[Dict[str, Any]] = None, cwd: Optional[str] = None,
                    scope: Optional[str] = None, now: Optional[float] = None,
                    **fields: Any) -> bool:
    """Set fields on an open delegation record (an executor claiming it,
    the child session it runs in). With ``expect`` this is a compare-and-set
    on the status: False (nothing written) when the record moved on."""
    out = {"ok": False}

    def op(r: Run) -> None:
        if r.status not in LIVE_STATUSES:
            return
        rec = r.state.get("delegations", {}).get(delegation)
        if rec is None or rec["status"] not in OPEN_DELEGATION:
            return
        if expect is not None and rec["status"] not in expect:
            return
        rec.update(fields)
        if event:
            ev = dict(event)
            r.emit(ev.pop("event"), delegation=delegation, node=rec["node"], **ev)
        out["ok"] = True
    _mutate(cwd, scope, now, op)
    return out["ok"]


def delegate_reroute(delegation: str, cause: str, *, cwd: Optional[str] = None,
                     scope: Optional[str] = None, now: Optional[float] = None) -> str:
    """The executor a delegation got cannot run it after all (spawn policy
    refused a session, no owning session to take a profile from): move it to
    the next executor of the node's preference list whose capabilities hold,
    journaled as ``fallback``. ``inline`` expands the subtree in place.
    Returns the new executor."""
    out = {"executor": ir.EXEC_INLINE}

    def op(r: Run) -> None:
        _require_live(r)
        rec = _open_delegation(r, delegation)
        node = r.chart.node(rec["node"])
        flat = bool(rec.get("flat", True))
        prefs = list(node.executor or [])
        old = rec["executor"]
        if old in prefs:
            rest = prefs[prefs.index(old) + 1:]
        else:
            # A removed executor (an old run's record) is no longer in the
            # node's list: every executor the node still names comes next.
            rest = prefs if old in ir.REMOVED_EXECUTORS else []
        nxt = ir.EXEC_INLINE
        if caps_mod is not None:
            try:
                nxt = caps_mod.choose_executor(rest, r.caps, needs=node.needs or [],
                                               flat=flat).executor
            except TypeError:
                nxt = caps_mod.choose_executor(rest, r.caps, needs=node.needs or []).executor
        if nxt in executors.DISPATCHED and not executors.available(str(r.store.cwd)):
            nxt = _next_undispatched(node, nxt, r.caps, flat)
        out["executor"] = nxt
        iid = rec["instance"]
        r.emit("fallback", node=node.id, instance=iid, delegation=delegation, cause=cause,
               route=f"{old} -> {nxt}", decision="executor")
        if nxt == ir.EXEC_INLINE:
            rec["status"] = "rerouted"
            rec["finished"] = store_mod.utcnow()
            if rec.get("nested"):
                r.nested_aborts.append(dict(rec["nested"]))
            r.inst[iid]["await"] = None
            r.inst[iid]["frame"].pop("delegation", None)
            _ENTER[node.type](r, iid, node)
            return
        plan = _plan(_with_executor(node, nxt), r.caps, flat)
        rec.update({"executor": nxt, "route": plan.to_dict(), "started_at": r.now,
                    "status": "pending" if nxt == ir.EXEC_SUBAGENT else "queued",
                    "child": None, "runner": None, "assignee": None})
        r.emit("delegate_started", node=node.id, instance=iid, delegation=delegation,
               executor=nxt, route=nxt, rerouted_from=old)
        if rec.get("nested") and nxt == ir.EXEC_SUBAGENT:
            # the nested run's driver becomes the delegating run's driver
            try:
                nst = RunStore(rec["nested"]["cwd"], rec["nested"]["scope"])
                with nst.lock():
                    ns = nst.load_state()
                    ns["driver"] = driver_of(r.state)
                    nst.save_state(ns)
            except (StoreError, OSError):
                pass
        if nxt in executors.DISPATCHED:
            executors.dispatch(rec, r)
    _mutate(cwd, scope, now, op)
    return out["executor"]


def set_input(name: str, value: Any, *, session: str = "", cwd: Optional[str] = None,
              scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Write a started run's empty start input ``name`` (docs/cflow-v2.md
    2.6): ``data.inputs.<name>`` takes ``value``, checked by
    :func:`.inputs.settle` -- the input's ``set_by`` names ``agent``, it is
    still empty, and the value passes the start's type, enum and constraint
    checks. Only the run's driver writes (:func:`may_drive`). Journal
    ``input_set {name, value, by, was}``. Returns the status payload."""
    from . import inputs as inputs_mod

    session = str(session or "").strip()

    def op(r: Run) -> None:
        _require_progress(r)
        if not may_drive(r.state, session):
            raise FlowError(f"run {r.state.get('run_id')} is driven by "
                            f"{driver_of(r.state) or 'no session'}; "
                            f"{session or 'an unmanaged caller'} may not write its inputs")
        meta = r.chart.meta or {}
        data = r.state.setdefault("data", {})
        current = dict(data.get("inputs") or {})
        try:
            got = inputs_mod.settle(meta.get("inputs") or {}, current, name, value,
                                    inputs_mod.SETTERS[0], meta.get("input_constraints"))
        except inputs_mod.InputsError as exc:
            raise FlowError(f"input not written: {exc}") from None
        was = current.get(name)
        current[name] = got
        data["inputs"] = current
        r.emit("input_set", name=name, value=got, by=session or "unmanaged")
        r.events[-1]["was"] = was  # kept when empty: what the write replaced
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def mesh_of(state: Dict[str, Any]) -> str:
    """The mesh a run's asks are routed in (``state.mesh``); empty when the
    run named none and the roster decides at each ask."""
    return str(state.get("mesh") or "").strip()


def set_mesh(mesh: str, *, session: str = "", caller: str = CALLER_AGENT,
             cwd: Optional[str] = None, scope: Optional[str] = None,
             now: Optional[float] = None) -> Dict[str, Any]:
    """Name the mesh this run's asks are routed in (claunch-5itbf).

    The driver needs it when it is in several team meshes and the roster
    cannot choose (``responders.Pool.ambiguous``). Asks that are still
    routing pick it up at their next routing; an ask already put to someone
    keeps the mesh it went out in. Only the run's driver (or a person,
    ``caller`` user) writes it. Empty clears it. Journal ``mesh_set {mesh,
    was, by}``. Returns the status payload."""
    name = str(mesh or "").strip()
    session = str(session or "").strip()

    def op(r: Run) -> None:
        if caller != CALLER_USER and not may_drive(r.state, session):
            raise FlowError(f"run {r.state.get('run_id')} is driven by "
                            f"{driver_of(r.state) or 'no session'}; "
                            f"{session or 'an unmanaged caller'} may not name its mesh")
        was = mesh_of(r.state)
        r.state["mesh"] = name
        for rec in (r.state.get("asks") or {}).values():
            if rec.get("status") == "open" and rec.get("resolution") == RES_ROUTING:
                rec["mesh"] = name
                rec["retry_at"] = None
        r.emit("mesh_set", mesh=name or None, was=was or None,
               by=session or ("user" if caller == CALLER_USER else "unmanaged"))
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def note(event: str, *, cwd: Optional[str] = None, scope: Optional[str] = None,
         **fields: Any) -> None:
    """Append one journal event to a run (an executor's own observation:
    a ``fallback`` taken while extracting a result, a ``monitor_violation``
    found by a porcelain check). Taken under the run lock like any write."""
    st = RunStore(cwd, scope)
    rec = {"ts": store_mod.utcnow(), "event": event}
    rec.update({k: v for k, v in fields.items() if v is not None})
    with st.lock():
        st.append([rec])


def delegate_brief(delegation: Optional[str] = None, *, cwd: Optional[str] = None,
                   scope: Optional[str] = None, session: Optional[str] = None,
                   via: str = VIA_INTERNAL) -> Dict[str, Any]:
    st = RunStore(cwd, scope)
    r = Run.load(st)
    dels = r.state.get("delegations", {})
    open_ids = [d for d, rec in dels.items() if rec["status"] in OPEN_DELEGATION]
    if not delegation:
        if len(open_ids) != 1:
            raise FlowError(f"name a delegation (open: {', '.join(open_ids) or 'none'})")
        delegation = open_ids[0]
    if delegation not in dels:
        raise FlowError(f"no delegation {delegation!r} (known: {', '.join(dels) or 'none'})")
    if via == VIA_MCP:
        _check_delegation_authority(r, dels[delegation], session or "", "brief")
    return brief(r, delegation)


def extend_loop(node: Optional[str] = None, by: int = 1, *, stop: bool = False,
                reset: bool = False, cwd: Optional[str] = None,
                scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Answer a ``loop_limit``: allow ``by`` more iterations, ``reset`` the
    count (the loop gets its full ``max`` again and earlier extensions are
    dropped; ``by`` is ignored), or ``stop`` the loop (it finishes ``fail``).
    Reset keeps the loop's place: a foreach goes on with its next item, a
    retry's ``attempt`` keeps counting, collected values stay."""
    def op(r: Run) -> None:
        _require_progress(r)
        if stop and reset:
            raise FlowError("reset or stop, not both")
        iid = _find(r, node, (AW_LIMIT,), "loop limit",
                    pred=lambda i: not r.inst[i]["frame"].get("visit_limit"))
        n = r.node_of(iid)
        frame = r.inst[iid]["frame"]
        if reset and _loop_count(frame) < 1:
            # A parallel foreach stops before any iteration because it has
            # more items than max; a reset there would stop again at once.
            raise FlowError("no iterations to reset: extend the loop (or stop it)")
        if not stop and not reset and by < 1:
            raise FlowError("extend by at least 1 (or stop the loop)")
        r.inst[iid]["await"] = None
        r.state["approvals"] = [a for a in r.state.get("approvals", [])
                                if a.get("instance") != iid]
        if stop:
            r.emit("selected", node=n.id, instance=iid, option="stop", kind=HOLD_LOOP)
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": "loop_limit", "iterations": frame["iter"]})
            return
        if reset:
            reset_from = _loop_count(frame)
            frame["base"] = int(frame.get("iter") or 0)
            frame["extra"] = 0
            r.emit("selected", node=n.id, instance=iid, option="reset", count=reset_from,
                   kind=HOLD_LOOP)
        else:
            frame["extra"] = int(frame.get("extra") or 0) + int(by)
            r.emit("selected", node=n.id, instance=iid, option="extend", by=by, kind=HOLD_LOOP)
        if frame.get("phase") == "limit_par":
            if len(frame["items"]) > _loop_max(r, iid):
                _loop_limit(r, iid)
                return
            _foreach_par_start(r, iid)
        elif frame["iter"] == 0:
            _loop_iterate(r, iid)
        else:
            _loop_between(r, iid)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def extend_visits(node: Optional[str] = None, by: int = 1, *, stop: bool = False,
                  cwd: Optional[str] = None, scope: Optional[str] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Answer a ``visit_limit``: allow the node ``by`` more visits over the
    run (its parked instance then starts), or ``stop`` it (the instance
    finishes ``fail``)."""
    def op(r: Run) -> None:
        _require_progress(r)
        iid = _find(r, node, (AW_LIMIT,), "visit limit",
                    pred=lambda i: bool(r.inst[i]["frame"].get("visit_limit")))
        n = r.node_of(iid)
        key = node_key(n)
        if not stop and by < 1:
            raise FlowError("extend by at least 1 (or stop)")
        inst = r.inst[iid]
        inst["await"] = None
        inst["frame"].pop("visit_limit", None)
        r.state["approvals"] = [a for a in r.state.get("approvals", [])
                                if a.get("instance") != iid]
        if stop:
            r.emit("selected", node=n.id, instance=iid, option="stop", kind=HOLD_VISIT)
            _complete(r, iid, ir.OUTCOME_FAIL,
                      {"reason": "visit_limit", "visits": r.state["visits"].get(key)})
            return
        extra = r.state.setdefault("visit_extra", {})
        extra[key] = int(extra.get(key) or 0) + int(by)
        r.emit("selected", node=n.id, instance=iid, option="extend", by=by, kind=HOLD_VISIT)
        _enter_body(r, iid, n)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def _abort(r: Run, reason: str) -> None:
    for iid in list(r.inst):
        if iid in r.inst and r.inst[iid].get("parent") is None:
            _drop(r, iid, "aborted")
    for rec in r.state.get("delegations", {}).values():
        if rec["status"] in OPEN_DELEGATION + ("verifying",):
            rec["status"] = "cancelled"
            if rec.get("nested"):
                r.nested_aborts.append(dict(rec["nested"]))
    r.state.update({"status": ST_ABORTED, "reason": reason, "approvals": [],
                    "finished": store_mod.utcnow()})
    r.emit("aborted", reason=reason)
    _reset_at(r, None)


def abort(reason: str, *, cwd: Optional[str] = None, scope: Optional[str] = None,
          now: Optional[float] = None) -> Dict[str, Any]:
    def op(r: Run) -> None:
        _require_live(r)
        _abort(r, reason or "aborted")
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def archive(*, cwd: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    """Move a finished run out of its slot into the archive, leaving the slot
    idle (a start would do the same first). A live run is refused: abort it
    first, so ending a run and clearing its record stay two decisions."""
    st = RunStore(cwd, scope)
    with st.lock():
        if not st.exists():
            raise FlowError(f"no cflow2 run in {st.dir}")
        state = st.load_state()
        if state.get("status") not in FINISHED_STATUSES:
            raise FlowError(f"run {state.get('run_id')} is {state.get('status')}; abort it "
                            f"before archiving")
        target = st.archive(state.get("run_id"))
    return {"status": "idle", "archived": str(target), "run": state.get("run_id"),
            "workflow": state.get("workflow")}


# -- a person's forced move (docs 2.11) ------------------------------------------


def _force_target_id(chart: ir.Chart, ref: str) -> str:
    """The node a move names (id or label) among the ``force_target`` nodes."""
    marked = [n for n in chart.nodes.values() if n.get(ir.FORCE_TARGET)]
    hits = [n for n in marked if ref in (n.id, n.label)]
    if len(hits) == 1:
        return hits[0].id
    if len(hits) > 1:
        raise FlowError(f"{ref!r} names several force targets ({', '.join(n.id for n in hits)}); "
                        f"give the node id")
    known = ", ".join(n.label or n.id for n in marked) or "none -- the definition marks no node"
    if any(ref in (n.id, n.label) for n in chart.nodes.values()):
        raise FlowError(f"{ref!r} is not a force target (the definition's '{ir.FORCE_TARGET}: "
                        f"true' nodes: {known})")
    raise FlowError(f"no node {ref!r} (force targets: {known})")


def _seat(r: Run, iid: str, node: ir.Node, slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Point instance ``iid`` at the child ``slot`` names, as if it had
    reached that child on its own: the frame field that tracks the child, any
    hold of its own released. Returns the environment the child is entered
    with (what the combinator would have given it), or ``None``."""
    inst = r.inst[iid]
    frame = inst["frame"]
    inst["await"] = None
    r.state["approvals"] = [a for a in r.state.get("approvals") or []
                            if a.get("instance") != iid]
    frame.pop("on", None)
    env: Optional[Dict[str, Any]] = None
    t = node.type
    if t == ir.T_SEQ:
        frame["idx"] = slot["idx"]
        if node.get("pass_prev"):
            env = {"prev": None}
    elif t == ir.T_COND:
        frame["branch"] = slot["branch"]
    elif t == ir.T_CHOOSE:
        frame["option"] = slot["option"]
    elif t == ir.T_LOOP:
        if not frame.get("iter"):
            frame.update({"iter": 1, "base": 0, "values": [], "extra": 0, "last": None})
        frame["phase"] = "body"
        frame.pop("resume_at", None)
        if node.get("mode") == ir.LOOP_RETRY:
            env = {"vars": {"attempt": frame["iter"]}, "prev": frame.get("last")}
    elif t == ir.T_TRY:
        frame["phase"] = "body"
        for key in ("caught", "pending", "signal"):
            frame.pop(key, None)
    elif t == ir.T_TIMEOUT:
        frame["phase"] = "body"
        frame.setdefault("deadline", r.now + float(node.get("seconds") or 0))
    return env


def force_move(target: str, reason: Optional[str] = None, *, by: str = CALLER_USER,
               cwd: Optional[str] = None, scope: Optional[str] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
    """Move the run to ``target`` -- a node its definition marks
    ``force_target: true`` -- as a person's override (docs 2.11).

    The deepest live instance on the root-to-target chain stays; everything
    below it is cancelled (open tasks, delegations, asks, holds), and the
    chain is entered again from there down to the target, each combinator
    seated on the child that leads on (a seq at that index, a cond on that
    branch, a loop in its current iteration, a try in its body). The nodes in
    between run their enter effects; nodes the move skips do not run at all,
    so their ``bind``s and ``do``s are whatever an earlier pass left. A
    target that is live already is entered afresh. ``ir.force_path`` decides
    which chains can be rebuilt; the compiler refuses a mark on one that
    cannot."""
    moved: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_progress(r)
        _move_run(r, target, reason, by, moved)

    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["forced"] = moved
    return payload


def _move_run(r: Run, target: str, reason: Optional[str], by: str, moved: Dict[str, Any],
              granted: Optional[Dict[str, Any]] = None) -> None:
    """The move itself, shared by a person's :func:`force_move` and by the
    approval of the driver's :func:`request_move`. ``granted`` is the request
    this move answers, when it answers one: it rides on the ``forced`` event,
    so the journal says the position was asked for and by whom.

    A pending request that this move does not answer is superseded: the
    person moved the run while the question was open, which answers it."""
    tid = _force_target_id(r.chart, target)
    path, problems = ir.force_path(r.chart, tid)
    if problems:
        raise FlowError(f"cannot move to {target!r}: " + "; ".join(problems))
    cur, k = _move_anchor(r, path)
    left = sorted(rec["node"] for rec in r.inst.values() if rec.get("await"))
    node = r.chart.node(tid)
    pending = pending_move(r.state)
    if granted is None and pending is not None:
        r.emit("move_superseded", request=pending.get("id"), node=pending.get("node"),
               forced_to=tid, by=by)
    r.state.pop(MOVE_REQUEST, None)
    r.emit("forced", node=tid, label=node.label or None, by=by, reason=reason or None,
           at=r.inst[cur]["node"], left=left,
           **({"granted": granted.get("id"), "asked_by": granted.get("by")}
              if granted else {}))
    moved.update(node=tid, label=node.label or None, at=r.inst[cur]["node"], left=left)
    _cancel_children(r, cur, "forced")
    env = _seat(r, cur, r.node_of(cur), path[k][1] or {})
    parent = cur
    for j in range(k, len(path) - 1):
        nid, slot = path[j]
        mid = r.chart.node(nid)
        iid = _new_instance(r, mid, parent, (slot or {}).get("role", ""), env)
        err = _apply_effects(r, iid, mid, AT_ENTER)
        if err:
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": err})
            return
        _queue_triggers(r, iid, mid, AT_ENTER)
        if mid.type == ir.T_SCOPE and not _scope_params(r, iid, mid):
            return
        env = _seat(r, iid, mid, path[j + 1][1] or {})
        parent = iid
    _enter(r, tid, parent, (path[-1][1] or {}).get("role", ""), env)



def _move_anchor(r: Run, path: List[Tuple[str, Optional[Dict[str, Any]]]]) -> Tuple[str, int]:
    """Where a move along ``path`` (``ir.force_path``) lands: the deepest
    live instance on the chain that stays, and the index in ``path`` of the
    first node entered under it. Shared by the move and its preview
    (:func:`move_plan`), so what the approval card shows is what the move
    does."""
    roots = [iid for iid, rec in r.inst.items()
             if rec.get("parent") is None and rec.get("node") == path[0][0]]
    if not roots:
        raise FlowError(f"run {r.state.get('run_id')} has no live root instance to move")
    cur, k = roots[0], 1
    while True:
        kids = [c for c in r.children_of(cur) if r.inst[c]["node"] == path[k][0]]
        if not kids or k == len(path) - 1:
            break
        cur, k = kids[0], k + 1
    return cur, k


def _plan_frame(r: Run, node_id: str, iid: Optional[str] = None) -> Dict[str, Any]:
    node = r.chart.node(node_id)
    out: Dict[str, Any] = {"node": node_id, "label": node.label or None, "type": node.type}
    if node.get("title"):
        out["title"] = node.get("title")
    if iid is not None:
        out["instance"] = iid
        frame = r.inst[iid].get("frame") or {}
        if node.type == ir.T_LOOP and frame.get("iter"):
            out["iter"] = frame["iter"]
    return out


def move_plan(r: Run, target: str) -> Dict[str, Any]:
    """What moving the run to ``target`` would do to its callstack, measured
    the way :func:`_move_run` moves it (claunch-1qsz4): the frames that stay
    (``kept``, root to the anchor), the live frames below the anchor that are
    cancelled (``unwound``; ``rewind`` levels deep), the frames entered from
    the anchor down to the target (``entered``; ``step_in`` levels), the
    callstack paths before and after, and ``relation``:

    - ``reenter``: the target is a live frame now; the move unwinds to it and
      enters it afresh
    - ``branch``: the target is off the current stack under a shared frame
      below the root
    - ``unrelated``: the root is the only frame the target shares with the
      current stack
    - ``step_in``: nothing below the anchor is live; the move only enters

    ``seq`` says where the anchor's sequence goes when the anchor is a seq
    (from its current step to the target's step: ``back``/``forward``/
    ``same``). A plan that cannot be made carries ``problems`` instead."""
    try:
        tid = _force_target_id(r.chart, target)
    except FlowError as e:
        return {"problems": [str(e)]}
    path, problems = ir.force_path(r.chart, tid)
    if problems:
        return {"problems": problems}
    try:
        cur, k = _move_anchor(r, path)
    except FlowError as e:
        return {"problems": [str(e)]}
    kept: List[Dict[str, Any]] = []
    iid: Optional[str] = cur
    while iid is not None:
        kept.insert(0, _plan_frame(r, r.inst[iid]["node"], iid))
        iid = r.inst[iid].get("parent")
    unwound: List[Dict[str, Any]] = []
    leaves: List[List[str]] = []

    def walk(parent: str, depth: int, trail: List[str]) -> None:
        kids = sorted(r.children_of(parent))
        if not kids and parent != cur:
            leaves.append(trail)
        for c in kids:
            unwound.append({**_plan_frame(r, r.inst[c]["node"], c), "depth": depth})
            walk(c, depth + 1, trail + [c])

    walk(cur, 1, [])
    entered = [_plan_frame(r, nid) for nid, _slot in path[k:]]
    # the anchor walk stops above a live target, so it is a cancelled child
    if any(f["node"] == tid and f["depth"] == 1 for f in unwound):
        relation = "reenter"
    elif not unwound:
        relation = "step_in"
    elif len(kept) == 1:
        relation = "unrelated"
    else:
        relation = "branch"
    names = {f["instance"]: f for f in kept + unwound}
    label = lambda f: f.get("label") or f["node"]  # noqa: E731
    before = [[label(f) for f in kept] + [label(names[i]) for i in trail]
              for trail in leaves] or [[label(f) for f in kept]]
    plan: Dict[str, Any] = {
        "target": _plan_frame(r, tid), "relation": relation,
        "anchor": kept[-1], "kept": kept, "unwound": unwound,
        "rewind": max((f["depth"] for f in unwound), default=0),
        "entered": entered, "step_in": len(entered),
        "before": before, "after": [label(f) for f in kept] + [label(f) for f in entered],
    }
    anchor_node = r.node_of(cur)
    slot = path[k][1] or {}
    if anchor_node.type == ir.T_SEQ and "idx" in slot:
        steps = list(anchor_node.get("children") or [])
        now_idx = (r.inst[cur].get("frame") or {}).get("idx")
        to_idx = int(slot["idx"])
        seq: Dict[str, Any] = {"steps": len(steps), "to": to_idx,
                               "to_label": label(_plan_frame(r, steps[to_idx]))}
        if isinstance(now_idx, int) and 0 <= now_idx < len(steps):
            seq.update(at=now_idx, at_label=label(_plan_frame(r, steps[now_idx])),
                       direction=("back" if to_idx < now_idx else
                                  "forward" if to_idx > now_idx else "same"))
        plan["seq"] = seq
    return plan


def move_plan_line(plan: Dict[str, Any]) -> str:
    """:func:`move_plan` in one line, for a surface that has one line (the
    terminal strip's answer): the relation, the levels unwound and entered,
    and the frame the move lands under."""
    if not plan or plan.get("problems"):
        return "callstack effect unknown"
    name = lambda f: (f or {}).get("label") or (f or {}).get("node") or "?"  # noqa: E731
    anchor, target = name(plan.get("anchor")), name(plan.get("target"))
    head = {
        "reenter": f"re-enters live {target!r}",
        "branch": f"off the callstack under {anchor!r}",
        "unrelated": f"unrelated to the callstack: shares only the root {anchor!r}",
        "step_in": f"steps in under {anchor!r}",
    }.get(str(plan.get("relation")), f"moves to {target!r}")
    return f"{head}; rewind {plan.get('rewind', 0)}, step in {plan.get('step_in', 0)}"


def step_out(instance: str, outcome: str = ir.OUTCOME_OK, value: Any = None,
             reason: Optional[str] = None, *, by: str = CALLER_USER,
             expected_run: Optional[str] = None,
             cwd: Optional[str] = None, scope: Optional[str] = None,
             now: Optional[float] = None) -> Dict[str, Any]:
    """Release one live container and return to its parent, as a user override.

    Unlike force_move (re-entry), this cancels the remaining subtree and
    delivers a result to the caller. Existing data and external effects stay.
    An exact instance id prevents a stale page from releasing a later visit.
    """
    def op(r: Run) -> None:
        _require_progress(r)
        if by != CALLER_USER:
            raise FlowError("step out is a user operation")
        if expected_run is not None and r.state.get("run_id") != expected_run:
            raise FlowError("run changed; refresh the callstack")
        inst = r.inst.get(instance)
        if inst is None:
            raise FlowError(f"no live frame {instance!r}; refresh the callstack")
        node = r.node_of(instance)
        if inst.get("parent") is None or node.type in (ir.T_TASK, ir.T_WAIT, ir.T_CONTROL):
            raise FlowError("step out requires a non-root container frame")
        result = fill_optional(returns_of(node), outcome, value)
        errors = validate_result(returns_of(node), outcome, result,
                                 qtypes=(r.chart.meta or {}).get("question_types") or {},
                                 params=r.env(instance).get("params"))
        if errors:
            raise FlowError("step out refused: " + "; ".join(errors))
        pending = pending_move(r.state)
        if pending:
            r.emit("move_superseded", request=pending.get("id"), node=pending.get("node"),
                   stepped_out=instance, by=by)
        r.state.pop(MOVE_REQUEST, None)
        r.emit("stepped_out", instance=instance, node=node.id, parent=inst["parent"],
               outcome=outcome, value=result, reason=reason or None, by=by)
        inst["await"] = None
        _release_holds(r, instance)
        _cancel_children(r, instance, "step_out")
        # A selected try still runs its finally body, just as a return does.
        if node.type == ir.T_TRY and inst["frame"].get("phase") != "finally":
            _try_finally(r, instance, outcome, result)
        else:
            _complete(r, instance, outcome, result)

    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def pending_move(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The driver's unanswered move request on this run, if any."""
    req = state.get(MOVE_REQUEST)
    if isinstance(req, dict) and not req.get("decision"):
        return req
    return None


def _require_no_move_request(r: Run) -> None:
    """A pending move request holds the driver: the run does not advance by
    the driver's hand until a person answers it (v1's ``waiting_goto``)."""
    pending = pending_move(r.state)
    if pending is not None:
        what = (f"a person to report {pending.get('label') or pending.get('node')!r} as "
                f"{pending.get('outcome')!r}" if pending.get("kind") == MOVE_KIND_REPORT
                else f"this run to be moved to {pending.get('node')!r}")
        raise FlowError(
            f"you asked for {what} "
            f"({pending.get('id')}) and nobody has answered yet; the run does not advance "
            f"by your hand until a person does. Stop your turn. If the reason stopped being "
            f"true, withdraw it with flow_request_move(cancel=true)")


def request_move(target: str, reason: str, *, by: str, cwd: Optional[str] = None,
                 scope: Optional[str] = None, now: Optional[float] = None,
                 outcome: Optional[str] = None, value: Any = None) -> Dict[str, Any]:
    """The driver's side of an off-route move: ask a person to move the run
    to ``target`` -- a node the definition marks ``force_target`` -- and hold
    until they answer (docs 2.11). Moves nothing.

    Why a request and not a move: the definition is the run's account of
    what may happen, and a driver that could re-position its own run could
    walk out of any ask or choice put in front of it. But the work does
    out-run the definition -- a new instruction arrives while the run waits
    for its end decision -- and without this door the driver's only way
    forward was to do the work outside the run (claunch-kgfr5). A person
    answers with :func:`resolve_move` (the run page, ``claunch cflow2 move
    --approve|--deny``), or moves the run elsewhere with :func:`force_move`,
    which supersedes the request.

    With ``outcome`` the request is of the other kind (claunch-vwmoj): a
    person reports the active task ``target`` with ``outcome``/``value`` --
    the forced report of 2.11, asked for instead of typed out as a CLI line
    for the user to run. The result is checked against the task's
    ``returns`` now, so nobody is asked to grant a report the engine would
    refuse. The task's ``verify`` runs on the grant as it would on any
    report -- a person's grant does not exempt the result from it."""
    if outcome is not None:
        return _request_report(target, reason, outcome, value, by=by, cwd=cwd,
                               scope=scope, now=now)
    if value is not None:
        raise FlowError("'value' belongs to a report request: give 'outcome' too (and "
                        "name the active task in 'node')")
    note = (reason or "").strip()
    if not note:
        raise FlowError("'reason' is required: the person answering has not watched you "
                        "work, and a node name on its own is not something anyone can "
                        "approve or refuse")
    filed: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_progress(r)
        tid = _force_target_id(r.chart, target)
        _path, problems = ir.force_path(r.chart, tid)
        if problems:
            raise FlowError(f"cannot move to {target!r}: " + "; ".join(problems))
        node = r.chart.node(tid)
        previous = pending_move(r.state)
        request = {
            "id": f"mr-{secrets.token_hex(3)}", "kind": MOVE_KIND_MOVE,
            "node": tid, "label": node.label or None,
            "title": node.get("title") or None, "reason": note, "by": by,
            "from": sorted(rec["node"] for rec in r.inst.values() if rec.get("await")),
            "at": _iso(r.now),
        }
        r.state[MOVE_REQUEST] = request
        r.emit("move_requested", request=request["id"], node=tid, label=node.label or None,
               by=by, reason=note, left=request["from"],
               **({"replaces": previous.get("id")} if previous else {}))
        filed.update(request)

    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["move_request"] = filed
    payload["note"] = (
        "recorded; the run does not advance by your hand until a person answers. Stop "
        "your turn and write them the decision brief -- where the run has to go, what "
        "you found that the definition declares no route for, what redoing from there "
        "costs, and what staying costs. They answer from the run page (Move / Refuse) or "
        "with 'claunch cflow2 move --approve' / '--deny'; they may also move the run "
        "somewhere else with 'claunch cflow2 move <node>'")
    return payload


def _request_report(target: str, reason: str, outcome: str, value: Any, *, by: str, cwd: Optional[str], scope: Optional[str],
                    now: Optional[float]) -> Dict[str, Any]:
    """The report kind of :func:`request_move`: file a request that a person
    report the active task ``target``. Moves nothing and holds the driver
    like a move request does."""
    note = (reason or "").strip()
    if not note:
        raise FlowError("'reason' is required: the person answering has not watched you "
                        "work -- say why the task cannot be reported the normal way, with "
                        "the evidence (the verify output, the command you ran instead)")
    outcome = str(outcome or "").strip()
    if not outcome:
        raise FlowError("'outcome' must name one of the task's declared outcomes")
    filed: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_progress(r)
        iid = _task_for_report(r, target)
        n = _check_report(r, iid, outcome, value)
        previous = pending_move(r.state)
        last = r.inst[iid]["frame"].get("last_verify") or None
        request = {
            "id": f"mr-{secrets.token_hex(3)}", "kind": MOVE_KIND_REPORT,
            "node": n.id, "instance": iid, "label": n.label or None,
            "title": n.get("title") or None, "outcome": outcome, "value": value,
            "reason": note, "by": by,
            "from": [iid], "at": _iso(r.now),
            **({"last_verify": last} if last else {}),
        }
        r.state[MOVE_REQUEST] = request
        r.emit("move_requested", request=request["id"], kind=MOVE_KIND_REPORT, node=n.id,
               instance=iid, label=n.label or None, by=by, reason=note, outcome=outcome,
               **({"replaces": previous.get("id")} if previous else {}))
        filed.update(request)

    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["move_request"] = filed
    payload["note"] = (
        "recorded; the run does not advance by your hand until a person answers. Stop your "
        "turn and write them the decision brief -- which task, the result you ask them to "
        "report, why the normal report cannot carry it (the evidence), and that its verify "
        "still runs on the grant. They answer from the run page (Report / Refuse) or with 'claunch cflow2 move "
        "--approve' / '--deny'")
    return payload


def cancel_move_request(*, by: str, cwd: Optional[str] = None, scope: Optional[str] = None,
                        now: Optional[float] = None) -> Dict[str, Any]:
    """Withdraw the driver's pending move request (the reason stopped being
    true), so nobody is left holding a question no answer helps."""
    def op(r: Run) -> None:
        pending = pending_move(r.state)
        if pending is None:
            raise FlowError("no pending move request on this run")
        r.state.pop(MOVE_REQUEST, None)
        r.emit("move_withdrawn", request=pending.get("id"), node=pending.get("node"), by=by)

    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["note"] = "request withdrawn; the run continues from where it stands"
    return payload


def resolve_move(decision: str, *, by: str = CALLER_USER, reason: Optional[str] = None,
                 cwd: Optional[str] = None, scope: Optional[str] = None,
                 now: Optional[float] = None,
                 verify_runner: Optional[VerifyRunner] = None) -> Dict[str, Any]:
    """A person answers the driver's pending move request. CLI / dashboard
    only -- not exposed over MCP, for the reason ``approve`` is not: a grant
    the asker can give itself is not a grant.

    ``approve`` performs the move asked for (:func:`_move_run`, journaled
    ``move_approved`` then ``forced`` with ``granted``); ``deny`` leaves the
    run where it is and keeps the refusal on the run for its driver to read
    on its next ``flow_status``, until it files another request or the run
    moves."""
    verdict = (decision or "").strip().lower()
    if verdict not in ("approve", "deny"):
        raise FlowError(f"decision must be 'approve' or 'deny', not {decision!r}")
    note = (reason or "").strip()
    out: Dict[str, Any] = {}
    job: Dict[str, Any] = {}

    def op(r: Run) -> None:
        pending = pending_move(r.state)
        if pending is None:
            raise FlowError("no pending move request on this run")
        if verdict == "approve":
            _require_progress(r)
            if pending.get("kind") == MOVE_KIND_REPORT:
                iid = str(pending.get("instance") or "")
                if (r.inst.get(iid) or {}).get("await") != AW_TASK:
                    raise FlowError(f"task {pending.get('label') or pending.get('node')} "
                                    f"({iid}) is no longer waiting for a report; refuse the "
                                    f"request (--deny) and let the driver ask again")
                r.emit("move_approved", request=pending.get("id"), node=pending.get("node"),
                       by=by, reason=note or None)
                r.state.pop(MOVE_REQUEST, None)
                text = (f"reported by a person on {pending.get('by') or 'the driver'}'s "
                        f"request ({pending.get('id')}): {pending.get('reason')}"
                        + (f" -- {note}" if note else ""))
                _report_task(r, iid, pending["outcome"], pending.get("value"), text, by,
                             verify_runner, now, job)
                out.update(move_request={**pending, "decision": "approved"})
                return
            r.emit("move_approved", request=pending.get("id"), node=pending.get("node"),
                   by=by, reason=note or None)
            moved: Dict[str, Any] = {}
            _move_run(r, pending["node"], note or pending.get("reason"), by, moved,
                      granted=pending)
            out.update(forced=moved, move_request={**pending, "decision": "approved"})
            return
        decided = {**pending, "decision": "denied", "decided_by": by,
                   "decided_reason": note, "decided_at": _iso(r.now)}
        r.state[MOVE_REQUEST] = decided
        r.emit("move_denied", request=pending.get("id"), node=pending.get("node"), by=by,
               reason=note or None)
        out.update(move_request=decided)

    _mutate(cwd, scope, now, op)
    payload = _with_verify_note(status(cwd=cwd, scope=scope, now=now, tick=False), job)
    payload.update(out)
    return payload

# -- time and probes ---------------------------------------------------------


def default_probe_runner(probe: Any, cwd: str, timeout: float = 60.0,
                         env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """Run a wait's or monitor's probe in ``cwd``: ``(exit code, output)``.
    The daemon clock's runner (own process group, killed whole on timeout);
    ``env`` is added to the process environment -- values reach a probe only
    that way."""
    try:
        from ..daemon.cflow2_clock import run_probe
    except ImportError:  # pragma: no cover - a runtime without the daemon
        run_probe = None
    if run_probe is not None:
        return run_probe(probe, cwd, timeout, env=env)
    full = dict(os.environ)
    full.update(env or {})
    cmd = expand_argv(probe, full) if isinstance(probe, list) else str(probe)
    try:
        proc = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, env=full)
    except subprocess.TimeoutExpired:
        return 124, "probe timed out"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _takes_env(fn: Callable[..., Any]) -> bool:
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "env" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _value_env(r: Run, iid: Optional[str]) -> Dict[str, str]:
    """``CFLOW2_*``, ``CFLOW_IN_*`` and ``CFLOW_DATA_*`` (top-level data,
    scalars as text, the rest as JSON) for a probe."""
    env = {"CFLOW2_RUN": str(r.state.get("run_id") or ""),
           "CFLOW2_SCOPE": str(r.state.get("scope") or ""),
           store_mod.SESSION_ENV: _probe_session(r)}
    params: Dict[str, Any] = dict(r.state.get("params") or {})
    if iid and iid in r.inst:
        params.update(r.env(iid).get("params") or {})
        env["CFLOW2_INSTANCE"] = iid
    for name, value in params.items():
        env[_env_name(name)] = _env_text(value)
    for name, value in (r.state.get("data") or {}).items():
        text = _env_text(value)
        if len(text) <= 64 * 1024:
            env[_data_env_name(name)] = text
    return env


def _probe_session(r: Run) -> str:
    """The session a probe acts as: the run's driver. The daemon's clock runs
    probes, and its own ``CLAUNCH_SESSION`` is whatever terminal started it
    -- one name for every run on the machine. A gate that asks "which
    checkout is mine" (``tools/landed_check.py`` through
    ``cflow.checkout.own_checkout``) would measure that session's tree, or
    the run's cwd on master, and answer exit 2 for a tip that landed
    (claunch-4zc51.1.4; v1's ``engine.probe_env`` fixed the same in
    claunch-04ru). An unmanaged run names nobody: the blank value overrides
    the daemon's when the runner merges ``env`` over ``os.environ``, and
    ``state.current_scope`` reads a blank as unset."""
    driver = str(r.state.get("driver") or "").strip()
    if driver:
        return driver
    scope = str(r.state.get("scope") or "").strip()
    return "" if scope in ("", store_mod.DEFAULT_SCOPE) else scope


def _run_probe(r: Run, iid: Optional[str], runner: ProbeRunner, probe: Any) -> Any:
    """Run one probe: value references become environment variable
    references (as in verify), the values go in ``env``."""
    problems = shell_ref_problems(probe, "probe")
    if problems:
        return (127, "; ".join(problems))
    cmd = _shell_command(probe) if isinstance(probe, str) else probe
    try:
        if _takes_env(runner):
            try:
                return runner(cmd, str(r.store.cwd), env=_value_env(r, iid))
            except TypeError:
                pass  # a wrapper passing **kwargs on to a runner without env
        return runner(cmd, str(r.store.cwd))
    except Exception as exc:  # noqa: BLE001 - a broken probe is an undecided one
        return (127, f"probe could not run: {exc}")


def _probe_ok(result: Any) -> Tuple[bool, Any]:
    if isinstance(result, bool):
        return result, None
    if isinstance(result, tuple) and result:
        return result[0] == 0, (result[1] if len(result) > 1 else None)
    if isinstance(result, int):
        return result == 0, None
    if isinstance(result, dict):
        return bool(result.get("ok")), result.get("output")
    return bool(result), None


PROBE_HOLDS = "holds"
PROBE_NOT_YET = "not_yet"
PROBE_UNDECIDED = "undecided"
#: Consecutive undecided probe results after which a wait gives up.
DEFAULT_UNDECIDED_MAX = 3
OUTCOME_UNDECIDABLE = ir.OUTCOME_UNDECIDABLE


def _probe_verdict(result: Any) -> Tuple[str, Any, Any]:
    """A wait probe's result: exit 0 holds, exit 1 not yet, anything else
    (a command that could not tell: 2, 124 timeout, 127 not found ...) is
    undecided. ``(verdict, output, exit)``."""
    if isinstance(result, bool):
        return (PROBE_HOLDS if result else PROBE_NOT_YET), None, None
    code: Any = None
    output: Any = None
    if isinstance(result, tuple) and result:
        code, output = result[0], (result[1] if len(result) > 1 else None)
    elif isinstance(result, int):
        code = result
    elif isinstance(result, dict):
        if result.get("undecided"):
            return PROBE_UNDECIDED, result.get("output"), result.get("exit")
        return (PROBE_HOLDS if result.get("ok") else PROBE_NOT_YET), result.get("output"), None
    else:
        return (PROBE_HOLDS if result else PROBE_NOT_YET), None, None
    if code == 0:
        return PROBE_HOLDS, output, code
    if code == 1:
        return PROBE_NOT_YET, output, code
    return PROBE_UNDECIDED, output, code


def _probing(node: ir.Node) -> bool:
    """A wait the clock measures: one ``probe`` or a list of ``checks``."""
    return bool(node.get("probe") or node.get("checks"))


def _run_wait_probe(r: Run, iid: str, runner: ProbeRunner, node: ir.Node) -> Any:
    """A wait's measurement: its probe's result, or for ``checks`` every
    item's verdict (``{"checks": [...]}``)."""
    if not node.get("checks"):
        return _run_probe(r, iid, runner, node.get("probe"))
    items = []
    for c in node.get("checks") or []:
        verdict, output, code = _probe_verdict(_run_probe(r, iid, runner, c.get("probe")))
        items.append({"id": c.get("id"), "describe": c.get("describe") or None,
                      "verdict": verdict, "exit": code,
                      "output": _tail(output if isinstance(output, str) else ""),
                      "measured_at": store_mod.utcnow()})
    return {"checks": items}


def _checks_verdict(items: List[Dict[str, Any]]) -> Tuple[str, Any]:
    """All items hold -> holds; any undecided -> undecided (its exit);
    otherwise not yet."""
    if all(i.get("verdict") == PROBE_HOLDS for i in items):
        return PROBE_HOLDS, None
    for i in items:
        if i.get("verdict") == PROBE_UNDECIDED:
            return PROBE_UNDECIDED, i.get("exit")
    return PROBE_NOT_YET, None


def _probe_due(r: Run, iid: str) -> bool:
    inst = r.inst[iid]
    node = r.chart.node(inst["node"])
    if inst.get("await") != AW_WAIT or not _probing(node):
        return False
    poll = float(node.get("poll") or DEFAULT_POLL)
    last = inst["frame"].get("last_poll")
    return last is None or r.now - last >= poll


def _due_probes(r: Run) -> Dict[str, Any]:
    """``{instance: probe}`` for every wait whose probe is due at ``r.now``."""
    if r.status not in LIVE_STATUSES:
        return {}
    return {iid: r.node_of(iid) for iid in r.inst if _probe_due(r, iid)}


def _tick_time(r: Run, probe_runner: Optional[ProbeRunner] = None,
               probe_results: Optional[Dict[str, Any]] = None) -> int:
    """Fire everything due at ``r.now``; returns how many things moved.

    Probes come either from ``probe_results`` (``{instance: result}``, run
    by the caller before it took the lock -- :func:`wait_tick`) or, when only
    ``probe_runner`` is given, are run here."""
    if _halted(r):
        return 0  # a halt stops the clock's moves too
    moved = 0
    if _lost_verifies(r):
        moved += 1
    for _ in range(1000):
        fired = False
        for iid in sorted(r.inst, key=lambda k: int(k.rsplit("@", 1)[1])):
            if iid not in r.inst:
                continue
            inst = r.inst[iid]
            node = r.chart.node(inst["node"])
            frame = inst["frame"]
            if node.type == ir.T_TIMEOUT and frame.get("phase") == "body" \
                    and frame.get("deadline") is not None and r.now >= frame["deadline"]:
                frame["phase"] = "else"
                _cancel_children(r, iid, "timeout")
                if node.get("else_child"):
                    _enter_child(r, iid, node.get("else_child"), "else")
                else:
                    _complete(r, iid, ir.OUTCOME_TIMEOUT, {"seconds": node.get("seconds")})
                fired = True
                break
            if inst.get("await") == AW_CHOOSE and frame.get("held") \
                    and r.now >= float(frame["held"].get("opens_at") or 0):
                if _release_hold(r, iid, node):
                    fired = True
                    break
            if inst.get("await") == AW_WAIT and node.get("until") is not None:
                if _until_step(r, iid, node):
                    fired = True
                    break
                continue
            if inst.get("await") == AW_WAIT:
                due = frame.get("due")
                if due is not None and r.now >= due:
                    if _probing(node):
                        # with a probe, seconds is the upper bound (doc 2.3, wait)
                        value = {"seconds": node.get("seconds"),
                                 "polls": int(frame.get("polls") or 0)}
                        if node.get("checks"):
                            value["checks"] = frame.get("checks")
                            r.emit("checks_expired", node=node.id, instance=iid,
                                   checks=frame.get("checks"))
                        _complete(r, iid, ir.OUTCOME_TIMEOUT, value)
                    else:
                        _complete(r, iid, ir.OUTCOME_OK, {"waited": node.get("seconds")})
                    fired = True
                    break
                have = probe_results is not None and iid in probe_results
                if _probing(node) and (have or (probe_runner is not None
                                                and probe_results is None)):
                    if _probe_due(r, iid):
                        frame["last_poll"] = r.now
                        result = probe_results.pop(iid) if have else \
                            _run_wait_probe(r, iid, probe_runner, node)
                        checks = result.get("checks") if isinstance(result, dict) \
                            and node.get("checks") else None
                        if checks is not None:
                            frame["checks"] = checks
                            verdict, code = _checks_verdict(checks)
                            output = None
                        else:
                            verdict, output, code = _probe_verdict(result)
                        frame["polls"] = int(frame.get("polls") or 0) + 1
                        if verdict == PROBE_HOLDS:
                            frame["undecided"] = 0
                            if checks is not None:
                                r.emit("checks_passed", node=node.id, instance=iid,
                                       checks=checks)
                                _complete(r, iid, ir.OUTCOME_OK, {"checks": checks})
                            else:
                                _complete(r, iid, ir.OUTCOME_OK, {"output": output})
                            fired = True
                            break
                        if verdict == PROBE_UNDECIDED:
                            frame["undecided"] = int(frame.get("undecided") or 0) + 1
                            limit = int(node.get("undecided_max") or DEFAULT_UNDECIDED_MAX)
                            r.emit("probe_undecided", node=node.id, instance=iid, exit=code,
                                   count=frame["undecided"], max=limit,
                                   output=_tail(output if isinstance(output, str) else ""))
                            if frame["undecided"] >= limit:
                                value = {"exit": code, "output": output,
                                         "undecided": frame["undecided"]}
                                if checks is not None:
                                    value["checks"] = checks
                                _complete(r, iid, OUTCOME_UNDECIDABLE, value)
                                fired = True
                                break
                        else:
                            frame["undecided"] = 0
            if inst.get("await") == AW_PAUSE and r.now >= float(frame.get("resume_at") or 0):
                inst["await"] = None
                _loop_iterate(r, iid)
                fired = True
                break
        if not fired and _expire_asks(r):
            fired = True
        if not fired:
            break
        moved += 1
    return moved


def _lost_verifies(r: Run) -> bool:
    """A verify that has not come back well past its own timeout ran in a
    process that went away: the task is active again (``verify_failed``,
    ``lost``); a delegation's result counts as invalid."""
    moved = False
    for iid, inst in list(r.inst.items()):
        v = inst["frame"].get("verify") if inst.get("await") == AW_VERIFY else None
        if not v or v.get("passed"):
            continue
        if r.now > float(v.get("started_at") or 0) + float(v.get("timeout") or 0) \
                + VERIFY_LOST_GRACE:
            inst["frame"].pop("verify", None)
            inst["await"] = AW_TASK
            inst["frame"]["last_verify"] = {"exit": None, "why": "lost", "tail": "",
                                            "command": None}
            r.emit("verify_failed", node=inst["node"], instance=iid, lost=True,
                   tail="the verify never reported back (its process went away)")
            moved = True
    for rec in (r.state.get("delegations") or {}).values():
        v = rec.get("verify") if rec.get("status") == "verifying" else None
        if v and r.now > float(v.get("started_at") or 0) + float(v.get("timeout") or 0) \
                + VERIFY_LOST_GRACE:
            rec.pop("verify", None)
            rec["status"] = v.get("before") or "pending"
            r.emit("verify_failed", delegation=rec["id"], node=rec["node"], lost=True)
            _reject(r, rec, ["the verify of the delegated tasks never reported back"])
            moved = True
    return moved


def wait_tick(now: Optional[float] = None, probe_runner: Optional[ProbeRunner] = None, *,
              cwd: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    """Advance waits, timeouts and retry pauses that are due at ``now``.

    ``probe_runner(probe, cwd)`` runs a wait's probe (``(exit, output)``,
    bool, or ``{ok, output}``); without one only time-based waits fire.

    Probes are commands that can take a while, so they never run under the
    run lock (the agent's next report would wait on them): the due ones are
    read off an unlocked snapshot and run first, then the lock is taken and
    their results applied to waits that are still there and still due. A
    wait the tick itself enters gets its probe on a following round (at most
    three per call).
    """
    st = RunStore(cwd, scope)
    moved = 0
    if probe_runner is not None and st.exists():
        try:
            token = _acceptance_recheck_due(Run.load(st, now))
        except StoreError:
            token = None
        if token:
            _acceptance_job(str(st.cwd), st.scope, token, now)
    for _round in range(3):
        results: Optional[Dict[str, Any]] = None
        if probe_runner is not None:
            snap = Run.load(st, now)
            due = _due_probes(snap)
            results = {iid: _run_wait_probe(snap, iid, probe_runner, wnode)
                       for iid, wnode in due.items()}

        # Not _mutate: its own time tick would apply what this call moves
        # and the count would miss it (the clock wakes the driver on it).
        with st.lock():
            r = Run.load(st, now)
            if r.status in LIVE_STATUSES:
                moved += _tick_time(r, None, results if results is not None else {})
            if r.status in LIVE_STATUSES:
                moved += _tick_until(r)
            r.save()
        r.run_after()
        if r.deferred_error:
            raise FlowError(r.deferred_error)
        if probe_runner is None:
            break
        if not _due_probes(Run.load(st, now)):
            break
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["ticked"] = moved
    return payload


# -- spec.monitors -------------------------------------------------------------

#: How often an ``always`` monitor without ``every`` is evaluated.
DEFAULT_MONITOR_EVERY = 300.0
MON_HALT = "halt"
MON_NOTIFY = "notify"
MON_RECORD = "record"


def _monitors(r: Run) -> Dict[str, Dict[str, Any]]:
    mons = (r.chart.spec or {}).get("monitors") or {}
    return {str(k): dict(v or {}) for k, v in mons.items()}


def _monitor_probes(body: Dict[str, Any], name: str) -> Dict[str, Any]:
    probe = body.get("probe")
    if probe is None:
        return {}
    if isinstance(probe, dict):
        return {str(k): v for k, v in probe.items()}
    return {name: probe}


def _always_due(r: Run, name: str, body: Dict[str, Any]) -> bool:
    if "always" not in body:
        return False
    last = ((r.state.get("monitors") or {}).get(name) or {}).get("last")
    every = float(body.get("every") or DEFAULT_MONITOR_EVERY)
    return last is None or r.now - float(last) >= every


def _ts_epoch(ts: Any) -> Optional[float]:
    from datetime import datetime

    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except (TypeError, ValueError):
        return None


def _ev_matches(r: Run, ev: Dict[str, Any], spec: Dict[str, Any]) -> bool:
    """``spec`` is ``{event: target | [targets]}``; a target is a node id or
    label (``*`` any node)."""
    for kind, targets in (spec or {}).items():
        if ev.get("event") != kind:
            continue
        if targets is None:
            return True
        targets = targets if isinstance(targets, list) else [targets]
        names = {ev.get("node"), ev.get("label"), ev.get("instance")}
        node_id = ev.get("node")
        if node_id and node_id in r.chart.nodes:
            names.add(r.chart.node(node_id).label)
        if "*" in targets or names & {str(t) for t in targets}:
            return True
    return False


def _violate(r: Run, name: str, body: Dict[str, Any], kind: str,
             found: List[Dict[str, Any]], **detail: Any) -> None:
    action = str(body.get("on_violation") or MON_RECORD)
    r.emit("monitor_violation", kind=kind, monitor=name, action=action, **detail)
    if action == MON_HALT and r.status == ST_RUNNING:
        r.state["status"] = ST_WAITING
        r.state["reason"] = "monitor_violation"
    if action == MON_HALT:
        approvals = r.state.setdefault("approvals", [])
        if not any(a.get("monitor") == name for a in approvals):
            approvals.append({"kind": "monitor_violation", "monitor": name, "instance": None})
    found.append({"monitor": name, "kind": kind, "action": action, **detail})


def monitors_tick(now: Optional[float] = None, probe_runner: Optional[ProbeRunner] = None, *,
                  cwd: Optional[str] = None, scope: Optional[str] = None,
                  journal_now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Evaluate ``spec.monitors`` once (doc 8); returns the violations found
    now, each with its ``action`` (``halt`` already applied: the run is
    ``waiting_approval`` with reason ``monitor_violation``; ``notify`` is for
    the caller to deliver; ``record`` is the journal entry alone).

    ``always``: every ``every`` seconds the monitor's probes run (outside the
    lock; stdout stripped, ``None`` when the command fails) into ``$probe``
    and the expression is evaluated. Reported once per violation spell: it
    is reported again only after it held in between.

    ``within``: each journal event matching ``when`` must be followed within
    ``within`` seconds by one matching ``expect``; an expired one is a
    violation, reported once. Journal times are wall-clock, so the deadline
    is compared against ``journal_now`` (default: the real clock).
    """
    st = RunStore(cwd, scope)
    snap = Run.load(st, now)
    if snap.status not in LIVE_STATUSES:
        return []
    mons = _monitors(snap)
    if not mons:
        return []
    values: Dict[str, Dict[str, Any]] = {}
    for name, body in mons.items():
        if not _always_due(snap, name, body):
            continue
        vals: Dict[str, Any] = {}
        for pname, cmd in _monitor_probes(body, name).items():
            if probe_runner is None:
                continue
            ok, output = _probe_ok(_run_probe(snap, None, probe_runner, cmd))
            vals[pname] = (str(output).strip() if output is not None else "") if ok else None
        values[name] = vals
    wall = time.time() if journal_now is None else float(journal_now)
    journal_events = st.journal() if any("within" in b for b in mons.values()) else []
    found: List[Dict[str, Any]] = []

    def op(r: Run) -> None:
        if r.status not in LIVE_STATUSES:
            return
        mstate = r.state.setdefault("monitors", {})
        for name, body in mons.items():
            ms = mstate.setdefault(name, {})
            if "always" in body:
                if name not in values or not _always_due(r, name, body):
                    continue
                r.state.setdefault("probe", {}).update(values[name])
                ms["last"] = r.now
                error = None
                try:
                    held = expr_mod.truthy(_eval(r, None, body["always"]))
                except expr_mod.ExprError as exc:
                    held, error = False, str(exc)
                was = bool(ms.get("violated"))
                ms["violated"] = not held
                if not held and not was:
                    _violate(r, name, body, "always", found, expr=body["always"],
                             probe=values[name] or None, error=error)
                continue
            within = float(body.get("within") or 0)
            done = set(ms.get("checked") or [])
            for i, ev in enumerate(journal_events):
                if i in done or not _ev_matches(r, ev, body.get("when") or {}):
                    continue
                t0 = _ts_epoch(ev.get("ts"))
                if t0 is None:
                    done.add(i)
                    continue
                answered = any(
                    _ev_matches(r, later, body.get("expect") or {})
                    and (_ts_epoch(later.get("ts")) or 0) <= t0 + within
                    for later in journal_events[i + 1:]
                )
                if answered:
                    done.add(i)
                elif wall > t0 + within:
                    done.add(i)
                    _violate(r, name, body, "within", found, when=body.get("when"),
                             expect=body.get("expect"), within=within, since=ev.get("ts"))
            ms["checked"] = sorted(done)
    _mutate(cwd, scope, now, op)
    return found


def release_halt(monitor: Optional[str] = None, *, cwd: Optional[str] = None,
                 scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """A person resumes a run a ``halt`` monitor stopped (all of them, or one
    by name). The monitor keeps watching; a new violation halts again.
    Results that arrived during the halt (a verify that passed, a daemon
    executor's delegation result) apply now. A run that finished keeps its
    halt on record and can be released the same way."""
    def op(r: Run) -> None:
        before = list(r.state.get("approvals", []))
        r.state["approvals"] = [
            a for a in before if not (a.get("kind") == "monitor_violation"
                                      and (monitor is None or a.get("monitor") == monitor))
        ]
        if len(r.state["approvals"]) == len(before):
            raise FlowError("no monitor holds this run" if monitor is None
                            else f"monitor {monitor!r} does not hold this run")
        r.emit("selected", option="resume", kind="monitor_violation", monitor=monitor)
        _sync_waiting(r)
        if r.status in LIVE_STATUSES and not _halted(r):
            _apply_held(r)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)

def _apply_held(r: Run) -> None:
    for iid, inst in list(r.inst.items()):
        v = inst["frame"].get("verify")
        if inst.get("await") == AW_VERIFY and v and v.get("passed"):
            inst["frame"].pop("verify", None)
            n = r.node_of(iid)
            r.emit("reported", node=n.id, instance=iid, outcome=v["outcome"],
                   value=v.get("value"), text=v.get("text"), by=v.get("by"))
            _complete(r, iid, v["outcome"], v.get("value"))
    for rec in list((r.state.get("delegations") or {}).values()):
        held = rec.get("held")
        if held and rec.get("status") in OPEN_DELEGATION:
            rec.pop("held", None)
            _accept(r, rec, held["outcome"], held.get("value"), held.get("source") or "held")

# -- reading -----------------------------------------------------------------


def rule(name: str, *, cwd: Optional[str] = None, scope: Optional[str] = None,
         chart: Optional[ir.Chart] = None) -> Dict[str, Any]:
    if chart is None:
        chart = RunStore(cwd, scope).load_chart()
    rules = chart.rules or {}
    if name not in rules:
        raise FlowError(f"no rule {name!r} (rules: {', '.join(rules) or 'none'})")
    body = dict(rules[name] or {})
    return {"rule": name, "text": body.get("text"), "why": body.get("why"),
            "deliver": body.get("deliver"), "effect": body.get("effect")}


def status(*, cwd: Optional[str] = None, scope: Optional[str] = None,
           now: Optional[float] = None, tick: bool = True) -> Dict[str, Any]:
    """The run as the driver needs it: active tasks with rendered prompts,
    choices with the options their guards allow, waits, delegations."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return {"status": "idle", "scope": st.scope, "cwd": str(st.cwd),
                "note": "no cflow2 run here; start one with flow_start"}
    if tick:
        _reconcile_nested(st)
        with st.lock():
            r = Run.load(st, now)
            if r.status in LIVE_STATUSES and _tick_time(r):
                r.save()
        r.run_after()
    r = Run.load(st, now)
    return describe(r)


def describe(r: Run) -> Dict[str, Any]:
    s = r.state
    out: Dict[str, Any] = {
        "run": s.get("run_id"), "workflow": s.get("workflow"), "status": s.get("status"),
        "scope": s.get("scope"), "started": s.get("started"),
        "driver": driver_of(s) or None,
    }
    if s.get("mesh"):
        out["mesh"] = s["mesh"]
    if s.get("parent"):
        out["parent"] = s["parent"]
    inputs = (s.get("data") or {}).get("inputs")
    if inputs is not None:
        out["inputs"] = inputs
    if s.get("reason"):
        out["reason"] = s["reason"]
    halts = _halted(r)
    if halts:
        out["halted"] = halts
    acc = s.get("acceptance")
    if acc:
        out["acceptance"] = {k: acc.get(k) for k in ("status", "failed", "checked_at")}
    if _col_specs(r):
        out["collections"] = _col_summary(r)
    if _col_specs(r) or s.get("triggers"):
        out["pending_triggers"] = len(s.get("triggers") or [])
    if s.get("round"):
        out["round"] = s["round"]
    if s.get("side"):
        out["side"] = s["side"]
    sides = (r.chart.meta or {}).get("sides") or {}
    if sides and store_mod.NESTED_SEP not in r.store.scope:
        main = store_mod.main_scope(r.store.scope)
        out["sides"] = {}
        for name, wf in sides.items():
            try:
                info = side_info(r.store.cwd, main, name)
            except StoreError:
                continue
            info["declared"] = wf
            if info["run"] is None:
                info["how"] = (f"not started: flow_start with side {name!r} (runs {wf}); "
                               f"its tools take side {name!r}")
            out["sides"][name] = info
    if s.get("status") in FINISHED_STATUSES:
        out["outcome"] = s.get("outcome")
        out["value"] = s.get("value")
        out["note"] = f"the run is {s.get('status')}"
        nxt = s.get("next_round")
        if nxt:
            out["next_round"] = nxt
            out["note"] += (
                f"; round {nxt.get('round')} is requested and the daemon starts it (recur auto)"
                if nxt.get("auto") else
                f"; round {nxt.get('round')} is requested: start it with "
                f"flow_start(workflow={s.get('workflow')!r})")
        return out
    nested_waits = [w for w in _gate_waits(s, None) if "(nested run " in w]
    if nested_waits:
        # The parent itself shows running; what holds it is inside (G2).
        out["waiting_in_nested"] = nested_waits
    tasks, choices, waits, groups, limits, asks = [], [], [], [], [], []
    verifying, visit_limits, windows = [], [], []
    for iid in sorted(r.inst, key=lambda k: int(k.rsplit("@", 1)[1])):
        inst = r.inst[iid]
        node = r.chart.node(inst["node"])
        aw = inst.get("await")
        env = r.env(iid)
        if aw == AW_TASK:
            tasks.append({
                "instance": iid, "node": node.id, "label": shown_label(r.chart, node),
                "title": node.get("title"), "prompt": render(node.get("prompt"), env),
                "done_when": node.get("done_when"), "verify": node.get("verify"),
                "returns": returns_of(node), "effects": node.get("effects") or [],
                "rules": [{"id": rid, "text": (r.chart.rules.get(rid) or {}).get("text")}
                          for rid in node.get("rules") or []],
                "ask": node.get("ask"),
                "item": env.get("item"),
            })
            if node.get("calls"):
                tasks[-1]["calls"] = list(node.get("calls"))
            if inst["frame"].get("call_result"):
                tasks[-1]["call_result"] = inst["frame"]["call_result"]
            if inst["frame"].get("last_verify"):
                tasks[-1]["last_verify"] = inst["frame"]["last_verify"]
            if node.get("restart"):
                tasks[-1]["restart"] = _restart_view(inst["frame"].get("restart"))
        elif aw == AW_VERIFY:
            v = inst["frame"].get("verify") or {}
            verifying.append({"instance": iid, "node": node.id, "label": shown_label(r.chart, node),
                              "verify": node.get("verify"), "started_at": v.get("started_at"),
                              "timeout": v.get("timeout"), "passed": bool(v.get("passed"))})
        elif aw == AW_CHOOSE:
            avail, unavail = [], []
            for opt in node.get("options") or []:
                try:
                    ok = _guard(r, iid, opt.get("guard"))
                except expr_mod.ExprError:
                    ok = False
                entry = {"name": opt["name"], "description": opt.get("description") or ""}
                if opt.get("interval"):
                    entry["interval"] = opt["interval"]
                    opens = _window_opens(r, node, opt)
                    if opens is not None and r.now < opens:
                        entry["window_opens_at"] = _iso(opens)
                (avail if ok else unavail).append(entry if ok else {**entry, "when": opt.get("guard")})
            choices.append({
                "instance": iid, "node": node.id, "label": shown_label(r.chart, node),
                "prompt": render(node.get("prompt"), env),
                "chooser": node.get("chooser") or ir.CHOOSER_AGENT,
                "require_reason": bool(node.get("require_reason")),
                "options": avail, "unavailable": unavail,
            })
            held = inst["frame"].get("held")
            if held:
                choices[-1]["held"] = {**held, "opens_at": _iso(held.get("opens_at"))}
                windows.append({"instance": iid, "node": node.id, "label": shown_label(r.chart, node),
                                "option": held.get("option"),
                                "opens_at": _iso(held.get("opens_at"))})
        elif aw == AW_WAIT:
            waits.append({
                "instance": iid, "node": node.id, "kind": "wait",
                "describe": node.get("describe"), "probe": node.get("probe"),
                "poll": node.get("poll"), "seconds": node.get("seconds"),
                "due": inst["frame"].get("due"), "polls": inst["frame"].get("polls", 0),
            })
            if node.get("checks"):
                measured = {c.get("id"): c for c in inst["frame"].get("checks") or []}
                waits[-1]["checks"] = [
                    {"id": c["id"], "describe": c.get("describe") or None, "probe": c.get("probe"),
                     **{k: (measured.get(c["id"]) or {}).get(k)
                        for k in ("verdict", "exit", "output", "measured_at")}}
                    for c in node.get("checks") or []]
        elif aw == AW_PAUSE:
            waits.append({"instance": iid, "node": node.id, "kind": "pause",
                          "resume_at": inst["frame"].get("resume_at")})
        elif aw == AW_APPROVAL:
            rec = (s.get("asks") or {}).get(inst["frame"].get("ask") or "")
            if rec:
                asks.append(_describe_ask(r, iid, node, rec, env))
        elif aw == AW_LIMIT and inst["frame"].get("visit_limit"):
            key = node_key(node)
            visit_limits.append({"instance": iid, "node": node.id, "label": shown_label(r.chart, node),
                                 "visits": (s.get("visits") or {}).get(key),
                                 "max": _max_visits(r, key)})
        elif aw == AW_LIMIT:
            limits.append({"instance": iid, "node": node.id, "label": shown_label(r.chart, node),
                           "iterations": _loop_count(inst["frame"]),
                           "max": _loop_cap(r, iid)})
        if node.type == ir.T_GROUP:
            frame = inst["frame"]
            groups.append({
                "instance": iid, "node": node.id, "name": node.get("name"),
                "limit": node.get("limit"), "members": len(frame.get("members") or []),
                "can_fork": frame.get("phase") == "do" and len(frame.get("members") or [])
                < int(node.get("limit") or 1 << 30),
            })
    dels = []
    for did, rec in (s.get("delegations") or {}).items():
        entry = {k: rec.get(k) for k in ("id", "node", "label", "executor", "status",
                                         "receipt", "started", "finished")}
        for k in ("nested", "assignee", "child"):
            if rec.get(k):
                entry[k] = rec[k]
        if rec.get("held"):
            entry["held"] = True
        if rec["status"] in OPEN_DELEGATION:
            entry["brief"] = brief(r, did)
            if rec.get("errors"):
                entry["errors"] = rec["errors"][-1]
        dels.append(entry)
    calls = []
    for cid, rec in (s.get("calls") or {}).items():
        entry = {k: rec.get(k) for k in ("id", "instance", "node", "label", "procedure",
                                         "status", "started", "finished")}
        entry["scope"] = rec["nested"]["scope"]
        if rec.get("status") != CALL_OPEN:
            entry["outcome"] = rec.get("outcome")
        calls.append(entry)
    out.update({
        "active": sorted(iid for iid, inst in r.inst.items() if inst.get("await")),
        "tasks": tasks, "choices": choices, "waits": waits,
    })
    if calls:
        out["calls"] = calls
    if groups:
        out["groups"] = groups
    if dels:
        out["delegations"] = dels
    if limits:
        out["loop_limits"] = limits
    if visit_limits:
        out["visit_limits"] = visit_limits
    if verifying:
        out["verifying"] = verifying
    if windows:
        out["waiting_window"] = windows
    if asks:
        out["asks"] = asks
    out["data"] = s.get("data")
    # Completed nodes' {outcome, value}, keyed by node label/id. The driver
    # reads its own status to decide what to do next, and a value can carry
    # the difference between a decision actually taken and one the run took
    # alone — an ask answered has the responder's handle in value.by, an ask
    # passed unanswered has "unanswered" (claunch-n74fs.1: a 0-answer pass
    # must be readable without spelunking the journal file).
    out["result"] = s.get("result") or {}
    if s.get(PERSON_PICKS):
        # the reason a person typed with a pick is their instruction to the
        # driver; the journal alone never reaches it (claunch-e437w)
        out[PERSON_PICKS] = s[PERSON_PICKS]
    move_req = s.get(MOVE_REQUEST)
    if isinstance(move_req, dict):
        if move_req.get("kind", MOVE_KIND_MOVE) == MOVE_KIND_MOVE and not move_req.get("decision") \
                and r.status in LIVE_STATUSES:
            # measured now, not when filed: the card shows what a grant does
            move_req = {**move_req, "plan": move_plan(r, str(move_req.get("node") or ""))}
        out["move_request"] = move_req
    hints = []
    if tasks:
        hints.append("do each task, then flow_report(node=<instance>, outcome, value)")
    if choices:
        hints.append("pick with flow_select(node, option, reason)")
    if any(d.get("status") == "pending" for d in dels):
        hints.append("run each pending delegation's brief in a sub-agent, then "
                     "flow_delegate(delegation, outcome, value)")
    if any(d.get("status") in ("queued", "running") and d.get("executor") in executors.DISPATCHED
           for d in dels):
        hints.append("session delegations are run by a child session the daemon spawns "
                     "and come back on their own; call flow_status again later")
    if waits:
        hints.append("waits fire on their own; call flow_status again later")
    if limits:
        hints.append("a loop hit its max: the user decides with 'claunch cflow2 extend'")
    if visit_limits:
        hints.append("a node passed the run's max_visits: the user decides with "
                     "'claunch cflow2 extend --visits'")
    if verifying:
        hints.append("verify commands are running; call flow_status again later")
    if windows:
        hints.append("a choice is held until its window opens (waiting_window): end your turn, "
                     "the daemon applies it then and wakes you; choosing the same option again "
                     "updates its reason, another option cancels the hold")
    if any((t.get("restart") or {}).get("status") in (RESTART_PENDING, RESTART_RUNNING)
           for t in tasks):
        hints.append("the daemon runs a task's restart command (never run it yourself); its "
                     "report is accepted once the restart result is in")
    if acc and acc.get("status") == "failed":
        hints.append("the run reached its end but acceptance checks failed: fix what they "
                     "found (flow_status checks again), or a person accepts it anyway with "
                     "'claunch cflow2 approve acceptance'")
    elif acc and acc.get("status") == "checking":
        hints.append("the run reached its end; its acceptance checks are running")
    if halts:
        hints.insert(0, f"HALTED by monitor {', '.join(halts)}: nothing moves until a person "
                        f"resumes the run ('claunch cflow2 resume'); tell the user")
    for a in asks:
        hints.append(f"{a['instance']}: {a['how']}")
    if isinstance(move_req, dict) and not move_req.get("decision") \
            and move_req.get("kind") == MOVE_KIND_REPORT:
        hints.insert(0, f"REPORT REQUESTED for {move_req.get('label') or move_req.get('node')!r} "
                        f"as {move_req.get('outcome')!r} ({move_req.get('id')}): nothing "
                        f"advances by your hand until a person answers (run page "
                        f"Report/Refuse, 'claunch cflow2 move --approve|--deny'); stop your "
                        f"turn, or withdraw it with flow_request_move(cancel=true)")
    elif isinstance(move_req, dict) and not move_req.get("decision"):
        hints.insert(0, f"MOVE REQUESTED to {move_req.get('node')!r} ({move_req.get('id')}): "
                        f"nothing advances by your hand until a person answers (run page "
                        f"Move/Refuse, 'claunch cflow2 move --approve|--deny'); stop your turn, "
                        f"or withdraw it with flow_request_move(cancel=true)")
    elif isinstance(move_req, dict) and move_req.get("decision") == "denied":
        what = ("report request for" if move_req.get("kind") == MOVE_KIND_REPORT
                else "move request to")
        hints.insert(0, f"your {what} {move_req.get('label') or move_req.get('node')!r} was "
                        f"REFUSED by "
                        f"{move_req.get('decided_by')}"
                        + (f" ({move_req.get('decided_reason')})"
                           if move_req.get("decided_reason") else "")
                        + "; continue on the declared route and say so in your next report")
    out["next"] = "; ".join(hints) or None
    return out


def journal(*, cwd: Optional[str] = None, scope: Optional[str] = None,
            limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return RunStore(cwd, scope).journal(limit)


# ---------------------------------------------------------------------------
# capability resolution for a driver


def load_definition(ref: str, cwd: Optional[str] = None) -> ir.Chart:
    """Compile ``ref``: a bare name loads from the layers; a path is
    accepted only when its real path lies inside ``<cwd>/.cflow2/generated/``
    (a generated procedure). Any other path is refused."""
    from . import layers

    ref = str(ref or "").strip()
    if layers.is_name(ref):
        return layers.load(ref, cwd)
    base = store_mod.resolve_cwd(cwd)
    home = os.path.realpath(str(base / store_mod.BASE_DIR / "generated"))
    path = Path(os.path.expanduser(ref))
    if not path.is_absolute():
        path = base / path
    real = os.path.realpath(str(path))
    if os.path.commonpath([home, real]) != home or real == home:
        raise FlowError(f"{ref!r} is a path outside {home}; a v2 definition is loaded "
                        f"by name, or by path only from .cflow2/generated/")
    if not os.path.isfile(real):
        raise FlowError(f"generated definition not found: {ref}")
    return layers.load_file(real, cwd)


def resolve_caps(profile: Optional[str] = None, harness: Optional[str] = None) -> Any:
    """The capability map for a profile (or the ambient one), or ``None``.

    The ambient profile is the one whose directory is ``$CLAUDE_CONFIG_DIR``
    (a managed Claude session); the harness defaults to the profile's.
    """
    if caps_mod is None:
        return None
    from .. import config

    if not profile:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        if cfg:
            try:
                p = Path(cfg).resolve()
                if p.parent == config.profiles_dir().resolve():
                    profile = p.name
            except OSError:
                profile = None
    if profile and not harness:
        try:
            from .. import lineage, profile as profile_mod
            harness = lineage.effective_harness(profile_mod.resolve_selector(profile))
        except Exception:  # noqa: BLE001 - a profile we cannot read: claude defaults
            harness = None
    try:
        return caps_mod.resolve(harness or "claude", profile=profile.split(":", 1)[0]
                                if profile else None)
    except Exception:  # noqa: BLE001 - capability layering must not block a run
        return None


# ---------------------------------------------------------------------------
# ask: approval to enter a task / a delegated decision (docs/cflow-v2.md 2.2)
#
# Entering a task with ``ask`` opens an ask record (``state["asks"][id]``,
# ids ``ask2-<hex>``) and parks the instance (``await: approval``); the driver
# cannot report on it. The record's ``resolution`` says who decides now:
#
#   routing -> the next ``from`` group has to be resolved against the mesh
#              (:mod:`.asks` does that outside the lock and applies it with
#              :func:`ask_apply_route`)
#   asked   -> responder sessions were asked; they answer with flow_answer
#              (:func:`answer`); past ``deadline`` the next group is tried
#   human   -> every group ran out under ``otherwise: human``: a person
#              answers (``claunch cflow2 approve``, the web UI)
#   self    -> every group ran out under bare ``otherwise: self``: the driver
#              decides with flow_approve
#
# A person's answer (:func:`approve` with caller user) lands whatever the
# resolution, like v1. Every new visit of the task asks again.


def _ask_spec(node: ir.Node) -> Dict[str, Any]:
    """The task's ``ask`` with every key present (charts compiled before the
    frontend normalised it stored the mapping as written)."""
    raw = dict(node.get("ask") or {})
    cands = []
    for c in raw.get("from") or []:
        c = dict(c or {})
        cands.append({"role": str(c.get("role") or "").lower(),
                      "scope": str(c.get("scope") or "any").lower(),
                      "connect": bool(c.get("connect"))})
    otherwise = str(raw.get("otherwise") or ASK_OTHERWISE_HUMAN)
    base, _, default = otherwise.partition(":")
    options = raw.get("options")
    if isinstance(options, dict):  # an un-normalised chart
        options = [{"name": k, "outcome": (v or {}).get("outcome", k) if isinstance(v, dict) else k,
                    "description": (v or {}).get("description", "") if isinstance(v, dict)
                    else str(v or "")} for k, v in options.items()]
    return {
        "prompt": str(raw.get("prompt") or ""),
        "brief": raw.get("brief"), "format": raw.get("format"),
        "choices": raw.get("choices"), "from_param": raw.get("from_param"),
        "require_reason": raw.get("require_reason") or False,
        "from": cands,
        "otherwise": base or ASK_OTHERWISE_HUMAN,
        "default_option": raw.get("default_option") or (default or None),
        "timeout": float(raw["timeout"]) if raw.get("timeout") else None,
        "on_decline": raw.get("on_decline") or ASK_DECLINED,
        "on_unroutable": raw.get("on_unroutable") or None,
        "urgent": raw.get("urgent") is True,
        "options": list(options) if options else None,
    }


def _ask_choices(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if rec.get("brief") is not None:
        return list(rec.get("options") or []) + [{
            "name": q_mod.REFRAME, "outcome": q_mod.REFRAME,
            "description": "none of the options fits the problem -- say what they miss in "
                           "the reason; the asker frames the question again from it and "
                           "asks anew"}]
    if rec.get("options"):
        return list(rec["options"])
    return [{"name": ASK_APPROVE, "outcome": None, "description": "enter the task"},
            {"name": ASK_DECLINE, "outcome": rec.get("on_decline") or ASK_DECLINED,
             "description": f"do not enter it; the task finishes "
                            f"'{rec.get('on_decline') or ASK_DECLINED}'"}]


def _brief_ask(r: Run, iid: str, node: ir.Node, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve a brief ask (docs 2.10) when it opens: the brief, its question
    type, the fixed choices and a ``from`` given as a param. Returns the
    record fields, or None after finishing the task ``fail`` (the reasons are
    in the value and the journal's ``ask_brief_invalid``)."""
    env = r.env(iid)
    params = env.get("params") or {}
    errors: List[str] = []

    def param(v: Any) -> Any:
        if isinstance(v, str) and v.startswith("$params."):
            return params.get(v[len("$params."):])
        return v

    decl, fmt = question_type(str(spec.get("format") or ""),
                              (r.chart.meta or {}).get("question_types") or {}, params)
    brief: Any = None
    try:
        brief = expr_mod.evaluate(spec["brief"], env)
    except expr_mod.ExprError as exc:
        errors.append(f"brief {spec['brief']}: {exc}")
    if decl is None:
        errors.append(f"no question type {fmt or spec.get('format')!r}")
    elif brief is None and not errors:
        errors.append(f"brief {spec['brief']} is empty -- the task that frames the question "
                      f"has not reported it")
    norm: Dict[str, Any] = {}
    if decl is not None and brief is not None:
        norm, errs = q_mod.validate(brief, decl)
        errors.extend(errs)
    choices = param(spec.get("choices"))
    if choices is not None and not errors:
        if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
            errors.append(f"choices must be a list of option names, got {choices!r}")
        else:
            got = sorted(n.lower() for n in q_mod.option_names(norm, decl))
            want = sorted(c.lower() for c in choices)
            if got != want:
                errors.append(f"the brief's options ({', '.join(q_mod.option_names(norm, decl))}) "
                              f"must be exactly the fixed choices ({', '.join(choices)})")
            else:
                # the definition branches on these names (cases): the answer
                # must come back spelled as the definition wrote them
                norm = _respell_choices(norm, decl, choices)
    cands = spec["from"]
    if spec.get("from_param") and not errors:
        raw_from = param(spec["from_param"])
        if raw_from:
            from ..cflow import model as v1

            try:
                d = v1._parse_delegate({"from": raw_from}, f"task {node.label}: ask")
                cands = [{"role": c.role, "scope": c.scope, "connect": c.connect}
                         for c in d.candidates]
            except v1.WorkflowError as exc:
                errors.append(f"from: {exc}")
        else:
            cands = []
    if errors:
        r.emit("ask_brief_invalid", node=node.id, instance=iid, format=fmt, errors=errors)
        _complete(r, iid, ir.OUTCOME_FAIL,
                  {"reason": "the question brief is not usable: " + "; ".join(errors),
                   "errors": errors})
        return None
    return {"format": fmt, "brief": norm, "options": q_mod.ask_options(norm, decl),
            "from": cands, "question_text": q_mod.render_text(norm, decl),
            "require_reason": bool(param(spec.get("require_reason")))}


def _respell_choices(brief: Dict[str, Any], decl: Dict[str, Any],
                     choices: List[str]) -> Dict[str, Any]:
    """The brief with its option names (and its recommendation) in the fixed
    choices' spelling; they already match without case."""
    spell = {c.casefold(): c for c in choices}
    out = json.loads(json.dumps(brief))
    for item in out.get(decl["options"]) or []:
        name = item.get("name")
        if isinstance(name, str) and name.casefold() in spell:
            item["name"] = spell[name.casefold()]
    rec = decl.get("recommendation")
    if rec and isinstance(out.get(rec), str) and out[rec].casefold() in spell:
        out[rec] = spell[out[rec].casefold()]
    return out


def _open_ask(r: Run, iid: str, node: ir.Node) -> None:
    spec = _ask_spec(node)
    brief = None
    if spec.get("brief"):
        brief = _brief_ask(r, iid, node, spec)
        if brief is None:
            return
        spec["options"], spec["from"] = brief["options"], brief["from"]
    aid = f"ask2-{secrets.token_hex(4)}"
    rec: Dict[str, Any] = {
        "id": aid, "instance": iid, "node": node.id, "label": shown_label(r.chart, node),
        "kind": "decision" if spec["options"] else "approval",
        "prompt": render(spec["prompt"], r.env(iid)),
        "from": spec["from"], "otherwise": spec["otherwise"],
        "default_option": spec["default_option"], "timeout": spec["timeout"],
        "on_decline": None if spec["options"] else spec["on_decline"],
        "on_unroutable": spec.get("on_unroutable"), "urgent": bool(spec.get("urgent")),
        "options": spec["options"], "status": "open",
        "resolution": RES_ROUTING if spec["from"] else None,
        "group": 0, "asked": [], "skipped": [], "deadline": None, "deferrals": 0,
        "retry_at": None, "opened": store_mod.utcnow(), "opened_at": r.now,
        "visit": int((r.state.get("visits") or {}).get(node_key(node)) or 0),
    }
    if brief is not None:
        rec.update(format=brief["format"], brief=brief["brief"],
                   question_text=brief["question_text"], intro=rec["prompt"],
                   require_reason=brief["require_reason"])
        if not rec["prompt"]:
            rec["prompt"] = brief["question_text"]
        else:
            rec["prompt"] = rec["prompt"].rstrip() + "\n\n" + brief["question_text"]
    r.state.setdefault("asks", {})[aid] = rec
    r.inst[iid]["await"] = AW_APPROVAL
    r.inst[iid]["frame"]["ask"] = aid
    r.state.setdefault("approvals", []).append({
        "kind": "ask", "instance": iid, "node": node.id, "label": shown_label(r.chart, node),
        "ask": aid, "resolution": rec["resolution"],
    })
    r.emit("waiting", node=node.id, instance=iid, kind="ask", ask=aid, ask_kind=rec["kind"],
           candidates=[_cand_text(c) for c in spec["from"]] or None,
           otherwise=spec["otherwise"])
    if not spec["from"]:
        _ask_fall(r, rec, "the ask declares no 'from' candidates")


def _cand_text(c: Dict[str, Any]) -> str:
    notes = [x for x in ("" if c.get("scope") in (None, "", "any") else c["scope"],
                         "connect" if c.get("connect") else "") if x]
    return f"{c.get('role')} ({', '.join(notes)})" if notes else str(c.get("role"))


def _ask_sync_approval(r: Run, rec: Dict[str, Any]) -> None:
    for a in r.state.get("approvals") or []:
        if a.get("kind") == "ask" and a.get("ask") == rec["id"]:
            a["resolution"] = rec.get("resolution")


def _ask_fall(r: Run, rec: Dict[str, Any], cause: str) -> None:
    """Every candidate group is spent: ``otherwise`` decides who answers."""
    rec["asked"], rec["deadline"], rec["retry_at"] = [], None, None
    rec["group"] = len(rec.get("from") or [])
    otherwise = rec.get("otherwise") or ASK_OTHERWISE_HUMAN
    default = rec.get("default_option") if otherwise == ASK_OTHERWISE_SELF else None
    r.emit("fallback", decision="ask", ask=rec["id"], node=rec["node"],
           instance=rec["instance"], cause=cause,
           route=f"ask -> {otherwise}" + (f":{default}" if default else ""))
    rec["resolution"] = RES_SELF if otherwise == ASK_OTHERWISE_SELF else RES_HUMAN
    _ask_sync_approval(r, rec)
    if default:
        try:
            default = _ask_decision(rec, default)
        except FlowError:
            return  # a default naming no option: the driver decides
        _ask_resolve(r, rec, default, by="unanswered",
                     reason="declared default, taken unanswered", channel="default")


def _ask_unroutable(r: Run, rec: Dict[str, Any], problem: str, meshes: List[str]) -> None:
    """Every group ran out because the roster could not be read as a pool --
    several meshes and none named, no membership, no daemon -- and the ask
    declares ``on_unroutable``: the task finishes with that outcome instead of
    taking ``otherwise`` (claunch-5itbf). ``otherwise`` answers "nobody holds
    the role"; this is "nobody could be looked for", and the definition
    decides what that means (improv-worker-v2 puts it to the agent as a
    choice, so the skipped review is a recorded decision, not a default)."""
    iid = rec["instance"]
    outcome = str(rec["on_unroutable"])
    rec["asked"], rec["deadline"], rec["retry_at"] = [], None, None
    rec["status"] = "unroutable"
    rec["closed"] = store_mod.utcnow()
    r.state["approvals"] = [a for a in r.state.get("approvals") or []
                            if not (a.get("kind") == "ask" and a.get("ask") == rec["id"])]
    r.emit("fallback", decision="ask", ask=rec["id"], node=rec["node"], instance=iid,
           cause=problem, route=f"ask -> {outcome}")
    if iid not in r.inst:
        return
    value: Dict[str, Any] = {"reason": problem, "ask": rec["id"],
                             "mesh": mesh_of(r.state) or None}
    if meshes:
        value["meshes"] = list(meshes)
    _complete(r, iid, outcome, value)


def _withdraw(r: Run, rec: Dict[str, Any], why: str,
              skip: Tuple[str, ...] = ()) -> None:
    """Responders that still had ``rec``'s question are told it is closed
    (sent after the lock is released)."""
    pending = [dict(e) for e in rec.get("asked") or []
               if e.get("session") not in skip
               and e.get("session") not in (rec.get("abstained") or [])]
    if rec.get("resolution") != RES_ASKED or not pending or not rec.get("mesh"):
        return
    r.withdrawals.append({
        "ask": rec["id"], "instance": rec.get("instance"), "node": rec.get("node"),
        "label": rec.get("label"), "decision": why, "workflow": r.state.get("workflow"),
        "mesh": rec.get("mesh"), "prompt": rec.get("prompt"), "pending": pending,
        "reason_kind": "closed",
    })
    r.emit("ask_withdrawn", ask=rec["id"], node=rec.get("node"), reason=why,
           to=[e.get("handle") for e in pending])


#: ``(answered, sender=...)`` -- how withdraw notices go out; ``None`` is
#: :func:`.asks.default_withdraw` (the mesh). Tests replace it.
WITHDRAW_FN: Optional[Callable[..., Any]] = None


def _send_withdrawals(items: List[Dict[str, Any]], sender: str) -> None:
    fn = WITHDRAW_FN
    if fn is None:
        from . import asks as asks_mod
        fn = asks_mod.default_withdraw
    for item in items:
        try:
            fn(item, sender=sender)
        except Exception:  # noqa: BLE001 - a notice; the state is recorded
            pass


def _ask_escalate(r: Run, rec: Dict[str, Any], why: str) -> None:
    """The group that had the question produced no answer: the next one."""
    _withdraw(r, rec, why)
    who = ", ".join(e.get("handle") or e.get("session") or "?" for e in rec.get("asked") or [])
    rec.setdefault("skipped", []).append(
        {"group": rec.get("group"), "candidate": who or "nobody", "reason": why})
    rec["group"] = int(rec.get("group") or 0) + 1
    rec["asked"], rec["deadline"], rec["retry_at"] = [], None, None
    rec["abstained"] = []
    if rec["group"] >= len(rec.get("from") or []):
        _ask_fall(r, rec, f"{why}; no candidate group left")
        return
    rec["resolution"] = RES_ROUTING
    _ask_sync_approval(r, rec)


def _ask_decision(rec: Dict[str, Any], decision: Optional[str], *,
                  allow_abstain: bool = False) -> str:
    """The option ``decision`` names, compared without case on both sides;
    returns the option's own spelling (``LGTM`` stays ``LGTM``)."""
    choice = str(decision or "").strip().casefold()
    names = [o["name"] for o in _ask_choices(rec)]
    for name in names:
        if str(name).casefold() == choice:
            return name
    if allow_abstain and choice == ASK_ABSTAIN:
        return ASK_ABSTAIN
    extra = f", or {ASK_ABSTAIN!r} if you have no basis to decide" if allow_abstain else ""
    raise FlowError(f"unknown decision {decision!r} for {rec['id']} -- answer with one of: "
                    f"{', '.join(names)}{extra}")


def _ask_need_reason(rec: Dict[str, Any], choice: str, reason: Optional[str], *,
                     by_user: bool = False) -> None:
    """An agent's or a responder's answer must carry the reason the ask asks
    for; a person's never has to (docs 2.11)."""
    if (by_user or rec.get("brief") is None or str(reason or "").strip()
            or choice == ASK_ABSTAIN):
        return
    if choice == q_mod.REFRAME:
        raise FlowError(f"'{q_mod.REFRAME}' needs a reason: say what the options miss, so the "
                        f"question can be framed again")
    if rec.get("require_reason"):
        raise FlowError(f"{rec['id']} needs a reason with the answer: say what you looked at "
                        f"to choose {choice!r}")


def _ask_resolve(r: Run, rec: Dict[str, Any], decision: str, *, by: str,
                 reason: Optional[str], channel: str) -> None:
    iid = rec["instance"]
    rec["status"] = "answered"
    rec["closed"] = store_mod.utcnow()
    rec["answer"] = {"decision": decision, "by": by, "reason": reason, "channel": channel}
    r.state["approvals"] = [a for a in r.state.get("approvals") or []
                            if not (a.get("kind") == "ask" and a.get("ask") == rec["id"])]
    r.emit("ask_answered", ask=rec["id"], node=rec["node"], instance=iid, decision=decision,
           by=by, reason=reason, channel=channel)
    if iid not in r.inst:
        return
    value = {"decision": decision, "reason": reason, "by": by, "ask": rec["id"]}
    if rec.get("brief") is not None:
        value["brief"] = rec["brief"]
        if decision == q_mod.REFRAME:
            value.pop("decision")
            _complete(r, iid, q_mod.REFRAME, value)
            return
    if rec.get("kind") == "decision":
        opt = next(o for o in rec["options"] if o["name"] == decision)
        _complete(r, iid, opt.get("outcome") or decision, value)
        return
    if decision == ASK_APPROVE:
        r.inst[iid]["await"] = AW_TASK
        return
    _complete(r, iid, rec.get("on_decline") or ASK_DECLINED, value)


def _expire_asks(r: Run) -> bool:
    """Asked groups past their deadline move on (``ask_timeout``)."""
    moved = False
    for rec in list((r.state.get("asks") or {}).values()):
        if rec.get("status") != "open" or rec.get("resolution") != RES_ASKED:
            continue
        if rec.get("deadline") is None or r.now < float(rec["deadline"]):
            continue
        who = [e.get("handle") or e.get("session") for e in rec.get("asked") or []]
        r.emit("ask_timeout", ask=rec["id"], node=rec["node"], instance=rec["instance"],
               group=rec.get("group"), asked=who, timeout=rec.get("timeout"))
        # _ask_escalate tells the responders the question moved on
        _ask_escalate(r, rec, f"no answer from {', '.join(map(str, who))} within "
                              f"{float(rec.get('timeout') or 0):g}s")
        moved = True
    return moved


def asks_to_route(r: Run) -> List[Dict[str, Any]]:
    """Open asks whose next candidate group has to be resolved now."""
    out = []
    for rec in (r.state.get("asks") or {}).values():
        if rec.get("status") == "open" and rec.get("resolution") == RES_ROUTING \
                and (rec.get("retry_at") is None or r.now >= float(rec["retry_at"])):
            out.append(rec)
    return out


def ask_apply_route(plans: List[Dict[str, Any]], *, cwd: Optional[str] = None,
                    scope: Optional[str] = None,
                    now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Apply routing plans computed off a snapshot (:func:`.asks.route`).

    A plan is ``{ask, from_group, group, asked, skipped, defer?, wired?}``; it
    applies only to an ask that is still open, still routing and still at
    ``from_group`` (anything else moved it meanwhile). Returns the applied
    plans whose ask now has responders (the caller delivers the question)."""
    routed: List[Dict[str, Any]] = []

    def op(r: Run) -> None:
        if r.status not in LIVE_STATUSES or store_mod.recovery_held(r.state):
            return
        for plan in plans:
            rec = (r.state.get("asks") or {}).get(plan["ask"])
            if not rec or rec.get("status") != "open" or rec.get("resolution") != RES_ROUTING \
                    or int(rec.get("group") or 0) != int(plan["from_group"]):
                continue
            if plan.get("defer"):
                rec["deferrals"] = int(rec.get("deferrals") or 0) + 1
                rec["retry_at"] = r.now + ROSTER_RETRY
                r.emit("ask_deferred", ask=rec["id"], node=rec["node"], group=rec["group"],
                       reason=plan["defer"], count=rec["deferrals"])
                continue
            rec.setdefault("skipped", []).extend(plan.get("skipped") or [])
            for s in plan.get("skipped") or []:
                r.emit("fallback", decision="ask", ask=rec["id"], node=rec["node"],
                       instance=rec["instance"], cause=s.get("reason"),
                       route=f"group {s.get('group')} -> next group")
            if plan.get("wired"):
                rec["wired"] = list(plan["wired"])
            if plan.get("asked"):
                rec["group"] = int(plan["group"])
                rec["asked"] = list(plan["asked"])
                rec["mesh"] = plan.get("mesh") or rec.get("mesh")
                rec["resolution"] = RES_ASKED
                rec["deadline"] = r.now + float(rec["timeout"]) if rec.get("timeout") else None
                rec["retry_at"] = None
                _ask_sync_approval(r, rec)
                r.emit("ask_routed", ask=rec["id"], node=rec["node"], instance=rec["instance"],
                       group=rec["group"], mesh=rec.get("mesh"),
                       to=[e.get("handle") for e in rec["asked"]], deadline=rec["deadline"])
                routed.append({**plan, "record": json.loads(json.dumps(rec)),
                               "workflow": r.state.get("workflow"),
                               "run": r.state.get("run_id")})
            elif plan.get("unroutable") and rec.get("on_unroutable"):
                rec["group"] = int(plan["group"])
                _ask_unroutable(r, rec, str(plan["unroutable"]), plan.get("meshes") or [])
            else:
                rec["group"] = int(plan["group"])
                reasons = [s.get("reason") for s in rec.get("skipped") or []]
                _ask_fall(r, rec, "no candidate could be asked: "
                          + ("; ".join(str(x) for x in reasons) or "none declared"))
    _mutate(cwd, scope, now, op)
    return routed


def _ask_how(iid: str, rec: Dict[str, Any]) -> str:
    res = rec.get("resolution")
    names = [o["name"] for o in _ask_choices(rec)]
    if rec.get("kind") == "decision":
        cli = f"'claunch cflow2 approve {iid} --option <{'|'.join(names)}>'"
        tool = f"flow_approve(node='{iid}', option=<{'|'.join(names)}>, reason)"
    else:
        cli = f"'claunch cflow2 approve {iid}' (or --decline --reason R)"
        tool = f"flow_approve(node='{iid}', reason) or with decline=true"
    if res == RES_ROUTING:
        return ("the question is being routed to "
                + " -> ".join(_cand_text(c) for c in rec.get("from") or [])
                + "; it moves on its own")
    if res == RES_ASKED:
        who = ", ".join(e.get("handle") or "?" for e in rec.get("asked") or [])
        return (f"asked {who} ({rec['id']}); their answer comes back on its own. A person "
                f"may answer first with {cli}")
    if res == RES_SELF:
        return f"nobody could be asked and the workflow leaves it to you: {tool}"
    return (f"a person decides: {cli} from a terminal that is not the run's own session, or "
            f"the web UI. Do not work around it; tell the user what is waiting")


def _describe_ask(r: Run, iid: str, node: ir.Node, rec: Dict[str, Any],
                  env: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "instance": iid, "node": node.id, "label": shown_label(r.chart, node),
        "title": node.get("title"), "ask": rec["id"], "kind": rec.get("kind"),
        "prompt": rec.get("prompt"), "task_prompt": render(node.get("prompt"), env),
        "options": [{"name": o["name"], "description": o.get("description") or ""}
                    for o in _ask_choices(rec)],
        "resolution": rec.get("resolution"), "urgent": bool(rec.get("urgent")),
        "asked": [e.get("handle") for e in rec.get("asked") or []],
        "deadline": rec.get("deadline"),
        "skipped": [s.get("reason") for s in rec.get("skipped") or []],
        "how": _ask_how(iid, rec),
        **(_ask_question(r, rec) if rec.get("brief") is not None else {}),
    }


def _ask_question(r: Run, rec: Dict[str, Any]) -> Dict[str, Any]:
    """A brief ask's question for status payloads (docs 2.10): the text the
    CLI and the mesh show, and ``question`` (``question.to_json``) for the web UI."""
    out = {"format": rec.get("format"), "brief": rec.get("brief"),
           "question_text": rec.get("question_text"), "intro": rec.get("intro") or "",
           "require_reason": bool(rec.get("require_reason"))}
    decl = ((r.chart.meta or {}).get("question_types") or {}).get(str(rec.get("format")))
    if decl is not None:
        out["question"] = q_mod.to_json(rec["brief"], decl)
    return out


def approve(node: Optional[str] = None, *, decision: Optional[str] = None,
            decline: bool = False, reason: Optional[str] = None,
            caller: str = CALLER_USER, by: Optional[str] = None,
            cwd: Optional[str] = None, scope: Optional[str] = None,
            now: Optional[float] = None) -> Dict[str, Any]:
    """Answer an open ask as a person (``caller`` user: the CLI, the web UI)
    or as the driver (``caller`` agent: flow_approve, only once the ask fell
    through to ``otherwise: self``).

    ``decision`` is an option name (an ask with ``options``) or approve /
    decline; ``decline`` is shorthand for the latter. The payload's
    ``answered`` names the responders that still had the question, so the
    caller can tell them it is closed (:func:`.asks.withdraw`)."""
    answered: Dict[str, Any] = {}

    acc_state = None
    try:
        acc_state = (RunStore(cwd, scope).load_state().get("acceptance") or {}).get("status")
    except StoreError:
        pass
    if acc_state == "failed" and node in (None, "", HOLD_ACCEPTANCE):
        if caller != CALLER_USER:
            raise FlowError("the run's acceptance check failed; a person accepts it anyway "
                            "('claunch cflow2 approve acceptance') or you fix what failed "
                            "and call flow_status, which checks again")
        payload = accept_anyway(reason, by=by or CALLER_USER, cwd=cwd, scope=scope, now=now)
        payload["answered"] = {"acceptance": "waived"}
        return payload

    def op(r: Run) -> None:
        _require_progress(r)
        if caller != CALLER_USER:
            _require_no_move_request(r)
        iid = _find(r, node, (AW_APPROVAL,), "ask")
        rec = r.state["asks"][r.inst[iid]["frame"]["ask"]]
        if caller != CALLER_USER and rec.get("resolution") != RES_SELF:
            raise FlowError(
                f"{rec['id']} on task {rec.get('label') or rec['node']} is not yours to "
                f"answer -- {_ask_how(iid, rec)}. A run cannot approve its own ask; only "
                f"an ask whose candidates all fell through to 'otherwise: self' is the "
                f"driver's")
        if decline and decision not in (None, ASK_DECLINE):
            raise FlowError("give --decline or a decision, not both")
        choice = decision if decision is not None else (
            ASK_DECLINE if decline else (ASK_APPROVE if rec.get("kind") == "approval" else None))
        if choice is None:
            raise FlowError(f"{rec['id']} is a decision: name an option "
                            f"({', '.join(o['name'] for o in _ask_choices(rec))})")
        choice = _ask_decision(rec, choice)
        _ask_need_reason(rec, choice, reason, by_user=caller == CALLER_USER)
        answered.update({
            "ask": rec["id"], "instance": iid, "node": rec["node"], "label": rec.get("label"),
            "decision": choice, "workflow": r.state.get("workflow"),
            "mesh": rec.get("mesh"), "prompt": rec.get("prompt"),
            "pending": [dict(e) for e in rec.get("asked") or []]
            if rec.get("resolution") == RES_ASKED else [],
        })
        who = by or (CALLER_USER if caller == CALLER_USER else "driver")
        _ask_resolve(r, rec, choice, by=who, reason=reason,
                     channel="human" if caller == CALLER_USER else "self")
    _mutate(cwd, scope, now, op)
    payload = status(cwd=cwd, scope=scope, now=now, tick=False)
    payload["answered"] = answered
    return payload


def answer(ask_id: str, decision: str, reason: Optional[str] = None, *,
           by_session: str, cwd: Optional[str] = None, scope: Optional[str] = None,
           now: Optional[float] = None) -> Dict[str, Any]:
    """A responder session's answer to an ask it was asked (flow_answer).

    ``by_session`` is the answering session's name, read by the MCP layer from
    the environment the daemon exported (``CLAUNCH_SESSION``), never from the
    call's arguments. Authority is membership in the group recorded when the
    question was routed; the run's own session can never answer it.
    ``abstain`` passes the question to the next group."""
    receipt: Dict[str, Any] = {}

    def op(r: Run) -> None:
        rec = (r.state.get("asks") or {}).get(ask_id)
        if r.status not in LIVE_STATUSES or not rec or rec.get("status") != "open" \
                or rec.get("resolution") != RES_ASKED:
            raise FlowError(
                f"request {ask_id!r} is not open for an answer any more -- it was answered, "
                f"it moved on to another group or a person, or the run left the task. "
                f"Nothing was applied")
        session = str(by_session or "").strip()
        if not session:
            raise FlowError("this call carries no session identity, so it cannot be "
                            "attributed; answer from a managed session")
        if session in (driver_of(r.state), str(r.state.get("scope") or "")):
            raise FlowError("this is your own run -- a task cannot approve itself. That is "
                            "the point of delegating the decision")
        _require_progress(r)
        mine = [e for e in rec.get("asked") or [] if e.get("session") == session]
        if not mine:
            who = ", ".join(e.get("handle") or "?" for e in rec.get("asked") or []) or "nobody"
            raise FlowError(f"{session!r} was not asked this -- it is with {who}")
        if session in (rec.get("abstained") or []):
            raise FlowError(f"you already abstained on {ask_id}; the others asked may still "
                            f"answer")
        handle = str(mine[0].get("handle") or session)
        choice = _ask_decision(rec, decision, allow_abstain=True)
        _ask_need_reason(rec, choice, reason)
        receipt.update({"ask": ask_id, "decision": choice, "reason": reason, "as": handle,
                        "workflow": r.state.get("workflow"), "task": rec.get("label")
                        or rec["node"], "prompt": rec.get("prompt")})
        if choice == ASK_ABSTAIN:
            r.emit("ask_answered", ask=ask_id, node=rec["node"], instance=rec["instance"],
                   decision=ASK_ABSTAIN, by=handle, by_session=session, reason=reason,
                   channel="responder")
            # one member's abstention leaves the question with the rest of
            # its group; it moves on when every member abstained (or the
            # group's deadline passes)
            rec.setdefault("abstained", []).append(session)
            asked = {e.get("session") for e in rec.get("asked") or []}
            if asked <= set(rec["abstained"]):
                _ask_escalate(r, rec, "every responder of the group abstained"
                              + (f" (last: {handle}: {reason})" if reason else ""))
                receipt["moved"] = True
                receipt["note"] = ("recorded as an abstention; everyone asked abstained, so "
                                   "the question moved on to the next candidate group (or to "
                                   "its fallback)")
            else:
                receipt["moved"] = False
                receipt["note"] = ("recorded as an abstention; the others asked with you may "
                                   "still answer")
            return
        _withdraw(r, rec, f"answered by {handle}: {choice}", skip=(session,))
        _ask_resolve(r, rec, choice, by=handle, reason=reason, channel="responder")
        receipt["note"] = "decision recorded; the asking session continues from it"
    _mutate(cwd, scope, now, op)
    return receipt


def open_asks(r: Run) -> List[Dict[str, Any]]:
    return [dict(rec) for rec in (r.state.get("asks") or {}).values()
            if rec.get("status") == "open"]


# ---------------------------------------------------------------------------
# collections, node effects ('do:'), triggers, 'wait: until' (doc 2.7, 2.8, 4.5)
#
# State (``state.json``):
#   collections: {name: {kind, items: [...], seq}}   (col_mod owns the shape)
#   cursors:     {address: seq}   -- a log's read position for this run; the
#                address is the collection name for this run's own log and the
#                full address (``side:stack:signals``) for a peer's;
#                ``<key>#<record name>`` is a named consume's position
#   cursor_epochs: {address: epoch} -- the peer log a peer cursor was moved on
#   triggers:    [{id, node, label, instance, at, do, push?, queued_at}]
#   trigger_seq: the last trigger id's number
#   watch_checked: {name: {str(key): epoch}} -- the last watch probe of a record

AT_ENTER = "enter"
AT_LEAVE = "leave"
#: The finish point of ``lifecycle.reset_at``.
RESET_FINISH = "finish"
#: ``wait: until`` read an address whose run is gone (doc 2.7).
OUTCOME_ABSENT = "absent"

PR_DRIVER = "driver"
PR_DAEMON = "daemon"
PR_CHILD = "child"
PR_PEER = "peer"
PR_PERSON = "person"

COLLECTION_OPS = ("push", "set", "remove")
EFFECT_OPS = ("push", "set", "remove", "consume")
#: A watch probe's time limit (doc 2.7, watch).
WATCH_TIMEOUT = 30


def _col_specs(r: "Run") -> Dict[str, Dict[str, Any]]:
    return dict((r.chart.meta or {}).get("collections") or {})


def _chart_col_specs(chart: ir.Chart) -> Dict[str, Dict[str, Any]]:
    return dict((chart.meta or {}).get("collections") or {})


def _col_state(r: "Run", name: str) -> Dict[str, Any]:
    specs = _col_specs(r)
    if name not in specs:
        raise col_mod.CollectionError(
            f"no collection {name!r} in this run (declared: {', '.join(specs) or 'none'})")
    cols = r.state.setdefault("collections", {})
    if not isinstance(cols.get(name), dict):
        cols[name] = _new_col(r, specs[name])
    return cols[name]


def _new_col(r: "Run", spec: Dict[str, Any]) -> Dict[str, Any]:
    """An empty collection of this run. ``epoch`` names the run that opened
    the log: a peer's cursor on it (doc 2.9) counts in that log's ``seq`` and
    is void on a log another run opened."""
    cs = col_mod.empty(spec)
    cs["epoch"] = str(r.state.get("run_id") or "")
    return cs


def _self_name(r: "Run", addr: Any) -> str:
    """The collection an effect names: a run writes only its own collections
    (doc 2.9: peers read each other, nobody writes another run's)."""
    target, name = col_mod.parse_address(addr)
    if _peer_scope(r, target) is not None:
        raise col_mod.CollectionError(
            f"collection address {addr!r}: a run writes only its own collections; "
            f"another run's are read (items, fresh, wait until) and consumed")
    _col_state(r, name)
    return name


# -- side runs (doc 2.9): the session's other runs -----------------------------


def _peer_scope(r: "Run", target: str) -> Optional[str]:
    """The slot an address target names, ``None`` for this run itself.
    ``main`` is the session's main run, ``side:<name>`` its side run
    ``name``; ``parent`` is not readable from inside a run (doc 2.8)."""
    if target == "self":
        return None
    if target in col_mod.TRIGGER_TARGETS:
        raise col_mod.CollectionError(f"{target}: addresses are only for triggers")
    own = r.store.scope
    main = store_mod.main_scope(own)
    if target == "main":
        scope = main
    elif target.startswith("side:"):
        try:
            scope = store_mod.side_scope(main, target.split(":", 1)[1])
        except StoreError as exc:
            raise col_mod.CollectionError(str(exc)) from None
    else:
        raise col_mod.CollectionError(f"unknown address target {target!r}")
    return None if scope == own else scope


def _load_peer(cwd: Path, scope: str) -> Optional[Dict[str, Any]]:
    """A peer run read without its lock (state files are replaced whole):
    ``{scope, state, specs, live}``, or None when the slot holds no run."""
    st = RunStore(str(cwd), scope)
    if not st.exists():
        return None
    try:
        state = st.load_state()
        chart = st.load_chart()
    except StoreError:
        return None
    return {"scope": scope, "state": state, "specs": _chart_col_specs(chart), "chart": chart,
            "live": state.get("status") in LIVE_STATUSES}


#: Joins a log's cursor key to a record name: the cursor a named consume
#: moves (``{from, name}``, doc 2.7).
NAME_SEP = "#"


def _cursor_key(own: bool, name: str, addr: str, record: Optional[str] = None) -> str:
    """The ``cursors`` key of a log: the collection name for this run's own
    log, the address for a peer's; ``#<record name>`` for a named cursor."""
    key = name if own else str(addr)
    return f"{key}{NAME_SEP}{record}" if record else key


def _cursor(r: "Run", key: str, cs: Optional[Dict[str, Any]], own: bool) -> int:
    """One cursor (0 when unset). A cursor on a peer's log is void when the
    log is not the one it was moved on (another epoch: a new run that did not
    inherit the log)."""
    cursor = (r.state.get("cursors") or {}).get(key)
    if cursor is None:
        return 0
    if not own and (r.state.get("cursor_epochs") or {}).get(key) != (cs or {}).get("epoch"):
        return 0
    return int(cursor)


def _log_position(r: "Run", addr: str, cs: Optional[Dict[str, Any]], own: bool,
                  record: Optional[str]) -> int:
    """Where this run stands in a log for ``fresh``: its whole-log cursor, or
    -- asking about one record name -- the later of that and the name's own."""
    _target, name = col_mod.parse_address(addr)
    pos = _cursor(r, _cursor_key(own, name, addr), cs, own)
    if record:
        pos = max(pos, _cursor(r, _cursor_key(own, name, addr, record), cs, own))
    return pos


def side_info(cwd: Any, main: str, name: str) -> Dict[str, Any]:
    """What ``side(name)`` reads (doc 2.9): the session's side run ``name``."""
    scope = store_mod.side_scope(main, name)
    peer = _load_peer(Path(str(cwd)), scope)
    if peer is None:
        return {"name": name, "scope": scope, "live": False, "status": None, "run": None,
                "workflow": None}
    st = peer["state"]
    return {"name": name, "scope": scope, "live": peer["live"], "status": st.get("status"),
            "run": st.get("run_id"), "workflow": st.get("workflow")}


#: Journal events that change what a peer reads off this run.
PEER_VISIBLE = ("collection_pushed", "collection_set", "collection_removed",
                "collection_reset", "collection_watch", "done", "aborted")


def _has_peers(r: "Run") -> bool:
    """A main run that declares sides, or a side run."""
    if store_mod.NESTED_SEP in r.store.scope:
        return False
    return bool((r.chart.meta or {}).get("sides")) or bool(store_mod.side_of(r.store.scope))



def peer_scopes(cwd: Any, scope: str) -> List[str]:
    """The other top-level slots of ``scope``'s session in ``cwd``: its main
    run and its side runs."""
    main = store_mod.main_scope(scope)
    out = []
    for other in store_mod.list_scopes(str(cwd)):
        if other == scope or store_mod.NESTED_SEP in other or store_mod.main_scope(other) != main:
            continue
        out.append(other)
    return out


#: how long a poke waits for a peer's lock: a peer that is busy is changing
#: its own state and re-evaluates its waits when it saves (or on the clock's
#: next pass), so the run that poked does not wait for it
POKE_LOCK_TIMEOUT = 0.1


def poke_peers(cwd: str, scope: str, now: Optional[float] = None) -> List[str]:
    """After a run changed what its peers read, re-evaluate the peers'
    ``wait: until`` now instead of on the clock's next pass. Runs after the
    changing run's lock is released; a busy peer is left to the clock."""
    poked = []
    for other in peer_scopes(cwd, scope):
        st = RunStore(cwd, other)
        try:
            state = st.load_state()
        except StoreError:
            continue
        if state.get("status") not in LIVE_STATUSES:
            continue
        if not any((i or {}).get("await") == AW_WAIT for i in (state.get("inst") or {}).values()):
            continue
        try:
            _mutate(cwd, other, now, lambda r: None, lock_timeout=POKE_LOCK_TIMEOUT)
            poked.append(other)
        except (FlowError, StoreError):
            continue
    return poked


def start_side(name: str, *, cwd: Optional[str] = None, main: Optional[str] = None,
               workflow: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
               inputs: Optional[Dict[str, Any]] = None, caps: Any = None,
               force: bool = False, now: Optional[float] = None,
               driver: Optional[str] = None, caller: str = CALLER_USER) -> Dict[str, Any]:
    """Start the side run ``name`` of the session whose main run is in slot
    ``main`` (doc 2.9): the main run must be live and declare it under
    ``sides:``; the side runs that definition in slot ``<main>+<name>``,
    driven by the main run's driver."""
    main = store_mod.normalize_scope(main)
    if store_mod.side_of(main) or store_mod.NESTED_SEP in main:
        raise FlowError(f"slot {main!r} is not a session's main run; a side run is started "
                        f"beside the main run")
    mst = RunStore(cwd, main)
    if not mst.exists():
        raise FlowError(f"no cflow2 run in slot {main!r}: a side run starts beside a live "
                        f"main run that declares it (sides:)")
    mstate = mst.load_state()
    mchart = mst.load_chart()
    if mstate.get("status") not in LIVE_STATUSES:
        raise FlowError(f"the main run ({mstate.get('workflow')}) is {mstate.get('status')}; "
                        f"side runs start only beside a live main run")
    sides = (mchart.meta or {}).get("sides") or {}
    if name not in sides:
        raise FlowError(f"{mchart.name} declares no side run {name!r} "
                        f"(sides: {', '.join(sides) or 'none'})")
    if workflow and str(workflow) != str(sides[name]):
        raise FlowError(f"side run {name!r} runs {sides[name]}, as {mchart.name} declares; "
                        f"got {workflow!r}")
    chart = load_definition(str(sides[name]), str(mst.cwd))
    who = driver_of(mstate) if driver is None else str(driver or "")
    return start(chart, cwd=str(mst.cwd), scope=store_mod.side_scope(main, name),
                 params=params, caps=caps, force=force, now=now, driver=who, caller=caller,
                 inputs=inputs, side={"name": name, "main": main,
                                      "main_run": mstate.get("run_id")})


def abort_sides(session: str, cwd: str, reason: str,
                now: Optional[float] = None) -> List[str]:
    """Abort every live side run of ``session`` in ``cwd`` (the daemon, when
    the session ended: doc 2.9). Returns the slots aborted."""
    out = []
    if not str(session or "").strip():
        return out
    try:
        main = store_mod.main_scope(store_mod.normalize_scope(str(session)))
    except StoreError:
        return out
    for scope in peer_scopes(cwd, main):
        if not store_mod.side_of(scope):
            continue
        try:
            state = RunStore(cwd, scope).load_state()
        except StoreError:
            continue
        if state.get("status") not in LIVE_STATUSES:
            continue
        try:
            abort(reason, cwd=cwd, scope=scope, now=now)
            out.append(scope)
        except (FlowError, StoreError):
            continue
    return out


class _ColResolver:
    """What :mod:`.expr` reads collections through (``items``, ``count``,
    ``fresh``, ``exists``, ``side``). ``strict`` (guards, effects): a
    collection a live peer does not declare is an error; a missing or
    finished peer reads as what it holds (nothing when there is no run).
    Not strict (``wait: until``): every such address is noted in ``absent``
    -- the wait ends ``absent`` unless the expression holds anyway."""

    def __init__(self, r: "Run", strict: bool = True) -> None:
        self.r = r
        self.strict = strict
        self.absent: List[str] = []
        self._peers: Dict[str, Optional[Dict[str, Any]]] = {}

    def _miss(self, addr: str) -> None:
        if addr not in self.absent:
            self.absent.append(addr)

    def _peer(self, scope: str) -> Optional[Dict[str, Any]]:
        if scope not in self._peers:
            self._peers[scope] = _load_peer(self.r.store.cwd, scope)
        return self._peers[scope]

    def _col(self, addr: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        """``(col_state, own)`` the address reads; ``(None, False)`` for a peer
        that holds nothing to read."""
        try:
            target, name = col_mod.parse_address(addr)
            if target in col_mod.TRIGGER_TARGETS and not self.strict:
                self._miss(addr)  # trigger-only (check refuses it); absent here
                return None, False
            scope = _peer_scope(self.r, target)
        except col_mod.CollectionError as exc:
            raise expr_mod.ExprError(str(exc)) from None
        if scope is None:
            specs = _col_specs(self.r)
            if name not in specs:
                raise expr_mod.ExprError(
                    f"no collection {name!r} in this run (declared: "
                    f"{', '.join(specs) or 'none'})")
            return (self.r.state.get("collections") or {}).get(name), True
        peer = self._peer(scope)
        if peer is None:
            self._miss(addr)
            return None, False
        if name not in peer["specs"]:
            if self.strict and peer["live"]:
                raise expr_mod.ExprError(
                    f"{addr!r}: the run in slot {scope!r} ({peer['state'].get('workflow')}) "
                    f"declares no collection {name!r}")
            self._miss(addr)
            return None, False
        if not peer["live"]:
            self._miss(addr)
        return (peer["state"].get("collections") or {}).get(name), False

    def items(self, addr: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        cs, _own = self._col(addr)
        return col_mod.items(cs, status)

    def fresh(self, addr: str, field: Optional[str] = None) -> bool:
        cs, own = self._col(addr)
        return col_mod.fresh(cs, _log_position(self.r, addr, cs, own, field), field)

    def exists(self, addr: str) -> bool:
        try:
            target, name = col_mod.parse_address(addr)
            scope = _peer_scope(self.r, target)
        except col_mod.CollectionError:
            return False
        if scope is None:
            return name in _col_specs(self.r)
        peer = self._peer(scope)
        return bool(peer and peer["live"] and name in peer["specs"])

    def side(self, name: str) -> Dict[str, Any]:
        try:
            return side_info(self.r.store.cwd, store_mod.main_scope(self.r.store.scope), name)
        except StoreError as exc:
            raise expr_mod.ExprError(str(exc)) from None


def _init_collections(r: "Run", previous: Optional[Dict[str, Any]]) -> None:
    """A new run's collections: empty, or -- ``lifecycle.inherit`` -- the
    previous run's in this slot (same workflow name), cleaned up, with this
    run's cursors on them. Cursors on peers' logs (doc 2.9) carry over with
    their epochs: they are this slot's place in another run's log."""
    old_cursors = (previous or {}).get("cursors") or {}
    old_epochs = (previous or {}).get("cursor_epochs") or {}
    for key in [k for k in old_cursors if ":" in str(k) and k in old_epochs]:
        r.state.setdefault("cursors", {})[key] = old_cursors[key]
        r.state.setdefault("cursor_epochs", {})[key] = old_epochs[key]
    specs = _col_specs(r)
    if not specs:
        return
    cols = r.state.setdefault("collections", {})
    cursors = r.state.setdefault("cursors", {})
    old_cols = (previous or {}).get("collections") or {}
    for name, spec in specs.items():
        if previous is not None and spec["lifecycle"].get("inherit"):
            cols[name], summary = col_mod.inherit_report(old_cols.get(name), spec)
            if not summary.get("inherited"):
                cols[name] = _new_col(r, spec)
            else:
                # an inherited log keeps its epoch: peers' cursors stay valid
                cols[name]["epoch"] = ((old_cols.get(name) or {}).get("epoch")
                                       or str(previous.get("run_id") or ""))
                for key in old_cursors:
                    if key == name or str(key).startswith(name + NAME_SEP):
                        cursors[key] = old_cursors[key]
                ev = dict(summary)
                ev.pop("event", None)
                r.emit("collection_inherited", run_from=previous.get("run_id"),
                       cursor=cursors.get(name), **ev)
            continue
        cols[name] = _new_col(r, spec)
    r.state.setdefault("triggers", [])


def _col_apply(r: "Run", op: str, name: str, *, principal: str, by: str, key: Any = None,
               item: Any = None, fields: Any = None, tid: Optional[str] = None) -> Dict[str, Any]:
    """One push/set/remove on this run's collection ``name`` (journaled);
    :class:`.collections.CollectionError` when refused."""
    cs = _col_state(r, name)
    spec = _col_specs(r)[name]
    if op == "push":
        ev = col_mod.apply_push(cs, spec, item, principal=principal, by=by, now=r.now, tid=tid)
    elif op == "set":
        ev = col_mod.apply_set(cs, spec, key, fields, principal=principal, by=by, now=r.now)
    elif op == "remove":
        ev = col_mod.apply_remove(cs, spec, key, principal=principal, by=by, now=r.now)
    else:
        raise col_mod.CollectionError(f"unknown collection operation {op!r} "
                                      f"(push, set, remove)")
    ev = dict(ev)
    r.emit(str(ev.pop("event")), **ev)
    return ev


def _consume(r: "Run", addr: Any, by: str, record: Optional[str] = None) -> None:
    """Move this run's cursor to the end of a log -- its own, or a peer's
    (the cursor is this run's, so nothing is written to the peer). With
    ``record``, only the cursor for records of that name moves: the log's
    other names stay fresh."""
    if isinstance(addr, dict):
        addr, record = addr.get("addr"), addr.get("name") or record
    target, name = col_mod.parse_address(addr)
    scope = _peer_scope(r, target)
    key = _cursor_key(scope is None, name, str(addr), record)
    if scope is None:
        cs = _col_state(r, name)
        epoch = None
    else:
        peer = _load_peer(r.store.cwd, scope)
        if peer is None or name not in peer["specs"]:
            raise col_mod.CollectionError(
                f"consume {addr!r}: no run in slot {scope!r} declares {name!r}")
        cs = (peer["state"].get("collections") or {}).get(name) or col_mod.empty(
            peer["specs"][name])
        epoch = cs.get("epoch")
    if cs.get("kind") != "log":
        raise col_mod.CollectionError(f"consume {addr!r}: only a log has a cursor")
    cursors = r.state.setdefault("cursors", {})
    before = cursors.get(key)
    cursors[key] = col_mod.end_cursor(cs)
    if scope is not None:
        r.state.setdefault("cursor_epochs", {})[key] = epoch
    r.emit("cursor_moved", address=key, before=before, after=cursors[key], by=by)


# -- node effects ('do:') ------------------------------------------------------------


def _effect_parts(raw: Any) -> Tuple[str, Any, str, Any]:
    """``(op, body, at, when)`` of one ``do:`` entry. The compiled shape is
    flat (``{op, addr, at, when, item | key, fields}``, frontend
    ``_do_list``); the surface form ``{push: {to, item}, at?, when?}``
    (``{consume: ADDR}``) is read as well."""
    if not isinstance(raw, dict):
        raise col_mod.CollectionError(f"a do: entry must be a mapping, got {raw!r}")
    op = raw.get("op")
    if op is None:
        found = [k for k in EFFECT_OPS if k in raw]
        if len(found) != 1:
            raise col_mod.CollectionError(
                f"a do: entry names one of {', '.join(EFFECT_OPS)}: {raw!r}")
        op = found[0]
    body = raw[op] if op in raw else raw
    return str(op), body, str(raw.get("at") or AT_LEAVE), raw.get("when")


def _effect_value(r: "Run", iid: Optional[str], value: Any, env: Dict[str, Any]) -> Any:
    """A field value of an effect: an expression (doc 2.7: string constants
    are quoted), or the compiler's ``{expr}``/``{lit}`` wrapping."""
    if isinstance(value, dict) and "expr" in value and len(value) == 1:
        return expr_mod.evaluate(value["expr"], env)
    if isinstance(value, dict) and "lit" in value and len(value) == 1:
        return value["lit"]
    if isinstance(value, str):
        return expr_mod.evaluate(value, env)
    return value


def _effect_env(r: "Run", iid: str, outcome: Optional[str]) -> Dict[str, Any]:
    env = r.env(iid)
    if outcome is not None:
        env["outcome"] = outcome
    return env


def _apply_effects(r: "Run", iid: str, node: ir.Node, at: str,
                   outcome: Optional[str] = None) -> Optional[str]:
    """Apply the node's ``do:`` entries for ``at``, all or nothing; the
    reason when one fails (refused access, a bad field, an expression
    error), ``None`` otherwise. The principal is the driver."""
    entries = node.get("do") or []
    if not entries or not isinstance(entries, list):
        return None
    saved = (copy.deepcopy(r.state.get("collections")), copy.deepcopy(r.state.get("cursors")))
    mark = len(r.events)
    by = driver_of(r.state) or PR_DRIVER
    try:
        for raw in entries:
            op, body, when_at, when = _effect_parts(raw)
            if when_at != at:
                continue
            env = _effect_env(r, iid, outcome if at == AT_LEAVE else None)
            if when is not None and not expr_mod.truthy(_effect_value(r, iid, when, env)):
                continue
            if op == "consume":
                addrs = body.get("addr") if isinstance(body, dict) else body
                record = body.get("name") if isinstance(body, dict) else None
                for addr in (addrs if isinstance(addrs, list) else [addrs]):
                    _consume(r, addr, by, record)
                continue
            if not isinstance(body, dict):
                raise col_mod.CollectionError(f"{op}: needs a mapping, got {body!r}")
            addr = body.get("addr") or (body.get("to") if op == "push" else body.get("in"))
            name = _self_name(r, addr)
            if op == "push":
                raw_item = body.get("item")
                if not isinstance(raw_item, dict):
                    raise col_mod.CollectionError(f"push to {addr!r}: 'item' must be a mapping")
                item = {k: _effect_value(r, iid, v, env) for k, v in raw_item.items()}
                _col_apply(r, "push", name, principal=PR_DRIVER, by=by, item=item)
                continue
            key = _effect_value(r, iid, body.get("key"), env)
            if op == "set":
                raw_fields = body.get("fields")
                if not isinstance(raw_fields, dict):
                    raise col_mod.CollectionError(f"set in {addr!r}: 'fields' must be a mapping")
                fields = {k: _effect_value(r, iid, v, env) for k, v in raw_fields.items()}
                _col_apply(r, "set", name, principal=PR_DRIVER, by=by, key=key, fields=fields)
            else:
                _col_apply(r, "remove", name, principal=PR_DRIVER, by=by, key=key)
    except (col_mod.CollectionError, expr_mod.ExprError) as exc:
        r.state["collections"], r.state["cursors"] = saved
        del r.events[mark:]
        return f"do ({at}) on {node.label or node.id}: {exc}"
    return None


def _leave_effects(r: "Run", iid: str, node: ir.Node, outcome: str,
                   value: Any) -> Tuple[str, Any]:
    """``do`` at leave; a failing effect turns the outcome to ``fail``."""
    err = _apply_effects(r, iid, node, AT_LEAVE, outcome)
    if not err:
        return outcome, value
    value = {"reason": err, "original": {"outcome": outcome, "value": value}}
    r.state.setdefault("result", {})[node_key(node)] = {"outcome": ir.OUTCOME_FAIL,
                                                        "value": value}
    return ir.OUTCOME_FAIL, value


# -- triggers --------------------------------------------------------------------------


def _trigger_parts(raw: Any) -> Tuple[Optional[str], str, Optional[Dict[str, Any]]]:
    """``(do, at, push)``: ``briefing`` (at enter), ``{do, at}``, ``{at, push}``."""
    if isinstance(raw, str):
        return raw, AT_ENTER, None
    if not isinstance(raw, dict):
        return None, AT_ENTER, None
    push = raw.get("push") if isinstance(raw.get("push"), dict) else None
    do = raw.get("do") or ("push" if push is not None else None)
    return (str(do) if do else None), str(raw.get("at") or AT_ENTER), push


def _push_data_keys(push: Dict[str, Any]) -> List[str]:
    """The top-level ``$data`` keys a trigger push's ``each``/``item``
    expressions read (doc 2.8), sorted; an expression that does not parse
    contributes none (the daemon reports it when it evaluates)."""
    keys: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for sub in node:
                walk(sub)
            return
        if not isinstance(node, tuple) or not node:
            return
        if node[0] == "ref":
            if node[1] == "data" and node[2] and node[2][0][0] == "field":
                keys.add(str(node[2][0][1]))
            for seg in node[2]:
                if seg[0] == "index":
                    walk(seg[1])
            return
        for item in node[1:]:
            walk(item)

    texts = [push.get("each")] + list((push.get("item") or {}).values())
    for text in texts:
        if isinstance(text, str):
            try:
                walk(expr_mod.parse(text).ast)
            except expr_mod.ExprError:
                continue
    return sorted(keys)


def _queue_triggers(r: "Run", iid: str, node: ir.Node, at: str,
                    outcome: Optional[str] = None) -> None:
    """Record the node's triggers for ``at`` (``trigger_queued``); the daemon
    clock claims and performs them (doc 2.8). Every pass records again. A
    trigger whose ``when`` is false now is not recorded (``trigger_not_queued``
    says so); one whose ``when``
    cannot be evaluated is not recorded either (``trigger_skipped`` says why
    -- a trigger never holds the run)."""
    for raw in node.get("triggers") or []:
        do, when_at, push = _trigger_parts(raw)
        if not do or when_at != at:
            continue
        when = raw.get("when") if isinstance(raw, dict) else None
        if when is not None:
            try:
                ok = expr_mod.truthy(_effect_value(
                    r, iid, when, _effect_env(r, iid, outcome if at == AT_LEAVE else None)))
            except expr_mod.ExprError as exc:
                r.emit("trigger_skipped", node=node.id, label=node.label or None,
                       instance=iid, at=at, do=do, reason=f"when ({when}): {exc}")
                continue
            if not ok:
                r.emit("trigger_not_queued", node=node.id, label=node.label or None,
                       instance=iid, at=at, do=do, reason=f"when ({when}) is false")
                continue
        r.state["trigger_seq"] = int(r.state.get("trigger_seq") or 0) + 1
        tid = f"t{r.state['trigger_seq']}"
        rec: Dict[str, Any] = {"id": tid, "run": str(r.state.get("run_id") or ""),
                               "node": node.id, "label": node.label or None,
                               "instance": iid, "at": at, "do": do,
                               "queued_at": store_mod.utcnow()}
        if push is not None:
            rec["push"] = copy.deepcopy(push)
            # $data.<key> in a push expression reads the run's data as it is
            # now, at the node change -- the daemon performs it later, from a
            # record that must not depend on what the run did meanwhile.
            keys = _push_data_keys(push)
            if keys:
                data = r.state.get("data") or {}
                rec["data"] = {k: copy.deepcopy(data.get(k)) for k in keys}
        r.state.setdefault("triggers", []).append(rec)
        r.emit("trigger_queued", trigger=tid, node=node.id, label=node.label or None,
               instance=iid, at=at, do=do)


#: A claim older than this many seconds is released and handed out again
#: (2x the daemon's per-trigger limit, ``cflow2_triggers.ACTION_TIMEOUT``).
TRIGGER_CLAIM_STALE = 240.0


def claim_triggers(cwd: Optional[str] = None, scope: Optional[str] = None, *,
                   boot: str = "", now: Optional[float] = None,
                   stale_after: float = TRIGGER_CLAIM_STALE,
                   held: Any = ()) -> List[Dict[str, Any]]:
    """Hand out the recorded triggers that are not claimed yet, marking each
    in ``state.triggers`` with ``claimed_at`` and ``claim_boot`` (the record
    stays until :func:`complete_trigger`: at least once, doc 2.8). A claim
    made by another daemon boot, or older than ``stale_after`` seconds, is
    released first (``trigger_requeued``) and handed out again -- unless its
    id is in ``held`` (the caller still has it in hand). A run in
    any status is claimable -- the last node's leave triggers are recorded
    as the run finishes."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return []
    clock_now = time.time() if now is None else float(now)
    boot = str(boot or "")
    held = {str(h) for h in held or ()}
    with st.lock():
        try:
            state = st.load_state()
        except StoreError:
            return []
        recs = state.get("triggers") or []
        if not recs:
            return []
        if any(a.get("monitor") == "storage-recovery" for a in state.get("approvals", [])):
            return []
        events: List[Dict[str, Any]] = []
        taken: List[Dict[str, Any]] = []
        for rec in recs:
            if rec.get("claimed_at") is not None:
                if str(rec.get("id") or "") in held:
                    continue
                why = None
                if str(rec.get("claim_boot") or "") != boot:
                    why = "the daemon restarted before this trigger completed"
                elif clock_now - float(rec.get("claimed_at") or 0) > stale_after:
                    why = f"the claim is older than {stale_after:g}s"
                if why is None:
                    continue
                events.append({"ts": store_mod.utcnow(), "event": "trigger_requeued",
                               "trigger": rec.get("id"), "reason": why})
            rec["claimed_at"] = clock_now
            rec["claim_boot"] = boot
            taken.append(copy.deepcopy(rec))
        if not taken:
            return []
        st.commit(state, events)
    return taken


def complete_trigger(cwd: Optional[str], scope: Optional[str], tid: str, performed: bool,
                     detail: Any = None) -> bool:
    """End a claimed trigger: remove its record from ``state.triggers`` and
    journal ``trigger_done`` (``detail``) or ``trigger_skipped``
    (``reason``). False when the slot holds no run."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return False
    ev: Dict[str, Any] = {"ts": store_mod.utcnow(),
                          "event": "trigger_done" if performed else "trigger_skipped",
                          "trigger": tid}
    if detail is not None:
        ev["detail" if performed else "reason"] = detail
    with st.lock():
        try:
            state = st.load_state()
        except StoreError:
            return False
        if state is not None:
            recs = state.get("triggers") or []
            kept = [rec for rec in recs if str(rec.get("id") or "") != str(tid)]
            if len(kept) != len(recs):
                state["triggers"] = kept
        st.commit(state, [ev])
    return True


# -- lifecycle.reset_at ----------------------------------------------------------------


def _reset_at(r: "Run", node: Optional[ir.Node]) -> None:
    """Clean up the collections whose ``reset_at`` names ``node`` (its id or
    label), or ``finish`` when ``node`` is None (the run ended, any status)."""
    specs = _col_specs(r)
    if not specs:
        return
    names = (RESET_FINISH,) if node is None else tuple(x for x in (node.id, node.label) if x)
    for name, spec in specs.items():
        points = spec.get("lifecycle", {}).get("reset_at") or []
        hit = next((p for p in names if p in points), None)
        if hit is None:
            continue
        summary = dict(col_mod.reset(_col_state(r, name), spec))
        summary.pop("event", None)
        r.emit("collection_reset", at=hit, **summary)


# -- wait: until ------------------------------------------------------------------------


def _until_step(r: "Run", iid: str, node: ir.Node) -> bool:
    """Evaluate a ``wait: until`` instance: ``ok`` (and consume) when the
    expression holds, ``absent`` when it read another run that is not there,
    ``timeout`` past ``seconds``; an expression error fails it. True when
    the wait finished."""
    resolver = _ColResolver(r, strict=False)
    env = r.env(iid)
    env[expr_mod.COLLECTIONS_KEY] = resolver
    try:
        held = expr_mod.truthy(expr_mod.evaluate(node.get("until"), env))
    except expr_mod.ExprError as exc:
        _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"until: {exc}"})
        return True
    if not held and resolver.absent:
        _complete(r, iid, OUTCOME_ABSENT, {"absent": resolver.absent})
        return True
    if held:
        by = driver_of(r.state) or PR_DRIVER
        try:
            for addr in node.get("consume") or []:
                _consume(r, addr, by)
        except col_mod.CollectionError as exc:
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"consume: {exc}"})
            return True
        _complete(r, iid, ir.OUTCOME_OK, {"until": node.get("until")})
        return True
    due = r.inst[iid]["frame"].get("due")
    if due is not None and r.now >= due:
        _complete(r, iid, ir.OUTCOME_TIMEOUT, {"seconds": node.get("seconds")})
        return True
    return False


def _tick_until(r: "Run") -> int:
    """Re-evaluate every ``wait: until`` after a change to the run."""
    if _halted(r):
        return 0
    moved = 0
    for _ in range(100):
        fired = False
        for iid in sorted(r.inst, key=lambda k: int(k.rsplit("@", 1)[1])):
            inst = r.inst.get(iid)
            if not inst or inst.get("await") != AW_WAIT:
                continue
            node = r.chart.node(inst["node"])
            if node.get("until") is not None and _until_step(r, iid, node):
                fired = True
                break
        if not fired:
            break
        moved += 1
    return moved


# -- external operations (daemon, MCP, CLI) ---------------------------------------------


def _col_summary(r: "Run") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    cols = r.state.get("collections") or {}
    cursors = r.state.get("cursors") or {}
    for name, spec in _col_specs(r).items():
        cs = cols.get(name) or col_mod.empty(spec)
        recs = col_mod.items(cs)
        entry: Dict[str, Any] = {"kind": spec["kind"], "count": len(recs), "seq": cs.get("seq", 0),
                                 "items": recs}
        if spec.get("key"):
            entry["key"] = spec["key"]
        if spec.get("status"):
            counts: Dict[str, int] = {}
            for rec in recs:
                counts[str(rec.get("status"))] = counts.get(str(rec.get("status")), 0) + 1
            entry["statuses"] = counts
        if spec["kind"] == "log":
            entry["cursor"] = cursors.get(name)
            entry["fresh"] = sum(1 for rec in recs
                                 if int(rec.get("seq") or 0) > int(cursors.get(name) or 0))
        out[name] = entry
    return out


def collection_view(cwd: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    """The run's collections (records, counts, cursors) and how many
    triggers wait for the daemon -- for the web UI, MCP and status."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return {"status": "idle", "scope": st.scope, "cwd": str(st.cwd), "collections": {},
                "pending_triggers": 0}
    r = Run.load(st)
    return {"run": r.state.get("run_id"), "workflow": r.state.get("workflow"),
            "status": r.status, "scope": st.scope, "cwd": str(st.cwd),
            "driver": driver_of(r.state) or None, "collections": _col_summary(r),
            "pending_triggers": len(r.state.get("triggers") or [])}


def collection_op(op: str, name: str, *, cwd: Optional[str] = None,
                  scope: Optional[str] = None, principal: str, session: str = "",
                  key: Any = None, item: Any = None, fields: Any = None,
                  now: Optional[float] = None, tid: Optional[str] = None) -> Dict[str, Any]:
    """push/set/remove on the run's collection ``name`` from outside the run
    (the daemon, an agent's MCP call, a person's CLI): under the run lock,
    after the time tick, with ``access`` enforced for ``principal``. A
    ``wait: until`` the change satisfies finishes in the same call. Returns
    :func:`status` with ``applied`` (the journaled event). ``tid`` marks a
    daemon trigger push (:func:`.collections.apply_push`)."""
    if op not in COLLECTION_OPS:
        raise FlowError(f"unknown collection operation {op!r} ({', '.join(COLLECTION_OPS)})")
    by = str(session or "").strip() or principal

    def fn(r: Run) -> Dict[str, Any]:
        _require_live(r)
        try:
            return _col_apply(r, op, name, principal=principal, by=by,
                              key=_typed_key(r, name, key), item=item, fields=fields,
                              tid=tid if op == "push" else None)
        except col_mod.CollectionError as exc:
            raise FlowError(str(exc)) from None

    ev = _mutate(cwd, scope, now, fn)
    out = status(cwd=cwd, scope=scope, now=now, tick=False)
    out["applied"] = ev
    return out


def _typed_key(r: "Run", name: str, key: Any) -> Any:
    """A key from outside (a CLI argument is text) read as the declared key
    field's type: ``"12"`` for an ``int`` key is 12, 12 for a ``str`` key is
    ``"12"``."""
    spec = _col_specs(r).get(name) or {}
    kfield = spec.get("key")
    if key is None or not kfield:
        return key
    ktype = ((spec.get("item") or {}).get(kfield) or {}).get("type")
    if ktype == "int" and isinstance(key, str):
        try:
            return int(key.strip())
        except ValueError:
            return key
    if ktype == "str" and isinstance(key, int) and not isinstance(key, bool):
        return str(key)
    return key


def _env_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def watch_due(cwd: Optional[str] = None, scope: Optional[str] = None,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The watch probes to run now: one per unsettled record of each watched
    queue whose last check is ``every`` seconds old or more. Each check is
    recorded as made (``watch_checked``) when it is handed out. Entries:
    ``{collection, key, probe, env, on, timeout, cwd}``; ``env`` carries
    ``CFLOW_ITEM_<FIELD>``, ``CFLOW_<NAME>`` and ``CFLOW_SESSION``."""
    st = RunStore(cwd, scope)
    if not st.exists():
        return []
    out: List[Dict[str, Any]] = []
    with st.lock():
        r = Run.load(st, now)
        if r.status not in LIVE_STATUSES:
            return []
        session = driver_of(r.state)
        checked = r.state.setdefault("watch_checked", {})
        for name, spec in _col_specs(r).items():
            watch = spec.get("watch")
            if not watch or spec["kind"] != "queue":
                continue
            every = max(int(watch.get("every") or col_mod.WATCH_DEFAULT_EVERY),
                        col_mod.WATCH_MIN_EVERY)
            seen = checked.setdefault(name, {})
            cs = (r.state.get("collections") or {}).get(name)
            live_keys = set()
            for rec in col_mod.unsettled(cs, spec):
                key = rec.get(spec["key"])
                live_keys.add(str(key))
                last = seen.get(str(key))
                if last is not None and r.now - float(last) < every:
                    continue
                seen[str(key)] = r.now
                env = {"CFLOW_SESSION": session}
                for ename, evalue in (spec.get("env") or {}).items():
                    if evalue is not None:
                        # upper case, like CFLOW_IN_*/CFLOW_DATA_* (doc 2.3)
                        env["CFLOW_" + str(ename).upper()] = \
                            str(evalue).replace("{session}", session)
                for field, value in rec.items():
                    if field in ("seq", "at", "by"):
                        continue
                    env["CFLOW_ITEM_" + re.sub(r"[^A-Za-z0-9]", "_", field).upper()] = \
                        _env_value(value)
                out.append({"collection": name, "key": key, "probe": watch.get("probe"),
                            "env": env, "on": copy.deepcopy(watch.get("on") or {}),
                            "timeout": WATCH_TIMEOUT, "cwd": str(st.cwd)})
            for k in [k for k in seen if k not in live_keys]:
                seen.pop(k, None)  # settled or removed records
        if out:
            r.save()
    return out


def watch_apply(cwd: Optional[str], scope: Optional[str], name: str, key: Any, code: int,
                now: Optional[float] = None) -> Dict[str, Any]:
    """Apply a watch probe's exit ``code`` to record ``key`` by the
    collection's ``watch.on`` (principal ``daemon``); ``collection_watch``
    records it. A code without an entry changes nothing and is not
    journaled; nor is a record that settled or went away meanwhile."""
    out: Dict[str, Any] = {"collection": name, "key": key, "code": code, "applied": False}

    def fn(r: Run) -> None:
        if r.status not in LIVE_STATUSES:
            out["reason"] = f"run is {r.status}"
            return
        specs = _col_specs(r)
        spec = specs.get(name)
        if not spec:
            out["reason"] = f"no collection {name!r}"
            return
        try:
            action = col_mod.watch_action(spec, int(code))
        except (TypeError, ValueError):
            action = None
        if not action:
            out["reason"] = "no action for this exit code"
            return
        live = [rec for rec in col_mod.unsettled((r.state.get("collections") or {}).get(name),
                                                 spec) if rec.get(spec["key"]) == key]
        if not live:
            out["reason"] = "the record settled or went away"
            return
        try:
            if action.get("remove"):
                _col_apply(r, "remove", name, principal=PR_DAEMON, by=PR_DAEMON, key=key)
                r.emit("collection_watch", collection=name, key=key, code=int(code),
                       removed=True)
            else:
                fields = dict(action.get("set") or {})
                _col_apply(r, "set", name, principal=PR_DAEMON, by=PR_DAEMON, key=key,
                           fields=fields)
                r.emit("collection_watch", collection=name, key=key, code=int(code),
                       fields=fields)
                out["fields"] = fields
            out["applied"] = True
        except col_mod.CollectionError as exc:
            r.emit("collection_watch", collection=name, key=key, code=int(code),
                   error=str(exc))
            out["reason"] = str(exc)

    _mutate(cwd, scope, now, fn)
    return out


def holders(name: str, session: str, cwds: List[str]) -> List[Tuple[str, str]]:
    """``(cwd, scope)`` of every live v2 run in ``cwds`` whose driver is
    ``session`` and whose chart declares collection ``name``."""
    return HolderIndex(name).holders(session, cwds)


class HolderIndex:
    """:func:`holders` for many sessions over one reading of the slots.

    Every live run of a directory is read once, whoever drives it, and the
    answer for a session is a filter over what was read. A caller that asks
    for many sessions in one pass -- the merger grant asks for every live
    mesh member each minute, a landing route for each ancestor in turn --
    reads each slot once instead of once per session: with ~90 members over
    ~66 slots that pass was ~6000 state reads a minute on the daemon, the
    largest GIL holder beside screen replay while a restart's queue ran
    (claunch-cb65c). What was read is not re-read, so an instance answers as
    of its first look at each directory: make one per pass.
    """

    def __init__(self, name: str):
        self.name = name
        #: base directory -> [(scope, driver)] of its live top-level runs
        self._bases: Dict[str, List[Tuple[str, str]]] = {}
        #: (base, scope) -> whether its chart declares ``name``; read only for
        #: a run some caller asked about, as :func:`holders` always did
        self._declares: Dict[Tuple[str, str], bool] = {}

    def holders(self, session: str, cwds: List[str]) -> List[Tuple[str, str]]:
        session = str(session or "").strip()
        out: List[Tuple[str, str]] = []
        if not session:
            return out
        seen = set()
        for cwd in cwds or []:
            try:
                base = str(store_mod.resolve_cwd(cwd))
            except (OSError, ValueError):
                continue
            if base in seen:
                continue
            seen.add(base)
            for scope, driver in self._live(base):
                if driver == session and self._declared(base, scope):
                    out.append((base, scope))
        return out

    def _live(self, base: str) -> List[Tuple[str, str]]:
        found = self._bases.get(base)
        if found is not None:
            return found
        found = []
        for scope in store_mod.list_scopes(base):
            if not store_mod.is_own(scope):
                continue  # another daemon's session of the same name
            try:
                state = RunStore(base, scope).load_state()
            except StoreError:
                continue
            if state.get("parent"):
                continue  # a nested delegation run holds no collections
            if state.get("status") in LIVE_STATUSES:
                found.append((scope, driver_of(state)))
        self._bases[base] = found
        return found

    def _declared(self, base: str, scope: str) -> bool:
        key = (base, scope)
        if key not in self._declares:
            try:
                chart = RunStore(base, scope).load_chart()
            except StoreError:
                self._declares[key] = False
            else:
                self._declares[key] = self.name in _chart_col_specs(chart)
        return self._declares[key]


_ENTER: Dict[str, Callable[[Run, str, ir.Node], None]] = {
    ir.T_TASK: _en_task, ir.T_WAIT: _en_wait, ir.T_SEQ: _en_seq, ir.T_COND: _en_cond,
    ir.T_CHOOSE: _en_choose, ir.T_LOOP: _en_loop, ir.T_PAR: _en_par,
    ir.T_TIMEOUT: _en_timeout, ir.T_TRY: _en_try, ir.T_GROUP: _en_group,
    ir.T_SCOPE: _en_scope, ir.T_CONTROL: _en_control,
}
