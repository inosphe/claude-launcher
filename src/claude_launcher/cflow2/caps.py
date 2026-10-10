"""Atomic capabilities, their layering, and fallback planning (doc 6).

A *capability* is one thing a delegation relies on that a harness, a backend
or the daemon may or may not provide (``subagent.parallel``,
``mcp.claunch`` ...). :data:`CAPABILITIES` is the closed list.

:func:`resolve` composes the value of each for one harness under one
provider/profile (doc 6.2), lowest layer first::

    harness defaults (package harnesses.yaml, or the user's harnesses: block)
    < providers.<p>.capabilities
    < providers.<p>.harness_options.<h>.capabilities
    < profiles.<root ancestor>... .capabilities < ... (each chain profile,
      its top-level block then its harness_options.<h> block)

Every planner below is a pure function of a node's needs and a resolved
capability map; each returns a plan with a ``route`` (doc 6.4's names) and
``degraded`` -- the human-readable reasons the plan fell below the first
route. The runtime journals them as ``fallback`` events and
``cflow2 validate --profile`` prints them ahead of a run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from . import ir

#: id -> short description (doc 6.1).
CAPABILITIES: Dict[str, str] = {
    "subagent.spawn": "the driver can start a fresh-context sub-agent inside its harness",
    "subagent.parallel": "several sub-agents can run at once",
    "subagent.tools.restrict": "a sub-agent's tools can be restricted",
    "subagent.worktree": "a sub-agent can run in its own git worktree",
    "mcp.claunch": "the executor can call claunch MCP tools",
    "session.spawn": "claunch spawn can create a child session (daemon policy)",
}

#: Capability families of executors the engine no longer has (claunch-xjbrh
#: removed ``headless``). A config or harness entry still naming one is
#: skipped without a warning: it is stale, not wrong.
REMOVED_FAMILIES = ("headless",)

#: The planner's reason for passing over an executor that cannot run a
#: subtree with control structure (doc 6.4, A2): subagent/session without
#: ``mcp.claunch`` (they cannot drive a nested run).
NOT_FLAT = "subtree not flat"

SOURCE_DEFAULT = "default"
SOURCE_DAEMON = "daemon-policy"
SESSION_SPAWN = "session.spawn"

#: The capability an executor cannot run without (doc 6.4, executor choice).
EXECUTOR_REQUIRES: Dict[str, Optional[str]] = {
    ir.EXEC_SUBAGENT: "subagent.spawn",
    ir.EXEC_SESSION: SESSION_SPAWN,
    ir.EXEC_INLINE: None,
}


@dataclass(frozen=True)
class Capability:
    enabled: bool
    args: List[str] = field(default_factory=list)
    #: The layer the value came from: ``harness:claude``,
    #: ``provider:X``, ``provider:X/harness:claude``, ``profile:nc``,
    #: ``profile:nc/harness:claude``, ``daemon-policy`` or ``default``.
    source: str = SOURCE_DEFAULT

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "args": list(self.args), "source": self.source}


class CapSet(Dict[str, Capability]):
    """What :func:`resolve` returns: id -> :class:`Capability`, plus the
    ``warnings`` met on the way (unknown ids, malformed values, an unknown
    harness/provider/profile)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.warnings: List[str] = []


CapsLike = Mapping[str, Union[Capability, bool]]


#: The source the runtime stamps on a capability its caller passed as a
#: bare bool (``runtime._caps_to_json``): no layer was consulted, so it is
#: taken as given, like a plain bool (tests, hand-written maps).
SOURCE_GIVEN = "given"


def enabled(caps: CapsLike, cap_id: str) -> bool:
    """Whether ``cap_id`` is on in ``caps`` (Capability values, their
    ``to_dict`` form, or plain bools from tests and hand-written maps)."""
    value = caps.get(cap_id) if caps else None
    if isinstance(value, Capability):
        return value.enabled
    if isinstance(value, dict):
        return bool(value.get("enabled"))
    return bool(value)


# --- layering ----------------------------------------------------------------


def _apply(out: CapSet, block: Any, source: str) -> None:
    """Lay one ``capabilities:`` block over ``out``."""
    if block is None:
        return
    if not isinstance(block, dict):
        out.warnings.append(f"{source}: capabilities must be a mapping, ignored")
        return
    for raw_key, value in block.items():
        key = str(raw_key).strip()
        if key.split(".", 1)[0] in REMOVED_FAMILIES:
            continue
        if key not in CAPABILITIES:
            out.warnings.append(f"{source}: unknown capability {key!r} ignored")
            continue
        prev = out.get(key)
        if isinstance(value, bool):
            args = list(prev.args) if prev is not None else []
            out[key] = Capability(enabled=value, args=args, source=source)
            continue
        if isinstance(value, dict):
            on = value.get("enabled", True)
            raw_args = value.get("args")
            if not isinstance(on, bool) or (
                raw_args is not None and not isinstance(raw_args, (list, tuple, str))
            ):
                out.warnings.append(f"{source}: capability {key!r} has a malformed value, ignored")
                continue
            if raw_args is None:
                args = list(prev.args) if prev is not None else []
            elif isinstance(raw_args, str):
                args = [raw_args]
            else:
                args = [str(a) for a in raw_args]
            out[key] = Capability(enabled=on, args=args, source=source)
            continue
        out.warnings.append(f"{source}: capability {key!r} must be true, false or {{enabled, args}}, ignored")


def _harness_block(entry: Any, harness: str) -> Any:
    if not isinstance(entry, dict):
        return None
    options = entry.get("harness_options")
    if not isinstance(options, dict):
        return None
    block = options.get(harness)
    return block.get("capabilities") if isinstance(block, dict) else None


def _profile_chain(name: str, doc: dict, warnings: List[str]) -> List[Tuple[str, dict]]:
    """``(name, entry)`` root ancestor first, read from ``doc`` alone."""
    section = doc.get("profiles") if isinstance(doc.get("profiles"), dict) else {}
    out: List[Tuple[str, dict]] = []
    seen = set()
    current: Optional[str] = name
    while current:
        if current in seen:
            warnings.append(f"profile {current!r}: parent cycle, chain cut")
            break
        seen.add(current)
        entry = section.get(current)
        if not isinstance(entry, dict):
            if current == name:
                entry = {}
            else:
                warnings.append(f"profile {current!r} (a parent) is not in the config")
                break
        out.append((current, entry))
        parent = entry.get("parent")
        current = str(parent) if parent else None
    out.reverse()
    return out


def _provider_name(chain: List[Tuple[str, dict]], doc: dict) -> Optional[str]:
    for _name, entry in reversed(chain):  # self first, then toward the root
        sel = entry.get("provider")
        if sel:
            return str(sel)
    top = doc.get("provider")
    return str(top) if top else None


def resolve(
    harness: str,
    *,
    provider: Optional[str] = None,
    profile: Optional[str] = None,
    doc: Optional[dict] = None,
    session_spawn: Optional[bool] = None,
) -> Dict[str, Capability]:
    """Every capability's value for ``harness`` (doc 6.2); a :class:`CapSet`.

    ``provider`` defaults to the profile chain's provider (self, then
    ancestors, then the global ``provider:``). ``session.spawn`` is the
    daemon's spawn policy, not a harness feature: it starts enabled with
    source ``daemon-policy`` and ``session_spawn`` (the policy's verdict, when
    the caller has one) overrides whatever the layers said.
    """
    from .. import harnesses  # late: harnesses imports store

    if doc is None:
        from .. import store
        doc = store.load()
    doc = doc if isinstance(doc, dict) else {}
    out = CapSet()
    out[SESSION_SPAWN] = Capability(enabled=True, source=SOURCE_DAEMON)

    reg = harnesses.registry(doc)
    entry = reg.get(harness)
    if entry is None:
        out.warnings.append(f"unknown harness {harness!r}: no harness defaults")
    else:
        _apply(out, entry.capabilities, f"harness:{harness}")

    chain: List[Tuple[str, dict]] = []
    if profile:
        chain = _profile_chain(profile, doc, out.warnings)
    name = provider or _provider_name(chain, doc)
    if name:
        section = doc.get("providers") if isinstance(doc.get("providers"), dict) else {}
        pentry = section.get(name)
        if isinstance(pentry, dict):
            _apply(out, pentry.get("capabilities"), f"provider:{name}")
            _apply(out, _harness_block(pentry, harness), f"provider:{name}/harness:{harness}")
        elif name not in ("default", "claude"):
            out.warnings.append(f"unknown provider {name!r}: no provider capabilities")
    for pname, pentry in chain:
        _apply(out, pentry.get("capabilities"), f"profile:{pname}")
        _apply(out, _harness_block(pentry, harness), f"profile:{pname}/harness:{harness}")

    if session_spawn is not None:
        out[SESSION_SPAWN] = Capability(enabled=bool(session_spawn), source=SOURCE_DAEMON)
    for cap_id in CAPABILITIES:
        if cap_id not in out:
            out[cap_id] = Capability(enabled=False, source=SOURCE_DEFAULT)
    ordered = CapSet((k, out[k]) for k in CAPABILITIES)
    ordered.warnings = out.warnings
    return ordered


# --- plans -------------------------------------------------------------------


@dataclass
class Plan:
    route: str
    degraded: List[str] = field(default_factory=list)
    #: The capability whose ``args`` the executor adds for this route.
    capability: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutorChoice:
    executor: str
    #: ``(executor, missing capability)`` for each preference passed over.
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def route(self) -> str:
        return self.executor

    @property
    def degraded(self) -> List[str]:
        return [f"executor {ex} skipped: {cap}" if cap == NOT_FLAT
                else f"executor {ex} skipped: {cap} unavailable" for ex, cap in self.skipped]

    def to_dict(self) -> Dict[str, Any]:
        return {"executor": self.executor, "route": self.route,
                "skipped": [list(s) for s in self.skipped], "degraded": self.degraded}


@dataclass
class ReturnPlan(Plan):
    pass


@dataclass
class ReadonlyPlan(Plan):
    pass


@dataclass
class ParallelPlan(Plan):
    concurrent: bool = True
    isolate: bool = False


@dataclass
class BudgetPlan(Plan):
    seconds: Optional[float] = None
    usd: Optional[float] = None
    usd_enforced: bool = False
    #: strict_budget and the ceiling cannot be enforced: skip this executor.
    blocked: bool = False


@dataclass
class CarryPlan(Plan):
    pass


@dataclass
class NodePlan:
    node: str
    executor: ExecutorChoice
    returns: ReturnPlan
    readonly: Optional[ReadonlyPlan] = None
    parallel: Optional[ParallelPlan] = None
    budget: Optional[BudgetPlan] = None
    carry: Optional[CarryPlan] = None

    @property
    def route(self) -> str:
        return self.executor.executor

    @property
    def degraded(self) -> List[str]:
        out = list(self.executor.degraded)
        for part in (self.returns, self.readonly, self.parallel, self.budget, self.carry):
            if part is not None:
                out.extend(part.degraded)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "route": self.route,
            "executor": self.executor.to_dict(),
            "returns": self.returns.to_dict(),
            "readonly": self.readonly.to_dict() if self.readonly else None,
            "parallel": self.parallel.to_dict() if self.parallel else None,
            "budget": self.budget.to_dict() if self.budget else None,
            "carry": self.carry.to_dict() if self.carry else None,
            "degraded": self.degraded,
        }


# --- planners ----------------------------------------------------------------


def need_applies(cap_id: str, executor: str) -> bool:
    """Whether a node's ``needs`` entry constrains ``executor``.

    ``subagent.*``/``session.*`` constrain only that executor;
    any other capability (``mcp.claunch``) constrains every delegating
    executor. ``inline`` is the guaranteed last route and is never skipped.
    """
    if executor == ir.EXEC_INLINE:
        return False
    family = cap_id.split(".", 1)[0]
    if family in (ir.EXEC_SUBAGENT, ir.EXEC_SESSION):
        return family == executor
    return True


def flat_blocks(executor: str, caps: CapsLike) -> bool:
    """Whether ``executor`` cannot run a subtree that is not flat (has
    control structure beyond task/seq): subagent/session when
    ``mcp.claunch`` is off (the delegate cannot drive the nested run through
    the flow tools)."""
    if executor in (ir.EXEC_SUBAGENT, ir.EXEC_SESSION):
        return not enabled(caps, "mcp.claunch")
    return False


def _missing_for(executor: str, caps: CapsLike, needs: Iterable[str],
                 flat: bool = True) -> Optional[str]:
    if executor not in EXECUTOR_REQUIRES:
        return "unknown executor"
    if not flat and flat_blocks(executor, caps):
        return NOT_FLAT
    required = EXECUTOR_REQUIRES[executor]
    if required and not enabled(caps, required):
        return required
    for cap_id in needs:
        if need_applies(cap_id, executor) and not enabled(caps, cap_id):
            return cap_id
    return None


def choose_executor(
    preferences: List[str], caps: CapsLike, *, needs: Iterable[str] = (),
    flat: bool = True,
) -> ExecutorChoice:
    """The first executor in ``preferences`` whose requirement (and the
    node's applicable ``needs``) holds; ``inline`` when none does. With
    ``flat`` false, executors that cannot run a non-flat subtree
    (:func:`flat_blocks`) are skipped with :data:`NOT_FLAT`."""
    needs = list(needs)
    skipped: List[Tuple[str, str]] = []
    for executor in preferences or []:
        missing = _missing_for(executor, caps, needs, flat)
        if missing is None:
            return ExecutorChoice(executor=executor, skipped=skipped)
        skipped.append((executor, missing))
    return ExecutorChoice(executor=ir.EXEC_INLINE, skipped=skipped)


def plan_return(executor: str, caps: CapsLike, has_schema: bool) -> ReturnPlan:
    """How the delegate's result comes back (doc 6.4, return values)."""
    if executor in (ir.EXEC_SUBAGENT, ir.EXEC_SESSION):
        if enabled(caps, "mcp.claunch"):
            return ReturnPlan("mcp-submit", capability="mcp.claunch")
        return ReturnPlan("receipt-file", [
            f"mcp.claunch unavailable in the {executor}: it writes a receipt "
            "file and the driver submits its path"])
    # inline: the driver itself completes the node through the flow tools.
    return ReturnPlan("mcp-submit")


def plan_readonly(executor: str, caps: CapsLike) -> ReadonlyPlan:
    """How a read-only delegation is kept read-only (doc 6.4, effects)."""
    if executor == ir.EXEC_SUBAGENT:
        if enabled(caps, "subagent.tools.restrict"):
            return ReadonlyPlan("tools-restrict", capability="subagent.tools.restrict")
        note = "subagent.tools.restrict unavailable"
        if enabled(caps, "subagent.worktree"):
            return ReadonlyPlan("worktree", [f"{note}: the sub-agent runs in its own worktree"],
                                capability="subagent.worktree")
        return ReadonlyPlan("brief+porcelain-check", [
            f"{note} and subagent.worktree unavailable: read-only is stated in "
            "the brief and checked by git status --porcelain afterwards"])
    if executor == ir.EXEC_SESSION:
        return ReadonlyPlan("child-worktree")
    return ReadonlyPlan("brief-only", ["inline: read-only is stated in the brief only"])


def plan_parallel(
    executor: str, caps: CapsLike, n: int, write_overlap: bool,
    *, settle: Optional[str] = None,
) -> ParallelPlan:
    """How ``n`` branches run (doc 6.4, parallel writes and concurrency).

    ``write_overlap`` is true when the branches write and their ``paths``
    overlap or are undeclared.
    """
    if n <= 1:
        return ParallelPlan("parallel")
    if executor == ir.EXEC_SESSION:
        concurrent, why = True, ""
    elif executor == ir.EXEC_SUBAGENT:
        concurrent = enabled(caps, "subagent.parallel")
        why = "subagent.parallel unavailable"
    else:
        concurrent, why = False, "inline runs one branch at a time"
    if concurrent and write_overlap:
        if executor == ir.EXEC_SUBAGENT:
            isolatable = enabled(caps, "subagent.worktree")
            if not isolatable:
                why = "overlapping writes and subagent.worktree unavailable"
        else:  # session: always its own worktree
            isolatable = True
        if isolatable:
            return ParallelPlan("parallel-isolated-worktrees", concurrent=True, isolate=True,
                                capability="subagent.worktree" if executor == ir.EXEC_SUBAGENT else None)
        concurrent = False
    if concurrent:
        return ParallelPlan("parallel", capability=(
            "subagent.parallel" if executor == ir.EXEC_SUBAGENT else None))
    degraded = [f"{why}: {n} branches run serially (settle semantics kept)"]
    if settle in (None, ir.SETTLE_RACE, ir.SETTLE_ANY):
        degraded.append("race/any settle runs branches in declaration order and "
                        "stops at the first success")
    return ParallelPlan("serial", degraded, concurrent=False)


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def plan_budget(executor: str, caps: CapsLike, budget: Optional[dict], strict: bool) -> BudgetPlan:
    """How a node's ``budget`` is enforced (doc 6.4, budget).

    Seconds are always enforced by the daemon's wall clock. No executor
    enforces dollars: they are unenforced, and ``strict`` blocks the
    executor.
    """
    budget = budget or {}
    seconds = _number(budget.get("seconds"))
    usd = _number(budget.get("usd"))
    if usd is None:
        return BudgetPlan("seconds-only" if seconds is not None else "none",
                          seconds=seconds)
    degraded = [f"{executor} has no cost ceiling: budget.usd {usd:g} is not enforced"]
    # inline is the last route and cannot be skipped, so it is never blocked.
    blocked = bool(strict) and executor != ir.EXEC_INLINE
    if blocked:
        degraded.append(f"strict_budget: executor {executor} is skipped")
    elif strict:
        degraded.append("strict_budget: inline is the last route and runs anyway")
    return BudgetPlan("unenforced", degraded, seconds=seconds, usd=usd,
                      usd_enforced=False, blocked=blocked)


def plan_retry_carry(executor: str, caps: CapsLike, carry: Optional[str]) -> CarryPlan:
    """How a retry carries the failed attempt (doc 6.4, retry carry)."""
    if carry == "resume":
        return CarryPlan("summary", [f"{executor} cannot resume a finished run: "
                                     "the retry gets a summary of the last attempt"])
    return CarryPlan("summary")


def _effects_readonly(effects: Any) -> bool:
    """Declared effects, none of which writes. Undeclared effects are not
    read-only: a delegate that might write is never sandboxed read-only."""
    if not effects:
        return False
    return all(str(e) in ir.READ_ONLY_EFFECTS for e in effects)


def _width(node: ir.Node) -> int:
    if node.type == ir.T_PAR:
        return len(node.get("branches") or {})
    if node.type == ir.T_GROUP:
        limit = node.get("limit")
        return int(limit) if isinstance(limit, int) and limit > 0 else 2
    if node.type == ir.T_LOOP:
        par = node.get("parallel")
        if isinstance(par, bool):
            return 2 if par else 1
        return int(par) if isinstance(par, int) and par > 0 else 1
    return 1


def plan_node(node: ir.Node, caps: CapsLike, flat: bool = True) -> NodePlan:
    """Every fallback decision for one delegation point.

    Reads ``node.executor`` (preference order), ``node.needs``, and from
    ``props``: ``effects``, ``returns``, ``budget`` (or the common
    ``node.budget``), ``strict_budget``, ``settle``, ``carry``. An executor
    whose required or needed capability is missing, or that cannot enforce
    a strict dollar budget, is skipped. ``flat`` is the caller's verdict on
    the subtree (only task/seq below the point); when false, executors that
    cannot run control structure are skipped with :data:`NOT_FLAT`.
    """
    budget = node.budget or node.get("budget")
    strict = bool(node.get("strict_budget"))
    prefs = list(node.executor or [])
    needs = list(node.needs or [])
    skipped: List[Tuple[str, str]] = []
    chosen = ir.EXEC_INLINE
    for executor in prefs:
        missing = _missing_for(executor, caps, needs, flat)
        if missing is None and strict and executor != ir.EXEC_INLINE:
            if plan_budget(executor, caps, budget, True).blocked:
                missing = "budget.usd enforcement"
        if missing is None:
            chosen = executor
            break
        skipped.append((executor, missing))
    choice = ExecutorChoice(executor=chosen, skipped=skipped)

    plan = NodePlan(
        node=node.id,
        executor=choice,
        returns=plan_return(chosen, caps, bool(node.get("returns"))),
    )
    if _effects_readonly(node.get("effects")):
        plan.readonly = plan_readonly(chosen, caps)
    width = _width(node)
    if width > 1:
        settle = str(node.get("settle") or "").split(":", 1)[0] or None
        plan.parallel = plan_parallel(
            chosen, caps, width, write_overlap=not _effects_readonly(node.get("effects")),
            settle=settle,
        )
    if budget:
        plan.budget = plan_budget(chosen, caps, budget, strict)
    if node.type == ir.T_LOOP and node.get("mode") == ir.LOOP_RETRY:
        plan.carry = plan_retry_carry(chosen, caps, node.get("carry"))
    return plan
