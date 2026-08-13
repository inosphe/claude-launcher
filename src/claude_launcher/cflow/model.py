"""Workflow YAML schema: a directed graph of steps, parsed and validated.

Steps are defined **once** in a mapping and wired by id — structure is the
inline ``next`` pointers (and select options' ``next``), so shared sequences
and loops need no duplicated content::

    name: feature-dev
    description: ...
    start: design           # optional (defaults to the first step)
    max_visits: 25          # optional loop guard (per step, per run)
    steps:
      design:
        instructions: |
          ...
        next: triage
      triage:
        select:
          prompt: ...
          chooser: user     # agent | user
          options:
            auto:  {description: low risk,  next: impl}
            human: {description: needs review, next: impl}
      impl:
        instructions: ...
        gate: "message"     # DEPRECATED spelling of `ask: {from: [human]}`
        verify: "pytest -q" # machine gate to LEAVE (exit 0)
        next: test
      test:
        instructions: ...
        next: review
      review:
        select:
          prompt: pass?
          chooser: user
          options:
            ok:     {description: done,    next: ship}
            rework: {description: loop back, next: impl}   # a cycle — warned
      ship:
        ask:                    # approval to ENTER, re-required per visit
          prompt: ship it?
          from: [{role: leader, up: 1}, human]   # preference order
          timeout: 900
          on_decline: impl
        instructions: ...
        # no 'next' (or 'next: end') = termination

Termination: omitting ``next`` (or the reserved target ``end``) ends the run.
Cycles are legal (they model iteration; a select is the loop exit) but are
**warned** about; a workflow whose start cannot reach any termination is an
**error** — at least one reachable end must be described.

Delegated decisions
-------------------
``ask`` (approval to enter a step) and ``select.chooser`` (which branch to
take) both accept the same declaration of **who may answer**: an ordered list
of candidate patterns, tried a group at a time. A group that resolves to
nobody — and, once the daemon owns the clock, a group that does not answer
within ``timeout`` — escalates to the next one, so "the direct parent, else a
grandparent, else the human" is one list rather than three mechanisms.

A member candidate MUST say how far **up** the spawn lineage to look. That is
not sugar: an agent can spawn its own children, so an unconstrained
``{role: reviewer}`` would let a run manufacture its own approver. Restricting
candidates to ancestors is what makes a delegated approval mean anything, and
is why ``up`` has no default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

DEFAULT_VERIFY_TIMEOUT = 900.0
DEFAULT_MAX_VISITS = 25

#: Reserved next-target meaning "the workflow ends here".
END = "end"

#: Reserved candidate meaning "a human, through the CLI or the dashboard".
#: Humans are not mesh members — they hold no handle, receive no stance and
#: have nothing typed into a terminal — so they are a token in the list rather
#: than a role in the mesh vocabulary. The answer channel differs (an agent
#: calls a tool, a human runs ``claunch cflow approve|select``); the request
#: they answer is the same one.
HUMAN = "human"

#: How far up a lineage a candidate may reach. Deliberately a local constant
#: rather than an import from the daemon's role vocabulary: this module parses
#: workflow files with no daemon in sight, and must keep doing so.
MAX_UP = 64


class WorkflowError(Exception):
    """Raised for unreadable or invalid workflow files."""


@dataclass(frozen=True)
class Verify:
    command: str
    timeout: float = DEFAULT_VERIFY_TIMEOUT


@dataclass(frozen=True)
class Option:
    name: str
    description: str
    next: Optional[str] = None  # None = termination


@dataclass(frozen=True)
class Candidate:
    """One entry of a delegation's preference list: a kind of responder.

    Either the reserved :data:`HUMAN` token, or a member pattern matched
    against the asking session's ancestors — ``up`` hops of lineage, with an
    optional ``role``. The hop range is inclusive and always at least 1: a
    candidate is by construction somebody *above* the run, never the run
    itself and never something it spawned.
    """

    human: bool = False
    role: Optional[str] = None
    #: Inclusive ``(from, to)`` hop range; ``None`` for the human token.
    up: Optional[Tuple[int, int]] = None

    def describe(self) -> str:
        """One line for a payload, a message or an error — never parsed."""
        if self.human:
            return HUMAN
        low, high = self.up  # type: ignore[misc]
        hops = f"{low}" if low == high else f"{low}..{high}"
        return f"{self.role or 'anyone'} {hops} up"


@dataclass(frozen=True)
class Delegate:
    """Who may answer a decision, in preference order.

    The list is read a group at a time — index 0 first — and every candidate
    in a group is asked together, so "the direct parent, else a grandparent"
    and "any of the three reviewers above me" are the same declaration read
    two ways. ``timeout`` is per group, not for the whole list.
    """

    candidates: List[Candidate] = field(default_factory=list)
    timeout: Optional[float] = None


@dataclass(frozen=True)
class Ask:
    """A delegated approval to ENTER a step, re-required on every visit.

    ``on_decline`` is what makes a refusal actionable. Left unset, a decline
    parks the run for a human rather than guessing a destination — the same
    conservative default as an unresolvable candidate list.
    """

    prompt: str
    delegate: Delegate
    #: Step id to route to on a decline, the literal :data:`END` to finish,
    #: or ``None`` for "hold the run and wait for a human".
    on_decline: Optional[str] = None


@dataclass(frozen=True)
class Select:
    prompt: str
    chooser: str  # "agent" | "user" | "delegate"
    options: Dict[str, Option] = field(default_factory=dict)
    #: Set exactly when ``chooser`` is "delegate".
    delegate: Optional[Delegate] = None


@dataclass(frozen=True)
class Step:
    id: str
    title: Optional[str] = None
    instructions: Optional[str] = None
    gate: Optional[str] = None  # DEPRECATED entry gate; see `ask`
    ask: Optional[Ask] = None  # entry gate, delegated; approval per visit
    verify: Optional[Verify] = None
    select: Optional[Select] = None
    next: Optional[str] = None  # None = termination (non-select steps)

    @property
    def is_select(self) -> bool:
        return self.select is not None

    @property
    def entry_prompt(self) -> Optional[str]:
        """What an entry gate on this step asks, whichever spelling wrote it."""
        return self.ask.prompt if self.ask else self.gate

    def successors(self) -> List[Optional[str]]:
        """Outgoing edges (None entries are terminations).

        A decline target is a real edge — it is where the run goes — so it
        counts for reachability, for the cycle warning and for the "can this
        workflow ever finish" check exactly like a ``next``. An ask with no
        declared decline target contributes none: that decline parks the run
        where it already is and waits for a human, which is not a move.
        """
        out: List[Optional[str]] = []
        if self.ask and self.ask.on_decline is not None:
            out.append(None if self.ask.on_decline == END else self.ask.on_decline)
        if self.select:
            out.extend(o.next for o in self.select.options.values())
        else:
            out.append(self.next)
        return out


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    start: str
    steps: Dict[str, Step]
    max_visits: int = DEFAULT_MAX_VISITS
    warnings: List[str] = field(default_factory=list)
    #: Superseded spellings this file still uses. Kept apart from
    #: :attr:`warnings` on purpose: a warning describes a graph that may
    #: misbehave and belongs in front of whoever *runs* it, while this is
    #: advice to whoever *writes* it. Surfaced by ``cflow show`` and the
    #: dashboard's workflow view; ``start`` stays quiet, so a workflow already
    #: in service does not nag on every run.
    deprecations: List[str] = field(default_factory=list)

    def step(self, step_id: str) -> Step:
        try:
            return self.steps[step_id]
        except KeyError:
            raise WorkflowError(f"unknown step {step_id!r}") from None

    def step_count(self) -> int:
        return len(self.steps)


def parse(text: str, *, default_name: str = "workflow") -> Workflow:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"invalid workflow YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise WorkflowError("workflow file must be a YAML mapping")
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, dict) or not raw_steps:
        raise WorkflowError(
            "workflow needs a non-empty 'steps' mapping (step id -> definition)"
        )
    if END in raw_steps:
        raise WorkflowError(f"step id {END!r} is reserved for termination")

    steps: Dict[str, Step] = {}
    for step_id, raw in raw_steps.items():
        steps[str(step_id)] = _parse_step(str(step_id), raw)

    start = str(doc.get("start") or next(iter(steps)))
    if start not in steps:
        raise WorkflowError(f"start step {start!r} is not defined")
    try:
        max_visits = int(doc.get("max_visits", DEFAULT_MAX_VISITS))
    except (TypeError, ValueError):
        raise WorkflowError("'max_visits' must be an integer")
    if max_visits < 1:
        raise WorkflowError("'max_visits' must be >= 1")

    workflow = Workflow(
        name=str(doc.get("name") or default_name),
        description=str(doc.get("description") or ""),
        start=start,
        steps=steps,
        max_visits=max_visits,
        warnings=[],
    )
    _validate_graph(workflow)
    return Workflow(
        name=workflow.name,
        description=workflow.description,
        start=workflow.start,
        steps=workflow.steps,
        max_visits=workflow.max_visits,
        warnings=_graph_warnings(workflow),
        deprecations=_deprecations(workflow),
    )


def _deprecations(workflow: Workflow) -> List[str]:
    return [
        f"step {step.id!r}: 'gate:' is deprecated — write it as "
        f"'ask: {{prompt: <the gate message>, from: [{HUMAN}]}}'. That is the "
        f"same human approval, in the form that can also name an agent "
        f"(role + how far up the lineage) as the approver"
        for step in workflow.steps.values()
        if step.gate
    ]


def load(path: Path) -> Workflow:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"cannot read workflow {path}: {exc}") from exc
    return parse(text, default_name=path.stem)


# --------------------------------------------------------------------------- #
# step parsing
# --------------------------------------------------------------------------- #
def _parse_next(raw, where: str) -> Optional[str]:
    if raw is None:
        return None
    target = str(raw)
    return None if target == END else target


def _parse_step(step_id: str, raw) -> Step:
    if not isinstance(raw, dict):
        raise WorkflowError(f"step {step_id!r} must be a mapping")

    gate = raw.get("gate")
    if gate is True:
        gate = "human approval required to enter this step"
    elif gate in (None, False):
        gate = None
    else:
        gate = str(gate)

    ask = _parse_ask(raw.get("ask"), step_id)
    if ask is not None and gate is not None:
        raise WorkflowError(
            f"step {step_id!r}: 'gate' and 'ask' are both entry approvals — "
            f"keep one. 'ask' with 'from: [{HUMAN}]' is the same gate"
        )
    verify = _parse_verify(raw.get("verify"), step_id)
    select = _parse_select(raw.get("select"), step_id)
    instructions = raw.get("instructions")
    if select is None and not instructions:
        raise WorkflowError(f"step {step_id!r} needs 'instructions' (or a 'select')")
    if select is not None:
        if verify is not None:
            raise WorkflowError(
                f"step {step_id!r}: 'verify' is not allowed on a select step"
            )
        if "next" in raw:
            raise WorkflowError(
                f"step {step_id!r}: a select step routes via its options; "
                f"'next' is not allowed"
            )
    return Step(
        id=step_id,
        title=str(raw["title"]) if raw.get("title") else None,
        instructions=str(instructions) if instructions else None,
        gate=gate,
        ask=ask,
        verify=verify,
        select=select,
        next=_parse_next(raw.get("next"), step_id),
    )


def _parse_up(raw, where: str) -> Tuple[int, int]:
    """``2`` or ``"2..4"`` -> an inclusive hop range.

    Absent is an ERROR, not "anywhere". See the module docstring: a candidate
    with no direction is a self-approval hole, because the asking agent can
    spawn a session with any handle it likes and so conjure a member matching
    a bare ``{role: ...}``.
    """
    if raw is None:
        raise WorkflowError(
            f"{where}: a member candidate needs 'up' (how many steps up the "
            f"spawn lineage to look, e.g. up: 1 for the direct parent, or "
            f"up: 2..4). It has no default on purpose — a candidate with no "
            f"direction could be matched by a session the run spawned itself"
        )
    if isinstance(raw, bool):
        raise WorkflowError(f"{where}: 'up' must be a number or 'a..b', got {raw!r}")
    if isinstance(raw, int):
        low = high = raw
    elif isinstance(raw, str) and ".." in raw:
        head, _, tail = raw.partition("..")
        try:
            low, high = int(head.strip()), int(tail.strip())
        except ValueError:
            raise WorkflowError(
                f"{where}: 'up' range must be two numbers like '2..4', got {raw!r}"
            ) from None
    else:
        raise WorkflowError(
            f"{where}: 'up' must be a number (up: 1) or an inclusive range "
            f"(up: '2..4'), got {raw!r}"
        )
    if low < 1:
        raise WorkflowError(
            f"{where}: 'up' starts at 1 (the direct parent); {low} would name "
            f"the asking session itself or something below it"
        )
    if high < low:
        raise WorkflowError(f"{where}: 'up' range {low}..{high} is empty")
    if high > MAX_UP:
        raise WorkflowError(f"{where}: 'up' reaches {high}, over the {MAX_UP} limit")
    return (low, high)


def _parse_candidate(raw, where: str) -> Candidate:
    if isinstance(raw, str):
        if raw.strip().lower() != HUMAN:
            raise WorkflowError(
                f"{where}: the only bare candidate is {HUMAN!r}; a member "
                f"candidate is a mapping like {{role: leader, up: 1}}, got {raw!r}"
            )
        return Candidate(human=True)
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"{where} must be {HUMAN!r} or a mapping like {{role: leader, up: 1}}, "
            f"got {raw!r}"
        )
    unknown = sorted(set(raw) - {"role", "up"})
    if unknown:
        raise WorkflowError(
            f"{where} has unknown key(s): {', '.join(unknown)} (allowed: role, up)"
        )
    role = raw.get("role")
    if role is not None:
        role = str(role).strip().lower()
        if not role:
            raise WorkflowError(f"{where}: 'role' must be a non-empty name")
        # NOT checked against a vocabulary here: roles are defined per mesh and
        # this parser runs with no daemon in reach. A role nothing answers to
        # surfaces when the ask is opened, naming the mesh it looked in.
    return Candidate(role=role, up=_parse_up(raw.get("up"), where))


def _parse_delegate(raw, where: str) -> Delegate:
    """The ``from``/``timeout`` pair shared by ``ask`` and a select chooser."""
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where} must be a mapping with a 'from:' list")
    candidates_raw = raw.get("from")
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise WorkflowError(
            f"{where} needs a non-empty 'from' list — who may answer, in "
            f"preference order, e.g. from: [{{role: leader, up: 1}}, {HUMAN}]"
        )
    candidates = [
        _parse_candidate(c, f"{where} from[{i}]")
        for i, c in enumerate(candidates_raw)
    ]
    timeout = raw.get("timeout")
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise WorkflowError(f"{where}: 'timeout' must be a number of seconds") from None
        if timeout <= 0:
            raise WorkflowError(f"{where}: 'timeout' must be greater than 0")
    return Delegate(candidates=candidates, timeout=timeout)


def _parse_ask(raw, step_id: str) -> Optional[Ask]:
    if raw is None:
        return None
    where = f"step {step_id!r}: ask"
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where} must be a mapping")
    unknown = sorted(set(raw) - {"prompt", "from", "timeout", "on_decline"})
    if unknown:
        raise WorkflowError(
            f"{where} has unknown key(s): {', '.join(unknown)} "
            "(allowed: prompt, from, timeout, on_decline)"
        )
    prompt = raw.get("prompt")
    if not prompt:
        raise WorkflowError(f"{where} needs a 'prompt' — what is being approved")
    on_decline = raw.get("on_decline")
    # Kept as written rather than normalized through `_parse_next`: for a
    # decline, "not declared" (park for a human) and "end" (finish the run)
    # are different answers, where for `next` they are the same one.
    if on_decline is not None:
        on_decline = str(on_decline)
    return Ask(
        prompt=str(prompt),
        delegate=_parse_delegate(raw, where),
        on_decline=on_decline,
    )


def _parse_verify(raw, step_id: str) -> Optional[Verify]:
    if raw is None:
        return None
    if isinstance(raw, str):
        return Verify(command=raw)
    if isinstance(raw, dict) and raw.get("command"):
        try:
            timeout = float(raw.get("timeout", DEFAULT_VERIFY_TIMEOUT))
        except (TypeError, ValueError):
            raise WorkflowError(f"step {step_id!r}: verify timeout must be a number")
        return Verify(command=str(raw["command"]), timeout=timeout)
    raise WorkflowError(
        f"step {step_id!r}: 'verify' must be a command string or {{command, timeout}}"
    )


def _parse_select(raw, step_id: str) -> Optional[Select]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkflowError(f"step {step_id!r}: 'select' must be a mapping")
    prompt = raw.get("prompt")
    if not prompt:
        raise WorkflowError(f"step {step_id!r}: select needs a 'prompt'")
    raw_chooser = raw.get("chooser") or "agent"
    delegate = None
    if isinstance(raw_chooser, dict):
        where = f"step {step_id!r}: select chooser"
        unknown = sorted(set(raw_chooser) - {"from", "timeout"})
        if unknown:
            raise WorkflowError(
                f"{where} has unknown key(s): {', '.join(unknown)} "
                "(allowed: from, timeout)"
            )
        delegate = _parse_delegate(raw_chooser, where)
        chooser = "delegate"
    else:
        chooser = str(raw_chooser)
        if chooser not in ("agent", "user"):
            raise WorkflowError(
                f"step {step_id!r}: select chooser must be 'agent', 'user', or "
                f"a mapping naming who decides, e.g. "
                f"chooser: {{from: [{{role: reviewer, up: 1}}, {HUMAN}]}}"
            )
    raw_options = raw.get("options")
    if not isinstance(raw_options, dict) or not raw_options:
        raise WorkflowError(f"step {step_id!r}: select needs non-empty 'options'")
    options: Dict[str, Option] = {}
    for name, spec in raw_options.items():
        name = str(name)
        if not isinstance(spec, dict):
            raise WorkflowError(f"step {step_id!r}: option {name!r} must be a mapping")
        options[name] = Option(
            name=name,
            description=str(spec.get("description") or ""),
            next=_parse_next(spec.get("next"), f"{step_id}.{name}"),
        )
    return Select(
        prompt=str(prompt), chooser=chooser, options=options, delegate=delegate
    )


# --------------------------------------------------------------------------- #
# graph validation
# --------------------------------------------------------------------------- #
def _validate_graph(workflow: Workflow) -> None:
    # 1. every edge target must exist (errors)
    for step in workflow.steps.values():
        for target in step.successors():
            if target is not None and target not in workflow.steps:
                raise WorkflowError(
                    f"step {step.id!r} points at unknown step {target!r} "
                    f"(use '{END}' to terminate)"
                )
    # 2. at least one termination must be reachable from start (error).
    #    In an acyclic graph this always holds; only cycles can starve it,
    #    which is exactly when an explicit end must be described.
    if workflow.start not in _can_finish(workflow):
        raise WorkflowError(
            "no termination is reachable from the start step — this workflow "
            f"loops forever; describe at least one end (omit 'next' or use "
            f"'next: {END}' somewhere reachable)"
        )


def _reachable(workflow: Workflow) -> Set[str]:
    seen: Set[str] = set()
    stack = [workflow.start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for target in workflow.steps[node].successors():
            if target is not None and target not in seen:
                stack.append(target)
    return seen


def _can_finish(workflow: Workflow) -> Set[str]:
    """Steps from which a termination is reachable (reverse closure)."""
    can: Set[str] = {
        s.id for s in workflow.steps.values() if any(t is None for t in s.successors())
    }
    changed = True
    while changed:
        changed = False
        for step in workflow.steps.values():
            if step.id in can:
                continue
            if any(t in can for t in step.successors() if t is not None):
                can.add(step.id)
                changed = True
    return can


def _cycle_nodes(workflow: Workflow) -> Set[str]:
    """Nodes that sit on some cycle (self-loops included)."""
    on_cycle: Set[str] = set()
    steps = workflow.steps

    def reaches(src: str, dst: str) -> bool:
        seen: Set[str] = set()
        stack = [src]
        while stack:
            node = stack.pop()
            if node == dst:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(t for t in steps[node].successors() if t is not None)
        return False

    for step_id, step in steps.items():
        for target in step.successors():
            if target is not None and reaches(target, step_id):
                on_cycle.add(step_id)
                break
    return on_cycle


def _graph_warnings(workflow: Workflow) -> List[str]:
    warnings: List[str] = []
    reachable = _reachable(workflow)
    cycles = _cycle_nodes(workflow) & reachable
    if cycles:
        warnings.append(
            "cycle detected (iteration is allowed, but make sure its exit "
            f"condition is real): {', '.join(sorted(cycles))}"
        )
    can_finish = _can_finish(workflow)
    trapped = sorted(reachable - can_finish)
    if trapped:
        warnings.append(
            "once entered, these steps can never reach a termination: "
            + ", ".join(trapped)
        )
    unreachable = sorted(set(workflow.steps) - reachable)
    if unreachable:
        warnings.append("defined but unreachable from start: " + ", ".join(unreachable))
    return warnings
