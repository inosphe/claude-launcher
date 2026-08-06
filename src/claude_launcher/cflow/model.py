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
        gate: "message"     # human approval to ENTER, re-required per visit
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
        instructions: ...
        # no 'next' (or 'next: end') = termination

Termination: omitting ``next`` (or the reserved target ``end``) ends the run.
Cycles are legal (they model iteration; a select is the loop exit) but are
**warned** about; a workflow whose start cannot reach any termination is an
**error** — at least one reachable end must be described.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

DEFAULT_VERIFY_TIMEOUT = 900.0
DEFAULT_MAX_VISITS = 25

#: Reserved next-target meaning "the workflow ends here".
END = "end"


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
class Select:
    prompt: str
    chooser: str  # "agent" | "user"
    options: Dict[str, Option] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    id: str
    title: Optional[str] = None
    instructions: Optional[str] = None
    gate: Optional[str] = None  # entry-gate message; approval per visit
    verify: Optional[Verify] = None
    select: Optional[Select] = None
    next: Optional[str] = None  # None = termination (non-select steps)

    @property
    def is_select(self) -> bool:
        return self.select is not None

    def successors(self) -> List[Optional[str]]:
        """Outgoing edges (None entries are terminations)."""
        if self.select:
            return [o.next for o in self.select.options.values()]
        return [self.next]


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    start: str
    steps: Dict[str, Step]
    max_visits: int = DEFAULT_MAX_VISITS
    warnings: List[str] = field(default_factory=list)

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
    )


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
        verify=verify,
        select=select,
        next=_parse_next(raw.get("next"), step_id),
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
    chooser = str(raw.get("chooser") or "agent")
    if chooser not in ("agent", "user"):
        raise WorkflowError(f"step {step_id!r}: select chooser must be 'agent' or 'user'")
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
    return Select(prompt=str(prompt), chooser=chooser, options=options)


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
