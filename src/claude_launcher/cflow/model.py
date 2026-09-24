"""Workflow YAML schema: a directed graph of steps, parsed and validated.

Steps are defined **once** in a mapping and wired by id — structure is the
inline ``next`` pointers (and select options' ``next``), so shared sequences
and loops need no duplicated content::

    name: feature-dev
    description: ...
    extends: feature-dev    # optional: this file is a LAYER over that
                            # workflow (searched from this file's own layer
                            # downward), carrying only the properties it
                            # changes. Mappings merge one property at a time,
                            # everything else replaces, `null` deletes
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
        triggers: [briefing]  # daemon side effects fired on entering (or,
                            # with `at: leave`, on moving off) this step.
                            # See "Daemon side effects" below
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
        escalate:               # optional: ending HERE hands the slot to
          workflow: improv-mid  # another run instead of going quiet. See
          context: ...          # "Escalation" below
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

``recur: {auto: true}`` is the same service loop with the repetition
performed by the daemon: when the pending next-round request is the run's
own (``by: recur``), the daemon starts the round by itself instead of the
driving agent performing the start — the loop keeps going while nobody is
looking. Plain ``recur: true`` keeps the original flow, where the agent
performs each next start per the protocol.

Escalation
----------
``escalate`` on a step is the same lifecycle move aimed at a DIFFERENT
workflow: ending the run through that step files a start request for the
named one (``by: escalate``) instead of letting the slot go quiet. Where
``recur`` says "this run happens again", an escalation says "this run's work
continues under other rules" — a worker round that turns out to need a
mid-worker's stack management, for example.

Three things follow from filing an ordinary start request, and all three are
the point: the pending request spares the session from the daemon's
kill-on-end, the finished run's payload names the start the driver must
perform, and the request carries the beads issue the finishing run named, so
one board record spans both runs.

The engine checks before it hands over: the target workflow is resolved and
its ``filter_roles`` held against the driving session while the run is still
this side of the ending. A target that turns this session away is NOT
escalated to — the run ends the ordinary way, and the refusal is journaled.
A check that could not run at all (no mesh identity to hold a role against)
is recorded as such and the escalation proceeds; "the filter said no" and
"nothing could ask the filter" are different answers and the journal keeps
them apart.

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

Timed wait — `timer:`
----------------------
A step may declare a ``timer:`` — a bounded, daemon-driven wait that MOVES
the run instead of reminding its agent::

    wait:
      timer:
        every: 300      # seconds between fires
        max: 4          # fires per round; once the budget is spent the
                        # run goes to `after`
        then: poll      # each fire moves the run HERE (a real state
                        # transition: the step is delivered, waking the
                        # session even when it is idle)
        after: end      # where the run goes when the budget is spent
      instructions: wait for the timer
      next: end         # the agent's own exit (close the round early)

While the run sits at a timer step it reports ``waiting_timer``: the agent
is told to end its turn, the ReminderClock stays quiet (a timed wait is not
a stall), and the daemon's clock fires every ``every`` seconds. A fire
moves the run to ``then`` (visit counted, delivered like any arrival) and
counts against ``max`` per round — the count survives the ``then`` round
trip, so the budget is the number of polls, not the number of visits — and
the fire past the budget moves the run to ``after`` instead, closing the
inner loop. The agent may close it early with a report and ``next``. This
is the tier inside a ``recur`` loop: one round polls at most ``max`` times,
and ``recur`` (possibly ``{auto: true}``) repeats the round. Fires count as
visits, so size ``max_visits`` past ``max``.

Checklist gate — `checklist:`
------------------------------
A step may declare a ``checklist:`` — a named list of machine-checked
conditions, and the run leaves it only when every one of them is true::

    landed:
      checklist:
        prompt: has this branch actually landed?
        then: wrapup        # where the run goes when every item passes
        poll: 60            # seconds between the daemon's re-measurements
        timeout: 30         # per-item command timeout
        otherwise:          # optional: where it goes when the gate has NOT
          after: 600        #   opened this many seconds after it was
          then: retry       #   presented (journalled `checklist_expired`)
        items:
          - id: merged
            describe: a merge commit on the target lists my tip as a parent
            check: 'python tools/landed_check.py'
          - id: frozen
            describe: the working tree is clean
            check: 'git diff --quiet && git diff --cached --quiet'
      instructions: |
        ...

It exists because the decisions that matter most in a multi-session workflow
— *did the parent merge my branch*, *did the live server pick up the merge* —
were carried by prose. A step stated them in its ``instructions`` and its
``done_when``, and the driving agent read that prose and decided for itself
whether to advance. A person watching could not see which conditions were
true; they could only read the agent's account of them.

A checklist answers the same question with a **list of exit codes**. Each item
is a command: exit 0 is true, any other code is false, and a command that
cannot be run or times out is ``unknown`` — never true. The run reports
``waiting_checklist``, carrying every item with its current state, so the
same list renders in ``claunch cflow status`` and on the dashboard.

**The transition is the daemon's, not the agent's.** While the run sits here
it is waiting, exactly like a ``timer:`` step: the agent ends its turn, the
ReminderClock stays quiet, and
:class:`..daemon.cflow_clock.ChecklistClock` re-measures every ``poll``
seconds. When every item is true the daemon moves the run to ``then``
(:func:`cflow.engine.check_checklist`) and the per-item evidence — id, exit
code, output tail, measurement time — is journalled as ``checklist_passed``.
The agent transcribes no hashes by hand.

Two properties make the gate mean something:

* **There is no agent exit.** ``next:`` is refused on a checklist step;
  ``then`` is the only edge out. An agent cannot file its way past a
  condition that is false, which is the whole point of moving these
  particular gates off prose. The escape is a person's ``claunch cflow
  goto``, and that is journalled as the override it is.
* **A report is still required.** The move waits for all items true AND the
  step's own report to have been filed. Items are what a command can check;
  the report is the rest, and a gate that skipped it would close a round
  with the human-checked half of ``done_when`` missing from the journal. A
  report does not open the gate — a filed report with a red item moves
  nothing.

``otherwise: {after, then}`` bounds the wait. ``after`` seconds from the
moment the gate was first presented in this visit, a list that is still not
all true moves the run to ``then`` instead — the daemon's move again, but
journalled as ``checklist_expired`` (with every item's state as evidence) and
without waiting for a report: it is the gate's "no", not the step's
completion. The step it names is where the retry is decided — a ``select``
whose one option re-enters the gate (a fresh visit, so a ``restart:`` on it
runs again) and whose other gives up. Two things it is not: it is not a
``goto`` (nothing is overridden; the workflow declared the edge), and it is
not a second exit for the agent (``next`` is still refused). Without it the
gate is unbounded, as before.

``poll`` and ``timeout`` carry the same floor and ceiling as ``awaits`` and
for the same reasons: below the floor the clock hammers the condition instead
of sampling it, and above the ceiling a checklist item becomes a way to run a
test suite on a loop. An item is a cheap question about state somebody else
changed.

Run state — `editable:`
-----------------------
A run's workflow is fixed at start; ``editable:`` names the paths that stay
writable while it runs (``steps.<id>.skip``, a checklist item ticked by
``by:`` instead of ``check:``, or a free name such as ``notes``), who may
write each (``user`` through ``claunch cflow set``, ``agent`` through the
``set_state`` MCP tool), and their defaults. A write is a patch on the run,
journalled ``state_set``; a skip is read on ENTRY only, so a write never
reaches back into a visit under way. See :class:`EditableSpec`.

Daemon side effects — `triggers:`
---------------------------------
A step may declare ``triggers:`` — daemon capabilities that exist OUTSIDE
cflow and that a run has no tool of its own to call, fired at a named moment
in the step's life::

    commit:
      instructions: ...
      triggers:
        - do: checks            # ask this session to re-report its Y/N
          at: leave             #   status checks, once it advances past here
        - do: briefing          # recompose this session's LLM briefing
          at: enter             #   (the dashboard's "what is it doing")
      next: queue-next

    work:
      instructions: ...
      triggers: [briefing]      # shorthand: the action name alone is
      next: review              #   `{do: <name>, at: enter}`

Two actions are defined, both daemon-side and both already reachable from the
web dashboard by hand:

* ``checks`` — the daemon types a status-check refresh request into the
  DRIVING session (the same text as ``POST
  /api/sessions/{name}/status-checks/refresh``), and the agent answers it
  with the ``status_checks`` and ``report_status_checks`` MCP tools. It costs
  the driver a few lines of context, so it belongs at the few positions where
  the answers have just changed.
* ``briefing`` — the daemon recomposes that session's LLM briefing
  (:func:`..daemon.briefing.compose` with ``refresh=True``). Nothing is typed
  into the terminal and the driver spends nothing; what it costs is one call
  to the configured endpoint.
* ``enqueue-landing`` — the daemon files the session's landing request (its
  ``in_review`` issues, branch and tip) on its parent's run, when that run's
  workflow declares ``landing_queue:`` (:class:`LandingQueueSpec`). The one
  action whose effect lands in another session's run.

``at`` takes ``enter`` (the run arrived at this step — the default) or
``leave`` (the run moved off it, whichever edge it took). It is spelled
``at`` and not ``on`` because this file is read by PyYAML, whose YAML 1.1
resolver turns a bare ``on:`` key into the boolean ``True``. Both moments
are recorded
by the engine at the moment it performs the move, so a trigger is queued
once per visit and a daemon that was down does not replay the ones it
missed on the next boot.

**A trigger is a side effect, never a gate.** It cannot hold a step, cannot
move a run and cannot fail one: an action with nothing to do (no enabled
status check, no ``llm:`` block, a driving session that has exited) is
journalled with its reason and the run carries on. This is the opposite of
``restart:``, which is followed by a checklist precisely so its outcome can
hold the run — and it is why a trigger may sit on any step, including a
``select``.

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
    decides alone, journaled as unanswered and never as an approval). On a
    select's chooser, ``self:<option>`` is the third spelling: the run takes
    the named option itself, journaled as unanswered — for the decision the
    driver must not be handed back.

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
happens to be connected" is not one. ``scope`` names which relatives of the
asking session in the spawn tree it may match (see "Hierarchy" in
:mod:`.responders` for the relations themselves):

- ``any`` (default) — anything the asking session can reach over the mesh
  that is not itself or something it spawned;
- ``ancestor`` — the chain of command only, nearest first;
- ``sibling`` — the sessions that share its parent (parentless roots of one
  mesh are siblings of each other: the operator made them all);
- ``descendant`` — what it spawned, and theirs.

Descendants are excluded from every scope but their own, and that exclusion
is what makes an *authority* decision mean anything: an agent can spawn
children and wire itself to them, so an unfiltered pool would let a run
manufacture its own approver. ``scope: descendant`` is the explicit opt-out,
for competence decisions a run may staff for itself — a review whose default
is to pass unreviewed anyway (``otherwise: self:pass``), where a reviewer the
worker spawned is strictly more review than none. Never write it on a decision
that grants authority (landing, shipping, spending). Collateral relatives —
cousins, uncles, a sibling's children — are matched by ``any`` alone: nothing
narrower names them, because a session another session spawned for its own
work is that session's, not a pool for its relatives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

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

#: How often the daemon re-measures a ``checklist:`` step's items, and the
#: floor under it. Same trade as ``awaits``: below the floor the clock stops
#: sampling a condition and starts hammering it.
DEFAULT_CHECKLIST_POLL = 60.0
MIN_CHECKLIST_POLL = 15.0

#: How long one checklist item's command may take. Capped for the reason the
#: probe ceiling exists: an item is a cheap question about state somebody else
#: changed, and a ceiling is what stops "wait until the suite is green" from
#: being written as "run the suite every minute".
DEFAULT_CHECKLIST_TIMEOUT = 10.0
MAX_CHECKLIST_TIMEOUT = 30.0

#: Reserved next-target meaning "the workflow ends here".
END = "end"

#: Top-level key naming the workflow this file is a *layer over*: the base is
#: read first and this file's properties are merged onto it, one property at a
#: time. It exists because the two halves of a workflow have different
#: owners — the prose, the graph and the protocol are written once and ship to
#: every repository, while what a step *checks* (``verify``, ``awaits``) names
#: this repository's own tools and cannot travel. Without it the only way to
#: add one command was to copy the whole file into the project layer, and this
#: repository measured what that costs: a 1073-line packaged workflow copied
#: to 1130 lines to add four, kept in step by a 216-line regex grafter
#: (``tools/sync_project_layer.py``) because the copy drifts otherwise.
#:
#: The value is a workflow NAME (searched in the layers *below* the file that
#: names it, so a project ``improv-worker.yaml`` may extend the global one of
#: the same name without extending itself) or a path ending in .yaml/.yml
#: (resolved against the extending file's own directory). Resolution needs to
#: know which layer the file came from, which this module cannot see — so a
#: base is resolved by :mod:`claude_launcher.cflow.state`, and parsing a doc
#: that still carries this key is refused rather than half-honoured.
EXTENDS_KEY = "extends"

#: ``otherwise``: what happens once every candidate group has been tried.
#: Hold the run for a human, through the CLI or the dashboard...
OTHERWISE_HUMAN = "human"
#: ...or let the driving agent carry on alone. Never called an approval.
#: On a select's chooser the spelling ``self:<option>`` narrows it further:
#: rather than handing the choice back to the driver (who may be the one
#: party that must not make it), the run takes the named option itself,
#: journaled as unanswered — the workflow's declared default, taken by
#: nobody.
OTHERWISE_SELF = "self"
OTHERWISE = (OTHERWISE_HUMAN, OTHERWISE_SELF)

#: ``scope``: which part of the reachable mesh a candidate may match.
#: Anything the asking session can reach that it did not spawn...
SCOPE_ANY = "any"
#: ...or its own chain of command only...
SCOPE_ANCESTOR = "ancestor"
#: ...or the sessions sharing its parent (roots of one mesh share the operator)...
SCOPE_SIBLING = "sibling"
#: ...or what it spawned itself — the one scope that admits descendants.
SCOPE_DESCENDANT = "descendant"
SCOPES = (SCOPE_ANY, SCOPE_ANCESTOR, SCOPE_SIBLING, SCOPE_DESCENDANT)

#: ``filter_roles.type``: admit only the listed roles...
FILTER_WHITELIST = "whitelist"
#: ...or admit every role except the listed ones.
FILTER_BLACKLIST = "blacklist"
FILTER_TYPES = (FILTER_WHITELIST, FILTER_BLACKLIST)


#: Who may write a run-state path while the run is going (``editable:``).
#: ``user`` is a person: the ``claunch cflow set`` CLI, which the harness
#: keeps out of the agent's shell the same way it keeps ``approve`` out.
#: ``agent`` is the driving session, through the ``set_state`` MCP tool.
EDITABLE_BY_USER = "user"
EDITABLE_BY_AGENT = "agent"
EDITABLE_BY = (EDITABLE_BY_USER, EDITABLE_BY_AGENT)

#: The value types a run-state path takes.
EDITABLE_BOOL = "bool"
EDITABLE_TEXT = "text"
EDITABLE_TYPES = (EDITABLE_BOOL, EDITABLE_TEXT)

#: A free run-state name (``notes``, ``target``): the same spelling as an
#: input name, so both reach a command as an upper-cased environment name.
_STATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class WorkflowError(Exception):
    """Raised for unreadable or invalid workflow files."""


@dataclass(frozen=True)
class Verify:
    command: str
    timeout: float = DEFAULT_VERIFY_TIMEOUT


@dataclass(frozen=True)
class Restart:
    """A project-local external restart command selected by host platform."""

    windows: Optional[str] = None
    linux: Optional[str] = None
    timeout: float = 120.0

    def command_for(self, system: str) -> Optional[str]:
        system = (system or "").lower()
        if system.startswith("win"):
            return self.windows
        if system in ("linux", "wsl"):
            return self.linux
        return None


#: The daemon capabilities a ``triggers:`` entry may name. Each one exists
#: outside cflow, is already reachable by hand from the web dashboard, and
#: has no tool a run could call for itself.
TRIGGER_CHECKS = "checks"
TRIGGER_BRIEFING = "briefing"
#: Put this session's landing request on its parent's landing queue (a
#: parent whose workflow declares ``landing_queue:``). Fired by the worker's
#: step, written into the parent's run: the one trigger whose effect lands in
#: a run other than the one that asked.
TRIGGER_ENQUEUE_LANDING = "enqueue-landing"
TRIGGER_ACTIONS = (TRIGGER_CHECKS, TRIGGER_BRIEFING, TRIGGER_ENQUEUE_LANDING)

#: When in a step's life a trigger fires. Both moments are recorded by the
#: engine as it performs the move, so neither depends on a clock catching a
#: position before it changes again.
TRIGGER_AT_ENTER = "enter"
TRIGGER_AT_LEAVE = "leave"
TRIGGER_MOMENTS = (TRIGGER_AT_ENTER, TRIGGER_AT_LEAVE)


#: The states an entry on a landing queue passes through. ``requested`` is
#: where an entry is born (and where a deferred one comes back to at a
#: round's end); ``waiting`` and ``deferred`` are the driving agent's word;
#: ``landed`` is the daemon's alone, measured in git; ``rejected`` is the
#: agent's word that the request is over.
LANDING_REQUESTED = "requested"
LANDING_WAITING = "waiting"
LANDING_DEFERRED = "deferred"
LANDING_LANDED = "landed"
LANDING_REJECTED = "rejected"
LANDING_STATES = (
    LANDING_REQUESTED,
    LANDING_WAITING,
    LANDING_DEFERRED,
    LANDING_LANDED,
    LANDING_REJECTED,
)
#: Settled: dropped from the queue when the round ends.
LANDING_SETTLED = (LANDING_LANDED, LANDING_REJECTED)


#: The one placeholder ``landing_queue.target`` knows.
SESSION_PLACEHOLDER = "{session}"


@dataclass(frozen=True)
class LandingQueueSpec:
    """A workflow's ``landing_queue:`` declaration.

    The queue itself is run state (``state["landing_queue"]``): entries
    arrive from children's ``enqueue-landing`` triggers, the driving agent
    moves them with the ``landing_queue`` MCP tool, the daemon marks one
    ``landed`` when its tip is an ancestor of :attr:`target`. When a round
    ends the settled entries (landed, rejected) are dropped and the rest are
    carried into the next round, a deferred one back as requested.
    """

    #: The branch a landed tip must be an ancestor of, read in the run's
    #: own checkout. ``{session}`` stands for the driving session's name,
    #: for a branch that exists once per session (the ``stack`` sub run's
    #: ``{session}-stack``).
    target: str = "master"

    def branch(self, session: str) -> str:
        """:attr:`target` with ``{session}`` filled in."""
        return self.target.replace(SESSION_PLACEHOLDER, session)


@dataclass(frozen=True)
class Trigger:
    """One daemon side effect a step asks for, and when.

    Deliberately two fields. A trigger names a capability the daemon already
    has and a moment the engine already passes through; everything about how
    the capability behaves (which checks are enabled, which endpoint composes
    a briefing) is configured where that capability lives, not here. An
    author who has to restate it in a workflow file is an author who can get
    it wrong in one workflow and right in the next.

    It is a side effect and never a gate: see "Daemon side effects" in this
    module's docstring for why a failed one does not stop the run.
    """

    do: str
    at: str = TRIGGER_AT_ENTER


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
    #: ``awaits: {sub: <name>}`` — the step waits for that SUB run of the
    #: same scope to finish; ``{sub: all}`` for every sub run. It is the
    #: ordinary probe with its command fixed: ``claunch cflow sub-done``,
    #: which answers with an exit code the daemon already knows how to read.
    #: Spelled as a kind rather than written out as a command so an author
    #: cannot get the command wrong and so the payload can say what is
    #: awaited in the run's own terms.
    sub: Optional[str] = None
    #: ``awaits: {sub: <name>, at: <milestone>}`` — wait for that sub run to
    #: PUBLISH ``milestone`` (a step's ``publishes:``) again, rather than to
    #: finish. "Again" is measured against what this step consumed: the
    #: milestone's publication count is recorded when the step is left, and
    #: the wait is over when the count has moved past that record. So a
    #: publication made while the step was being approached, or while it was
    #: left and not yet re-entered, still counts — a handshake cannot be
    #: missed by arriving late.
    at: Optional[str] = None
    #: ``awaits: {main: <milestone>}`` — a SUB run's step waits for its main
    #: run to publish ``milestone`` again, under the same consumed rule. The
    #: one direction a sub run may wait: it reads the main run, it never
    #: holds it (a main run's gates do not read this).
    main: Optional[str] = None
    #: (With ``at`` or ``main``, ``probe`` may still be given: it replaces
    #: the ``claunch cflow published`` command and nothing else.)

    @property
    def milestone(self) -> Optional[str]:
        """The milestone this await is for, whichever direction."""
        return self.at or self.main

    def command(self, step: "Step") -> Optional[str]:
        """The command to actually run, resolving the reserved spellings.

        A milestone await may name its own ``probe``: the same question
        asked through a command the repository chooses (a checkout-pinned
        twin of ``claunch cflow published``). The milestone still decides
        what leaving the step consumes; only the command is replaced.
        """
        if self.milestone is not None and self.probe is not None:
            return self.probe
        if self.main is not None:
            return f"claunch cflow published {MAIN_RUN_NAME} {self.main} --step {step.id}"
        if self.at is not None:
            return f"claunch cflow published {self.sub} {self.at} --step {step.id}"
        if self.sub is not None:
            if self.sub == SUB_ALL:
                return "claunch cflow sub-done --all"
            return f"claunch cflow sub-done {self.sub}"
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
    that pool to one relation — the session's own ancestors when only the
    chain of command will do, its siblings — or, for ``descendant`` alone,
    turns it to the sessions below it instead.

    ``connect`` says what to do when the role is held by somebody this run is
    not wired to. Off (the default), the group is skipped with that as its
    reason — the wiring is a human's to add. On, the edge is made for the run
    and the candidate is matched again. It is declared per candidate because
    the answer differs per candidate: a peer review wants the reviewer reached
    however the tree happens to be wired, while a decision reserved for the
    chain of command should not manufacture a path to somebody it cannot
    already reach.
    """

    role: str
    scope: str = SCOPE_ANY
    connect: bool = False

    def describe(self) -> str:
        """One line for a payload, a message or an error — never parsed."""
        notes = [n for n in (
            "" if self.scope == SCOPE_ANY else self.scope,
            "connect" if self.connect else "",
        ) if n]
        return f"{self.role} ({', '.join(notes)})" if notes else self.role


@dataclass(frozen=True)
class Delegate:
    """Who may answer a decision, and what to do when nobody does.

    ``candidates`` is read a group at a time — index 0 first — and everybody a
    group matches is asked together, so "a reviewer, else a leader" and "any of
    the three reviewers I can reach" are the same declaration read two ways.
    ``timeout`` is per group, not for the whole list. An empty list is legal
    and means no agent is asked: ``otherwise`` decides straight away.

    ``default_option`` is the parsed half of ``otherwise: self:<option>`` —
    the branch a select takes itself when every candidate group has run out,
    where bare ``self`` would hand the choice back to the driver. It is only
    meaningful on a select's chooser; an approval has no options to take.
    """

    candidates: List[Candidate] = field(default_factory=list)
    otherwise: str = OTHERWISE_HUMAN
    timeout: Optional[float] = None
    default_option: Optional[str] = None

    def describe(self) -> str:
        """The preference list as one line, ending in the fallback."""
        fallback = self.otherwise + (
            f":{self.default_option}" if self.default_option else ""
        )
        return " -> ".join([c.describe() for c in self.candidates] + [fallback])


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

    def allows_any(self, roles) -> bool:
        """Whether a session holding ALL of ``roles`` may drive.

        A whitelist admits the session when any role it holds is listed; a
        blacklist turns it away when any role it holds is listed. Both read
        "holds" the same way the mesh does — the primary and every subrole —
        so a leader that took ``worker`` as a subrole may drive a
        workers-only workflow, and a worker that took ``leader`` may not
        drive one that bars leaders. An empty list is judged as "" (the
        role of a session in no mesh).
        """
        held = [str(r or "").strip().lower() for r in (roles or ())]
        held = [r for r in held if r] or [""]
        if self.type == FILTER_WHITELIST:
            return any(r in self.roles for r in held)
        return not any(r in self.roles for r in held)

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
class Timer:
    """A step's timed wait: the daemon moves the run on a schedule.

    ``every`` — seconds between fires; ``max`` — fires per round; ``then``
    — where each fire moves the run (the step is delivered, waking the
    session); ``after`` — where the run goes when the budget is spent.
    ``after`` is an ordinary graph edge (it participates in reachability),
    while ``then`` is entered only by a fire, so graph validation checks it
    separately.
    """

    every: float
    max: int
    then: str
    after: str


@dataclass(frozen=True)
class EditableSpec:
    """One run-state path the workflow lets somebody change while it runs.

    A run's workflow is fixed when it starts: the file is composed (with any
    ``extends`` overlay), snapshotted, and every later read comes from the
    snapshot. ``editable:`` is the one door left open after that, and only
    for the paths it names. A write is a *patch* on the run -- the path, the
    value, who wrote it and when -- journalled as ``state_set``; the current
    value of a path is its declared default with the patches laid over it in
    order. Three path forms exist:

    * ``steps.<id>.skip`` (bool) -- the step's ``skip`` property. When true
      at the moment the run ENTERS the step, the run passes straight through
      to its ``next`` (journalled ``step_skipped``). Read at entry only, so a
      write never reaches back: a visit already under way, and a step
      already passed, stay what they were.
    * ``steps.<id>.checklist.<item>`` (bool) -- a checklist item a person (or
      the agent) ticks instead of a command deciding it. Declared by the
      item itself (``by:`` in place of ``check:``), never here.
    * ``<name>`` (bool or text) -- a free value such as ``notes``: shown in
      the payload, and handed to ``verify``/``check`` commands as
      ``CFLOW_VAR_<NAME>``.

    ``text`` is writable by a person only: the agent's record of what it
    did belongs on the issue board, and a note field it could also write
    would become a second copy of that.
    """

    path: str
    type: str
    by: Tuple[str, ...]
    default: object = None
    describe: Optional[str] = None
    #: Declared by a checklist item's ``by:`` rather than in ``editable:``.
    implicit: bool = False

    @property
    def env_name(self) -> str:
        return "CFLOW_VAR_" + re.sub(r"[^A-Za-z0-9]", "_", self.path).upper()


def skip_path(step_id: str) -> str:
    return f"steps.{step_id}.skip"


def checklist_path(step_id: str, item_id: str) -> str:
    return f"steps.{step_id}.checklist.{item_id}"


@dataclass(frozen=True)
class ChecklistItem:
    """One machine-checked condition of a :class:`Checklist`.

    ``check`` is a command and its exit code is the whole answer: 0 is true,
    any other code is false. Nothing here interprets a particular non-zero
    code — an item is a yes/no question, and an author who needs to tell 1
    from 3 writes two items.

    ``describe`` is what a person reads next to the checkbox. It is required
    rather than optional because the command is not a description: a reader
    looking at a stuck gate needs to know what is not true, not which script
    was run.
    """

    id: str
    describe: str
    #: The command whose exit code decides the item. ``None`` exactly when
    #: :attr:`by` is set: then the item is ticked by a person or the agent
    #: (run-state path ``steps.<step>.checklist.<id>``) instead of measured.
    check: Optional[str] = None
    by: Tuple[str, ...] = ()

    @property
    def manual(self) -> bool:
        return bool(self.by)


@dataclass(frozen=True)
class ChecklistOtherwise:
    """Where a checklist sends the run when it has NOT opened in time.

    ``after`` seconds from the visit's first presentation, a gate that is
    still shut moves the run to ``then`` — a daemon move like the pass, but
    journalled as ``checklist_expired`` and needing no report: it is the
    gate's "no", not the step's completion. The step it names is usually one
    that decides whether to come back and ask again (a fresh visit, so a
    ``restart:`` declared on the gate runs again) or to give up.
    """

    after: float
    then: str


@dataclass(frozen=True)
class Checklist:
    """A step's gate as a list of conditions, moved by the daemon.

    Every item must measure true before the run leaves for :attr:`then`,
    which is the only edge out — a checklist step takes no ``next``. See the
    module docstring's "Checklist gate" section for why the agent has no exit
    of its own and why a report is still required.

    :attr:`otherwise` is the one other daemon edge: a bounded wait. Without
    it a gate that never turns green holds the run until a person's ``goto``.
    """

    items: Tuple[ChecklistItem, ...]
    #: Where the run goes once every item is true. Not optional: a checklist
    #: with nowhere to go is a step nothing can leave.
    then: str
    #: One line naming what the whole list is deciding, for the payload and
    #: the dashboard heading.
    prompt: Optional[str] = None
    poll: float = DEFAULT_CHECKLIST_POLL
    timeout: float = DEFAULT_CHECKLIST_TIMEOUT
    #: Optional expiry edge; ``None`` is today's unbounded wait.
    otherwise: Optional[ChecklistOtherwise] = None

    def item(self, item_id: str) -> Optional[ChecklistItem]:
        for entry in self.items:
            if entry.id == item_id:
                return entry
        return None


@dataclass(frozen=True)
class Escalate:
    """A step's declaration that ending HERE hands the slot to another run.

    A run that finishes normally goes quiet and its session's slot is
    returned (the daemon's kill-on-end). ``escalate`` is how a workflow says
    that one particular ending is not the end of the work: leaving through
    this step files a start request for :attr:`workflow`, exactly the record
    a human's ``claunch cflow request`` would leave, and the pending request
    both keeps the driving session alive and tells it what to start next.

    The mechanism is not new — the request file, the kill-on-end condition
    that spares a run with a pending start, and the payload note that names
    it were all already there. What was missing was a way for a workflow to
    declare it, so the hand-off had to be typed by a human between two runs.

    ``context`` is what the next run is told it is for. The request also
    carries the finishing run's own context and the beads issue named in it,
    so the two runs stay findable from one board record.
    """

    #: The workflow to start next: a name, or a path, resolved the same way
    #: ``claunch cflow start`` resolves one — the layers are searched when the
    #: request is fulfilled, not here.
    workflow: str
    #: What the escalated run is for, in the author's words. Optional: with
    #: none, the request carries the finishing run's context alone.
    context: Optional[str] = None


@dataclass(frozen=True)
class Select:
    prompt: str
    chooser: str  # "agent" | "user" | "delegate"
    options: Dict[str, Option] = field(default_factory=dict)
    #: Set exactly when ``chooser`` is "delegate".
    delegate: Optional[Delegate] = None
    #: Require the driving agent to provide a reason with its selection.
    require_reason: bool = False


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
    #: A timed wait: the daemon moves the run to :attr:`Timer.then` every
    #: :attr:`Timer.every` seconds, at most :attr:`Timer.max` times per round,
    #: then to :attr:`Timer.after` once the budget is spent. While the run
    #: sits here it reports ``waiting_timer`` and the ReminderClock stays
    #: quiet — a timed wait is not a stall. See :class:`Timer`.
    timer: Optional[Timer] = None
    #: A machine-checked gate: the run leaves for :attr:`Checklist.then` only
    #: once every item's command exits 0 and the step's report has been
    #: filed, and the DAEMON performs that move. While the run sits here it
    #: reports ``waiting_checklist``. See :class:`Checklist`.
    checklist: Optional[Checklist] = None
    #: A project-local external service restart, executed by the daemon from
    #: the run's CWD after its entry gate has opened.
    restart: Optional[Restart] = None
    #: Daemon side effects this step asks for, each with the moment it fires
    #: (``enter`` or ``leave``). Queued by the engine as it performs the
    #: move and performed by the daemon afterwards; a failed one is
    #: journalled and never holds the run. See :class:`Trigger`.
    triggers: Tuple["Trigger", ...] = ()
    select: Optional[Select] = None
    #: Ending the run HERE hands the slot to another workflow instead of
    #: going quiet: the engine files a start request for it. Only meaningful
    #: on a step that can terminate, which the parser enforces. See
    #: :class:`Escalate`.
    escalate: Optional[Escalate] = None
    #: Sub runs the engine starts when this step is ENTERED (every visit that
    #: finds the slot empty or finished): side tracks the same session drives
    #: beside this run, each from a ``kind: subflow`` definition. Coupling
    #: back is ``awaits: {sub: <name>}`` or a checklist item running
    #: ``claunch cflow sub-done <name>``. See :class:`SubflowRef`.
    subflows: Tuple["SubflowRef", ...] = ()
    #: Milestones this step PUBLISHES when it is left by the run's own
    #: progress (a person's ``goto`` away publishes nothing): each name's
    #: publication count goes up by one. The other run of the same scope
    #: reads it with ``awaits: {sub: <name>, at: <milestone>}`` (main reading
    #: a sub run) or ``awaits: {main: <milestone>}`` (a sub run reading its
    #: main run), and a checklist can ask the same with
    #: ``claunch cflow published``.
    publishes: Tuple[str, ...] = ()
    next: Optional[str] = None  # None = termination (non-select steps)
    #: Pass straight through this step to :attr:`next` on entry. Static here
    #: (an ``extends`` overlay can set it for one run's composition); a run
    #: can flip it while going only when ``editable:`` declares
    #: ``steps.<id>.skip``. Only a step with a single plain exit may carry
    #: it -- see :attr:`skippable`.
    skip: bool = False

    @property
    def skippable(self) -> bool:
        """A step whose one exit is ``next``: no choice, no daemon edge.

        A select has no single edge to pass through to, and a checklist or a
        timer IS a daemon-held condition -- skipping one would be the way
        past a gate those constructs exist to refuse.
        """
        return self.select is None and self.checklist is None and self.timer is None

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
        elif self.checklist is not None:
            # Unlike `timer.then`, this IS the step's ordinary exit — the only
            # one it has, since a checklist step takes no `next` — so it
            # counts for reachability and for the "can this workflow finish"
            # check exactly like a `next` would. Appending `self.next` as
            # well would be worse than redundant: it is always None here, and
            # None reads as a termination, which would let every checklist
            # step silently satisfy the loop check.
            out.append(
                None if self.checklist.then == END else self.checklist.then
            )
            if self.checklist.otherwise is not None:
                # The expiry is an exit too: the run really goes there, on
                # the daemon's clock, so reachability and the loop check see
                # it. (`timer.then` is kept off this list because a fire is a
                # scheduled re-entry; an expiry is a departure.)
                target = self.checklist.otherwise.then
                out.append(None if target == END else target)
        else:
            out.append(self.next)
        if self.timer is not None:
            # `after` is where the run goes when the timer's budget is spent —
            # an exit edge, so it participates in reachability exactly like
            # `next`. `then` is deliberately NOT an edge here: it is entered
            # only by a fire, and counting it would make every timer loop read
            # as a warned agent cycle.
            out.append(None if self.timer.after == END else self.timer.after)
        return out


#: The one value ``kind:`` takes today.
KIND_SUBFLOW = "subflow"
KINDS = (KIND_SUBFLOW,)

#: How many sub runs one scope may hold at once, and so how many a step may
#: declare. Three is a ceiling on attention, not on disk: every sub run is a
#: position the same agent has to keep in its head beside the main run's.
MAX_SUBFLOWS = 3

#: The reserved sub run name (the main run) and the reserved ``awaits.sub``
#: word for "every sub run of this scope".
MAIN_RUN_NAME = "main"
SUB_ALL = "all"

#: A sub run name becomes a directory (``runs/<scope>/sub/<name>/``), so it
#: is spelled like a scope. Mirrors ``state._SCOPE_RE`` without importing the
#: state module into the model.
_SUB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_sub_name(name: str) -> bool:
    return bool(_SUB_NAME_RE.match(name)) and name not in (MAIN_RUN_NAME, SUB_ALL, "..", ".")


@dataclass(frozen=True)
class SubflowRef:
    """One sub run a step declares: started by the engine when the step is
    entered, under ``name``, from the ``kind: subflow`` definition
    ``workflow``, with ``inputs`` as its input values.

    ``workflow`` is a NAME resolved through the same layers as any other
    workflow (project, then global, then package), so the same definition
    can be called from several workflows and started twice under two names.
    ``inputs`` values are strings handed through as they are written — a
    value like ``auto`` is not interpreted here; it reaches the sub run's
    commands as ``CFLOW_IN_<NAME>`` and means whatever the tool reading it
    says (``changed_tests.py --base auto`` is the precedent).
    """

    name: str
    workflow: str
    inputs: Dict[str, str] = field(default_factory=dict)


def _parse_subflows(raw, step_id: str) -> Tuple[SubflowRef, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise WorkflowError(
            f"step {step_id!r}: 'subflows' must be a list of "
            f"{{name, workflow, with}} mappings"
        )
    out: List[SubflowRef] = []
    seen: Set[str] = set()
    for i, item in enumerate(raw):
        where = f"step {step_id!r}: subflows[{i}]"
        if not isinstance(item, dict):
            raise WorkflowError(f"{where} must be a mapping {{name, workflow, with}}")
        extra = sorted(set(item) - {"name", "workflow", "with"})
        if extra:
            raise WorkflowError(
                f"{where} has unknown key(s): {', '.join(extra)} "
                f"(allowed: name, workflow, with)"
            )
        name = str(item.get("name") or "").strip()
        if not valid_sub_name(name):
            raise WorkflowError(
                f"{where}: 'name' must be a sub run name (letters, digits, "
                f"'.', '_' or '-'; not {MAIN_RUN_NAME!r} or {SUB_ALL!r}), got {name!r}"
            )
        if name in seen:
            raise WorkflowError(f"{where}: sub run name {name!r} is declared twice")
        seen.add(name)
        workflow = str(item.get("workflow") or "").strip()
        if not workflow:
            raise WorkflowError(f"{where}: 'workflow' names the 'kind: {KIND_SUBFLOW}' definition to start")
        given = item.get("with")
        inputs: Dict[str, str] = {}
        if given is not None:
            if not isinstance(given, dict):
                raise WorkflowError(f"{where}: 'with' must be a mapping of input name -> value")
            for key, value in given.items():
                key = str(key or "").strip()
                if not _INPUT_NAME_RE.match(key):
                    raise WorkflowError(f"{where}: 'with' key {key!r} is not an input name")
                if value is None or isinstance(value, (dict, list)):
                    raise WorkflowError(f"{where}: 'with.{key}' must be a scalar")
                inputs[key] = str(value)
        out.append(SubflowRef(name=name, workflow=workflow, inputs=inputs))
    if len(out) > MAX_SUBFLOWS:
        raise WorkflowError(
            f"step {step_id!r}: declares {len(out)} sub runs; the limit is "
            f"{MAX_SUBFLOWS} per scope"
        )
    return tuple(out)

#: An input name has to survive as an environment variable suffix.
_INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class InputSpec:
    """One named value a sub definition asks its starter for."""

    name: str
    required: bool = False
    default: Optional[str] = None

    @property
    def env_name(self) -> str:
        return "CFLOW_IN_" + self.name.upper()


def resolve_inputs(specs: Dict[str, InputSpec], given: Optional[dict]) -> Dict[str, str]:
    """The inputs a run starts with: ``given`` checked against ``specs``.

    Unknown names are refused rather than dropped (a misspelt input would
    otherwise start a run that silently reads its default), a required one
    missing is refused, and every value is a string — that is what an
    environment variable can carry, and what a command can read back.
    """
    given = dict(given or {})
    unknown = sorted(set(given) - set(specs))
    if unknown:
        raise WorkflowError(
            f"unknown input(s): {', '.join(unknown)} (this definition takes: "
            f"{', '.join(sorted(specs)) or 'none'})"
        )
    out: Dict[str, str] = {}
    for name, spec in specs.items():
        if name in given and given[name] is not None:
            out[name] = str(given[name])
        elif spec.default is not None:
            out[name] = spec.default
        elif spec.required:
            raise WorkflowError(f"required input {name!r} was not given")
    return out


def _parse_inputs(raw) -> Dict[str, InputSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WorkflowError(
            "'inputs' must be a mapping of name -> {required: bool, default: str}"
        )
    out: Dict[str, InputSpec] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not _INPUT_NAME_RE.match(name):
            raise WorkflowError(
                f"input name {name!r} must be lower-case letters, digits or '_' "
                f"and start with a letter (it becomes CFLOW_IN_<NAME>)"
            )
        if value is None:
            out[name] = InputSpec(name=name)
            continue
        if not isinstance(value, dict):
            raise WorkflowError(
                f"input {name!r} must be a mapping like {{required: true}} or "
                f"{{default: <text>}}, or null"
            )
        extra = sorted(set(value) - {"required", "default"})
        if extra:
            raise WorkflowError(
                f"input {name!r} has unknown key(s): {', '.join(extra)} "
                f"(allowed: required, default)"
            )
        required = value.get("required", False)
        if not isinstance(required, bool):
            raise WorkflowError(f"input {name!r}: 'required' must be true or false")
        default = value.get("default")
        if default is not None and not isinstance(default, (str, int, float, bool)):
            raise WorkflowError(f"input {name!r}: 'default' must be a scalar")
        if required and default is not None:
            raise WorkflowError(
                f"input {name!r} is both required and defaulted — a default "
                f"means the value is never missing; keep one"
            )
        out[name] = InputSpec(
            name=name,
            required=required,
            default=None if default is None else str(default),
        )
    return out


def _parse_kind(doc: dict) -> Optional[str]:
    raw = doc.get("kind")
    if raw is None:
        return None
    kind = str(raw).strip().lower()
    if kind not in KINDS:
        raise WorkflowError(
            f"'kind' must be one of {', '.join(KINDS)} (or absent for an "
            f"ordinary workflow), got {raw!r}"
        )
    return kind


def _validate_subflow(workflow: "Workflow") -> None:
    """What a ``kind: subflow`` definition may not carry.

    Each refused key is a statement about the driving SESSION's lifecycle —
    whether it runs again, who may drive it, what its children start with,
    where the slot goes when it ends. A sub run shares its session with a
    main run that already answers those, so a second answer is a conflict
    with no reading, and the file is refused at parse rather than at the
    step where the two would disagree.
    """
    where = f"'kind: {KIND_SUBFLOW}' definition {workflow.name!r}"
    if workflow.recur:
        raise WorkflowError(f"{where} may not declare 'recur' (a sub run has no rounds)")
    if workflow.filter_roles is not None:
        raise WorkflowError(
            f"{where} may not declare 'filter_roles' (the main run decides who drives)"
        )
    if workflow.default_child_cflow:
        raise WorkflowError(
            f"{where} may not declare '{CHILD_CFLOW_KEY}' (children are paired "
            f"by the main run)"
        )
    if workflow.default_role:
        raise WorkflowError(f"{where} may not declare 'default_role'")
    # `landing_queue` is allowed: a sub run that manages the session's
    # children (a stack) is where their landing requests belong. Only one run
    # of a scope may hold the queue — refused at start, when both definitions
    # are known (engine.start_sub).
    escalating = sorted(s.id for s in workflow.steps.values() if s.escalate is not None)
    if escalating:
        raise WorkflowError(
            f"{where} may not 'escalate' (step(s) {', '.join(escalating)}): a "
            f"sub run's end frees its own slot only"
        )
    nesting = sorted(s.id for s in workflow.steps.values() if s.subflows)
    if nesting:
        raise WorkflowError(
            f"{where} may not declare 'subflows' (step(s) {', '.join(nesting)}): "
            f"a sub run has no sub runs of its own — one level, by design"
        )
    awaiting = sorted(
        s.id for s in workflow.steps.values() if s.awaits is not None and s.awaits.sub
    )
    if awaiting:
        raise WorkflowError(
            f"{where} may not 'awaits: {{sub}}' (step(s) {', '.join(awaiting)}): "
            f"only the main run waits on sub runs"
        )


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
    #: Daemon-driven recurrence: when a recurring run's own next-round
    #: request is pending, the daemon performs the start (see the engine's
    #: ``auto_start_next_round``) instead of the driving agent. Only ever set
    #: together with ``recur``; plain ``recur: true`` leaves the start to the
    #: agent exactly as before.
    recur_auto: bool = False
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
    #: ``kind: subflow`` marks a definition written to run as a SUB run — a
    #: second state machine the same session drives beside its main run
    #: (``runs/<scope>/sub/<name>/``). Such a definition is shared by name
    #: like any other workflow file, and it is what a main run's step may
    #: call. The mark is a contract, not a directory: a sub definition may
    #: not carry anything that is a fact about a *session's* lifecycle
    #: (``recur``, ``filter_roles``, ``default_child_cflow``, ``escalate``),
    #: because the session already has a main run that owns those. ``None``
    #: is an ordinary workflow; a sub run refuses to start from one.
    kind: Optional[str] = None
    #: Values a sub definition takes from whoever starts it, by name (see
    #: :class:`InputSpec`). Given as ``inputs:`` at the top of the file and
    #: supplied at start; a required one missing refuses the start. Delivered
    #: to the run's ``verify`` / ``check`` commands as ``CFLOW_IN_<NAME>``
    #: environment variables and to the agent in the payload's ``inputs`` —
    #: never substituted into instructions, whose text ids must stay stable.
    inputs: Dict[str, "InputSpec"] = field(default_factory=dict)
    #: Run-state paths writable while a run goes, by path (see
    #: :class:`EditableSpec`). Includes the implicit ones a manual checklist
    #: item declares.
    editable: Dict[str, "EditableSpec"] = field(default_factory=dict)
    #: ``landing_queue:`` — this workflow keeps its children's landing
    #: requests as run state (see :class:`LandingQueueSpec`). ``None`` when
    #: undeclared, and then an ``enqueue-landing`` aimed at it is skipped.
    landing_queue: Optional["LandingQueueSpec"] = None
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


def read_doc(text: str, *, where: str = "workflow") -> dict:
    """The YAML mapping behind a workflow file, unvalidated.

    Split out of :func:`parse` because a file that ``extends`` another is read
    long before it can be parsed: the merge happens on docs, and only the
    merged doc is a workflow. ``where`` names the file in the error, which is
    the whole difference between "invalid workflow YAML" and knowing which of
    two layers is broken.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"invalid workflow YAML in {where}: {exc}") from exc
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise WorkflowError(f"workflow file must be a YAML mapping: {where}")
    return doc


def extends_ref(doc: dict) -> Optional[str]:
    """What this doc declares as its base, or ``None``.

    Refuses an empty or non-scalar value here rather than at merge time: a
    file that says ``extends:`` and nothing else is a layer over nothing, and
    the author meant to name something.
    """
    raw = doc.get(EXTENDS_KEY)
    if raw is None:
        return None
    if not isinstance(raw, (str, int, float)):
        raise WorkflowError(
            f"{EXTENDS_KEY!r} must be a workflow name or a .yaml path, "
            f"not {type(raw).__name__}"
        )
    ref = str(raw).strip()
    if not ref:
        raise WorkflowError(
            f"{EXTENDS_KEY!r} is empty — name the workflow this file layers "
            f"over, or drop the key"
        )
    return ref


def merge_docs(base: dict, overlay: dict) -> dict:
    """``overlay`` laid over ``base``, one property at a time.

    Three rules, and they are the whole contract:

    * two mappings merge recursively — that is what makes ``steps: {review:
      {verify: ...}}`` reach one field of one step and leave the other
      thousand lines alone;
    * anything else replaces. A list replaces wholesale on purpose: merging
      two lists has no reading a writer can predict (append? by index? by
      key?), and a half-merged ``filter_roles.roles`` is worse than either;
    * an explicit ``null`` DELETES the key. It is how a layer says "this step
      has no verify here", which omitting cannot say — omitting is how you
      inherit.

    The base is never mutated: layers are read once and merged into fresh
    dicts, so a base shared by two overlays cannot be polluted by the first.
    """
    out = dict(base)
    for key, value in overlay.items():
        if value is None:
            out.pop(key, None)
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_docs(out[key], value)
            continue
        out[key] = value
    return out


def compose_docs(docs: Sequence[dict]) -> dict:
    """Merge a chain of docs given NEAREST FIRST (the overlay before its base).

    The chain is spelled in the direction it was walked — this file, then what
    it extends, then what that extends — so composing folds it from the far
    end back. The ``extends`` key itself does not survive: it named an edge of
    the chain, and the composed doc has no chain left.
    """
    merged: dict = {}
    for doc in reversed(list(docs)):
        merged = merge_docs(merged, doc)
    merged.pop(EXTENDS_KEY, None)
    return merged


def parse(text: str, *, default_name: str = "workflow") -> Workflow:
    return parse_doc(read_doc(text, where=default_name), default_name=default_name)


def parse_doc(doc: dict, *, default_name: str = "workflow") -> Workflow:
    """Validate one already-composed mapping into a :class:`Workflow`.

    A doc still carrying ``extends`` is refused instead of parsed: honouring
    the key needs the layers, which this module cannot see, and ignoring it
    would hand back a workflow missing everything the base was carrying —
    silently, and only at the step where the missing half was needed.
    """
    if doc.get(EXTENDS_KEY) is not None:
        raise WorkflowError(
            f"this workflow {EXTENDS_KEY} {doc[EXTENDS_KEY]!r} and has not "
            f"been composed with it — load it through the layer-aware loader "
            f"(claude_launcher.cflow.state.load_workflow), which resolves the "
            f"base and merges this file over it"
        )
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
    recur_raw = doc.get("recur", False)
    recur_auto = False
    if isinstance(recur_raw, bool):
        recur = recur_raw
    elif isinstance(recur_raw, dict):
        unknown = sorted(set(recur_raw) - {"auto"})
        if unknown:
            raise WorkflowError(
                f"'recur' has unknown key(s): {', '.join(unknown)} "
                f"(allowed: auto)"
            )
        auto = recur_raw.get("auto", False)
        if not isinstance(auto, bool):
            raise WorkflowError("'recur.auto' must be true or false")
        recur, recur_auto = True, auto
    else:
        raise WorkflowError(
            "'recur' must be true or false, or a mapping {auto: true} — "
            "'auto' hands the next round's start to the daemon"
        )
    filter_roles = _parse_role_filter(doc.get("filter_roles"))
    default_role: Optional[str] = None
    if doc.get("default_role") is not None:
        default_role = str(doc.get("default_role")).strip().lower()
        if not default_role:
            raise WorkflowError("'default_role' must be a non-empty role name")
    default_child_cflow = _parse_child_cflow(doc)
    kind = _parse_kind(doc)
    inputs = _parse_inputs(doc.get("inputs"))
    if inputs and kind != KIND_SUBFLOW:
        raise WorkflowError(
            f"'inputs' is only taken by a 'kind: {KIND_SUBFLOW}' definition — "
            f"an ordinary workflow starts with 'context', not inputs"
        )
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

    editable = _parse_editable(doc.get("editable"), steps, start)
    landing_queue = _parse_landing_queue(doc.get("landing_queue"))
    workflow = Workflow(
        name=str(doc.get("name") or default_name),
        description=str(doc.get("description") or ""),
        start=start,
        steps=steps,
        max_visits=max_visits,
        recur=recur,
        recur_auto=recur_auto,
        filter_roles=filter_roles,
        default_role=default_role,
        default_child_cflow=default_child_cflow,
        priority=priority,
        kind=kind,
        inputs=inputs,
        editable=editable,
        landing_queue=landing_queue,
        warnings=[],
    )
    _validate_graph(workflow)
    if kind == KIND_SUBFLOW:
        _validate_subflow(workflow)
    else:
        reading_main = sorted(
            s.id for s in workflow.steps.values() if s.awaits is not None and s.awaits.main
        )
        if reading_main:
            raise WorkflowError(
                f"step(s) {', '.join(reading_main)}: 'awaits: {{main}}' reads the "
                f"main run from a sub run — only a 'kind: {KIND_SUBFLOW}' "
                f"definition has a main run to read"
            )
    return Workflow(
        name=workflow.name,
        description=workflow.description,
        start=workflow.start,
        steps=workflow.steps,
        max_visits=workflow.max_visits,
        recur=workflow.recur,
        recur_auto=workflow.recur_auto,
        filter_roles=workflow.filter_roles,
        default_role=workflow.default_role,
        default_child_cflow=workflow.default_child_cflow,
        priority=workflow.priority,
        kind=workflow.kind,
        inputs=workflow.inputs,
        editable=workflow.editable,
        landing_queue=workflow.landing_queue,
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
    # A timer step is exempt like a select: its leave is timed (or the
    # agent's own reported exit), not certified against a criterion.
    silent = [
        s.id
        for s in workflow.steps.values()
        if not s.is_select
        and s.timer is None
        # A checklist step states its completion in the most checkable form
        # this schema has — a list of commands — so it is exempt like a
        # select, whatever else it does or does not declare.
        and s.checklist is None
        and not s.verify
        and not s.done_when
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
    """One file, parsed on its own.

    Kept for callers that hold a path and no layers — and it refuses a file
    that ``extends`` another rather than half-loading it (see
    :func:`parse_doc`). The layer-aware entry point is
    :func:`claude_launcher.cflow.state.load_workflow`.
    """
    return compose(path, resolve=None).workflow


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"cannot read workflow {path}: {exc}") from exc


#: How deep an ``extends`` chain may go before it is called a mistake rather
#: than a design. Cycles are caught by identity (a file already in the chain),
#: so this only bounds a legal-but-absurd tower — it is a guard, not a budget.
MAX_EXTENDS_DEPTH = 8


@dataclass(frozen=True)
class Composed:
    """A workflow and the files it was composed from, nearest first.

    ``chain[0]`` is the file that was asked for; everything after it is a base
    it reached through ``extends``. A file that extends nothing composes to a
    chain of one, and to exactly the bytes it already had — layering costs
    nothing where it is not used.
    """

    workflow: Workflow
    doc: dict
    chain: Tuple[Path, ...]
    #: The composed doc as YAML — what a run snapshots at ``start``, so its
    #: position cannot shift when a base file is edited mid-run. Verbatim
    #: source when nothing was merged; re-serialised (and therefore
    #: comment-free) when it was, because the merge happened on data and the
    #: comments belong to two different files by then.
    text: str

    @property
    def path(self) -> Path:
        return self.chain[0]

    @property
    def bases(self) -> Tuple[Path, ...]:
        return self.chain[1:]

    @property
    def layered(self) -> bool:
        return len(self.chain) > 1


def compose(
    path: Path,
    *,
    resolve: Optional[Callable[[str, Path], Path]] = None,
) -> Composed:
    """Read ``path``, follow its ``extends`` chain, and parse the merge.

    ``resolve`` turns one file's ``extends`` value into the base's path; it is
    supplied by the layer-aware caller because which layers exist depends on
    the directory the run stands in. Without one, a file that extends anything
    is refused — the caller asked for a file and would otherwise be handed a
    workflow with a silent hole where the base's half was.
    """
    chain: List[Path] = []
    docs: List[dict] = []
    seen: Dict[Path, int] = {}
    current = path
    while True:
        key = _identity(current)
        if key in seen:
            raise WorkflowError(
                f"{EXTENDS_KEY} cycle: "
                + " -> ".join(str(p) for p in chain + [current])
            )
        seen[key] = len(chain)
        text = read_file(current)
        doc = read_doc(text, where=str(current))
        chain.append(current)
        docs.append(doc)
        ref = extends_ref(doc)
        if ref is None:
            break
        if resolve is None:
            raise WorkflowError(
                f"{current} {EXTENDS_KEY} {ref!r}, and this loader resolves no "
                f"layers — load it through claude_launcher.cflow.state."
                f"load_workflow, which knows where the base lives"
            )
        if len(chain) > MAX_EXTENDS_DEPTH:
            raise WorkflowError(
                f"{EXTENDS_KEY} chain deeper than {MAX_EXTENDS_DEPTH}: "
                + " -> ".join(str(p) for p in chain)
            )
        current = resolve(ref, current)
    if len(chain) == 1:
        return Composed(
            workflow=parse_doc(docs[0], default_name=path.stem),
            doc=docs[0],
            chain=tuple(chain),
            text=text,
        )
    merged = compose_docs(docs)
    workflow = parse_doc(merged, default_name=path.stem)
    return Composed(
        workflow=workflow,
        doc=merged,
        chain=tuple(chain),
        text=yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
    )


def _identity(path: Path) -> Path:
    """A path in the one spelling two references to the same file share."""
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


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
    timer = _parse_timer(raw.get("timer"), step_id)
    checklist = _parse_checklist(raw.get("checklist"), step_id)
    restart = _parse_restart(raw.get("restart"), step_id)
    triggers = _parse_triggers(raw.get("triggers"), step_id)
    select = _parse_select(raw.get("select"), step_id)
    escalate = _parse_escalate(raw.get("escalate"), step_id)
    subflows = _parse_subflows(raw.get("subflows"), step_id)
    publishes = _parse_publishes(raw.get("publishes"), step_id)
    if checklist is not None:
        # Every one of these is the same refusal: a checklist step's exit is
        # its items going green, and a second way out is a way past a
        # condition that is false.
        if select is not None:
            raise WorkflowError(
                f"step {step_id!r}: a select step routes via its options; "
                f"'checklist' is not allowed on one"
            )
        if timer is not None:
            raise WorkflowError(
                f"step {step_id!r}: 'timer' and 'checklist' are two different "
                f"daemon-driven exits from the same step — keep one. A timed "
                f"wait moves on a schedule; a checklist moves when its items "
                f"are true"
            )
        if "next" in raw:
            raise WorkflowError(
                f"step {step_id!r}: a checklist step leaves through "
                f"'checklist.then' only; 'next' is not allowed. An agent exit "
                f"is a way past an item that is false, which is what this "
                f"gate exists to refuse"
            )
        if verify is not None:
            raise WorkflowError(
                f"step {step_id!r}: 'verify' is the machine gate on leaving "
                f"through 'next', and a checklist step has no 'next' — write "
                f"that command as a checklist item instead"
            )
        if awaits is not None:
            raise WorkflowError(
                f"step {step_id!r}: 'awaits' has nothing to add to a "
                f"checklist — the checklist IS the re-measured condition, and "
                f"the daemon already samples every item every "
                f"'checklist.poll' seconds"
            )
    if timer is not None and select is not None:
        raise WorkflowError(
            f"step {step_id!r}: a select step routes via its options; "
            f"'timer' is not allowed on one"
        )
    if restart is not None and checklist is None:
        raise WorkflowError(
            f"step {step_id!r}: 'restart' requires a 'checklist' — the "
            "restart command must be followed by a daemon-checked outcome"
        )
    if timer is not None and awaits is not None:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits' has nothing to probe on a timer "
            f"step — a timed wait is a ``waiting_timer`` position, and the "
            f"ReminderClock never samples one. Wait either on a probe "
            f"('awaits:') or on a schedule ('timer:'), not both"
        )
    if (
        awaits is not None
        and awaits.probe is None
        and awaits.sub is None
        and awaits.main is None
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
        if (
            awaits is not None and awaits.probe is None and awaits.sub is None
            and awaits.main is None
        ):
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
    skip = raw.get("skip", False)
    if not isinstance(skip, bool):
        raise WorkflowError(f"step {step_id!r}: 'skip' must be true or false")
    step = Step(
        id=step_id,
        title=str(raw["title"]) if raw.get("title") else None,
        instructions=str(instructions) if instructions else None,
        gate=gate,
        ask=ask,
        verify=verify,
        done_when=done_when,
        awaits=awaits,
        timer=timer,
        checklist=checklist,
        restart=restart,
        triggers=triggers,
        select=select,
        escalate=escalate,
        subflows=subflows,
        publishes=publishes,
        next=_parse_next(raw.get("next"), step_id),
        skip=skip,
    )
    if skip and not step.skippable:
        raise WorkflowError(
            f"step {step_id!r}: 'skip' passes the run through to 'next', and "
            f"this step has no single plain exit (a select, a checklist or a "
            f"timer) -- it cannot be skipped"
        )
    if escalate is not None and None not in step.successors():
        # An escalation fires when the run ENDS here, so a step that cannot
        # end is a step whose declaration can never be read. Refused rather
        # than warned: the author wrote a hand-off, and a hand-off that never
        # happens looks exactly like one that did until somebody checks the
        # board.
        raise WorkflowError(
            f"step {step_id!r}: 'escalate' hands the slot over when the run "
            f"ENDS here, and this step never terminates (every edge out of "
            f"it names another step) — put the escalation on the step that "
            f"ends the run, or give this one a terminating edge"
        )
    return step


def _parse_triggers(raw, step_id: str) -> Tuple[Trigger, ...]:
    """``triggers: [checks]`` or ``triggers: [{do: checks, at: leave}, ...]``.

    The shorthand — a bare action name — means ``{do: <name>, at: enter}``,
    which is the common case: most of these fire because the run has just
    arrived somewhere worth recomposing a briefing from.

    An unknown action name is refused rather than ignored. A trigger is a
    side effect nobody watches for, so a misspelled one would be
    indistinguishable from a daemon that never got round to it — silence
    means the same thing in both cases, and only one of them is a bug the
    author can fix.
    """
    if raw is None or raw is False:
        return ()
    if not isinstance(raw, list):
        raise WorkflowError(
            f"step {step_id!r}: 'triggers' must be a list of action names or "
            f"{{do, at}} mappings"
        )
    out: List[Trigger] = []
    seen: Set[Tuple[str, str]] = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"do": entry}
        if not isinstance(entry, dict):
            raise WorkflowError(
                f"step {step_id!r}: each 'triggers' entry must be an action "
                f"name or a {{do, at}} mapping"
            )
        if True in entry or False in entry:
            # PyYAML's 1.1 resolver turns a bare `on:`/`off:` key into a
            # boolean, so the natural spelling of this field arrives here as
            # `True`. Say that, rather than reporting an unknown key named
            # "True" and leaving the author to work out where it came from.
            raise WorkflowError(
                f"step {step_id!r}: a 'triggers' entry has a boolean key — "
                f"YAML reads a bare 'on:' as true. Spell the moment 'at:'"
            )
        unknown = sorted(str(k) for k in set(entry) - {"do", "at"})
        if unknown:
            raise WorkflowError(
                f"step {step_id!r}: a 'triggers' entry has unknown key(s): "
                f"{', '.join(unknown)}"
            )
        do = str(entry.get("do") or "").strip()
        if do not in TRIGGER_ACTIONS:
            raise WorkflowError(
                f"step {step_id!r}: unknown trigger action {do!r} — "
                f"one of {', '.join(TRIGGER_ACTIONS)}"
            )
        at = str(entry.get("at") or TRIGGER_AT_ENTER).strip()
        if at not in TRIGGER_MOMENTS:
            raise WorkflowError(
                f"step {step_id!r}: trigger {do!r} has unknown moment {at!r} "
                f"— one of {', '.join(TRIGGER_MOMENTS)}"
            )
        if (do, at) in seen:
            raise WorkflowError(
                f"step {step_id!r}: trigger {do!r} is declared twice for "
                f"{at!r} — one firing is one firing"
            )
        seen.add((do, at))
        out.append(Trigger(do=do, at=at))
    return tuple(out)


def _parse_restart(raw, step_id: str) -> Optional[Restart]:
    """Parse CWD-local restart commands without assuming a daemon target."""
    if raw in (None, False):
        return None
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"step {step_id!r}: 'restart' must map platform names to commands"
        )
    unknown = sorted(set(raw) - {"windows", "linux", "timeout"})
    if unknown:
        raise WorkflowError(
            f"step {step_id!r}: 'restart' has unknown key(s): {', '.join(unknown)}"
        )
    windows, linux = raw.get("windows"), raw.get("linux")
    if not windows and not linux:
        raise WorkflowError(
            f"step {step_id!r}: 'restart' needs a 'windows' or 'linux' command"
        )
    if windows is not None and not isinstance(windows, str):
        raise WorkflowError(f"step {step_id!r}: 'restart.windows' must be a string")
    if linux is not None and not isinstance(linux, str):
        raise WorkflowError(f"step {step_id!r}: 'restart.linux' must be a string")
    try:
        timeout = float(raw.get("timeout", 120.0))
    except (TypeError, ValueError):
        raise WorkflowError(f"step {step_id!r}: 'restart.timeout' must be a number")
    if timeout <= 0 or timeout > 900:
        raise WorkflowError(
            f"step {step_id!r}: 'restart.timeout' must be greater than 0 and at most 900"
        )
    return Restart(
        windows=windows.strip() if windows else None,
        linux=linux.strip() if linux else None,
        timeout=timeout,
    )


def _parse_escalate(raw, step_id: str) -> Optional["Escalate"]:
    """``escalate: <workflow>`` or ``escalate: {workflow: ..., context: ...}``.

    The workflow reference is NOT resolved here: this parser runs with no
    layer search in reach, and the file that will be started is the one on
    disk when the request is fulfilled, not the one that exists now.
    """
    if raw is None or raw is False:
        return None
    if isinstance(raw, str):
        raw = {"workflow": raw}
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"step {step_id!r}: 'escalate' must be a workflow name, or a "
            f"mapping like {{workflow: improv-mid, context: '...'}}, got "
            f"{raw!r}"
        )
    unknown = sorted(set(raw) - {"workflow", "context"})
    if unknown:
        raise WorkflowError(
            f"step {step_id!r}: 'escalate' has unknown key(s): "
            f"{', '.join(unknown)} (allowed: workflow, context)"
        )
    workflow = str(raw.get("workflow") or "").strip()
    if not workflow:
        raise WorkflowError(
            f"step {step_id!r}: 'escalate' needs a 'workflow' — which run "
            f"takes the slot over when this one ends"
        )
    context = raw.get("context")
    if context is not None and not isinstance(context, str):
        raise WorkflowError(
            f"step {step_id!r}: 'escalate.context' must be a string — what "
            f"the escalated run is for"
        )
    context = context.strip() if context else None
    return Escalate(workflow=workflow, context=context or None)


def _parse_candidate(raw, where: str) -> Candidate:
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"{where} must be a mapping naming a role, like {{role: reviewer}} "
            f"or {{role: leader, scope: ancestor}}, got {raw!r}"
        )
    unknown = sorted(set(raw) - {"role", "scope", "connect"})
    if unknown:
        raise WorkflowError(
            f"{where} has unknown key(s): {', '.join(unknown)} "
            f"(allowed: role, scope, connect)"
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
            f"{SCOPE_ANCESTOR} = its own chain of command only, "
            f"{SCOPE_SIBLING} = the sessions sharing its parent, "
            f"{SCOPE_DESCENDANT} = the sessions it spawned)"
        )
    connect = raw.get("connect", False)
    if not isinstance(connect, bool):
        # Not coerced: 'connect: no' read as a truthy string would turn the
        # opt-in inside out, and this key adds a mesh edge when it is on.
        raise WorkflowError(
            f"{where}: 'connect' must be true or false, got {connect!r} "
            f"(true = wire this run to a session that holds the role but is "
            f"not reachable, instead of skipping the candidate)"
        )
    return Candidate(role=role, scope=scope, connect=connect)


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
    otherwise_raw = str(raw.get("otherwise") or OTHERWISE_HUMAN).strip()
    base, sep, default_option = otherwise_raw.partition(":")
    base = base.lower()
    if base not in OTHERWISE:
        raise WorkflowError(
            f"{where}: 'otherwise' must be one of {', '.join(OTHERWISE)}, got "
            f"{otherwise_raw!r} (what happens once every candidate has been "
            f"tried: {OTHERWISE_HUMAN} = hold for a person, {OTHERWISE_SELF} = "
            f"the running agent decides alone; on a select's chooser, "
            f"{OTHERWISE_SELF}:<option> = the run takes that option, journaled "
            f"as unanswered)"
        )
    default_option = default_option.strip()
    if sep and not default_option:
        raise WorkflowError(
            f"{where}: 'otherwise: {base}:' names no option — write "
            f"'{base}:<option>' with one of the select's option names, or "
            f"bare '{base}'"
        )
    if base == OTHERWISE_HUMAN and sep:
        raise WorkflowError(
            f"{where}: 'otherwise: human:<option>' means nothing — a person "
            f"answers for themselves; the default a run takes itself belongs "
            f"to '{OTHERWISE_SELF}:<option>'"
        )
    timeout = raw.get("timeout")
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise WorkflowError(f"{where}: 'timeout' must be a number of seconds") from None
        if timeout <= 0:
            raise WorkflowError(f"{where}: 'timeout' must be greater than 0")
    return Delegate(
        candidates=candidates,
        otherwise=base,
        timeout=timeout,
        default_option=default_option or None,
    )


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
    delegate = _parse_delegate(raw, where)
    if delegate.default_option is not None:
        raise WorkflowError(
            f"{where}: 'otherwise: self:{delegate.default_option}' names a "
            f"branch to take, and an approval has no branches — use bare "
            f"'self' (enter unapproved, journaled as unanswered) or 'human'"
        )
    return Ask(
        prompt=str(prompt),
        delegate=delegate,
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


def _parse_milestone_name(raw, step_id: str, where: str) -> Optional[str]:
    """A milestone name (``publishes:``, ``awaits.at``, ``awaits.main``):
    spelled like a run name, so it is one token in a probe command line."""
    if raw is None:
        return None
    name = str(raw).strip()
    if not _SUB_NAME_RE.match(name):
        raise WorkflowError(
            f"step {step_id!r}: {where!r} must be a milestone name (letters, "
            f"digits, '.', '_', '-'), got {raw!r}"
        )
    return name


def _parse_publishes(raw, step_id: str) -> Tuple[str, ...]:
    """``publishes: <name>`` or a list of names."""
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    out: List[str] = []
    for item in items:
        name = _parse_milestone_name(item, step_id, "publishes")
        if name in out:
            raise WorkflowError(f"step {step_id!r}: 'publishes' names {name!r} twice")
        out.append(name)
    return tuple(out)


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
            f"{{probe, poll, timeout, describe}}, {{sub, at?, poll, timeout, "
            f"describe}} or {{main, poll, timeout, describe}} — got {raw!r}. "
            f"A command is written as "
            f"{{probe: '<command>'}}, never as a bare string"
        )
    unknown = sorted(set(raw) - {"probe", "sub", "at", "main", "poll", "timeout", "describe"})
    if unknown:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits' has unknown key(s): "
            f"{', '.join(unknown)} (allowed: probe, sub, at, main, poll, "
            f"timeout, describe)"
        )
    at = _parse_milestone_name(raw.get("at"), step_id, "awaits.at")
    main = _parse_milestone_name(raw.get("main"), step_id, "awaits.main")
    if main is not None and "sub" in raw:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.main' waits on the main run's "
            f"milestone and takes no 'sub'"
        )
    if at is not None and raw.get("sub") is None:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.at' names a milestone of a sub run — "
            f"give the run as 'sub'"
        )
    sub = raw.get("sub")
    if sub is not None:
        if "probe" in raw and at is None:
            raise WorkflowError(
                f"step {step_id!r}: 'awaits' takes 'sub' or 'probe', not both — "
                f"a sub run's end IS the probe"
            )
        sub = str(sub).strip()
        if sub != SUB_ALL and not valid_sub_name(sub):
            raise WorkflowError(
                f"step {step_id!r}: 'awaits.sub' must be a sub run name or the "
                f"word {SUB_ALL!r}, got {sub!r}"
            )
        if at is not None and sub == SUB_ALL:
            raise WorkflowError(
                f"step {step_id!r}: 'awaits.at' reads ONE sub run's milestone — "
                f"name the run, not {SUB_ALL!r}"
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

    poll = _parse_seconds(
        raw.get("poll"), DEFAULT_AWAITS_POLL, step_id, "awaits.poll"
    )
    if poll < MIN_AWAITS_POLL:
        raise WorkflowError(
            f"step {step_id!r}: 'awaits.poll' must be at least "
            f"{MIN_AWAITS_POLL:.0f}s, got {poll:g} — below that the daemon is "
            f"not sampling a condition, it is hammering it"
        )
    timeout = _parse_seconds(
        raw.get("timeout"), min(DEFAULT_AWAITS_TIMEOUT, poll), step_id, "awaits.timeout"
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
        sub=sub,
        at=at,
        main=main,
        poll=poll,
        timeout=timeout,
        describe=describe.strip() if describe else None,
    )


def _parse_seconds(raw, default: float, step_id: str, field_name: str) -> float:
    """A number of seconds from a duration field, or its default.

    ``field_name`` is the dotted path as an author wrote it (``awaits.poll``,
    ``checklist.timeout``): it goes into the error verbatim, so a message
    always names the key that is actually wrong.
    """
    if raw is None:
        return float(default)
    if isinstance(raw, bool):
        raise WorkflowError(
            f"step {step_id!r}: {field_name!r} must be a number of seconds"
        )
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise WorkflowError(
            f"step {step_id!r}: {field_name!r} must be a number of "
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


def _parse_timer(raw, step_id: str) -> Optional[Timer]:
    """Parse a step's ``timer:`` — a bounded, daemon-driven wait.

    Required keys are all four: ``every`` (seconds between fires, positive),
    ``max`` (fires per round, >= 1), ``then`` (where a fire moves the run —
    validated against the step graph later, since it is entered only by a
    fire), ``after`` (where the run goes when the budget is spent — an
    ordinary edge, so it flows through the usual graph check).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"step {step_id!r}: 'timer' must be a mapping "
            f"{{every, max, then, after}}"
        )
    unknown = sorted(set(raw) - {"every", "max", "then", "after"})
    if unknown:
        raise WorkflowError(
            f"step {step_id!r}: 'timer' has unknown key(s): "
            f"{', '.join(unknown)} (allowed: every, max, then, after)"
        )
    every_raw = raw.get("every")
    if isinstance(every_raw, bool) or every_raw is None:
        raise WorkflowError(
            f"step {step_id!r}: 'timer.every' must be a number of seconds"
        )
    try:
        every = float(every_raw)
    except (TypeError, ValueError):
        raise WorkflowError(
            f"step {step_id!r}: 'timer.every' must be a number of seconds, "
            f"got {every_raw!r}"
        ) from None
    if every <= 0:
        raise WorkflowError(
            f"step {step_id!r}: 'timer.every' must be greater than 0"
        )
    max_raw = raw.get("max")
    try:
        max_fires = int(max_raw)
    except (TypeError, ValueError):
        raise WorkflowError(
            f"step {step_id!r}: 'timer.max' must be an integer — how many "
            f"fires the budget holds per round"
        ) from None
    if max_fires < 1:
        raise WorkflowError(
            f"step {step_id!r}: 'timer.max' must be >= 1"
        )
    then = str(raw.get("then") or "").strip()
    if not then:
        raise WorkflowError(
            f"step {step_id!r}: 'timer.then' is required — where each fire "
            f"moves the run"
        )
    after = str(raw.get("after") or "").strip()
    if not after:
        raise WorkflowError(
            f"step {step_id!r}: 'timer.after' is required — where the run "
            f"goes when the budget is spent"
        )
    return Timer(every=every, max=max_fires, then=then, after=after)


def _parse_checklist(raw, step_id: str) -> Optional["Checklist"]:
    """Parse a step's ``checklist:`` — a gate written as machine-checked items.

    ``items`` and ``then`` are required; ``prompt``, ``poll`` and ``timeout``
    are not. Each item needs all three of ``id``, ``describe`` and ``check``:
    the id is what the payload, the journal and the dashboard key on, the
    description is what a person reads next to the checkbox, and the check is
    the command whose exit code decides it.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"step {step_id!r}: 'checklist' must be a mapping "
            f"{{items, then, ...}}"
        )
    unknown = sorted(
        set(raw) - {"items", "then", "prompt", "poll", "timeout", "otherwise"}
    )
    if unknown:
        raise WorkflowError(
            f"step {step_id!r}: 'checklist' has unknown key(s): "
            f"{', '.join(unknown)} "
            f"(allowed: items, then, prompt, poll, timeout, otherwise)"
        )
    then = str(raw.get("then") or "").strip()
    if not then:
        raise WorkflowError(
            f"step {step_id!r}: 'checklist.then' is required — where the run "
            f"goes once every item is true. It is the step's only exit"
        )
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise WorkflowError(
            f"step {step_id!r}: 'checklist.items' must be a non-empty list — "
            f"a gate with no conditions is a gate that is always open"
        )
    items: List[ChecklistItem] = []
    seen: Set[str] = set()
    for index, entry in enumerate(raw_items):
        where = f"step {step_id!r}: checklist item {index}"
        if not isinstance(entry, dict):
            raise WorkflowError(
                f"{where} must be a mapping {{id, describe, check}}"
            )
        extra = sorted(set(entry) - {"id", "describe", "check", "by"})
        if extra:
            raise WorkflowError(
                f"{where} has unknown key(s): {', '.join(extra)} "
                f"(allowed: id, describe, check, by)"
            )
        item_id = str(entry.get("id") or "").strip()
        if not item_id:
            raise WorkflowError(f"{where} needs an 'id'")
        if item_id in seen:
            raise WorkflowError(
                f"step {step_id!r}: checklist item id {item_id!r} appears "
                f"twice — ids key the payload, the journal and the dashboard, "
                f"so they must be unique within a list"
            )
        seen.add(item_id)
        describe = str(entry.get("describe") or "").strip()
        if not describe:
            raise WorkflowError(
                f"{where} ({item_id!r}) needs a 'describe' — the command is "
                f"not a description, and a person reading a stuck gate needs "
                f"to know what is not true"
            )
        check = str(entry.get("check") or "").strip()
        by: Tuple[str, ...] = ()
        if entry.get("by") is not None:
            by = _parse_editable_by(entry.get("by"), f"{where} ({item_id!r})")
        if check and by:
            raise WorkflowError(
                f"{where} ({item_id!r}) has both 'check' and 'by' -- an item "
                f"is decided by a command's exit code OR ticked by the "
                f"session(s) 'by' names, not both"
            )
        if not check and not by:
            raise WorkflowError(
                f"{where} ({item_id!r}) needs a 'check' — the command whose "
                f"exit code decides it (0 = true) -- or 'by: [user]' for an "
                f"item a person ticks"
            )
        items.append(
            ChecklistItem(id=item_id, describe=describe, check=check or None, by=by)
        )
    poll = _parse_seconds(
        raw.get("poll"), DEFAULT_CHECKLIST_POLL, step_id, "checklist.poll"
    )
    timeout = _parse_seconds(
        raw.get("timeout"), DEFAULT_CHECKLIST_TIMEOUT, step_id, "checklist.timeout"
    )
    prompt = raw.get("prompt")
    return Checklist(
        items=tuple(items),
        then=then,
        prompt=str(prompt).strip() if prompt else None,
        poll=max(poll, MIN_CHECKLIST_POLL),
        timeout=min(timeout, MAX_CHECKLIST_TIMEOUT),
        otherwise=_parse_checklist_otherwise(raw.get("otherwise"), step_id),
    )


def _parse_checklist_otherwise(raw, step_id: str) -> Optional[ChecklistOtherwise]:
    """Parse ``checklist.otherwise: {after: <seconds>, then: <step>}``.

    Both keys are required: an expiry with no destination is a gate that
    just stops being one, and a destination with no deadline is a ``then``
    spelled twice. The seconds are read the way ``timer.every`` is — a
    positive number — because this too is a schedule the daemon keeps.
    """
    if raw is None:
        return None
    where = f"step {step_id!r}: 'checklist.otherwise'"
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where} must be a mapping {{after: <seconds>, then: <step>}}")
    unknown = sorted(set(raw) - {"after", "then"})
    if unknown:
        raise WorkflowError(
            f"{where} has unknown key(s): {', '.join(unknown)} (allowed: after, then)"
        )
    after_raw = raw.get("after")
    if isinstance(after_raw, bool) or after_raw is None:
        raise WorkflowError(f"{where}.after must be a number of seconds")
    try:
        after = float(after_raw)
    except (TypeError, ValueError):
        raise WorkflowError(f"{where}.after must be a number of seconds") from None
    if after <= 0:
        raise WorkflowError(f"{where}.after must be positive, got {after_raw!r}")
    then = str(raw.get("then") or "").strip()
    if not then:
        raise WorkflowError(
            f"{where}.then is required — where the run goes when the gate has "
            f"not opened within 'after' seconds"
        )
    return ChecklistOtherwise(after=after, then=then)


def _parse_landing_queue(raw) -> Optional[LandingQueueSpec]:
    """Parse the top-level ``landing_queue:`` (``true`` or ``{target: <branch>}``)."""
    if raw is None or raw is False:
        return None
    if raw is True:
        return LandingQueueSpec()
    if not isinstance(raw, dict):
        raise WorkflowError(
            "'landing_queue' must be true or a mapping {target: <branch>}"
        )
    unknown = sorted(set(raw) - {"target"})
    if unknown:
        raise WorkflowError(
            f"'landing_queue' has unknown key(s): {', '.join(unknown)} "
            f"(allowed: target)"
        )
    target = str(raw.get("target") or "master").strip()
    bare = target.replace(SESSION_PLACEHOLDER, "s")
    if not bare or any(c.isspace() or c in "{}" for c in bare):
        raise WorkflowError(
            "'landing_queue.target' must be a branch name (it may use "
            f"{SESSION_PLACEHOLDER} for the driving session's name)"
        )
    return LandingQueueSpec(target=target)


def _parse_editable_by(raw, where: str) -> Tuple[str, ...]:
    """``by:`` -- who may write: a name or a list of ``user`` / ``agent``."""
    names = [raw] if isinstance(raw, str) else raw
    if not isinstance(names, list) or not names:
        raise WorkflowError(
            f"{where}: 'by' must be one of {', '.join(EDITABLE_BY)} or a "
            f"non-empty list of them"
        )
    out: List[str] = []
    for name in names:
        name = str(name).strip().lower()
        if name not in EDITABLE_BY:
            raise WorkflowError(
                f"{where}: 'by' names {name!r} -- allowed: {', '.join(EDITABLE_BY)}"
            )
        if name not in out:
            out.append(name)
    return tuple(out)


def _parse_editable(raw, steps: Dict[str, Step], start: str) -> Dict[str, EditableSpec]:
    """Parse the top-level ``editable:`` mapping (path -> spec).

    Every path is checked against the steps it names here, at load, so a
    ``claunch cflow set`` never meets a path that turns out to mean nothing.
    The implicit paths of manual checklist items are added last; declaring
    one of those by hand is refused, since the item already says who ticks it.
    """
    out: Dict[str, EditableSpec] = {}
    implicit: Dict[str, EditableSpec] = {}
    for step in steps.values():
        if step.checklist is None:
            continue
        for item in step.checklist.items:
            if item.manual:
                path = checklist_path(step.id, item.id)
                implicit[path] = EditableSpec(
                    path=path, type=EDITABLE_BOOL, by=item.by, default=False,
                    describe=item.describe, implicit=True,
                )
    if raw is None:
        return implicit
    if not isinstance(raw, dict):
        raise WorkflowError(
            "'editable' must be a mapping of run-state path -> "
            "{type, by, default, describe}"
        )
    for path, spec in raw.items():
        path = str(path).strip()
        where = f"editable {path!r}"
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            raise WorkflowError(f"{where} must be a mapping {{type, by, default, describe}}")
        unknown = sorted(set(spec) - {"type", "by", "default", "describe"})
        if unknown:
            raise WorkflowError(
                f"{where} has unknown key(s): {', '.join(unknown)} "
                f"(allowed: type, by, default, describe)"
            )
        parts = path.split(".")
        forced_type: Optional[str] = None
        static_default: object = None
        if parts[0] == "steps":
            if len(parts) == 4 and parts[2] == "checklist":
                raise WorkflowError(
                    f"{where}: a checklist item is made tickable by giving "
                    f"the item itself 'by:' in place of 'check:', not here"
                )
            if len(parts) != 3 or parts[2] != "skip":
                raise WorkflowError(
                    f"{where}: the only step property writable while a run "
                    f"goes is 'steps.<id>.skip'"
                )
            step = steps.get(parts[1])
            if step is None:
                raise WorkflowError(f"{where}: no step {parts[1]!r} in this workflow")
            if not step.skippable:
                raise WorkflowError(
                    f"{where}: step {step.id!r} has no single plain exit (a "
                    f"select, a checklist or a timer) and cannot be skipped"
                )
            if step.id == start:
                raise WorkflowError(
                    f"{where}: {step.id!r} is the start step -- a run is "
                    f"already standing on it before anything can be written"
                )
            forced_type = EDITABLE_BOOL
            static_default = step.skip
        elif len(parts) != 1 or not _STATE_NAME_RE.match(path):
            raise WorkflowError(
                f"{where}: a path is 'steps.<id>.skip' or a name "
                f"([a-z][a-z0-9_]*)"
            )
        kind = str(spec.get("type") or forced_type or "").strip().lower()
        if not kind:
            raise WorkflowError(f"{where} needs a 'type' ({', '.join(EDITABLE_TYPES)})")
        if kind not in EDITABLE_TYPES:
            raise WorkflowError(
                f"{where}: type {kind!r} -- allowed: {', '.join(EDITABLE_TYPES)}"
            )
        if forced_type and kind != forced_type:
            raise WorkflowError(f"{where}: a step's 'skip' is a {forced_type}")
        by = _parse_editable_by(spec.get("by", [EDITABLE_BY_USER]), where)
        if kind == EDITABLE_TEXT and by != (EDITABLE_BY_USER,):
            raise WorkflowError(
                f"{where}: a text value is written by a person only "
                f"(by: [user]) -- the agent's own record belongs on the issue "
                f"board, not in a second note beside it"
            )
        default = spec.get("default", static_default)
        if kind == EDITABLE_BOOL:
            if default is None:
                default = False
            if not isinstance(default, bool):
                raise WorkflowError(f"{where}: 'default' must be true or false")
        else:
            default = "" if default is None else str(default)
        describe = spec.get("describe")
        out[path] = EditableSpec(
            path=path, type=kind, by=by, default=default,
            describe=str(describe).strip() if describe else None,
        )
    out.update(implicit)
    return out


def coerce_value(spec: EditableSpec, value) -> object:
    """``value`` as the type ``spec`` declares, or a :class:`WorkflowError`.

    A bool accepts the spellings a shell hands over (``true``/``false``,
    ``yes``/``no``, ``on``/``off``, ``1``/``0``) as well as a real bool.
    """
    if spec.type == EDITABLE_BOOL:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
        raise WorkflowError(
            f"{spec.path!r} is a bool -- give true or false, not {value!r}"
        )
    if value is None:
        return ""
    return str(value)


def _parse_select(raw, step_id: str) -> Optional[Select]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkflowError(f"step {step_id!r}: 'select' must be a mapping")
    unknown = sorted(set(raw) - {"prompt", "chooser", "options", "require_reason"})
    if unknown:
        raise WorkflowError(
            f"step {step_id!r}: select has unknown key(s): {', '.join(unknown)} "
            "(allowed: prompt, chooser, options, require_reason)"
        )
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
    if delegate is not None and delegate.default_option is not None:
        if delegate.default_option not in options:
            raise WorkflowError(
                f"step {step_id!r}: select chooser 'otherwise: "
                f"self:{delegate.default_option}' names no option of this "
                f"select (options: {', '.join(options)}) — the run would have "
                f"nothing to take when every candidate runs out"
            )
    require_reason = raw.get("require_reason", False)
    if not isinstance(require_reason, bool):
        raise WorkflowError(
            f"step {step_id!r}: select require_reason must be a boolean"
        )
    return Select(
        prompt=str(prompt), chooser=chooser, options=options, delegate=delegate,
        require_reason=require_reason,
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
        if step.checklist is not None and step.checklist.then == step.id:
            raise WorkflowError(
                f"step {step.id!r}: 'checklist.then' must be a different "
                f"step — a gate whose only exit re-enters itself never leaves"
            )
        if (
            step.checklist is not None
            and step.checklist.otherwise is not None
            and step.checklist.otherwise.then == step.id
        ):
            # Re-entering the same step would be a fresh visit and would
            # re-run its `restart:` — which is exactly the loop the edge is
            # for — but with nobody in between to stop it. The decision
            # belongs in a step of its own.
            raise WorkflowError(
                f"step {step.id!r}: 'checklist.otherwise.then' must be a "
                f"different step — put the retry decision in a step of its "
                f"own so an expired gate does not re-enter itself unattended"
            )
        if step.timer is not None:
            # `timer.then` is entered only by a fire, so it is not one of the
            # successors() above; it still must name a real, different step.
            if step.timer.then not in workflow.steps:
                raise WorkflowError(
                    f"step {step.id!r}: 'timer.then' names unknown step "
                    f"{step.timer.then!r}"
                )
            if step.timer.then == step.id:
                raise WorkflowError(
                    f"step {step.id!r}: 'timer.then' must be a different "
                    f"step — a fire that re-enters the same step is a livelock"
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
