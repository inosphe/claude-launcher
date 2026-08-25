"""Workflow YAML schema: a directed graph of steps, parsed and validated.

Steps are defined **once** in a mapping and wired by id — structure is the
inline ``next`` pointers (and select options' ``next``), so shared sequences
and loops need no duplicated content::

    name: feature-dev
    description: ...
    start: design           # optional (defaults to the first step)
    max_visits: 25          # optional loop guard (per step, per run)
    recur: true             # optional: a finished round requests the next one
    filter_roles:           # optional: which mesh roles may DRIVE this run
      type: whitelist       # whitelist (only these) | blacklist (all but these)
      roles: [worker]
    default_role: worker    # optional: pickers auto-select this workflow
                            # when that role is chosen
    default_child_cflow: worker-flow   # optional: the run a child spawned
                            # by a session driving THIS one starts on
    priority: 10            # optional tie-breaker between defaults for the
                            # same role (higher wins; 0 when unstated)
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
        gate: "message"     # DEPRECATED spelling of `ask: {prompt: message}`
        verify: "pytest -q" # machine gate to LEAVE (exit 0)
        next: test
      test:
        instructions: ...
        done_when: ...      # completion criterion; restated by status/reminders
        verify: "pytest -q"
        awaits: verify      # what this step WAITS for: the daemon re-measures
                            # this step's verify and speaks only when its exit
                            # code moves. See "Waiting for a signal" below.
        next: review
      review:
        select:
          prompt: pass?
          chooser: user
          options:
            ok:     {description: done,    next: ship}
            rework: {description: loop back, next: impl}   # a cycle — warned
      # an option may carry `interval: <seconds>` — a cadence: taken by the
      # driving agent, it is HELD until that long has passed since it was
      # last taken (across rounds of a recurring run), then released by the
      # daemon's clock. See "Cadence" below.
      ship:
        ask:                    # approval to ENTER, re-required per visit
          prompt: ship it?
          from:                 # preference order; each entry names a role
            - {role: reviewer}
            - {role: leader, scope: ancestor}
          otherwise: human      # human (default) | self
          timeout: 900
          on_decline: impl
        instructions: ...
        # no 'next' (or 'next: end') = termination

Termination: omitting ``next`` (or the reserved target ``end``) ends the run.
Cycles are legal (they model iteration; a select is the loop exit) but are
**warned** about; a workflow whose start cannot reach any termination is an
**error** — at least one reachable end must be described.

Completion criteria
-------------------
``verify`` is the machine-checkable completion criterion: a command that must
exit 0 before ``next`` may leave the step. ``done_when`` is the declarative
one — prose stating what must be *true* for the step to count as done — for
the steps (most of them) whose done-ness no command can check. It enforces
nothing; its value is placement: it rides in the step payload and in the
reminder a stalled run hears, so "may I advance?" is answered against a
criterion the author wrote instead of one the driver improvises. The two
compose — ``verify`` checks what a command can, ``done_when`` states the
rest. A select step takes neither: its completion IS the choice. Steps that
declare neither are collected into :attr:`Workflow.advice` — the driver
certifies its own way past them, which is worth a line in ``show`` but is
not an error.

``recur: true`` is how a service loop is written without hiding one in the
graph: every round still reaches a real end, and a run that terminates
normally files the start request for its next round (see the engine). The
repetition is a property of the run's lifecycle, stopped by a human — never
a cycle the reachability rule would have to excuse.

Cadence
-------
A select option may declare ``interval: <seconds>``: the driving agent may
take that option at most once per interval, measured from the last time it
was taken in this slot — across rounds of a recurring run, since the record
outlives the run (``state.windows``). Chosen inside the interval, the choice
is *held* rather than refused: the run reports ``waiting_window`` with the
moment the window opens, the daemon's clock releases it then (moving the run
and waking the driver), and the reason recorded at release is the latest one
the agent filed — re-selecting the same option while held only updates it,
selecting another option cancels the hold. It is how "merge what has
accumulated every N minutes" is written without a timer in the agent: the
first take is immediate (nothing to pace against yet), the rest batch. A
human confirming the option (CLI, dashboard) is not paced — that is the
override, and it is journaled as one.

Waiting for a signal
--------------------
``verify`` and ``done_when`` both answer "may this step be left?". ``awaits``
answers a different question — "what is it standing still *for*?" — and it is
the only one of the three the run itself never reads: the daemon's reminder
clock does.

Without it a stalled run is on a clock. The reminder repeats the step's
instructions every interval for as long as the position does not move, and it
has no opinion about whether what the step is waiting for has arrived — the
step can be quoting a ``verify`` that went green ten minutes ago and the
reminder will still read as "not yet". With ``awaits`` declared, the clock
runs the probe every ``poll`` seconds and:

* **speaks once when the exit code changes**, carrying what moved and the
  probe's own output as evidence — "what you were waiting for arrived", not
  the step restated;
* **says nothing at all while it does not change.** That silence is the
  feature. A condition that has not moved is not news, and a reminder that
  fires anyway teaches its reader to skim past the one that matters;
* **falls back to the clock the moment it cannot measure.** A probe that
  times out or cannot be launched is not the answer "not yet" — it is no
  answer, so the ordinary reminder resumes rather than a broken probe
  silencing the run. That distinction is the whole safety story: silence is
  only ever granted while something is actually watching.

The exit code is the fact; output is evidence and is not compared, so a probe
free to print a timestamp does not "change" every sample. The first
measurement at a position is the baseline and never fires — an agent that has
just been handed the step does not need to be told the state it arrived in.

``awaits: verify`` re-measures the step's own ``verify`` rather than repeating
its command, and is written per step on purpose. A ``verify`` is contracted to
run *once, on the way out*; sampling one every minute demands that it also be
read-only and idempotent, and that demand is reasonable to make of a command an
author nominated and unreasonable to impose on every ``verify`` ever written.
Cost is not left to the promise: the probe runs under ``awaits.timeout``
(default 10s, hard-capped at 30s), never the verify's own, so a suite
nominated by mistake times out into "cannot measure" instead of being run on a
loop. ``poll`` has a floor for the mirror-image reason.

A workflow that gains an ``awaits`` does not change any run already in
flight: a run reads the snapshot it started on, where the field is simply
absent, and an absent ``awaits`` is exactly today's clock. New behaviour
arrives with the next run, never under one already moving.

Delegated decisions
-------------------
``ask`` (approval to enter a step) and ``select.chooser`` (which branch to
take) both accept the same declaration, and it has **two independent axes**:

``from``
    Who is *asked* — an ordered list of roles, tried a group at a time. A
    group that resolves to nobody, or that does not answer within ``timeout``,
    escalates to the next one, so "a reviewer, else a leader" is one list
    rather than two mechanisms. Omitted, no agent is asked at all.
``otherwise``
    What happens when that list runs out: ``human`` (default — hold the run
    for ``claunch cflow approve|select``) or ``self`` (the driving agent
    decides alone, journaled as unanswered and never as an approval).

A human is never an entry in ``from``: nothing resolves them, nothing notifies
them, and they answer through a different door. Keeping them on the other axis
is what stops the two spellings from saying the same thing twice — and is why
``gate: <msg>`` deprecates into a ``from``-less ``ask: {prompt: <msg>}``.

Role filter
-----------
``filter_roles`` names which mesh roles may *drive* a run of this workflow —
the session that starts it, not the ones it delegates to. ``type: whitelist``
admits only the listed roles; ``type: blacklist`` admits everything but them.
Role names are not checked against a vocabulary here for the same reason a
candidate's role is not: roles are defined per mesh and the parser runs with
no daemon in reach. The filter is enforced at ``start`` against the driving
session's recorded mesh role; a driver with no resolvable mesh identity (not
a managed session, not enrolled, daemon down) is admitted with the fact
journaled — the filter is a guardrail on the fleet's division of labour, and
a standalone run has no labour to divide.

``default_role`` is the opposite arrow, and advisory where the filter is an
enforcement: it names the mesh role this workflow volunteers itself to, so a
picker (the wizard's Role row) that has that role chosen selects this workflow
without being asked. Several workflows may volunteer for the same role;
``priority`` breaks the tie (higher wins) and orders them wherever they are
listed as candidates. Neither admits anybody anywhere — ``filter_roles``
still decides who may drive — and a ``default_role`` the workflow's own
filter turns away is a contradiction refused at parse time.

Pairing a child's run
---------------------
``default_child_cflow`` is the third arrow, and it points *down the spawn
tree*: it names the workflow a child gets when the session driving this one
spawns one without naming a run. That is what makes "leader flow and worker
flow" a **pair** rather than a convention two files each half-remember —
declared once, on the parent's side, where the pairing is actually known.

It is deliberately not the role's business. A role says what a member IS on
the mesh and travels with it across every workflow; which run its children
drive is a property of *the procedure the parent is running*, and reading it
off the role instead was the bug this field closes — a leader driving some
other workflow entirely still handed its children the worker flow, because
the child's role was worker and that role volunteered one. With the pair, a
parent driving a workflow that declares none gives its children none, and
"no run" is a legible answer rather than a forgotten field.

Unresolved on purpose (see :func:`_parse_child_cflow`): a workflow may pair
with a name that is not declared in the directory a child ends up standing
in, and only the spawn knows that directory.

A candidate needs a ``role`` — a delegation is to a *function*, and "whoever
happens to be connected" is not one. ``scope`` narrows further: ``any``
(default) is anything the asking session can reach over the mesh that is not
itself or something it spawned, ``ancestor`` is the chain of command only.
Descendants are excluded either way, and that exclusion is what makes a
delegated approval mean anything: an agent can spawn children and wire itself
to them, so an unfiltered pool would let a run manufacture its own approver.
It cannot spawn a sibling or wire itself to one, so siblings, uncles and roots
are as safe as ancestors — and a sibling reviewer is the common shape here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

DEFAULT_VERIFY_TIMEOUT = 900.0
DEFAULT_MAX_VISITS = 25

#: ``awaits.probe`` spelled as this reserved word re-measures the step's own
#: ``verify`` command instead of naming a second one. Never a default: writing
#: it is the author's assertion that *that* command is cheap and read-only,
#: which is a promise about one step and not a new demand on every ``verify``.
AWAITS_VERIFY = "verify"

#: How often the daemon may re-run a probe, and the floor under it. Below the
#: floor the clock stops sampling a condition and starts hammering it.
DEFAULT_AWAITS_POLL = 60.0
MIN_AWAITS_POLL = 15.0

#: How long one run of a probe may take. The ceiling is the load-bearing part:
#: a probe is a cheap question about state somebody else changed, and thirty
#: seconds is not enough to run a test suite in. It is what stops "wait until
#: the sweep is green" from being written as "run the sweep every minute".
DEFAULT_AWAITS_TIMEOUT = 10.0
MAX_AWAITS_TIMEOUT = 30.0

#: Reserved next-target meaning "the workflow ends here".
END = "end"

#: ``otherwise``: what happens once every candidate group has been tried.
#: Hold the run for a human, through the CLI or the dashboard...
OTHERWISE_HUMAN = "human"
#: ...or let the driving agent carry on alone. Never called an approval.
OTHERWISE_SELF = "self"
OTHERWISE = (OTHERWISE_HUMAN, OTHERWISE_SELF)

#: ``scope``: which part of the reachable mesh a candidate may match.
#: Anything the asking session can reach that it did not spawn...
SCOPE_ANY = "any"
#: ...or its own chain of command only.
SCOPE_ANCESTOR = "ancestor"
SCOPES = (SCOPE_ANY, SCOPE_ANCESTOR)

#: ``filter_roles.type``: admit only the listed roles...
FILTER_WHITELIST = "whitelist"
#: ...or admit every role except the listed ones.
FILTER_BLACKLIST = "blacklist"
FILTER_TYPES = (FILTER_WHITELIST, FILTER_BLACKLIST)


class WorkflowError(Exception):
    """Raised for unreadable or invalid workflow files."""


@dataclass(frozen=True)
class Verify:
    command: str
    timeout: float = DEFAULT_VERIFY_TIMEOUT


@dataclass(frozen=True)
class Awaits:
    """What a step is waiting for, in a form the daemon can re-measure.

    ``verify`` and ``done_when`` say when a step may be *left*. This says what
    it is *sitting still for* — and unlike those two it is read by nobody in
    the run: the daemon's reminder clock runs :attr:`probe` every
    :attr:`poll` seconds while the run holds this position, and speaks only
    when the answer changes.

    The exit code is the fact. Output is carried into the signal as evidence
    and is deliberately not part of the comparison: a probe that prints a
    timestamp would otherwise "change" on every sample, which is the noise
    this field exists to remove.

    ``probe`` of ``None`` is the reserved spelling :data:`AWAITS_VERIFY` — re-measure
    the step's own ``verify`` command. It is spelled per step, never defaulted:
    a ``verify`` is contracted to run once, on the way out, and re-running one
    every minute demands that it be read-only and idempotent. That demand is
    fair to make of a command an author explicitly nominated and unfair to
    make of every ``verify`` already written. Cheapness is not left to the
    promise either — the probe runs under :attr:`timeout` (capped at
    :data:`MAX_AWAITS_TIMEOUT`), not under the verify's own, so a heavy command
    nominated by mistake times out into "cannot measure" rather than running.
    """

    #: The command whose exit code answers "has it arrived?", or ``None`` for
    #: the step's own ``verify`` command (see :data:`AWAITS_VERIFY`).
    probe: Optional[str] = None
    poll: float = DEFAULT_AWAITS_POLL
    timeout: float = DEFAULT_AWAITS_TIMEOUT
    #: One line naming the condition in human terms, for the signal's text.
    #: Without it the signal shows the command, which is true but rarely says
    #: what the waiting was *about*.
    describe: Optional[str] = None

    def command(self, step: "Step") -> Optional[str]:
        """The command to actually run, resolving the reserved spelling."""
        if self.probe is not None:
            return self.probe
        return step.verify.command if step.verify else None


@dataclass(frozen=True)
class Option:
    name: str
    description: str
    next: Optional[str] = None  # None = termination
    #: Cadence in seconds: the driving agent's take of this option is held
    #: until this long has passed since it was last taken (see "Cadence" in
    #: the module docstring). ``None`` = no pacing.
    interval: Optional[float] = None


@dataclass(frozen=True)
class Candidate:
    """One entry of a delegation's preference list: a kind of responder.

    A role, matched against the mesh members the asking session can reach,
    minus itself and everything below it in the spawn tree. ``scope`` narrows
    that pool to the session's own ancestors when only the chain of command
    will do.
    """

    role: str
    scope: str = SCOPE_ANY

    def describe(self) -> str:
        """One line for a payload, a message or an error — never parsed."""
        return self.role if self.scope == SCOPE_ANY else f"{self.role} ({self.scope})"


@dataclass(frozen=True)
class Delegate:
    """Who may answer a decision, and what to do when nobody does.

    ``candidates`` is read a group at a time — index 0 first — and everybody a
    group matches is asked together, so "a reviewer, else a leader" and "any of
    the three reviewers I can reach" are the same declaration read two ways.
    ``timeout`` is per group, not for the whole list. An empty list is legal
    and means no agent is asked: ``otherwise`` decides straight away.
    """

    candidates: List[Candidate] = field(default_factory=list)
    otherwise: str = OTHERWISE_HUMAN
    timeout: Optional[float] = None

    def describe(self) -> str:
        """The preference list as one line, ending in the fallback."""
        return " -> ".join([c.describe() for c in self.candidates] + [self.otherwise])


@dataclass(frozen=True)
class RoleFilter:
    """Which mesh roles may drive a run of this workflow.

    A statement about the *driver* — the session that starts the run — not
    about who it may delegate to (that is each decision's ``from``). Roles are
    stored lower-cased, matching how the mesh stores a member's resolved role.
    """

    type: str  # FILTER_WHITELIST | FILTER_BLACKLIST
    roles: Tuple[str, ...]

    def allows(self, role: str) -> bool:
        held = str(role or "").strip().lower()
        if self.type == FILTER_WHITELIST:
            return held in self.roles
        return held not in self.roles

    def describe(self) -> str:
        """One line for `show`, a payload or an error — never parsed."""
        return f"{self.type}({', '.join(self.roles)})"


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
    #: Declarative completion criterion — what must be TRUE for this step to
    #: count as done, for the parts no command can check (those are `verify`).
    #: Never enforced; surfaced by the step payload and the reminder clock.
    done_when: Optional[str] = None
    #: What this step is WAITING for, re-measurable by the daemon. Where
    #: `verify` and `done_when` say when the step may be left, this says what
    #: the standing still is for — and it is the only one of the three the
    #: run itself never reads. See :class:`Awaits`.
    awaits: Optional[Awaits] = None
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
    #: A service loop: a run that terminates normally files a start request
    #: for its next round instead of going quiet. The graph is untouched —
    #: every round must still reach a real end, so ``max_visits`` keeps
    #: meaning the rework budget *within* a round — and stopping is a human
    #: act (withdraw the request, or abort/archive mid-round), never the
    #: driving agent's decision.
    recur: bool = False
    #: Which mesh roles may drive a run of this workflow; ``None`` = any.
    filter_roles: Optional[RoleFilter] = None
    #: The mesh role this workflow volunteers itself to: pickers auto-select
    #: it when that role is chosen. Advisory — :attr:`filter_roles` remains
    #: the enforcement. Stored lower-cased, like every resolved role.
    default_role: Optional[str] = None
    #: The workflow a session driving THIS one gives to a child it spawns —
    #: the other half of a pair, named from the parent's side. ``None`` means
    #: this workflow pairs with nothing, and a child of it starts with no run
    #: unless the spawn names one.
    default_child_cflow: Optional[str] = None
    #: Tie-breaker between workflows volunteering for the same role, and the
    #: order pickers list them in: higher first, 0 when unstated.
    priority: int = 0
    warnings: List[str] = field(default_factory=list)
    #: Superseded spellings this file still uses. Kept apart from
    #: :attr:`warnings` on purpose: a warning describes a graph that may
    #: misbehave and belongs in front of whoever *runs* it, while this is
    #: advice to whoever *writes* it. Surfaced by ``cflow show`` and the
    #: dashboard's workflow view; ``start`` stays quiet, so a workflow already
    #: in service does not nag on every run.
    deprecations: List[str] = field(default_factory=list)
    #: More advice to the writer, same channel as :attr:`deprecations`:
    #: steps whose done-ness nothing states (no ``verify``, no ``done_when``),
    #: so the driver certifies its own way past them. Legal — plenty of steps
    #: are cheap enough not to care — but worth showing whoever reviews the
    #: file, and never in front of a run.
    advice: List[str] = field(default_factory=list)

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
    recur = doc.get("recur", False)
    if not isinstance(recur, bool):
        raise WorkflowError("'recur' must be true or false")
    filter_roles = _parse_role_filter(doc.get("filter_roles"))
    default_role: Optional[str] = None
    if doc.get("default_role") is not None:
        default_role = str(doc.get("default_role")).strip().lower()
        if not default_role:
            raise WorkflowError("'default_role' must be a non-empty role name")
    default_child_cflow = _parse_child_cflow(doc)
    try:
        priority = int(doc.get("priority", 0))
    except (TypeError, ValueError):
        raise WorkflowError("'priority' must be an integer")
    if default_role and filter_roles and not filter_roles.allows(default_role):
        raise WorkflowError(
            f"'default_role' {default_role!r} is turned away by this "
            f"workflow's own filter_roles {filter_roles.describe()} — a "
            f"default nobody may drive; drop one of the two"
        )

    workflow = Workflow(
        name=str(doc.get("name") or default_name),
        description=str(doc.get("description") or ""),
        start=start,
        steps=steps,
        max_visits=max_visits,
        recur=recur,
        filter_roles=filter_roles,
        default_role=default_role,
        default_child_cflow=default_child_cflow,
        priority=priority,
        warnings=[],
    )
    _validate_graph(workflow)
    return Workflow(
        name=workflow.name,
        description=workflow.description,
        start=workflow.start,
        steps=workflow.steps,
        max_visits=workflow.max_visits,
        recur=workflow.recur,
        filter_roles=workflow.filter_roles,
        default_role=workflow.default_role,
        default_child_cflow=workflow.default_child_cflow,
        priority=workflow.priority,
        warnings=_graph_warnings(workflow),
        deprecations=_deprecations(workflow),
        advice=_advice(workflow),
    )


def _deprecations(workflow: Workflow) -> List[str]:
    return [
        f"step {step.id!r}: 'gate:' is deprecated — write it as "
        f"'ask: {{prompt: <the gate message>}}'. That is the same human "
        f"approval, in the form that can also name agents ('from: "
        f"[{{role: reviewer}}]') as the approvers"
        for step in workflow.steps.values()
        if step.gate
    ]


def _advice(workflow: Workflow) -> List[str]:
    """One aggregated note for the steps whose done-ness nothing states.

    A select step is exempt (its completion is the choice), and one line
    covers them all: this is a review aid, not a per-step nag.
    """
    silent = [
        s.id
        for s in workflow.steps.values()
        if not s.is_select and not s.verify and not s.done_when
    ]
    if not silent:
        return []
    return [
        "steps with no completion criterion (neither 'verify' nor 'done_when'): "
        + ", ".join(silent)
        + " — the driver certifies its own way past them. State what must be "
        "true to leave each one ('done_when'), or make it a command "
        "('verify') where one can check"
    ]


def load(path: Path) -> Workflow:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"cannot read workflow {path}: {exc}") from exc
    return parse(text, default_name=path.stem)


#: The pair field's canonical spelling, and the dashed one accepted beside
#: it. Every other key here is underscored, so that is the spelling ``show``
#: and the docs use; the dash is taken too because it is the shape a person
#: writes when the word "cflow" is on their mind rather than the file's other
#: keys, and a silently-ignored key would be a pair that never fires.
CHILD_CFLOW_KEY = "default_child_cflow"
CHILD_CFLOW_ALIAS = "default-child-cflow"


def _parse_child_cflow(doc: dict) -> Optional[str]:
    """``default_child_cflow``: the workflow this one hands to its children.

    Read from either spelling, refusing a file that uses both to say
    different things — two keys that disagree have no reading, and picking
    one would decide which of the author's two intentions is the typo.

    The name is *not* resolved here: which workflows exist depends on the
    directory a child will stand in, which this parser cannot see. A pair
    naming a workflow that is not declared there is settled at spawn time,
    where the answer is knowable.
    """
    present = [k for k in (CHILD_CFLOW_KEY, CHILD_CFLOW_ALIAS) if doc.get(k) is not None]
    if not present:
        return None
    values = {str(doc[k]).strip() for k in present}
    if len(values) > 1:
        raise WorkflowError(
            f"{CHILD_CFLOW_KEY!r} and {CHILD_CFLOW_ALIAS!r} are the same key "
            f"spelled two ways, but this file gives them different values "
            f"({', '.join(sorted(repr(v) for v in values))}) — keep one"
        )
    name = values.pop()
    if not name:
        raise WorkflowError(
            f"{present[0]!r} must name a workflow — drop the key to pair "
            f"with nothing"
        )
    return name


def _parse_role_filter(raw) -> Optional[RoleFilter]:
    if raw is None:
        return None
    where = "'filter_roles'"
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"{where} must be a mapping like "
            f"{{type: {FILTER_WHITELIST}, roles: [worker]}}"
        )
    unknown = sorted(set(raw) - {"type", "roles"})
    if unknown:
        raise WorkflowError(
            f"{where} has unknown key(s): {', '.join(unknown)} "
            "(allowed: type, roles)"
        )
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in FILTER_TYPES:
        raise WorkflowError(
            f"{where}: 'type' must be one of {', '.join(FILTER_TYPES)}, got "
            f"{raw.get('type')!r} ({FILTER_WHITELIST} = only these roles may "
            f"drive a run, {FILTER_BLACKLIST} = every role but these may)"
        )
    raw_roles = raw.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise WorkflowError(
            f"{where}: 'roles' must be a non-empty list of role names, "
            f"e.g. roles: [worker, specialist]"
        )
    roles: List[str] = []
    for entry in raw_roles:
        name = str(entry or "").strip().lower()
        if not name:
            raise WorkflowError(f"{where}: a role name must be non-empty")
        if name not in roles:
            roles.append(name)
        # Not checked against a vocabulary: roles are defined per mesh and
        # this parser runs with no daemon in reach (same rule as a
        # delegation candidate's role).
    return RoleFilter(type=kind, roles=tuple(roles))


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
            f"keep one. An 'ask' with no 'from' is the same gate"
        )
    verify = _parse_verify(raw.get("verify"), step_id)
    done_when = raw.get("done_when")
    if done_when is not None and not isinstance(done_when, str):
        raise WorkflowError(
            f"step {step_id!r}: 'done_when' must be a string — what must be "
            f"true for this step to count as done"
        )
    done_when = done_when.strip() if done_when else None
    awaits = _parse_awaits(raw.get("awaits"), step_id)
    select = _parse_select(raw.get("select"), step_id)
    if (
        awaits is not None
        and awaits.probe is None
        and verify is None
        and select is None  # a select step gets the sharper message below
    ):
        raise WorkflowError(
            f"step {step_id!r}: 'awaits: {AWAITS_VERIFY}' re-measures this "
            f"step's own verify command, and it has none — either give the "
            f"step a 'verify', or name the probe: "
            f"awaits: {{probe: '<command>'}}"
        )
    instructions = raw.get("instructions")
    if select is None and not instructions:
        raise WorkflowError(f"step {step_id!r} needs 'instructions' (or a 'select')")
    if select is not None:
        if verify is not None:
            raise WorkflowError(
                f"step {step_id!r}: 'verify' is not allowed on a select step"
            )
        if done_when:
            raise WorkflowError(
                f"step {step_id!r}: 'done_when' is not allowed on a select "
                f"step — its completion is the choice itself"
            )
        if awaits is not None and awaits.probe is None:
            raise WorkflowError(
                f"step {step_id!r}: 'awaits: {AWAITS_VERIFY}' has nothing to "
                f"re-measure on a select step — a select step takes no "
                f"'verify'. Name the probe: awaits: {{probe: '<command>'}}"
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
        done_when=done_when,
        awaits=awaits,
        select=select,
        next=_parse_next(raw.get("next"), step_id),
    )


def _parse_candidate(raw, where: str) -> Candidate:
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"{where} must be a mapping naming a role, like {{role: reviewer}} "
            f"or {{role: leader, scope: ancestor}}, got {raw!r}"
        )
    unknown = sorted(set(raw) - {"role", "scope"})
    if unknown:
        raise WorkflowError(
            f"{where} has unknown key(s): {', '.join(unknown)} (allowed: role, scope)"
        )
    role = str(raw.get("role") or "").strip().lower()
    if not role:
        # No default, and not optional: a delegation is to a function. Asking
        # "whoever I happen to be wired to" would make the answer depend on
        # topology alone, which is not a decision anybody declared.
        raise WorkflowError(
            f"{where} needs a 'role' — which kind of session may answer, e.g. "
            f"{{role: reviewer}}"
        )
        # The role is NOT checked against a vocabulary here: roles are defined
        # per mesh and this parser runs with no daemon in reach. A role nothing
        # answers to surfaces when the ask is opened, naming the mesh it
        # looked in.
    scope = str(raw.get("scope") or SCOPE_ANY).strip().lower()
    if scope not in SCOPES:
        raise WorkflowError(
            f"{where}: 'scope' must be one of {', '.join(SCOPES)}, got {scope!r} "
            f"({SCOPE_ANY} = anyone reachable that this run did not spawn, "
            f"{SCOPE_ANCESTOR} = its own chain of command only)"
        )
    return Candidate(role=role, scope=scope)


def _parse_delegate(raw, where: str) -> Delegate:
    """The ``from``/``otherwise``/``timeout`` trio shared by ask and chooser."""
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where} must be a mapping")
    candidates_raw = raw.get("from")
    if candidates_raw is None:
        # Legal, and the shape `gate:` deprecates into: nobody is asked, so
        # `otherwise` decides immediately.
        candidates_raw = []
    if not isinstance(candidates_raw, list):
        raise WorkflowError(
            f"{where}: 'from' must be a list of roles in preference order, "
            f"e.g. from: [{{role: reviewer}}, {{role: leader, scope: ancestor}}]"
        )
    candidates = [
        _parse_candidate(c, f"{where} from[{i}]")
        for i, c in enumerate(candidates_raw)
    ]
    otherwise = str(raw.get("otherwise") or OTHERWISE_HUMAN).strip().lower()
    if otherwise not in OTHERWISE:
        raise WorkflowError(
            f"{where}: 'otherwise' must be one of {', '.join(OTHERWISE)}, got "
            f"{otherwise!r} (what happens once every candidate has been tried: "
            f"{OTHERWISE_HUMAN} = hold for a person, {OTHERWISE_SELF} = the "
            f"running agent decides alone)"
        )
    timeout = raw.get("timeout")
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise WorkflowError(f"{where}: 'timeout' must be a number of seconds") from None
        if timeout <= 0:
            raise WorkflowError(f"{where}: 'timeout' must be greater than 0")
    return Delegate(candidates=candidates, otherwise=otherwise, timeout=timeout)


def _parse_ask(raw, step_id: str) -> Optional[Ask]:
    if raw is None:
        return None
    where = f"step {step_id!r}: ask"
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where} must be a mapping")
    unknown = sorted(set(raw) - {"prompt", "from", "otherwise", "timeout", "on_decline"})
    if unknown:
        raise WorkflowError(
            f"{where} has unknown key(s): {', '.join(unknown)} "
            "(allowed: prompt, from, otherwise, timeout, on_decline)"
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


def _parse_awaits(raw, step_id: str) -> Optional[Awaits]:
    """Parse a step's ``awaits``: the reserved word, or a mapping.

    There is deliberately no bare-command shorthand. ``awaits: verify`` has to
    mean the reserved spelling, and a config language in which one scalar is
    sometimes a keyword and sometimes a shell command is a trap nobody reads
    the docs in time to avoid — so a command is always written
    ``{probe: '<command>'}``.

    The ceilings below are refusals, not advice. "Wait until the suite is
    green" is the shape this field invites and the one thing it must not
    allow: a probe is a cheap question, and a workflow that hangs a test run
    on one has the daemon re-running the suite for as long as the step sits
    there. That is a parse error here rather than a note in a docstring
    somebody skims.
    """
    if raw is None:
        return None
    if raw is True or (isinstance(raw, str) and raw.strip() == AWAITS_VERIFY):
        # `awaits: verify` (and YAML's `awaits: true`, which reads the same
        # way at a glance) — re-measure this step's own verify command.
        return Awaits()
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"step {step_id!r}: 'awaits' must be the word {AWAITS_VERIFY!r} "
            f"(re-measure this step's own verify) or a mapping "
            f"{{probe, poll, timeout, describe}} — got {raw!r}. A command is "
            f"written as {{probe: '<command>'}}, never as a bare string"
        )
    unknown = sorted(set(raw) - {"probe", "poll", "timeout", "describe"})
    if unknown:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits' has unknown key(s): "
            f"{', '.join(unknown)} (allowed: probe, poll, timeout, describe)"
        )
    probe = raw.get("probe")
    if probe is None or (isinstance(probe, str) and probe.strip() == AWAITS_VERIFY):
        probe = None
    elif not isinstance(probe, str) or not probe.strip():
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.probe' must be a command string, or "
            f"the word {AWAITS_VERIFY!r} to re-measure this step's own verify"
        )
    else:
        probe = probe.strip()

    poll = _parse_seconds(raw.get("poll"), DEFAULT_AWAITS_POLL, step_id, "poll")
    if poll < MIN_AWAITS_POLL:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.poll' must be at least "
            f"{MIN_AWAITS_POLL:.0f}s, got {poll:g} — below that the daemon is "
            f"not sampling a condition, it is hammering it"
        )
    timeout = _parse_seconds(
        raw.get("timeout"), min(DEFAULT_AWAITS_TIMEOUT, poll), step_id, "timeout"
    )
    if timeout <= 0:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.timeout' must be positive, got {timeout:g}"
        )
    if timeout > MAX_AWAITS_TIMEOUT:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.timeout' may not exceed "
            f"{MAX_AWAITS_TIMEOUT:.0f}s, got {timeout:g} — a probe is a cheap "
            f"check on state something else changed. If the condition takes "
            f"longer than that to measure, what you have is a job, and a job "
            f"does not belong on a clock that re-runs it every {poll:g}s"
        )
    if timeout > poll:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.timeout' ({timeout:g}s) is longer than "
            f"'awaits.poll' ({poll:g}s) — a probe that cannot finish inside "
            f"its own interval never yields a stable answer"
        )
    describe = raw.get("describe")
    if describe is not None and not isinstance(describe, str):
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.describe' must be a string — one line "
            f"naming, in human terms, what the step is waiting for"
        )
    return Awaits(
        probe=probe,
        poll=poll,
        timeout=timeout,
        describe=describe.strip() if describe else None,
    )


def _parse_seconds(raw, default: float, step_id: str, field_name: str) -> float:
    """A number of seconds from an ``awaits`` field, or its default."""
    if raw is None:
        return float(default)
    if isinstance(raw, bool):
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.{field_name}' must be a number of seconds"
        )
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.{field_name}' must be a number of "
            f"seconds, got {raw!r}"
        ) from None


def _parse_interval(raw, where: str) -> Optional[float]:
    """An option's cadence, in seconds. ``None`` when it declares none."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise WorkflowError(f"option {where!r}: 'interval' must be a number of seconds")
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        raise WorkflowError(
            f"option {where!r}: 'interval' must be a number of seconds — how "
            f"long must pass between two takes of this option"
        ) from None
    if interval <= 0:
        raise WorkflowError(f"option {where!r}: 'interval' must be greater than 0")
    return interval


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
        unknown = sorted(set(raw_chooser) - {"from", "otherwise", "timeout"})
        if unknown:
            raise WorkflowError(
                f"{where} has unknown key(s): {', '.join(unknown)} "
                "(allowed: from, otherwise, timeout)"
            )
        delegate = _parse_delegate(raw_chooser, where)
        chooser = "delegate"
    else:
        chooser = str(raw_chooser)
        if chooser not in ("agent", "user"):
            raise WorkflowError(
                f"step {step_id!r}: select chooser must be 'agent', 'user', or "
                f"a mapping naming who decides, e.g. "
                f"chooser: {{from: [{{role: reviewer}}], otherwise: human}}"
            )
    raw_options = raw.get("options")
    if not isinstance(raw_options, dict) or not raw_options:
        raise WorkflowError(f"step {step_id!r}: select needs non-empty 'options'")
    options: Dict[str, Option] = {}
    for name, spec in raw_options.items():
        name = str(name)
        if not isinstance(spec, dict):
            raise WorkflowError(f"step {step_id!r}: option {name!r} must be a mapping")
        unknown = sorted(set(spec) - {"description", "next", "interval"})
        if unknown:
            raise WorkflowError(
                f"step {step_id!r}: option {name!r} has unknown key(s): "
                f"{', '.join(unknown)} (allowed: description, next, interval)"
            )
        options[name] = Option(
            name=name,
            description=str(spec.get("description") or ""),
            next=_parse_next(spec.get("next"), f"{step_id}.{name}"),
            interval=_parse_interval(spec.get("interval"), f"{step_id}.{name}"),
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
