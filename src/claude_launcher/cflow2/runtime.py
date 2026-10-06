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
               ``waiting_approval`` with reason ``loop_limit``)
=============  ==============================================================

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
- ``bind`` stores the node's value whatever its outcome.
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

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import executors, ir
from . import store as store_mod
from .store import RunStore, StoreError

try:  # sibling modules; the runtime degrades without them
    from . import caps as caps_mod
except ImportError:  # pragma: no cover
    caps_mod = None  # type: ignore
from . import expr as expr_mod

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

CALLER_AGENT = "agent"
CALLER_USER = "user"

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

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, st: RunStore, now: Optional[float] = None) -> "Run":
        return cls(st, st.load_chart(), st.load_state(), now)

    def save(self) -> None:
        self.state["active"] = sorted(
            iid for iid, inst in self.state["inst"].items() if inst.get("await")
        )
        self.state["updated"] = store_mod.utcnow()
        self.store.save_state(self.state)
        self.store.append(self.events)
        self.events = []

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
            "result": self.state.get("result", {}),
            "visits": self.state.get("visits", {}),
            "caps": {k: _cap_on(v) for k, v in self.caps.items()},
            "probe": self.state.get("probe", {}),
        })
        return out


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
        src = inner if inner.startswith("$") else "$" + inner
        try:
            value = expr_mod.evaluate(src, env)
        except expr_mod.ExprError:
            return m.group(0)
        if value is None:
            return m.group(0)
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

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


def returns_of(node: ir.Node) -> Dict[str, Dict[str, str]]:
    """The schema a node's result is validated against (``{ok: {}}`` default)."""
    schema = node.get("returns")
    if isinstance(schema, dict) and schema:
        return schema
    return {ir.OUTCOME_OK: {}}


def validate_result(schema: Dict[str, Any], outcome: str, value: Any) -> List[str]:
    """Problems with ``(outcome, value)`` against ``schema``; empty when fine.

    ``fail`` is always accepted (the engine's own failure outcome); a declared
    ``fail`` still has its fields checked. Fields are required unless the type
    ends in ``?``; extra fields are allowed.
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
        check = _TYPES.get(base)
        if check is not None and not check(value[name]):
            errors.append(
                f"field {name!r} must be {base}, got {type(value[name]).__name__}"
            )
    return errors


# ---------------------------------------------------------------------------
# entering nodes


def _enter(r: Run, node_id: str, parent: Optional[str], role: str,
           env: Optional[Dict[str, Any]] = None) -> str:
    node = r.chart.node(node_id)
    iid = r.new_id(node_id)
    base = dict(r.inst[parent]["env"]) if parent and parent in r.inst else {}
    if env:
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

    if node.type == ir.T_SCOPE and not _scope_params(r, iid, node):
        return iid
    if node.executor and node.executor != [ir.EXEC_INLINE] and _delegate(r, iid):
        return iid
    _ENTER[node.type](r, iid, node)
    return iid


def _enter_child(r: Run, parent: str, node_id: Optional[str], role: str,
                 env: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if not node_id:
        return None
    return _enter(r, node_id, parent, role, env)


def _en_task(r: Run, iid: str, node: ir.Node) -> None:
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
    params: Dict[str, Any] = {}
    if inst["role"] == "member":
        params.update(inst["env"].get("params") or {})
    try:
        params.update(_values(r, iid, node.get("with_")))
    except expr_mod.ExprError as exc:
        _complete(r, iid, ir.OUTCOME_FAIL, {"reason": f"call arguments: {exc}"})
        return False
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


def _loop_max(r: Run, iid: str) -> int:
    node = r.node_of(iid)
    return int(node.get("max") or 1) + int(r.inst[iid]["frame"].get("extra") or 0)


def _en_loop(r: Run, iid: str, node: ir.Node) -> None:
    frame = r.inst[iid]["frame"]
    frame.update({"iter": 0, "values": [], "extra": 0, "phase": "body", "last": None})
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
        "label": node.label or None, "iterations": frame.get("iter"),
        "max": _loop_max(r, iid), "mode": node.get("mode"),
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
        outcome, value = _ensures(r, iid, node, outcome, value)
        r.state["result"][key] = {"outcome": outcome, "value": value}
        if node.bind:
            r.state.setdefault("data", {})[node.bind] = value
            r.emit("bound", node=node.id, instance=iid, var=node.bind)
        handler = node.on.get(outcome) if node.on else None
        if handler:
            inst["frame"]["on"] = outcome
            _enter_child(r, iid, handler, "on")
            return
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
                vals = frame.get("values") if node.get("mode") != ir.LOOP_RETRY else frame.get("last")
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
        if node.type == ir.T_SCOPE and op == ir.CTL_RETURN:
            _cancel_children(r, cur, "return")
            _complete(r, cur, sig.get("outcome") or ir.OUTCOME_OK, sig.get("value"))
            return
        below = cur
        cur = r.inst[cur].get("parent")
    # nothing caught it: the run ends
    _drop(r, start, op)
    if op == ir.CTL_END:
        _finish_run(r, ir.OUTCOME_OK, None, how="end")
    elif op == ir.CTL_RETURN:
        _finish_run(r, sig.get("outcome") or ir.OUTCOME_OK, sig.get("value"), how="return")
    else:
        _finish_run(r, ir.OUTCOME_FAIL, {"reason": f"{op} found no enclosing loop"})


# -- removal -----------------------------------------------------------------


def _forget(r: Run, iid: str) -> None:
    r.inst.pop(iid, None)
    for d in r.state.get("delegations", {}).values():
        if d.get("instance") == iid and d.get("status") in ("pending", "queued", "running"):
            d["status"] = "cancelled"
            d["finished"] = store_mod.utcnow()
    r.state["approvals"] = [a for a in r.state.get("approvals", []) if a.get("instance") != iid]


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
    r.state["status"] = ST_DONE if ok else ST_FAILED
    r.state["reason"] = None
    r.state["approvals"] = []
    r.state["outcome"] = outcome
    r.state["value"] = value
    r.state["finished"] = store_mod.utcnow()
    r.emit("done", outcome=outcome, value=value, how=how, status=r.state["status"])


# ---------------------------------------------------------------------------
# delegation


def _delegate(r: Run, iid: str) -> bool:
    """Enter a delegation point. True when the subtree is NOT expanded here."""
    node = r.node_of(iid)
    if caps_mod is None:
        r.emit("fallback", node=node.id, cause="caps module unavailable", route="inline")
        return False
    caps = r.caps
    plan = caps_mod.plan_node(node, caps)
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
        nxt = executors.InlineFallback.next_executor(node, executor, caps)
        r.emit("fallback", node=node.id, instance=iid,
               cause=f"no {executor} dispatcher available (no daemon clock watching "
                     f"this directory)",
               route=f"{executor} -> {nxt}", decision="executor")
        executor = nxt
        if executor != plan.route:
            plan = caps_mod.plan_node(_with_executor(node, executor), caps)
    if executor == ir.EXEC_INLINE:
        return False

    dels = r.state.setdefault("delegations", {})
    did = f"d{len(dels) + 1}"
    rec: Dict[str, Any] = {
        "id": did, "instance": iid, "node": node.id, "label": node.label or None,
        "executor": executor, "status": "pending" if executor == ir.EXEC_SUBAGENT else "queued",
        "route": plan.to_dict(), "receipt": None, "started": store_mod.utcnow(),
        "started_at": r.now, "finished": None, "attempts": 0, "errors": [],
        "returns": returns_of(node),
    }
    dels[did] = rec
    r.inst[iid]["await"] = AW_DELEGATION
    r.inst[iid]["frame"]["delegation"] = did
    r.emit("delegate_started", node=node.id, instance=iid, delegation=did,
           executor=executor, route=plan.route)
    if executor in executors.DISPATCHED:
        executors.dispatch(rec, r)
    return True


def _with_executor(node: ir.Node, executor: str) -> ir.Node:
    clone = ir.Node.from_dict(node.to_dict())
    clone.executor = [executor]
    return clone


def brief(r: Run, did: str) -> Dict[str, Any]:
    """What a delegate gets: rendered prompts of the subtree's tasks in order,
    the return schema, the rules, the route plan."""
    rec = r.state["delegations"][did]
    iid = rec["instance"]
    node = r.chart.node(rec["node"])
    env = r.env(iid) if iid in r.inst else r.env(None)
    tasks = []
    rule_ids: List[str] = []
    for n in r.chart.walk(node.id):
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
    return {
        "delegation": did, "node": node.id, "label": node.label or None,
        "type": node.type, "executor": rec["executor"], "status": rec["status"],
        "with": env.get("params") if node.type == ir.T_SCOPE else None,
        "tasks": tasks, "returns": rec.get("returns") or returns_of(node),
        "rules": rules, "route": rec.get("route"),
        "receipt_path": receipt,
        "submit": (
            "run the tasks above in order in a fresh context, then submit ONE result "
            "for the whole delegation with flow_delegate(delegation, outcome, value) "
            "matching 'returns'. Without MCP access the delegate writes "
            f'{{"outcome": ..., "value": {{...}}}} to {receipt} and the driver '
            "submits that path as receipt_path."
        ),
    }


# ---------------------------------------------------------------------------
# public operations


def _mutate(cwd: Optional[str], scope: Optional[str], now: Optional[float],
            fn: Callable[[Run], Any]) -> Any:
    st = RunStore(cwd, scope)
    with st.lock():
        r = Run.load(st, now)
        out = fn(r)
        r.save()
    if r.deferred_error:
        raise FlowError(r.deferred_error)
    return out


def _require_live(r: Run) -> None:
    if r.status not in LIVE_STATUSES:
        raise FlowError(
            f"run {r.state.get('run_id')} is {r.status}; start a new one with flow_start"
        )


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
        if name in params:
            out[name] = params[name]
        elif "default" in decl:
            out[name] = decl["default"]
        elif decl.get("required"):
            raise FlowError(f"missing required param {name!r}")
        else:
            out[name] = None
    for name, value in params.items():
        out.setdefault(name, value)
    return out


def start(chart: ir.Chart, *, cwd: Optional[str] = None, scope: Optional[str] = None,
          params: Optional[Dict[str, Any]] = None, caps: Any = None,
          force: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """Start a run of ``chart`` in the (cwd, scope) slot; returns :func:`status`.

    A finished run in the slot is archived first. A live one refuses the start
    unless ``force`` (it is then aborted and archived).
    """
    if chart.kind != ir.KIND_WORKFLOW:
        raise FlowError(f"{chart.name} is a {chart.kind}; only a workflow can be started")
    ir.validate(chart)
    values = _validate_params(chart, params)
    st = RunStore(cwd, scope)
    with st.lock():
        if st.exists():
            try:
                old = st.load_state()
            except StoreError:
                old = {}
            if old.get("status") in LIVE_STATUSES:
                if not force:
                    raise FlowError(
                        f"a cflow2 run ({old.get('workflow')}, {old.get('run_id')}) is still "
                        f"{old.get('status')} here; continue it, or abort it first"
                    )
                old_run = Run.load(st, now)
                _abort(old_run, "replaced by a forced start")
                old_run.save()
            st.archive(old.get("run_id"))
        elif st.chart_path.exists():
            st.chart_path.unlink()
        run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else None)) \
            + "-" + uuid.uuid4().hex[:6]
        state: Dict[str, Any] = {
            "version": 1, "run_id": run_id, "workflow": chart.name, "source": chart.source,
            "status": ST_RUNNING, "reason": None, "started": store_mod.utcnow(),
            "updated": None, "finished": None, "params": values,
            "data": json.loads(json.dumps(chart.data or {})), "result": {}, "visits": {},
            "inst": {}, "active": [], "delegations": {}, "approvals": [], "seq": 0,
            "caps": _caps_to_json(caps), "probe": {}, "outcome": None, "value": None,
            "scope": st.scope, "cwd": str(st.cwd),
        }
        st.write_chart(chart)
        r = Run(st, chart, state, now)
        r.emit("started", run=run_id, workflow=chart.name, source=chart.source or None,
               params=values)
        _enter(r, chart.root, None, "root")
        _tick_time(r)
        r.save()
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def _find(r: Run, ref: Optional[str], kinds: Tuple[str, ...], what: str) -> str:
    cands = [iid for iid, inst in r.inst.items() if inst.get("await") in kinds]
    if ref:
        if ref in cands:
            return ref
        hits = [iid for iid in cands
                if r.inst[iid]["node"] == ref or r.node_of(iid).label == ref]
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


def report(node: Optional[str], outcome: str = ir.OUTCOME_OK, value: Any = None,
           text: Optional[str] = None, *, cwd: Optional[str] = None,
           scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Report an active task's result and advance."""
    def op(r: Run) -> None:
        _require_live(r)
        iid = _find(r, node, (AW_TASK,), "task")
        n = r.node_of(iid)
        errors = validate_result(returns_of(n), outcome, value)
        if errors:
            raise FlowError(f"report for {n.label or n.id} refused: " + "; ".join(errors)
                            + f". Declared returns: {json.dumps(returns_of(n))}")
        r.emit("reported", node=n.id, instance=iid, outcome=outcome, value=value, text=text)
        _complete(r, iid, outcome, value)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def select(node: Optional[str], option: str, reason: Optional[str] = None, *,
           caller: str = CALLER_AGENT, cwd: Optional[str] = None,
           scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Pick an option of an active ``choose``."""
    def op(r: Run) -> None:
        _require_live(r)
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
        if n.get("require_reason") and not (reason or "").strip():
            raise FlowError(f"choice {n.label or n.id} requires a reason")
        r.inst[iid]["await"] = None
        r.inst[iid]["frame"]["option"] = option
        r.emit("selected", node=n.id, instance=iid, option=option, reason=reason, by=caller)
        if opt.get("child"):
            _enter_child(r, iid, opt["child"], "option")
        else:
            _complete(r, iid, ir.OUTCOME_OK, None)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


def fork(group: Optional[str], with_: Optional[Dict[str, Any]] = None, *,
         cwd: Optional[str] = None, scope: Optional[str] = None,
         now: Optional[float] = None) -> Dict[str, Any]:
    """Add a member to a running ``group`` (its ``do`` must still be running)."""
    out: Dict[str, Any] = {}

    def op(r: Run) -> None:
        _require_live(r)
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


def delegate_submit(delegation: str, outcome: Optional[str] = None, value: Any = None,
                    receipt_path: Optional[str] = None, *, cwd: Optional[str] = None,
                    scope: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Submit a delegate's result (directly, or as a receipt file to read).

    An invalid result is refused once with the problems listed (the delegate
    is re-run with them); a second invalid one fails the delegation and the
    node finishes ``fail`` (``delegate_failed``).
    """
    def op(r: Run) -> None:
        _require_live(r)
        dels = r.state.get("delegations", {})
        rec = dels.get(delegation)
        if rec is None:
            raise FlowError(f"no delegation {delegation!r} (known: {', '.join(dels) or 'none'})")
        if rec["status"] not in ("pending", "queued", "running"):
            raise FlowError(f"delegation {delegation} is {rec['status']}; nothing to submit")
        res_outcome, res_value = outcome, value
        source = "direct"
        if receipt_path:
            path = Path(receipt_path)
            if not path.is_absolute():
                path = r.store.cwd / path
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
        iid = rec["instance"]
        if errors:
            if _reject(r, rec, errors):
                r.deferred_error = (
                    f"delegation {delegation} result refused: {'; '.join(errors)}. Re-run the "
                    f"delegate once with these problems and submit again; a second invalid "
                    f"result fails the node. Declared returns: {json.dumps(rec.get('returns'))}"
                )
            return
        receipt = r.store.write_receipt(delegation, {
            "delegation": delegation, "node": rec["node"], "outcome": res_outcome,
            "value": res_value, "source": source, "submitted": store_mod.utcnow(),
        })
        rec.update({"status": "returned", "receipt": str(receipt),
                    "finished": store_mod.utcnow(), "outcome": res_outcome})
        r.emit("delegate_returned", delegation=delegation, node=rec["node"],
               outcome=res_outcome, receipt=str(receipt))
        _complete(r, iid, res_outcome, res_value)
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


OPEN_DELEGATION = ("pending", "queued", "running")


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
        prefs = list(node.executor or [])
        old = rec["executor"]
        rest = prefs[prefs.index(old) + 1:] if old in prefs else []
        nxt = ir.EXEC_INLINE
        if caps_mod is not None:
            nxt = caps_mod.choose_executor(rest, r.caps, needs=node.needs or []).executor
        if nxt in executors.DISPATCHED and not executors.available(str(r.store.cwd)):
            nxt = executors.InlineFallback.next_executor(node, nxt, r.caps)
        out["executor"] = nxt
        iid = rec["instance"]
        r.emit("fallback", node=node.id, instance=iid, delegation=delegation, cause=cause,
               route=f"{old} -> {nxt}", decision="executor")
        if nxt == ir.EXEC_INLINE:
            rec["status"] = "rerouted"
            rec["finished"] = store_mod.utcnow()
            r.inst[iid]["await"] = None
            r.inst[iid]["frame"].pop("delegation", None)
            _ENTER[node.type](r, iid, node)
            return
        plan = caps_mod.plan_node(_with_executor(node, nxt), r.caps)
        rec.update({"executor": nxt, "route": plan.to_dict(), "started_at": r.now,
                    "status": "pending" if nxt == ir.EXEC_SUBAGENT else "queued",
                    "child": None, "runner": None})
        r.emit("delegate_started", node=node.id, instance=iid, delegation=delegation,
               executor=nxt, route=nxt, rerouted_from=old)
        if nxt in executors.DISPATCHED:
            executors.dispatch(rec, r)
    _mutate(cwd, scope, now, op)
    return out["executor"]


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
                   scope: Optional[str] = None) -> Dict[str, Any]:
    st = RunStore(cwd, scope)
    r = Run.load(st)
    dels = r.state.get("delegations", {})
    open_ids = [d for d, rec in dels.items() if rec["status"] in ("pending", "queued", "running")]
    if not delegation:
        if len(open_ids) != 1:
            raise FlowError(f"name a delegation (open: {', '.join(open_ids) or 'none'})")
        delegation = open_ids[0]
    if delegation not in dels:
        raise FlowError(f"no delegation {delegation!r} (known: {', '.join(dels) or 'none'})")
    return brief(r, delegation)


def extend_loop(node: Optional[str] = None, by: int = 1, *, stop: bool = False,
                cwd: Optional[str] = None, scope: Optional[str] = None,
                now: Optional[float] = None) -> Dict[str, Any]:
    """Answer a ``loop_limit``: allow ``by`` more iterations, or ``stop`` the
    loop (it finishes ``fail``)."""
    def op(r: Run) -> None:
        _require_live(r)
        iid = _find(r, node, (AW_LIMIT,), "loop limit")
        n = r.node_of(iid)
        frame = r.inst[iid]["frame"]
        r.inst[iid]["await"] = None
        r.state["approvals"] = [a for a in r.state.get("approvals", []) if a["instance"] != iid]
        if not r.state["approvals"] and r.state["status"] == ST_WAITING:
            r.state["status"] = ST_RUNNING
            r.state["reason"] = None
        elif r.state["approvals"] and r.state["status"] == ST_WAITING:
            r.state["reason"] = r.state["approvals"][0].get("kind")
        if stop:
            r.emit("selected", node=n.id, instance=iid, option="stop", kind="loop_limit")
            _complete(r, iid, ir.OUTCOME_FAIL, {"reason": "loop_limit", "iterations": frame["iter"]})
            return
        if by < 1:
            raise FlowError("extend by at least 1 (or stop the loop)")
        frame["extra"] = int(frame.get("extra") or 0) + int(by)
        r.emit("selected", node=n.id, instance=iid, option="extend", by=by, kind="loop_limit")
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


def _abort(r: Run, reason: str) -> None:
    for iid in list(r.inst):
        if iid in r.inst and r.inst[iid].get("parent") is None:
            _drop(r, iid, "aborted")
    for rec in r.state.get("delegations", {}).values():
        if rec["status"] in ("pending", "queued", "running"):
            rec["status"] = "cancelled"
    r.state.update({"status": ST_ABORTED, "reason": reason, "approvals": [],
                    "finished": store_mod.utcnow()})
    r.emit("aborted", reason=reason)


def abort(reason: str, *, cwd: Optional[str] = None, scope: Optional[str] = None,
          now: Optional[float] = None) -> Dict[str, Any]:
    def op(r: Run) -> None:
        _require_live(r)
        _abort(r, reason or "aborted")
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)


# -- time and probes ---------------------------------------------------------


def default_probe_runner(probe: Any, cwd: str, timeout: float = 60.0) -> Tuple[int, str]:
    """Run a wait's probe command in ``cwd``: ``(exit code, output)``."""
    cmd = probe if isinstance(probe, list) else str(probe)
    try:
        proc = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "probe timed out"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


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


def _probe_due(r: Run, iid: str) -> bool:
    inst = r.inst[iid]
    node = r.chart.node(inst["node"])
    if inst.get("await") != AW_WAIT or not node.get("probe"):
        return False
    poll = float(node.get("poll") or DEFAULT_POLL)
    last = inst["frame"].get("last_poll")
    return last is None or r.now - last >= poll


def _due_probes(r: Run) -> Dict[str, Any]:
    """``{instance: probe}`` for every wait whose probe is due at ``r.now``."""
    if r.status not in LIVE_STATUSES:
        return {}
    return {iid: r.node_of(iid).get("probe") for iid in r.inst if _probe_due(r, iid)}


def _tick_time(r: Run, probe_runner: Optional[ProbeRunner] = None,
               probe_results: Optional[Dict[str, Any]] = None) -> int:
    """Fire everything due at ``r.now``; returns how many things moved.

    Probes come either from ``probe_results`` (``{instance: result}``, run
    by the caller before it took the lock -- :func:`wait_tick`) or, when only
    ``probe_runner`` is given, are run here."""
    moved = 0
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
            if inst.get("await") == AW_WAIT:
                due = frame.get("due")
                if due is not None and r.now >= due:
                    _complete(r, iid, ir.OUTCOME_OK, {"waited": node.get("seconds")})
                    fired = True
                    break
                have = probe_results is not None and iid in probe_results
                if node.get("probe") and (have or (probe_runner is not None
                                                   and probe_results is None)):
                    if _probe_due(r, iid):
                        frame["last_poll"] = r.now
                        result = probe_results.pop(iid) if have else \
                            probe_runner(node.get("probe"), str(r.store.cwd))
                        ok, output = _probe_ok(result)
                        frame["polls"] = int(frame.get("polls") or 0) + 1
                        if ok:
                            _complete(r, iid, ir.OUTCOME_OK, {"output": output})
                            fired = True
                            break
            if inst.get("await") == AW_PAUSE and r.now >= float(frame.get("resume_at") or 0):
                inst["await"] = None
                _loop_iterate(r, iid)
                fired = True
                break
        if not fired:
            break
        moved += 1
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
    for _round in range(3):
        results: Optional[Dict[str, Any]] = None
        if probe_runner is not None:
            snap = Run.load(st, now)
            due = _due_probes(snap)
            results = {iid: probe_runner(probe, str(snap.store.cwd))
                       for iid, probe in due.items()}

        def op(r: Run) -> int:
            if r.status not in LIVE_STATUSES:
                return 0
            return _tick_time(r, None, results if results is not None else {})
        moved += _mutate(cwd, scope, now, op)
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
            ok, output = _probe_ok(probe_runner(cmd, str(st.cwd)))
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
    by name). The monitor keeps watching; a new violation halts again."""
    def op(r: Run) -> None:
        _require_live(r)
        before = list(r.state.get("approvals", []))
        r.state["approvals"] = [
            a for a in before if not (a.get("kind") == "monitor_violation"
                                      and (monitor is None or a.get("monitor") == monitor))
        ]
        if len(r.state["approvals"]) == len(before):
            raise FlowError("no monitor holds this run" if monitor is None
                            else f"monitor {monitor!r} does not hold this run")
        r.emit("selected", option="resume", kind="monitor_violation", monitor=monitor)
        rest = r.state["approvals"]
        if r.state["status"] == ST_WAITING and r.state.get("reason") == "monitor_violation" \
                and not any(a.get("kind") == "monitor_violation" for a in rest):
            if rest:
                r.state["reason"] = rest[0].get("kind")
            else:
                r.state["status"] = ST_RUNNING
                r.state["reason"] = None
    _mutate(cwd, scope, now, op)
    return status(cwd=cwd, scope=scope, now=now, tick=False)

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
        with st.lock():
            r = Run.load(st, now)
            if r.status in LIVE_STATUSES and _tick_time(r):
                r.save()
    r = Run.load(st, now)
    return describe(r)


def describe(r: Run) -> Dict[str, Any]:
    s = r.state
    out: Dict[str, Any] = {
        "run": s.get("run_id"), "workflow": s.get("workflow"), "status": s.get("status"),
        "scope": s.get("scope"), "started": s.get("started"),
    }
    if s.get("reason"):
        out["reason"] = s["reason"]
    if s.get("status") in FINISHED_STATUSES:
        out["outcome"] = s.get("outcome")
        out["value"] = s.get("value")
        out["note"] = f"the run is {s.get('status')}"
        return out
    tasks, choices, waits, groups, limits = [], [], [], [], []
    for iid in sorted(r.inst, key=lambda k: int(k.rsplit("@", 1)[1])):
        inst = r.inst[iid]
        node = r.chart.node(inst["node"])
        aw = inst.get("await")
        env = r.env(iid)
        if aw == AW_TASK:
            tasks.append({
                "instance": iid, "node": node.id, "label": node.label or None,
                "title": node.get("title"), "prompt": render(node.get("prompt"), env),
                "done_when": node.get("done_when"), "verify": node.get("verify"),
                "returns": returns_of(node), "effects": node.get("effects") or [],
                "rules": [{"id": rid, "text": (r.chart.rules.get(rid) or {}).get("text")}
                          for rid in node.get("rules") or []],
                "ask": node.get("ask"),
                "item": env.get("item"),
            })
        elif aw == AW_CHOOSE:
            avail, unavail = [], []
            for opt in node.get("options") or []:
                try:
                    ok = _guard(r, iid, opt.get("guard"))
                except expr_mod.ExprError:
                    ok = False
                entry = {"name": opt["name"], "description": opt.get("description") or ""}
                (avail if ok else unavail).append(entry if ok else {**entry, "when": opt.get("guard")})
            choices.append({
                "instance": iid, "node": node.id, "label": node.label or None,
                "prompt": render(node.get("prompt"), env),
                "chooser": node.get("chooser") or ir.CHOOSER_AGENT,
                "require_reason": bool(node.get("require_reason")),
                "options": avail, "unavailable": unavail,
            })
        elif aw == AW_WAIT:
            waits.append({
                "instance": iid, "node": node.id, "kind": "wait",
                "describe": node.get("describe"), "probe": node.get("probe"),
                "poll": node.get("poll"), "seconds": node.get("seconds"),
                "due": inst["frame"].get("due"), "polls": inst["frame"].get("polls", 0),
            })
        elif aw == AW_PAUSE:
            waits.append({"instance": iid, "node": node.id, "kind": "pause",
                          "resume_at": inst["frame"].get("resume_at")})
        elif aw == AW_LIMIT:
            limits.append({"instance": iid, "node": node.id, "label": node.label or None,
                           "iterations": inst["frame"].get("iter"), "max": _loop_max(r, iid)})
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
        if rec["status"] in ("pending", "queued", "running"):
            entry["brief"] = brief(r, did)
        dels.append(entry)
    out.update({
        "active": sorted(iid for iid, inst in r.inst.items() if inst.get("await")),
        "tasks": tasks, "choices": choices, "waits": waits,
    })
    if groups:
        out["groups"] = groups
    if dels:
        out["delegations"] = dels
    if limits:
        out["loop_limits"] = limits
    out["data"] = s.get("data")
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
        hints.append("headless/session delegations are run by the daemon and come back on "
                     "their own; call flow_status again later")
    if waits:
        hints.append("waits fire on their own; call flow_status again later")
    if limits:
        hints.append("a loop hit its max: the user decides with 'claunch cflow2 extend'")
    out["next"] = "; ".join(hints) or None
    return out


def journal(*, cwd: Optional[str] = None, scope: Optional[str] = None,
            limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return RunStore(cwd, scope).journal(limit)


# ---------------------------------------------------------------------------
# capability resolution for a driver


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


_ENTER: Dict[str, Callable[[Run, str, ir.Node], None]] = {
    ir.T_TASK: _en_task, ir.T_WAIT: _en_wait, ir.T_SEQ: _en_seq, ir.T_COND: _en_cond,
    ir.T_CHOOSE: _en_choose, ir.T_LOOP: _en_loop, ir.T_PAR: _en_par,
    ir.T_TIMEOUT: _en_timeout, ir.T_TRY: _en_try, ir.T_GROUP: _en_group,
    ir.T_SCOPE: _en_scope, ir.T_CONTROL: _en_control,
}
