"""The cflow state machine: start / next / select / approve / status / abort
/ archive.

Position is simply the current node of the workflow graph plus per-run
counters. Cycles are legal (they model iteration; selects are the loop
exits), so:

- gate approvals and selections are **per visit** — a review gate inside a
  loop closes again on every pass;
- every step's visit count is tracked, and arriving beyond ``max_visits``
  pauses the run like a gate (a human extends it via ``claunch cflow
  approve``) so an agent-chooser loop cannot spin forever.

Enforcement lives here, not in prompts:

- ``verify`` commands run server-side on ``next``; a non-zero exit refuses to
  advance and hands the output back.
- ``gate`` steps withhold their instructions until an approval that only the
  CLI can grant (``claunch cflow approve``). There is deliberately no
  MCP-callable approve — an agent-callable approval is not a gate.
- ``select`` with ``chooser: user`` records the agent's call as a *proposal*
  and blocks until ``claunch cflow select`` confirms (any option).
- ``ask`` steps, and selects whose ``chooser`` names responders, put the
  decision to somebody else entirely (see :func:`answer`).

Delegated decisions
-------------------
The rule the no-approve-tool decision protects is not "only humans approve" —
it is that *the identity recording an approval is not the identity being
approved*. A human satisfies that; so does another agent, provided three
things hold, and all three are enforced here rather than asked for:

1. **The responder's identity is ambient, never an argument.** ``answer``
   takes no "who am I"; the MCP layer fills ``by_session`` from the session
   environment the daemon set, and a session that is not in the ask's
   recorded list is refused.
2. **Candidates are ancestors.** The schema requires ``up``, so a run cannot
   spawn its own approver (see :mod:`.responders`).
3. **The question is closed.** A decision is one of the options the workflow
   declared, plus ``abstain``; nothing is parsed out of prose, so no wording
   an LLM happens to produce can widen the answer set.

Anything unanswerable — no candidate resolved, a group timed out, everyone
abstained, a decline with no declared route — lands the run in front of a
human on the channels that already exist (``claunch cflow approve|select``).
Failing *open* is not an option a workflow can select: a delegated approval
that fell back to the run approving itself would be worse than no gate.
- ``next`` is refused until the step's completion **report** is filed
  (``report {summary, details?}``) — every advance therefore leaves an
  explicit, timestamped account of what happened, which the daemon web
  dashboard shows live next to the run. A failed verify clears the report:
  the outcome it described did not survive, so the fix must be re-reported.

Two processes write a run: the agent's MCP server and the daemon (dashboard
and CLI actions). Every state transition therefore runs under the slot's
lock, and the one long operation — a step's verify command — runs *outside*
it and commits only if the run has not moved underneath (see
:func:`next_step`). A human who wants a run started asks for one
(:func:`request_start`); the agent still performs the ``start`` itself, so
the run it drives and the run on disk can never be two different things.
"""

from __future__ import annotations

import functools
import os
import secrets
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .. import daemon_client, digests
from . import checkout, model, responders, state as state_mod
from .model import Delegate, Step, Workflow

#: Kept from a verify command's combined output when reporting failure.
VERIFY_OUTPUT_TAIL = 4000

#: Kept from a probe's output as the evidence a signal carries. Far smaller
#: than a verify's tail on purpose: this lands pasted into a terminal every
#: time a condition moves, and a probe that needs more than a few lines to say
#: what happened is answering the wrong question.
PROBE_OUTPUT_TAIL = 600

#: How long to wait on a killed probe before giving up on it entirely. Only
#: ever spent after a probe has already blown its own timeout, and only to
#: stop holding pipes nobody is going to write to.
KILL_GRACE = 5.0

#: The two decisions an ``ask`` (an approval) offers, and the escape hatch
#: every delegated decision offers on top of its own options. ``abstain`` is
#: first-class on purpose: a responder with no basis to decide must have
#: something to say other than a guess, and saying it escalates.
APPROVE = "approve"
DECLINE = "decline"
ABSTAIN = "abstain"

APPROVAL_OPTIONS = [
    {"name": APPROVE, "description": "grant entry to this step"},
    {
        "name": DECLINE,
        "description": (
            "refuse entry; the run takes the workflow's declared decline route, "
            "or holds for a human if none was declared"
        ),
    },
]

#: Typed into the run directory's managed sessions after a human unblocks the
#: run, so a stopped agent resumes without a manual nudge. Per the /cflow
#: skill, any user message makes the agent re-check 'status' first.
NUDGE_APPROVED = "cflow: approved - continue per the /cflow protocol"
NUDGE_SELECTED = "cflow: selection confirmed - continue per the /cflow protocol"
NUDGE_ANSWERED = "cflow: your request was answered - continue per the /cflow protocol"
NUDGE_CONTINUE = "cflow: continue per the /cflow protocol"
NUDGE_ARCHIVED = "cflow: run archived - the slot is free for a new workflow"
NUDGE_STARTED = (
    "cflow: a new workflow run was started - continue per the /cflow protocol"
)
NUDGE_GOTO_DENIED = (
    "cflow: your step-change request was refused - call the cflow 'status' "
    "tool and continue per the /cflow protocol"
)


def _t_hint() -> str:
    """Appended to CLI commands relayed to a human, for the shell that stands
    somewhere else entirely. From anywhere in or under the run's directory the
    CLI finds the run by itself (it walks up, then checks the run registry for
    a named session) — but a chat session's ``!`` shell can be pinned to an
    unrelated path, and there the session must be named."""
    scope = state_mod.current_scope()
    if scope == state_mod.DEFAULT_SCOPE:
        return ""
    return f"; if the shell reports no run here, add '-t {scope}'"


def _asking_well(lead: str) -> str:
    """The closing every human-facing ``how_to_unblock`` shares.

    A gate is a question put to a person, and their answer is only as good
    as what they were handed to answer it with. "Present your recommendation
    and wait" -- which is all this used to say -- reliably produced a one-line
    demand to confirm: an option name, no evidence, no cost, nothing the
    reader could weigh. So the requirement is spelled out here rather than
    left to taste, and it lives *in the payload* because the payload is
    re-delivered on every poll: an agent whose context was compacted between
    reaching the gate and being nudged still reads it. The ``/cflow`` skill
    text carries the same rule at more length, for the agent that still has
    its briefing.

    Asking well is not a courtesy here. The person at the gate is standing
    there precisely because the run does not get to decide this one, and a
    request that hides its weak half buys a confirmation that was never
    really given.

    Waiting counts as an answer, which is why the cost clause names it. A
    gate that reads "the live server is serving pre-merge code -- restart
    now?" is not missing politeness; it is missing what the reader is
    actually weighing -- what a restart cuts, whether it can be undone, and
    what piles up for as long as they leave it. Say those and the same
    question becomes answerable.
    """
    return (
        f"Stop your turn and put this to them as a request, not an "
        f"instruction. {lead} Give what the decision actually rests on: the "
        f"concrete evidence (commands run, test counts, commit hashes, files "
        f"touched -- not adjectives); what each answer costs, holding off "
        f"included -- what it does, what can be undone, and what piles up "
        f"while the run waits; your recommendation, its reasoning, and what "
        f"would overturn it; and the weakest part of your own case. Close "
        f"with how to answer, and make clear any option is theirs to take, "
        f"including one you did not recommend. Then wait to be nudged -- no "
        f"urgency you invented, no asking again."
    )


def nudge_for_request(workflow: str) -> str:
    return (
        f"cflow: a start of workflow '{workflow}' was requested - call the "
        f"cflow 'status' tool, then start it per the /cflow protocol"
    )


def nudge_for_state(step_id: str) -> str:
    return (
        f"cflow: current step forced to '{step_id}' - "
        "continue per the /cflow protocol"
    )


class CflowError(Exception):
    """Raised for protocol misuse (wrong tool for the current position)."""


def _scoped_op(fn):
    """Give a public operation an optional ``scope=`` kwarg.

    Runs are keyed by (cwd, scope); the scope defaults to the ambient one
    (the session's ``CLAUNCH_SESSION`` env, inherited by the MCP server) and
    is overridden explicitly by human channels (CLI ``-t``, web ``scope``).
    The override is installed for the duration of the call so every state
    access inside resolves against the same run.
    """

    @functools.wraps(fn)
    def wrapper(*args, scope: Optional[str] = None, **kwargs):
        token = state_mod.push_scope(scope)
        try:
            return fn(*args, **kwargs)
        finally:
            state_mod.pop_scope(token)

    return wrapper


def _locked_op(fn):
    """A scoped operation that also holds the slot's cross-process lock.

    Everything that reads-then-writes run state goes through here: the agent's
    MCP server and the daemon are separate processes acting on the same files,
    so 'check it is idle, then write the run' has to be indivisible.
    """

    @functools.wraps(fn)
    def wrapper(*args, scope: Optional[str] = None, **kwargs):
        token = state_mod.push_scope(scope)
        try:
            with state_mod.run_lock(kwargs.get("cwd")):
                return fn(*args, **kwargs)
        finally:
            state_mod.pop_scope(token)

    return wrapper


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _base(state: dict) -> dict:
    base = {
        "run": state["run_id"],
        "workflow": state["workflow"],
        "steps_completed": state["completed"],
    }
    # Which pass of a recurring workflow this run is. Absent on round 1 (and
    # on every run of a non-recurring workflow) so the common case stays as
    # it always read.
    if int(state.get("round") or 1) > 1:
        base["round"] = int(state["round"])
    return base


def _visits(state: dict, step_id: str) -> int:
    return int(state["visits"].get(step_id, 0))


def _limit(workflow: Workflow, state: dict, step_id: str) -> int:
    extensions = int(state["loop_extensions"].get(step_id, 0))
    return workflow.max_visits * (1 + extensions)


def _current_report(state: dict, step_id: str) -> Optional[dict]:
    """The filed report for this step *and visit*, if any."""
    report = state.get("report")
    if (
        report
        and report.get("step") == step_id
        and report.get("visit") == _visits(state, step_id)
    ):
        return report
    return None


# --------------------------------------------------------------------------- #
# cadence: a paced select option, held until its window opens
# --------------------------------------------------------------------------- #
def _utc_now() -> datetime:
    """The clock every cadence decision reads (tests replace it)."""
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def _parse_at(text) -> Optional[datetime]:
    try:
        at = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _window_opens(
    state: dict, step: Step, option: str, cwd, now: datetime
) -> Optional[datetime]:
    """When the driver may next take this option, or None for "now".

    None covers both "no cadence declared" and "the interval has passed" —
    and the first take ever, which has nothing to pace against. The record
    read here is the slot's (:func:`state.read_windows`), so a recurring
    run's round N+1 paces against round N's take.
    """
    interval = step.select.options[option].interval
    if not interval:
        return None
    key = state_mod.window_key(state["workflow"], step.id, option)
    last = _parse_at(state_mod.read_windows(cwd).get(key) or "")
    if last is None:
        return None
    opens = last + timedelta(seconds=float(interval))
    return opens if opens > now else None


def _current_window(state: dict, step_id: str, visit: int) -> Optional[dict]:
    """The held choice on this step and visit, if any — keyed like a report,
    for the same reason: a hold from a previous pass must not survive into
    this one."""
    window = state.get("window")
    if window and window.get("step") == step_id and window.get("visit") == visit:
        return window
    return None


def _window_due(window: dict, now: datetime) -> bool:
    opens = _parse_at(window.get("opens_at"))
    return opens is None or opens <= now


def _hold(
    state: dict, step: Step, option: str, reason: str, opens: datetime, cwd, now: datetime
) -> None:
    """Record the driver's choice and park the run until ``opens``."""
    visit = _visits(state, step.id)
    previous = _current_window(state, step.id, visit)
    renewed = bool(previous and previous.get("option") == option)
    state["window"] = {
        "step": step.id,
        "visit": visit,
        "option": option,
        "reason": reason,
        "by": "agent",
        "at": previous["at"] if renewed else _iso(now),
        "opens_at": _iso(opens),
    }
    state_mod.save_state(state, cwd)
    state_mod.journal(
        "select_held",
        {"run": state["run_id"], "step": step.id, "visit": visit, "option": option,
         "reason": reason, "opens_at": _iso(opens), "renewed": renewed},
        cwd,
    )


def _take(state: dict, step: Step, option: str, cwd, now: datetime) -> None:
    """Note a take of a paced option — the moment its next window is
    measured from. Every take counts, a human's confirm included: the
    cadence is about how often the option happens, not who pressed it."""
    if step.select.options[option].interval:
        state_mod.record_window(
            state_mod.window_key(state["workflow"], step.id, option), _iso(now), cwd
        )


def _release(workflow: Workflow, state: dict, step: Step, cwd, now: datetime) -> dict:
    """Confirm a held choice whose window has opened, and move the run.

    The reason journaled is the LATEST the driver filed while holding — that
    is the point of letting a renewal replace it: what accumulated during
    the wait belongs in the record the next step reads.
    """
    window = dict(state["window"])
    state["window"] = None
    option = window["option"]
    state_mod.journal(
        "select_confirmed",
        {"run": state["run_id"], "step": step.id, "option": option,
         "reason": window.get("reason") or "", "by": "window",
         "visit": _visits(state, step.id), "held_since": window.get("at"),
         "opens_at": window.get("opens_at")},
        cwd,
    )
    _take(state, step, option, cwd, now)
    state["completed"] += 1
    _move_to(workflow, state, step.select.options[option].next, cwd)
    return window


def _window_payload(base: dict, step: Step, window: dict, now: datetime) -> dict:
    opens = _parse_at(window.get("opens_at"))
    remaining = max(0, int((opens - now).total_seconds())) if opens else 0
    option = step.select.options[window["option"]]
    return {
        **base,
        "status": "waiting_window",
        "prompt": step.select.prompt,
        "option": window["option"],
        "interval": option.interval,
        "held_since": window.get("at"),
        "opens_at": window.get("opens_at"),
        "remaining": remaining,
        "note": (
            f"your choice {window['option']!r} is recorded and HELD: this option "
            f"runs at most once per {option.interval:g}s and its window opens "
            f"at {window.get('opens_at')} (~{remaining}s). Stop your turn — do "
            f"not poll. The daemon's clock releases it then, moves the run and "
            f"nudges you; without a daemon, your next 'next' call after that "
            f"moment releases it. Meanwhile keep doing this step's standing "
            f"work: if what the choice rests on changes, call 'select' again "
            f"with the same option and the updated reason (it replaces the "
            f"held one — the reason confirmed at release is the latest), or "
            f"with a different option to cancel the hold"
        ),
    }


# --------------------------------------------------------------------------- #
# delegated decisions
# --------------------------------------------------------------------------- #
#: How long a deferred ask waits before the daemon's clock re-reads the roster.
#: Sized against what it is waiting out: an :data:`daemon_client.UNRESPONSIVE`
#: verdict is one blocked turn of the daemon's single event loop, and the
#: daemon's own start path grants a slow one :data:`daemon_client.START_TIMEOUT`
#: (15s) to answer. Two of those is patience enough to outlast a stall without
#: making a person watch an unmoving run for minutes.
ROSTER_RETRY = 30.0

#: How many times one ask may be deferred for an unreadable roster before its
#: candidate groups are spent anyway. The bound is what keeps deferral from
#: becoming a way for a run to stop forever: after this, the ask lands in front
#: of a person exactly as it did before, with the same reasons in ``skipped``.
MAX_ROSTER_DEFERRALS = 4


def _deadline(timeout: Optional[float]) -> Optional[str]:
    """When the current group's turn expires, or None for "no clock".

    Recorded rather than enforced here: nothing calls into a stopped run, so
    the expiry belongs to the daemon, which is already scanning the registry.
    Without a daemon the question simply keeps waiting — which is the same
    thing a human gate has always done, and is the safe direction to fail.
    """
    if not timeout:
        return None
    at = datetime.now(timezone.utc) + timedelta(seconds=float(timeout))
    return at.isoformat(timespec="seconds")


def _delegate_for(step: Step, kind: str) -> Delegate:
    """The declaration behind an open ask of this ``kind`` on this step."""
    if kind == "approval":
        return step.ask.delegate
    return step.select.delegate


def _current_ask(state: dict, step_id: str, visit: int, kind: str) -> Optional[dict]:
    """The open ask for this step, visit and kind — anything else is stale.

    Keyed exactly like a step report (:func:`_current_report`), and for the
    same reason: a gate inside a loop closes again on every pass, so an answer
    to the previous pass must not be able to land on this one.
    """
    ask = state.get("ask")
    if (
        ask
        and ask.get("step") == step_id
        and ask.get("visit") == visit
        and ask.get("kind") == kind
    ):
        return ask
    return None


def _awaits_human(ask: dict) -> bool:
    """Whether this ask now sits in front of a person.

    An open ask with nobody in it is one whose candidate list ran out under
    ``otherwise: human`` — the workflow's own instruction for that case, and
    the only way an ask survives with an empty group. The CLI and the dashboard
    answer it exactly as they always answered a gate; ``skipped`` is what tells
    a reader no agent got there first.
    """
    return not any(entry.get("kind") == "member" for entry in ask.get("asked") or [])


def _proceed_alone(
    workflow: Workflow,
    state: dict,
    step: Step,
    *,
    kind: str,
    prompt: str,
    skipped: List[dict],
    cwd,
) -> None:
    """Nobody could be asked and the workflow said carry on regardless.

    The escape hatch of ``otherwise: self``, and the one place a decision goes
    unmade without the run stopping. It is journaled as *unanswered*, never as
    an approval: an entry saying a step was approved must always name who
    approved it, and here nobody did. What "carry on" means is the workflow's
    declaration, read per kind:

    * an **approval** opens its gate unapproved, and the step is entered;
    * a **branch** with no declared default (bare ``self``) becomes the
      ordinary agent-chooses select it would have been without a ``from``;
    * a **branch** with ``self:<option>`` takes the named option here, by
      nobody — for the decision the driver is the one party who must not
      make. The take is journaled as a ``select_confirmed`` whose ``by`` is
      ``unanswered``, so the record says the branch was chosen and that
      nobody chose it.
    """
    state["ask"] = None
    # Keyed like a report, and kept for the same reason: this is a fact about
    # one visit to one step, and a later pass round a loop must ask again
    # rather than inherit it. It is also what stops the run from re-opening the
    # question every time somebody reads its status.
    state["unanswered"] = {
        "step": step.id,
        "visit": _visits(state, step.id),
        "kind": kind,
        "at": state_mod.utcnow(),
    }
    if kind == "approval":
        state["gate_approved"] = True
    state_mod.save_state(state, cwd)
    state_mod.journal(
        "ask_unanswered_proceeded",
        {
            "run": state["run_id"],
            "step": step.id,
            "visit": _visits(state, step.id),
            "kind": kind,
            "prompt": prompt,
            "skipped": [s["reason"] for s in skipped],
        },
        cwd,
    )
    default = None
    if kind == "branch" and step.select is not None and step.select.delegate:
        default = step.select.delegate.default_option
    if default is not None:
        now = _utc_now()
        state_mod.journal(
            "select_confirmed",
            {"run": state["run_id"], "step": step.id, "option": default,
             "reason": "declared default, taken unanswered", "by": "unanswered",
             "visit": _visits(state, step.id)},
            cwd,
        )
        _take(state, step, default, cwd, now)
        state["completed"] += 1  # the decision itself counts as a completed step
        _move_to(workflow, state, step.select.options[default].next, cwd)


def _live_chooser(state: dict, step: Step) -> str:
    """Who decides this select *now*, which is not always who was declared.

    A delegated select whose candidates all fell through under
    ``otherwise: self`` becomes an ordinary agent-chooses select for the rest
    of this visit. Reading that from the run rather than from the workflow
    keeps one answer for everybody who has to know it — the payload, the
    ``select`` tool's refusal, and anything watching.
    """
    if step.select.chooser != "delegate":
        return step.select.chooser
    fell = state.get("unanswered") or {}
    if (
        fell.get("kind") == "branch"
        and fell.get("step") == step.id
        and fell.get("visit") == _visits(state, step.id)
    ):
        return "agent"
    return "delegate"


def _open_ask(
    workflow: Workflow,
    state: dict,
    step: Step,
    *,
    kind: str,
    prompt: str,
    options: List[dict],
    delegate: Delegate,
    cwd,
    ask_id: Optional[str] = None,
    from_group: int = 0,
    skipped: Optional[List[dict]] = None,
    deferrals: int = 0,
) -> Optional[dict]:
    """Put the decision to the first candidate group that resolves to anyone.

    Groups are tried in declared order and every one that resolves to nobody
    is recorded in ``skipped`` with its reason, so a question that reaches a
    human arrives with the account of why no agent took it. Running out of
    groups is not an error — it is where the second axis takes over:
    ``otherwise: human`` leaves the ask open with nobody in it (the hold state
    the CLI has always answered), and ``otherwise: self`` returns ``None``,
    having let the run carry on alone. On a branch whose ``self`` names an
    option, "carry on" has already happened: the run took the declared default
    and MOVED, so the caller must re-read the position rather than present
    the step it was on.

    ``ask_id`` is carried across an escalation on purpose — the decision is
    the same one, so a responder from an earlier group that answers late is
    told it is no longer theirs to answer rather than that the id is unknown.

    **Spending a group needs an answer, not silence.** Walking past a group is
    irreversible: the list only ever runs forward, and running out of it hands
    the decision to ``otherwise`` for good. So the roster has to have been
    *read* before any of that happens. When it could not be
    (:attr:`.responders.Pool.unreadable` — a daemon that is up and did not
    reply), the ask is DEFERRED instead: recorded at the same group, with
    nobody asked and a :data:`ROSTER_RETRY` deadline, so the daemon's existing
    expiry clock comes back and re-resolves the very same question. Measured
    without this (issue ``claunch-ojci``): one busy moment sent an ``errand``
    run's ``end-gate`` past both agent groups with ``asked: []`` and
    ``deadline: null``, where the leader it was meant for could not answer it
    and a person had to. Five sibling runs in the same hour routed normally.

    A deferral is bounded by :data:`MAX_ROSTER_DEFERRALS` and is never entered
    when there is no daemon to come back — :attr:`~.responders.Pool.unreadable`
    is false for the states that establish absence, so a machine with no daemon
    behaves exactly as it did before. Both halves matter: a run must not be
    able to wait forever on a question, and a person must not be handed one
    that was never actually put to anybody.
    """
    session = state_mod.current_scope()
    if session == state_mod.DEFAULT_SCOPE:
        session = ""  # not a managed session: it has no mesh identity
    # One roster read for the whole list. The groups are a preference order
    # over a single moment's mesh, not a series of questions about a moving
    # one — resolving each against its own snapshot could pick a responder
    # that only exists in between two of them.
    reach = responders.pool(
        session=session, mesh=str(state.get("mesh") or ""), cwd=cwd
    )
    candidates = delegate.candidates
    skipped = list(skipped or [])
    asked: List[dict] = []
    found: List[responders.Responder] = []
    group = from_group
    # Nothing is spent on silence. `unreadable` is the pool saying it has no
    # answer yet rather than the answer "nobody", and every group below would
    # otherwise be skipped for that non-reason -- see the docstring.
    deferred: Optional[dict] = None
    if (
        reach.unreadable
        and group < len(candidates)
        and deferrals < MAX_ROSTER_DEFERRALS
    ):
        deferred = {"count": deferrals + 1, "reason": reach.problem}
    while not deferred and group < len(candidates):
        candidate = candidates[group]
        # The one call that passes `autowire`. This is the moment a question
        # is actually being put to somebody, so it is the moment a candidate's
        # `connect: true` is allowed to have the effect it declares; the
        # preview (`_delegation_preview`) reads the same pool and must not
        # change it.
        found, reason = reach.match(candidate, autowire=True)
        if found:
            asked = [r.to_dict() for r in found]
            break
        skipped.append(
            {
                "group": group,
                "candidate": candidate.describe(),
                "reason": reason or "nobody matched",
            }
        )
        group += 1

    if not asked and not deferred and delegate.otherwise == model.OTHERWISE_SELF:
        # `otherwise: self` is a decision too, and taking it because the
        # roster went quiet would be the same category error one step over.
        _proceed_alone(
            workflow, state, step, kind=kind, prompt=prompt, skipped=skipped,
            cwd=cwd,
        )
        return None

    ask = {
        "id": ask_id or f"ask-{secrets.token_hex(3)}",
        "step": step.id,
        "visit": _visits(state, step.id),
        "kind": kind,
        "prompt": prompt,
        "options": options,
        "group": group,
        "asked": asked,
        "skipped": skipped,
        "opened_at": state_mod.utcnow(),
        # A deferred ask's clock is the retry, not the group's turn: nobody
        # holds it, so there is no turn to time out.
        "deadline": _deadline(
            ROSTER_RETRY if deferred else (delegate.timeout if asked else None)
        ),
        # Why this ask is sitting at a group it has not tried, and how many
        # times it has done so. Its absence is what tells a reader (and
        # `expire_ask`) that an empty `asked` is settled rather than pending.
        **({"deferred": deferred} if deferred else {}),
        # Edges this ask made for itself, because a candidate declared
        # `connect: true`. Recorded on the ask rather than left to the mesh:
        # the member graph stores an edge, not who decided it, and a leader
        # who wires reviewers apart on purpose needs to be able to tell an
        # edge a workflow produced from one they chose.
        **({"wired": list(reach.wired)} if reach.wired else {}),
    }
    if reach.wired:
        state_mod.journal(
            "ask_wired",
            {"run": state["run_id"], "ask": ask["id"], "step": step.id,
             "mesh": reach.mesh, "to": list(reach.wired)},
            cwd,
        )
    if found:
        # Announcing it is the last thing, and the least load-bearing: the
        # question is already recorded and answerable without the message.
        undelivered = responders.deliver(
            ask,
            mesh=reach.mesh,
            sender=reach.me,
            workflow=state["workflow"],
            to=[r.handle for r in found],
        )
        if undelivered:
            ask["undelivered"] = undelivered
    state["ask"] = ask
    state_mod.save_state(state, cwd)
    state_mod.journal(
        "ask_deferred" if deferred else ("ask_unresolved" if not asked else "ask_opened"),
        {
            "run": state["run_id"],
            "ask": ask["id"],
            "step": step.id,
            "visit": ask["visit"],
            "kind": kind,
            "group": group,
            "asked": [e.get("handle") or e["kind"] for e in asked],
            "skipped": [s["reason"] for s in skipped],
            **({"deferred": deferred} if deferred else {}),
            **({"undelivered": ask["undelivered"]} if ask.get("undelivered") else {}),
        },
        cwd,
    )
    return ask


def _escalate(
    workflow: Workflow, state: dict, step: Step, ask: dict, cwd, why: str
) -> Optional[dict]:
    """Hand the same decision to the next candidate group.

    The one path that serves all three ways a group can fail to produce an
    answer — nobody resolved, everybody abstained, and nobody replying in
    time. That is the whole reason the preference list is one declaration
    instead of separate "fallback" and "escalation" settings: they are the
    same list read on different triggers.

    ``None`` back means the list ran out under ``otherwise: self`` and the run
    has already moved past the question — and on a branch whose ``self`` named
    an option, "moved past" is literal: the declared default was taken and the
    run is on another step.
    """
    state_mod.journal(
        "ask_escalated",
        {
            "run": state["run_id"],
            "ask": ask["id"],
            "step": step.id,
            "from_group": ask["group"],
            "why": why,
        },
        cwd,
    )
    skipped = list(ask.get("skipped") or [])
    skipped.append(
        {
            "group": ask["group"],
            "candidate": ", ".join(
                e.get("handle") or e["kind"] for e in ask.get("asked") or []
            )
            or "nobody",
            "reason": why,
        }
    )
    return _open_ask(
        workflow,
        state,
        step,
        kind=ask["kind"],
        prompt=ask["prompt"],
        options=ask["options"],
        delegate=_delegate_for(step, ask["kind"]),
        cwd=cwd,
        ask_id=ask["id"],
        from_group=ask["group"] + 1,
        skipped=skipped,
    )


def _retry_deferred(
    workflow: Workflow, state: dict, step: Step, ask: dict, cwd
) -> Optional[dict]:
    """Re-read the roster for an ask that was deferred because it was silent.

    The other half of the deferral in :func:`_open_ask`, and deliberately not
    :func:`_escalate`: escalation moves the question ON to the next group
    because the group it was with produced no answer, and this question has
    never been with a group at all. So it re-opens at the SAME
    ``from_group``, adds nothing to ``skipped``, and carries the deferral
    count forward — which is what makes :data:`MAX_ROSTER_DEFERRALS` a bound
    on the whole wait rather than on one attempt.

    Called from :func:`expire_ask`, so the retry rides the clock the daemon
    already runs over every registered run; there is no second timer.
    """
    deferred = ask.get("deferred") or {}
    state_mod.journal(
        "ask_retried",
        {
            "run": state["run_id"],
            "ask": ask["id"],
            "step": step.id,
            "group": ask["group"],
            "attempt": int(deferred.get("count") or 0),
            "why": str(deferred.get("reason") or ""),
        },
        cwd,
    )
    return _open_ask(
        workflow,
        state,
        step,
        kind=ask["kind"],
        prompt=ask["prompt"],
        options=ask["options"],
        delegate=_delegate_for(step, ask["kind"]),
        cwd=cwd,
        ask_id=ask["id"],
        from_group=ask["group"],
        skipped=list(ask.get("skipped") or []),
        deferrals=int(deferred.get("count") or 0),
    )


def _user_door(kind: str, options: Optional[List[dict]] = None) -> dict:
    """The command a person can settle this ask with, while an agent holds it.

    Delegating a decision takes it away from the *run*, never from the user:
    :func:`select` and :func:`approve` have always let a human answer over an
    open ask, because the CLI is the trusted channel by construction (anyone
    holding it can already ``goto`` the run anywhere). What was missing was
    anybody saying so — the payload told the driver "you cannot answer it
    yourself" and stopped there, so the one person who *could* answer it was
    the only party never told the door existed.

    Announcing it does not turn the ask back into a gate. The run is not
    waiting on this and the driver must not stand the user up on it; it is a
    door held open beside the delegation, and it is the user's to use or
    ignore.
    """
    if kind == "branch":
        names = "|".join(o["name"] for o in options or [])
        command = f"claunch cflow select <{names}>"
    else:
        command = "claunch cflow approve"
    return {
        "command": command,
        # Stated as a fact about the engine, not a promise about manners: an
        # answer through this door closes the ask where it stands, so a
        # responder answering after it is told the question is gone.
        "wins": True,
        "note": (
            "a person may settle this at any time while it is out; their "
            "answer lands over the responder's and closes the question"
        ),
    }


def _settled_by_person(
    state: dict, ask: dict, *, decision: str, by: str, cwd
) -> None:
    """Record a person's answer to an open ask, and tell whoever was holding it.

    One path for both doors (``select`` for a branch, ``approve`` for an entry
    approval), because to the run they are the same event: the question is
    closed by somebody who was not asked. Two cases hide under that, and the
    journal has to keep them apart —

    * the ask had fallen to a human already (nobody in the group): this IS the
      intended answer, ``in_group`` true, nobody to tell;
    * responders are still holding it: the person **overrode** them. The entry
      names who was preempted, and they are told so in the thread the question
      arrived in rather than finding out by having their answer refused.

    The authority is not new and not checked here: the CLI is the trusted
    channel by construction — anyone holding it can already ``goto`` the run
    anywhere — so the honest thing is to record the override, not to pretend
    it is impossible.
    """
    held = [e for e in ask.get("asked") or [] if e.get("kind") == "member"]
    who = [str(e.get("handle") or e.get("session") or "?") for e in held]
    state_mod.journal(
        "ask_answered",
        {
            "run": state["run_id"],
            "ask": ask.get("id"),
            "step": ask.get("step"),
            "decision": decision,
            "by": by,
            "by_session": None,
            "in_group": not held,
            **({"override": who} if held else {}),
        },
        cwd,
    )
    if not held:
        return
    session = state_mod.current_scope()
    if session == state_mod.DEFAULT_SCOPE:
        session = ""
    reach = responders.pool(
        session=session, mesh=str(state.get("mesh") or ""), cwd=cwd
    )
    failure = (
        reach.problem
        or responders.withdraw(
            ask,
            mesh=reach.mesh,
            sender=reach.me,
            workflow=str(state.get("workflow") or ""),
            to=who,
            decision=decision,
        )
    )
    if failure:
        # Not an error for the person at the CLI: their answer is recorded and
        # the run has moved. It is a fact the responder's wasted turn will be
        # explained by, so it goes in the journal rather than nowhere.
        state_mod.journal(
            "ask_withdraw_failed",
            {"run": state["run_id"], "ask": ask.get("id"), "to": who,
             "reason": failure},
            cwd,
        )


def _unopened_human_gate(
    base: dict,
    delegate: Delegate,
    *,
    kind: str,
    prompt: str,
    options: Optional[List[dict]] = None,
) -> Optional[dict]:
    """A gate-shaped ask described before ``next`` has opened it, or ``None``.

    An ask with no candidates under ``otherwise: human`` is the form
    ``gate:`` deprecates into: nobody is asked, and the only answer that
    moves the run is a person's. Opening it is still a write ``next``
    performs — so between arriving here without ``next`` (``goto``, a
    person confirming a select from the CLI or the dashboard) and the
    driver's next call, the read-only ``status`` used to describe this as
    ``waiting_answer`` that "has not been put to anyone yet".

    That description was true and useless. True: the ask record did not
    exist. Useless: the daemon's clocks read that shape as *a delegated
    decision the driver still has to route* (:func:`daemon.cflow_clock
    ._ask_reached_nobody`) and typed "call 'next'" at a busy driver every
    reminder interval, while ``next`` could only ever open the same gate in
    front of the same person. A driver that answered by reading ``status``
    and waiting for the approval was nagged for as long as the approval took
    (issue ``claunch-ueku``, the improv-worker ``end-gate``). So this shape
    is reported as the gate it is — the same payload the opened ask produces
    once it has fallen to a human — and every clock stays out of it, exactly
    as they stay out of ``gate:``.

    The two shapes that DO need the driver keep their old reading: a
    candidate list (``next`` is what routes it) and ``otherwise: self``
    (``next`` is what journals the decision unmade and hands out the step).
    """
    if delegate.candidates or delegate.otherwise != model.OTHERWISE_HUMAN:
        return None
    note = (
        "this decision has not been opened for the record yet -- the "
        "driver's 'next' does that, and it changes nothing about who "
        "answers: nobody is delegated to, so it is a person's from the start"
    )
    if kind == "branch":
        return {
            **base,
            "status": "waiting_selection",
            "prompt": prompt,
            "options": list(options or []),
            "how_to_unblock": (
                f"a human must choose with 'claunch cflow select <option>' "
                f"(inside a chat session: '! claunch cflow select <option>'"
                f"{_t_hint()}) or an option button on the daemon web "
                f"dashboard. "
                + _asking_well(
                    "Name the decision in one line, then say which option "
                    "you recommend."
                )
            ),
            "note": note,
        }
    return {
        **base,
        "status": "waiting_approval",
        "reason": "ask",
        "gate": prompt,
        "how_to_unblock": (
            f"a human must approve: 'claunch cflow approve' (inside a chat "
            f"session: '! claunch cflow approve'{_t_hint()}) or the Approve "
            f"button on the daemon web dashboard; the agent cannot approve. "
            + _asking_well(
                "Say plainly what you are asking them to approve, and show "
                "the work it would be approved on."
            )
        ),
        "note": note,
    }


def _ask_payload(base: dict, ask: dict) -> dict:
    """How an open ask is described to whoever reads the run.

    A question waiting on an *agent* is its own status, because it is not the
    operator's move — the run is not stopped on a person, and painting it as a
    gate grows a queue of things that look like work and are not. It is not
    beyond their reach either: ``user_door`` carries the press that settles it
    anyway, for the reader who decides this one is theirs after all. Once the
    ask falls to a human it is reported as the plain approval/selection it has
    become, so the CLI and the dashboard keep working on it unchanged.
    """
    payload = {**base, "ask": ask}
    if not _awaits_human(ask):
        who = ", ".join(e.get("handle", "?") for e in ask["asked"])
        door = _user_door(ask["kind"], ask.get("options"))
        payload["status"] = "waiting_answer"
        payload["reason"] = ask["kind"]
        payload["user_door"] = door
        payload["how_to_unblock"] = (
            f"{who} was asked to decide this and has not answered yet. You "
            f"cannot answer it yourself. Stop your turn, present what you have "
            f"so far, and wait to be nudged. The user is not shut out of it: "
            f"say in one line who holds it and that they can settle it now "
            f"with '{door['command']}' — a person's answer lands over the "
            f"responder's. That is a door, not a gate: do not stand them up "
            f"on it, do not wait for them, and do not ask twice."
        )
        return payload
    unresolved = bool(ask["skipped"]) and not ask["asked"]
    if ask["kind"] == "branch":
        payload["status"] = "waiting_selection"
        payload["prompt"] = ask["prompt"]
        payload["options"] = ask["options"]
        payload["how_to_unblock"] = (
            f"a human must choose with 'claunch cflow select <option>' (inside "
            f"a chat session: '! claunch cflow select <option>'{_t_hint()}) or "
            f"an option button on the daemon web dashboard. "
            + _asking_well(
                "Name the decision in one line, then say which option you "
                "recommend."
            )
        )
    else:
        payload["status"] = "waiting_approval"
        payload["reason"] = "ask"
        payload["gate"] = ask["prompt"]
        payload["how_to_unblock"] = (
            f"a human must approve: 'claunch cflow approve' (inside a chat "
            f"session: '! claunch cflow approve'{_t_hint()}) or the Approve "
            f"button on the daemon web dashboard; the agent cannot approve. "
            + _asking_well(
                "Say plainly what you are asking them to approve, and show "
                "the work it would be approved on."
            )
        )
    if ask.get("deferred"):
        payload["note"] = (
            f"this is meant for another agent and the mesh roster could not be "
            f"read to find them ({ask['deferred'].get('reason') or 'unknown'}) "
            f"— the daemon retries shortly (attempt "
            f"{ask['deferred'].get('count')} of {MAX_ROSTER_DEFERRALS}), and "
            f"after that it falls to a human. Say so in one line and wait; a "
            f"person can still settle it now if they want to."
        )
    elif unresolved:
        payload["note"] = (
            "this was meant to be answered by another agent, but no candidate "
            "could be reached — it is in front of a human instead. Tell the "
            "user that, and why (see ask.skipped)."
        )
    return payload


def _settle_timers(workflow: Workflow, state: dict, target: Optional[str], cwd) -> None:
    """Leave a timed wait when the run moves away from it.

    ``target != current_step.timer.then`` means the loop is over: the
    agent's early ``next``, the budget's ``after``, a human's ``goto``, or
    termination — the arm AND the fire count die with it. Moving to ``then``
    — a fire — is the one move that keeps the budget alive: the arm dies
    with the step it waited on, but ``timer_fires`` survives the round trip
    so the return to the timer step re-arms with the count intact.
    """
    current = state.get("current")
    if not current:
        return
    step = workflow.steps[current]
    if step.timer is None:
        return
    if target == step.timer.then:
        state.pop("timer_armed", None)
        return
    state.pop("timer_armed", None)
    state.pop("timer_fires", None)


def _arrive_timer(
    workflow: Workflow, state: dict, target: str, from_step: Optional[str], cwd
) -> None:
    """Arm (or re-arm) a timed wait when the run arrives at its step.

    The fire count carries across the ``then`` round trip — the budget is
    the number of polls per round, not the number of visits — and resets
    when the step is entered from anywhere else. Expects ``state["current"]``
    and ``visits`` already set for ``target``.
    """
    step = workflow.steps[target]
    timer = step.timer
    if timer is None:
        return
    # Carried only when this arrival is the RETURN FROM A FIRE: the arrival
    # follows the run having sat at the fire's `then` AND a fire having
    # recorded a count for this step. The first arrival (the agent's own
    # poll -> wait) is a fresh arm. `from_step == timer.then` alone is not
    # enough — poll is also the timer's `then`.
    carried = from_step == timer.then and target in (state.get("timer_fires") or {})
    fires = int((state.get("timer_fires") or {}).get(target, 0)) if carried else 0
    fires_by_step = dict(state.get("timer_fires") or {})
    fires_by_step[target] = fires
    state["timer_fires"] = fires_by_step
    opens = _utc_now() + timedelta(seconds=float(timer.every))
    state["timer_armed"] = {
        "step": target,
        "visit": _visits(state, target),
        "fires": fires,
        "opens_at": _iso(opens),
    }
    state_mod.journal(
        "timer_re_armed" if carried else "timer_armed",
        {
            "run": state["run_id"],
            "step": target,
            "visit": _visits(state, target),
            "fires": fires,
            "max": timer.max,
            "opens_at": _iso(opens),
            "carried": carried,
        },
        cwd,
    )


def _move_to(workflow: Workflow, state: dict, target: Optional[str], cwd) -> None:
    """Advance to ``target`` (None = termination)."""
    # An ask still open here is one nobody answered — a human forced the run
    # somewhere else while it was out. Say so in the journal rather than
    # letting the question evaporate: a responder about to answer it is about
    # to be told it is closed, and this is the entry that explains why.
    open_ask = state.get("ask")
    if open_ask:
        state_mod.journal(
            "ask_discarded",
            {
                "run": state["run_id"],
                "ask": open_ask.get("id"),
                "step": open_ask.get("step"),
                "reason": "the run moved before it was answered",
            },
            cwd,
        )
    held = state.get("window")
    if held:
        # Same story as the ask above: a human moved the run while a choice
        # was parked here waiting for its window.
        state_mod.journal(
            "window_discarded",
            {"run": state["run_id"], "step": held.get("step"),
             "option": held.get("option"),
             "reason": "the run moved before the window opened"},
            cwd,
        )
    state["delivered"] = False
    state["gate_approved"] = False
    state["gate_logged"] = None
    state["pending_select"] = None
    state["window"] = None
    state["ask"] = None
    state["declined"] = None
    state["unanswered"] = None
    state["report"] = None
    # Measurements belong to the position that was being measured. They are
    # keyed on (step, visit) as well, so this is tidiness rather than
    # correctness — but a stale record in the file reads as a live one to
    # anyone opening it.
    state["checklist"] = None
    state["checklist_opened"] = None
    from_step = state.get("current")
    _settle_timers(workflow, state, target, cwd)
    if target is None:
        # BEFORE the run is marked done, and deliberately: `status == "done"`
        # with no pending start is the daemon's kill-on-end condition, and it
        # is sampled by a clock that takes no lock against this function. The
        # escalation's own work — a workflow file read, and two daemon round
        # trips for the role and the issue — is exactly the window in which
        # that sample would find a finished run with nothing pending and
        # start ending the session. It would not be undone by the request
        # arriving a moment later: the clock latches the run in `_end_done`
        # and its kill task rechecks only `session.exited` and `keep_alive`,
        # never the pending start again. `recur` sits after the save and is
        # safe there because the clock has a second, independent guard for it
        # (`not payload.get("recur")`); an escalation has no such guard, so
        # the ONLY thing standing between it and a killed session is that its
        # request is already on disk when `done` becomes visible.
        #
        # An escalation declared on the step the run just left wins over
        # `recur`: the workflow-wide "this run happens again" is the default,
        # and a particular ending saying "the work continues under other
        # rules" is the more specific statement about THIS ending. When the
        # escalation is declined (an unresolvable target, or one whose
        # filter_roles turns this session away) the run falls back to the
        # ordinary ending, which for a recurring workflow is its next round —
        # a declined hand-off must not also silence a service loop.
        ended_at = workflow.steps.get(from_step) if from_step else None
        escalated = False
        if ended_at is not None and ended_at.escalate is not None:
            escalated = _request_escalation(ended_at, state, cwd)
        state["current"] = None
        state["status"] = "done"
        state_mod.save_state(state, cwd)
        state_mod.journal("done", {"run": state["run_id"]}, cwd)
        if workflow.recur and not escalated:
            _request_next_round(workflow, state, cwd)
        return
    state["current"] = target
    state["visits"][target] = _visits(state, target) + 1
    _arrive_timer(workflow, state, target, from_step, cwd)
    state_mod.save_state(state, cwd)


def _request_next_round(workflow: Workflow, state: dict, cwd) -> None:
    """A recurring run's normal end files the start request for its round + 1.

    Recurrence is a property of the run's LIFECYCLE, not of the graph: every
    round still reaches a real termination, and what loops is that finishing
    asks — through the same request channel a human uses — for the workflow
    to be started again. The driving agent performs that start itself, exactly
    as it does for a human's request, so the single-writer rule holds and each
    round gets its own run, visit counters and journal. A human stops the loop
    between rounds by withdrawing the request ('claunch cflow request
    --cancel', or the dashboard) and mid-round by aborting or archiving: an
    aborted run never comes through here, which is precisely what makes those
    the off switch.

    ``workflow.recur_auto`` (``recur: {auto: true}``) stamps the request as
    the DAEMON's to fulfil: the machine that reads it is
    :func:`auto_start_next_round`, not the driving agent — see that function
    for why the single-writer rule stays intact there.
    """
    if state_mod.read_request(cwd):
        # Somebody asked for something while this round was finishing. Their
        # request outranks the loop's own — recurrence must never overwrite a
        # person's declared intent.
        return
    source = str(state.get("source") or "")
    if not source:
        return  # a legacy run with no recorded source cannot restart itself
    request = {
        "id": f"req-{secrets.token_hex(3)}",
        # The snapshot's own file, not the name: a name can resolve to a
        # different layer between rounds, and the loop that was started is
        # the one that should keep running. Edits to that file DO take
        # effect — each round re-reads and re-snapshots it.
        "workflow": source,
        "name": state["workflow"],
        "resolved": source,
        "context": str(state.get("context") or ""),
        "by": "recur",
        "round": int(state.get("round") or 1) + 1,
        # ``auto`` — the daemon starts this round (recur: {auto: true}).
        # ``mesh`` — carried so the auto-start hands the delegation lookup
        # the same mesh the loop started with.
        "auto": bool(workflow.recur_auto),
        "mesh": str(state.get("mesh") or ""),
        "at": state_mod.utcnow(),
    }
    state_mod.write_request(request, cwd)
    state_mod.journal(
        "recur_requested",
        {"run": state["run_id"], "request": request["id"], "round": request["round"]},
        cwd,
    )


#: Ceiling on the escalation's daemon lookup. It runs while the run's slot
#: lock is held, like the other daemon calls this package makes, so it is
#: bounded well under the lock's own patience.
_ISSUE_LOOKUP_TIMEOUT = 5.0


def _session_issue(cwd: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """The beads issue this run's session is for, and why it is unknown.

    Read from the daemon's own session record (``GET /api/sessions/{name}``,
    which serves ``SessionDef.issue``) rather than parsed out of prose. The
    field is set when the session is created — from the ``issue: <id>`` the
    request named, or from the issue the daemon minted for the task — so it
    holds whether or not any agent remembered to write the id into a report.
    A run whose id came from a report would lose it exactly in the rounds
    where the reporting was thin, and lose it silently.

    Never raises: the second element says why the answer is ``None``, so a
    hand-off with no issue on it records the reason instead of looking like a
    session that simply had no issue.
    """
    session = state_mod.current_scope()
    if not session or session == state_mod.DEFAULT_SCOPE:
        return None, "not a managed session: it has no issue of its own"
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        return None, f"the claunch {daemon_client.unreachable_reason(why)}"
    try:
        doc = client.get(
            f"/api/sessions/{session}", timeout=_ISSUE_LOOKUP_TIMEOUT
        )
    except daemon_client.DaemonClientError as exc:
        return None, f"the daemon did not answer: {exc}"
    if not isinstance(doc, dict):
        return None, "the daemon's session record was unreadable"
    issue = str(doc.get("issue") or "").strip()
    if not issue:
        return None, None  # answered, and the session has no issue
    return issue, None


def _request_escalation(step: Step, state: dict, cwd: Optional[str]) -> bool:
    """File the start request a finishing step's ``escalate`` declares.

    Returns whether the hand-off was actually filed. The whole point of the
    request is what it does to the ending: a pending start spares the session
    from the daemon's kill-on-end, and the finished run's payload names the
    start its driver must perform. So the checks run HERE, on this side of the
    ending, where declining costs nothing but a journal line — declining at
    start time is far more expensive, because ``_start_impl`` archives the old
    run BEFORE it holds the new one against ``filter_roles``, leaving the
    session holding an unfulfillable request with no active run at all.

    **Called before the run is saved as done, and it must stay that way.**
    Everything below takes real time — a workflow file read and two daemon
    round trips — and for all of it the run must not yet look finished to the
    kill-on-end clock, which samples the state without taking this lock and
    does not look again. See the comment at the call site in ``_advance``.

    A request already waiting in the slot is left alone and the escalation
    stands down: a person's declared intent outranks a step's declaration, and
    the run ends the ordinary way.

    The role check has three outcomes and they are not interchangeable:
    approved (escalate), refused (do NOT escalate — the run ends the ordinary
    way), and unenforceable, which is what a session with no mesh identity
    gets. Unenforceable proceeds — the filter guards a fleet's division of
    labour and a standalone run has no fleet — but it is recorded as its own
    answer rather than folded into "approved".
    """
    escalate = step.escalate
    assert escalate is not None  # only called for steps that declare one
    run_id = state["run_id"]
    # The mesh the run was started with, not the empty default: an empty mesh
    # reads as "any mesh this session is in", which does hold for a session
    # that belongs to one mesh but goes unenforced for a session in several.
    mesh = str(state.get("mesh") or "")

    pending = state_mod.read_request(cwd)
    if pending:
        # The slot holds one request. Somebody asked for something while this
        # run was finishing, and that request outranks the step's declaration
        # for the same reason recurrence yields to it in
        # `_request_next_round` — except the stake is higher here: `recur`
        # merely postpones the same workflow's next round, while an escalation
        # changes the rules the session works under, so overwriting one would
        # wake the session up under B when a person asked for A.
        #
        # Recorded rather than silent, and under a name of its own. A declined
        # escalation (`escalate_declined`) says the hand-off could not be made;
        # this one says it COULD and was stood down. The distinction matters
        # after the fact because a step's escalation fires at one particular
        # ending: unlike a recurring round it does not come around again, so
        # without this line there is nothing to explain why a declared hand-off
        # never happened. The pending request's `by` is carried so the reader
        # can tell a person's intent from this run's own earlier escalation —
        # which is what a forced `goto` reopening a finished run and ending it
        # at the same step produces, and what keeps that from firing twice.
        state_mod.journal(
            "escalate_skipped",
            {"run": run_id, "step": step.id, "workflow": escalate.workflow,
             "reason": "a start request was already pending",
             "pending": pending.get("id"),
             "pending_by": pending.get("by")},
            cwd,
        )
        return False

    try:
        composed = state_mod.load_workflow(escalate.workflow, cwd)
    except Exception as exc:  # unresolvable name, unreadable or invalid file
        state_mod.journal(
            "escalate_declined",
            {"run": run_id, "step": step.id, "workflow": escalate.workflow,
             "reason": "the target workflow could not be loaded",
             "detail": str(exc)},
            cwd,
        )
        return False

    target = composed.workflow
    try:
        filter_note = _enforce_role_filter(target, mesh=mesh, cwd=cwd)
    except CflowError as exc:
        state_mod.journal(
            "escalate_declined",
            {"run": run_id, "step": step.id, "workflow": escalate.workflow,
             "reason": "the target workflow's filter_roles turns this "
                       "session away",
             "detail": str(exc)},
            cwd,
        )
        return False

    issue, issue_problem = _session_issue(cwd)
    # Carried, not rewritten: `recur` hands the finishing run's own context to
    # its next round, and an escalated run needs it for the same reason — it
    # continues the same work. The issue line goes FIRST and is the piece that
    # makes the hand-off findable: two runs, two journals, one board record.
    parts: List[str] = []
    if issue:
        parts.append(f"issue: {issue}")
    if escalate.context:
        parts.append(escalate.context)
    own = str(state.get("context") or "").strip()
    if own:
        parts.append(own)

    request = {
        "id": f"req-{secrets.token_hex(3)}",
        "workflow": escalate.workflow,
        "name": target.name,
        "resolved": str(composed.path),
        "context": "\n\n".join(parts),
        # A `by` of its own, so the journal says what happened. It also keeps
        # the escalation out of the daemon's auto-start path, which admits
        # `by: recur` only — an escalation changes which rules the session
        # works under, and the driver performs that start itself.
        "by": "escalate",
        "mesh": mesh,
        "from_run": run_id,
        "from_workflow": str(state.get("workflow") or ""),
        "from_step": step.id,
        **({"issue": issue} if issue else {}),
        **({"role_filter": filter_note} if filter_note else {}),
        "at": state_mod.utcnow(),
    }
    state_mod.write_request(request, cwd)
    state_mod.journal(
        "escalate_requested",
        {"run": run_id, "step": step.id, "request": request["id"],
         "workflow": target.name,
         # All three recorded, and each says a different thing: the id that
         # was carried, the reason there is none, and the fact that the role
         # check could not be made. A hand-off missing its issue must not read
         # like a session that had none.
         "issue": issue,
         **({"issue_problem": issue_problem} if issue_problem else {}),
         **({"role_filter": filter_note} if filter_note else {})},
        cwd,
    )
    return True


def _done_payload(state: dict, cwd: Optional[str]) -> dict:
    entries = state_mod.read_journal(cwd, run_id=state["run_id"])
    summaries = [
        {"step": e.get("step"), "summary": e.get("summary"), "details": e.get("details")}
        for e in entries
        if e.get("event") == "step_completed"
    ]
    payload = {
        **_base(state),
        "status": state["status"],  # done | aborted
        "journal": summaries,
        # Every tool call on a finished run funnels through here, so this is
        # where the run says so itself: an agent handed a new task after
        # 'done' must not stretch the closed run to cover it, and must not
        # start a new one over the user's head. The skill states the same
        # rule ("a new task is a new run"), but a skill can fall out of a
        # long context — the payload cannot.
        "note": (
            "workflow finished; report the journal to the user. This run is "
            "closed — do not keep working under it. A NEW task needs a NEW "
            "run: confirm the workflow with the user, then call 'start' "
            "(finished runs are archived automatically, journal kept)"
        ),
    }
    pending = state_mod.read_request(cwd)
    if pending:
        payload["pending_start"] = pending
        if pending.get("by") == "recur":
            if pending.get("auto"):
                # The daemon performs this loop's next start (see
                # auto_start_next_round): the agent pointed at it would race
                # the clock. Ending the turn is the whole instruction.
                payload["note"] = (
                    "round finished; this workflow recurs and the daemon "
                    "starts the next round itself — report this round's "
                    "journal to the user and END your turn here. Do not call "
                    "'start': the requested next round begins on its own. "
                    "Only a human stops the loop ('claunch cflow request "
                    "--cancel', or aborting/archiving the run)"
                )
            else:
                payload["note"] = (
                    "round finished; this workflow recurs — report this "
                    "round's journal to the user, then start the next round: "
                    "call 'start' with exactly the requested workflow and "
                    "context. Do not invent a reason to stop: only a human "
                    "ends the loop ('claunch cflow request --cancel', or "
                    "archiving the run)"
                )
        elif pending.get("by") == "escalate":
            # The run declared its own hand-off: the step it ended on named
            # the workflow that takes the slot over. Spelled out rather than
            # folded into the human-request note below, because nobody asked
            # for this one — the driver would otherwise have to work out from
            # a bare request why a finished run is not finished.
            payload["note"] = (
                "workflow finished, and the step it ended on escalates to "
                f"{pending.get('name') or pending.get('workflow')!r} (see "
                "pending_start) — report this run's journal to the user, "
                "then start the requested workflow with exactly the requested "
                "context. That context carries this run's issue, which is "
                "what keeps the two runs one piece of work"
            )
        else:
            # A human already asked for the next run; telling the agent to go
            # ask again would bounce the request back at its own author.
            payload["note"] = (
                "workflow finished, and a start was requested here (see "
                "pending_start) — report this run's journal to the user, "
                "then perform the requested start per the protocol"
            )
    return payload


#: Re-exported so a cflow caller need not reach past its own package for the
#: id space it shares with every other block of prompt text (:mod:`digests`).
DIGEST_CHARS = digests.DIGEST_CHARS


def step_digest(payload: dict) -> str:
    """A stable id for the *instructional content* of one position.

    The id a reminder quotes instead of re-pasting the step, and the id the
    ``recall`` tool takes. Two rules make it work, and both are constraints
    rather than choices:

    **Content only, never framing.** The rendered block carries an interval
    ("every 300s") and a staleness ("unmoved for ~25 min"), and both move on
    every fire. Hashing the rendered text would hand out a different id each
    time, which is precisely the opposite of the point. So this hashes what
    the position *says* and nothing about when it was said.

    **Whatever the agent was actually given.** A step's text is its
    instructions, its completion test and its verify; a branch choice's text
    is the prompt and the options, because that is what the chooser reads.
    Hashing a step's absent instructions for a select would give every
    select at a workflow the same id.

    Returns ``""`` for a position with no instructional content to name — a
    gate, a done run — where there is nothing for an agent to have lost.

    Note that this is stable for the life of a position but NOT a global
    name for a step: the workflow is snapshotted into run state at start, so
    editing the file cannot change a running position's text, and a revisit
    bumps ``visit``, which the reminder treats as a new position anyway.
    """
    parts: List[str] = []
    if payload.get("status") == "select":
        parts.append(str(payload.get("prompt") or ""))
        for opt in payload.get("options") or []:
            parts.append(f"{opt.get('name')}\x1f{opt.get('description')}")
    else:
        parts.append(str(payload.get("instructions") or ""))
        parts.append(str(payload.get("done_when") or ""))
        parts.append(str(payload.get("verify") or ""))
        if not any(p.strip() for p in parts):
            # No instructions of its own: an ask whose step body is withheld
            # behind an approval still has a question, and that question is
            # the thing an agent can lose.
            parts = [str(payload.get("prompt") or "")]
    if not any(p.strip() for p in parts):
        return ""
    return digests.text_digest("\x1e".join(parts))


# --------------------------------------------------------------------------- #
# checklist: a gate written as machine-checked items, moved by the daemon
# --------------------------------------------------------------------------- #
def _checklist_record(state: dict, step: Step) -> dict:
    """The stored measurements for this step AND visit, or an empty record.

    Keyed on the visit for the same reason a gate approval is: a run that
    loops back here is asking the question again, and last visit's green
    items are not an answer to it.
    """
    record = state.get("checklist") or {}
    if (
        record.get("step") == step.id
        and record.get("visit") == _visits(state, step.id)
    ):
        return record
    return {}


def _checklist_items(step: Step, record: dict) -> List[dict]:
    """Every declared item with whatever is known about it right now.

    ``ok`` has three values and the third is the load-bearing one: ``True``
    (exit 0), ``False`` (any other code), and ``None`` — not measured yet, or
    measured and *unmeasurable* (the command could not be launched, or timed
    out). Unknown is never true, so a checklist cannot be opened by a broken
    command any more than by a failing one.
    """
    measured = (record or {}).get("items") or {}
    items: List[dict] = []
    for item in step.checklist.items:
        seen = measured.get(item.id) or {}
        items.append(
            {
                "id": item.id,
                "describe": item.describe,
                "check": item.check,
                "ok": seen.get("ok"),
                "exit_code": seen.get("code"),
                "output": seen.get("says"),
                "measured_at": seen.get("at"),
            }
        )
    return items


def _checklist_green(items: Sequence[dict]) -> bool:
    return bool(items) and all(entry.get("ok") is True for entry in items)


def _checklist_opened(state: dict, step: Step) -> Optional[dict]:
    """When this visit's gate was first presented, or None if not yet.

    Written at presentation (the same moment ``checklist_presented`` is
    journalled) and keyed on (step, visit) like the measurements: an expiry
    counts from when THIS visit started waiting, not from a previous one.
    """
    opened = state.get("checklist_opened") or {}
    if (
        opened.get("step") == step.id
        and opened.get("visit") == _visits(state, step.id)
        and opened.get("at")
    ):
        return opened
    return None


def _checklist_expires_at(state: dict, step: Step) -> Optional[datetime]:
    """The instant this visit's gate expires, or None (no edge, or not open yet)."""
    otherwise = step.checklist.otherwise
    opened = _checklist_opened(state, step)
    if otherwise is None or opened is None:
        return None
    since = _parse_at(opened.get("at"))
    if since is None:
        return None
    return since + timedelta(seconds=otherwise.after)


def _checklist_payload(state: dict, step: Step) -> dict:
    """The structured checklist a payload carries: what is true, and what is not."""
    record = _checklist_record(state, step)
    items = _checklist_items(step, record)
    payload = {
        "prompt": step.checklist.prompt,
        "then": step.checklist.then,
        "poll": step.checklist.poll,
        "items": items,
        "passed": sum(1 for entry in items if entry.get("ok") is True),
        "total": len(items),
        "all_true": _checklist_green(items),
        "report_filed": _current_report(state, step.id) is not None,
        "checked_at": record.get("checked_at"),
    }
    otherwise = step.checklist.otherwise
    if otherwise is not None:
        expires = _checklist_expires_at(state, step)
        payload["otherwise"] = {
            "after": otherwise.after,
            "then": otherwise.then,
            "expires_at": _iso(expires) if expires is not None else None,
        }
    return payload


def _restart_payload(state: dict, step: Step) -> Optional[dict]:
    """The current visit's external-restart receipt, if it declares one."""
    if step.restart is None:
        return None
    visit = _visits(state, step.id)
    record = state.get("restart")
    if not isinstance(record, dict) or record.get("step") != step.id or record.get("visit") != visit:
        record = {}
    return {
        "windows": bool(step.restart.windows),
        "linux": bool(step.restart.linux),
        "timeout": step.restart.timeout,
        "status": record.get("status", "pending"),
        "requested_at": record.get("requested_at"),
        "completed_at": record.get("completed_at"),
        "exit_code": record.get("exit_code"),
        "output": record.get("output"),
    }


def _payload(workflow: Workflow, state: dict, cwd: Optional[str], *, mutate: bool) -> dict:
    """Describe the current position, with the content id every reader needs.

    The id is stamped here rather than at the callers because *every* door
    that hands an agent a position has to carry it: the agent that receives
    a step from ``next`` and the one that is reminded of it later must be
    able to see that they are the same text. A door that served the body
    without the id would give the agent no way to answer the one question
    the whole scheme rests on — "do I already have this?"
    """
    payload = _position_payload(workflow, state, cwd, mutate=mutate)
    digest = step_digest(payload)
    if digest:
        payload["digest"] = digest
    return payload


def _position_payload(
    workflow: Workflow, state: dict, cwd: Optional[str], *, mutate: bool
) -> dict:
    """Describe the current position; with ``mutate`` also mark delivery."""
    if state["status"] in ("done", "aborted"):
        return _done_payload(state, cwd)
    step = workflow.step(state["current"])
    visit = _visits(state, step.id)
    base = {
        **_base(state),
        "step_id": step.id,
        "title": step.title or step.id,
        "visit": visit,
    }
    restart = _restart_payload(state, step)
    if restart is not None:
        base["restart"] = restart
    awaited = _awaits_payload(step)
    if awaited:
        # On `base`, so it rides every payload this position can produce. The
        # daemon's reminder clock reads runs through `status` and nothing
        # else, so this dict is the entire interface between a workflow's
        # `awaits` and the thing that acts on it — the clock never opens a
        # workflow file, and never learns that `awaits: verify` was a spelling
        # rather than a command.
        base["awaits"] = awaited

    # Loop guard first: arriving past the visit limit pauses the run.
    if visit > _limit(workflow, state, step.id):
        payload = {
            **base,
            "status": "waiting_approval",
            "reason": "loop_limit",
            "gate": (
                f"loop guard: step {step.id!r} has been visited {visit} times "
                f"(limit {_limit(workflow, state, step.id)})"
            ),
            "how_to_unblock": (
                f"a human must extend the loop limit: 'claunch cflow approve' "
                f"(inside a chat session: '! claunch cflow approve'{_t_hint()}) "
                f"or the Approve button on the daemon web dashboard. "
                + _asking_well(
                    "Explain why the loop keeps repeating and what another "
                    "pass would do differently -- whether anything will "
                    "change is the thing they are actually weighing."
                )
            ),
        }
        if mutate and state.get("gate_logged") != f"loop:{step.id}:{visit}":
            state["gate_logged"] = f"loop:{step.id}:{visit}"
            state_mod.journal(
                "loop_limit", {"run": state["run_id"], "step": step.id, "visit": visit}, cwd
            )
            state_mod.save_state(state, cwd)
        return payload

    # A refusal with nowhere declared to go. Checked BEFORE the entry
    # approvals below, or the ask that was just declined would be re-opened
    # and put to the same responder in a loop.
    declined = state.get("declined")
    if declined and declined.get("step") == step.id and declined.get("visit") == visit:
        return {
            **base,
            "status": "waiting_approval",
            "reason": "declined",
            "gate": (
                f"{declined.get('by') or 'a responder'} declined this step"
                + (f": {declined['reason']}" if declined.get("reason") else "")
            ),
            "declined": declined,
            "how_to_unblock": (
                f"the workflow declares no route for a decline, so the run is "
                f"held. A human decides what happens: 'claunch cflow approve' "
                f"to override and enter anyway, or 'claunch cflow goto <step>' "
                f"to send the run somewhere else{_t_hint()}. "
                + _asking_well(
                    "Relay the refusal and its stated reason first, in the "
                    "responder's own words and ahead of any answer of your "
                    "own -- what is being weighed is whether to overrule a "
                    "colleague who looked at this."
                )
            ),
        }

    # Human entry gate: re-required on every visit.
    if step.gate and not state["gate_approved"]:
        payload = {
            **base,
            "status": "waiting_approval",
            "reason": "gate",
            "gate": step.gate,
            "how_to_unblock": (
                f"a human must approve: 'claunch cflow approve' (inside a chat "
                f"session: '! claunch cflow approve'{_t_hint()}) or the "
                f"Approve button on the daemon web dashboard; the agent "
                f"cannot approve. "
                + _asking_well(
                    "Say plainly what entering this step will do, and show "
                    "the work the gate is standing in front of."
                )
            ),
        }
        if mutate and state.get("gate_logged") != f"gate:{step.id}:{visit}":
            state["gate_logged"] = f"gate:{step.id}:{visit}"
            state_mod.journal(
                "gate_wait", {"run": state["run_id"], "step": step.id, "visit": visit}, cwd
            )
            state_mod.save_state(state, cwd)
        return payload

    # Delegated entry approval — the same per-visit gate, put to somebody.
    if step.ask and not state["gate_approved"]:
        ask = _current_ask(state, step.id, visit, "approval")
        if ask is None:
            if not mutate:
                # A read-only look between arriving here and the agent's next
                # call. Describe the position honestly rather than opening a
                # question as a side effect of somebody watching. Honestly
                # includes the gate-shaped ask: with nobody to route to it is
                # a person's approval already, not a question awaiting its
                # routing (see `_unopened_human_gate`).
                gate = _unopened_human_gate(
                    base, step.ask.delegate, kind="approval",
                    prompt=step.ask.prompt,
                )
                if gate is not None:
                    return gate
                return {
                    **base,
                    "status": "waiting_answer",
                    "reason": "approval",
                    "prompt": step.ask.prompt,
                    "user_door": _user_door("approval"),
                    "note": "this approval has not been put to anyone yet",
                }
            ask = _open_ask(
                workflow,
                state,
                step,
                kind="approval",
                prompt=step.ask.prompt,
                options=APPROVAL_OPTIONS,
                delegate=step.ask.delegate,
                cwd=cwd,
            )
        if ask is not None:
            return _ask_payload(base, ask)
        # `otherwise: self`: nobody could be asked and the workflow says to go
        # on regardless. The gate is open (unapproved, and journaled as such),
        # so fall through to the step itself.

    # A timed wait: the run sits here and the DAEMON moves it (see
    # `fire_timer`), so the position reads as waiting, never as work to do.
    # The `fires`/`opens_at` fields are the clock's contract with the agent —
    # what is scheduled, not what is asked of it.
    if step.timer is not None:
        armed = state.get("timer_armed") or {}
        armed_here = armed.get("step") == step.id
        fires = int(armed.get("fires") or 0) if armed_here else 0
        opens = armed.get("opens_at") if armed_here else None
        payload = {
            **base,
            "status": "waiting_timer",
            "instructions": step.instructions,
            "fires": fires,
            "max": step.timer.max,
            "then": step.timer.then,
            "after": step.timer.after,
            "opens_at": opens,
            "note": (
                f"this step is a timed wait: the daemon moves the run to "
                f"'{step.timer.then}' when the timer fires (next fire at "
                f"{opens or 'arming'}), at most {step.timer.max} times this "
                f"round, then to '{step.timer.after}'. End your turn — do "
                f"not poll by hand, and do not read silence as the timer "
                f"having stopped. To close the round early, file a one-line "
                f"'report' and call 'next'"
            ),
        }
        if mutate and not state["delivered"]:
            state["delivered"] = True
            state_mod.journal(
                "timer_presented",
                {"run": state["run_id"], "step": step.id, "visit": visit},
                cwd,
            )
            state_mod.save_state(state, cwd)
        return payload

    # A checklist gate: the run sits here and the DAEMON moves it once every
    # item measures true (see `check_checklist`). Like a timed wait this is a
    # waiting position, never work to do — but unlike one it has no schedule
    # to state, so what the payload carries is the list itself.
    if step.checklist is not None:
        checklist = _checklist_payload(state, step)
        payload = {
            **base,
            "status": "waiting_checklist",
            "instructions": step.instructions,
            "checklist": checklist,
        }
        if step.done_when:
            payload["done_when"] = step.done_when
        outstanding = [
            entry["describe"]
            for entry in checklist["items"]
            if entry.get("ok") is not True
        ]
        if not checklist["all_true"]:
            payload["note"] = (
                f"this step is a checklist gate: "
                f"{checklist['passed']}/{checklist['total']} items are true, "
                f"and the run leaves for '{step.checklist.then}' only when "
                f"all of them are. The daemon re-measures every "
                f"{int(step.checklist.poll)}s and moves the run itself — end "
                f"your turn, do not poll by hand, and do not try to advance "
                f"with 'next' (there is no agent exit from here). File this "
                f"step's 'report' so the move is not held up on it. Still "
                f"false: " + "; ".join(outstanding)
            )
            expiry = checklist.get("otherwise") or {}
            if expiry.get("expires_at"):
                payload["note"] += (
                    f". This wait is bounded: if the list is still not all "
                    f"true at {expiry['expires_at']} the daemon moves the run "
                    f"to '{expiry['then']}' instead (journalled as "
                    f"'checklist_expired')"
                )
        elif not checklist["report_filed"]:
            payload["note"] = (
                f"every checklist item is true; the move to "
                f"'{step.checklist.then}' is waiting on this step's report. "
                f"File it with 'report' {{summary, details?}} and the daemon "
                f"moves the run — a report does not open the gate, it only "
                f"stops being what holds it"
            )
        else:
            payload["note"] = (
                f"every checklist item is true and the report is filed — the "
                f"daemon moves this run to '{step.checklist.then}' on its "
                f"next pass. End your turn"
            )
        if mutate and not state["delivered"]:
            state["delivered"] = True
            # The expiry clock (`checklist.otherwise`) starts here, at the
            # first presentation of this visit — the moment the run began
            # waiting, which is also the moment a `restart:` is claimed.
            state["checklist_opened"] = {
                "step": step.id,
                "visit": visit,
                "at": _iso(_utc_now()),
            }
            state_mod.journal(
                "checklist_presented",
                {
                    "run": state["run_id"],
                    "step": step.id,
                    "visit": visit,
                    "items": [entry.id for entry in step.checklist.items],
                },
                cwd,
            )
            state_mod.save_state(state, cwd)
        return payload

    if step.is_select:
        pending = state.get("pending_select")
        options = [
            {"name": o.name, "description": o.description}
            for o in step.select.options.values()
        ]
        # Who is choosing *now*: a delegated select whose candidates ran out
        # under `otherwise: self` is an agent-chooses select from here on, and
        # must read as one everywhere — including to `select`, which refuses
        # an agent's choice on a decision that is somebody else's.
        chooser = _live_chooser(state, step)
        if chooser == "delegate":
            ask = _current_ask(state, step.id, visit, "branch")
            if ask is None:
                if not mutate:
                    # Same read-only honesty as the entry approval above: a
                    # chooser with nobody to delegate to is the user's
                    # selection from the start.
                    gate = _unopened_human_gate(
                        base, step.select.delegate, kind="branch",
                        prompt=step.select.prompt, options=options,
                    )
                    if gate is not None:
                        return gate
                    return {
                        **base,
                        "status": "waiting_answer",
                        "reason": "branch",
                        "prompt": step.select.prompt,
                        "options": options,
                        "user_door": _user_door("branch", options),
                        "note": "this decision has not been put to anyone yet",
                    }
                ask = _open_ask(
                    workflow,
                    state,
                    step,
                    kind="branch",
                    prompt=step.select.prompt,
                    options=options,
                    delegate=step.select.delegate,
                    cwd=cwd,
                )
            if ask is not None:
                return _ask_payload(base, ask)
            if state["current"] != step.id or state["status"] in ("done", "aborted"):
                # `otherwise: self:<option>`: nobody could be asked, and the
                # workflow's declared default was just taken unanswered — the
                # run is on another step (or done). Describe THAT position;
                # presenting the select it left behind would ask the driver
                # to re-decide a decision the journal says is made.
                return _payload(workflow, state, cwd, mutate=True)
            chooser = "agent"  # nobody to ask; this run decides it after all
        held = _current_window(state, step.id, visit)
        if held:
            now = _utc_now()
            if mutate and _window_due(held, now):
                # The no-daemon path (and the daemon's own, through
                # `release_window`): the window has opened, so the held choice
                # is confirmed here and the run reads from its new position.
                _release(workflow, state, step, cwd, now)
                return _payload(workflow, state, cwd, mutate=True)
            return _window_payload(base, step, held, now)
        if pending and pending.get("step") == step.id:
            return {
                **base,
                "status": "waiting_selection",
                "prompt": step.select.prompt,
                "options": options,
                "proposal": pending,
                "how_to_unblock": (
                    f"a human must confirm with 'claunch cflow select "
                    f"<option>' (inside a chat session: '! claunch cflow "
                    f"select <option>'{_t_hint()}) or an option button on the "
                    f"daemon web dashboard. "
                    + _asking_well(
                        "Name the decision in one line, then say which option "
                        "you recommend -- what you called is recorded as a "
                        "proposal, not taken."
                    )
                ),
            }
        # A decision the USER owns is "your move" from the moment it is
        # presented — there is no pre-proposal phase in which the run reads as
        # still running. One state (waiting_selection) carries the whole
        # decision; the driver's recommendation (pending above) lands in it as
        # an annotation, not as a phase change. The engine still has to tell
        # the driver to record one.
        if chooser == "user":
            if mutate and not state["delivered"]:
                state["delivered"] = True
                state_mod.journal(
                    "select_presented",
                    {"run": state["run_id"], "step": step.id, "visit": visit},
                    cwd,
                )
                state_mod.save_state(state, cwd)
            return {
                **base,
                "status": "waiting_selection",
                "prompt": step.select.prompt,
                "options": options,
                "how_to_unblock": (
                    f"a human must confirm with 'claunch cflow select "
                    f"<option>' (inside a chat session: '! claunch cflow "
                    f"select <option>'{_t_hint()}) or an option button on the "
                    f"daemon web dashboard."
                ),
                "note": (
                    "this decision is the user's, open from the moment it is "
                    "presented — record your recommendation with the 'select' "
                    "tool ({option, reason}) and the run stays here for the "
                    "user to confirm"
                ),
            }
        payload = {
            **base,
            "status": "select",
            "prompt": step.select.prompt,
            "chooser": chooser,
            "options": options,
            "note": (
                "decide and call the 'select' tool with {option, reason}"
                if chooser == "agent"
                else "call 'select' once with your recommendation; a human then "
                "confirms out-of-band ('claunch cflow select <option>')"
            ),
        }
        if chooser != step.select.chooser:
            payload["note"] += (
                ". This was meant to be somebody else's decision: nobody could "
                "be reached, and the workflow says to proceed anyway — say so "
                "when you report it (see the run's journal for who was missed)"
            )
        if mutate and not state["delivered"]:
            state["delivered"] = True
            state_mod.journal(
                "select_presented",
                {"run": state["run_id"], "step": step.id, "visit": visit},
                cwd,
            )
            state_mod.save_state(state, cwd)
        return payload

    payload = {
        **base,
        "status": "step",
        "instructions": step.instructions,
        "note": (
            "do this step now, then file its outcome with 'report' "
            "{summary, details?} and advance with 'next'"
        ),
    }
    if step.done_when:
        # The declarative completion criterion — judge "may I advance?"
        # against this, not against a feeling of having done enough.
        payload["done_when"] = step.done_when
    if step.verify:
        payload["verify"] = (
            f"leaving this step runs: {step.verify.command!r} — 'next' is "
            f"refused until it exits 0"
        )
    if mutate and not state["delivered"]:
        state["delivered"] = True
        state_mod.journal(
            "step_delivered",
            {"run": state["run_id"], "step": step.id, "visit": visit},
            cwd,
        )
        state_mod.save_state(state, cwd)
    return payload


def _awaits_payload(step: Step) -> Optional[dict]:
    """A step's awaited condition, resolved, or None when it declares none.

    ``note`` is here for the agent, and says the thing the agent most needs to
    hear: the waiting is being watched, so ending the turn is correct and
    polling the condition by hand is not. That instruction is the difference
    between this field saving a session's turns and merely adding to them.
    """
    if step.awaits is None:
        return None
    command = step.awaits.command(step)
    if not command:  # parse forbids it; a hand-built Step could still do it
        return None
    out = {
        "probe": command,
        "poll": step.awaits.poll,
        "timeout": step.awaits.timeout,
        "note": (
            f"this step declares what it is waiting for, and the daemon is "
            f"re-measuring it every {step.awaits.poll:g}s. You will be told "
            f"when it changes and told nothing while it does not — so do not "
            f"poll it yourself, and do not read silence as the condition "
            f"being unmet"
        ),
    }
    if step.awaits.describe:
        out["describe"] = step.awaits.describe
    return out


def _delegations(workflow: Workflow) -> List[tuple]:
    """Every delegated decision in a workflow, in declared order.

    A step with no ``from`` is not one: it names nobody, so there is nothing to
    resolve and nothing a person could fix before the run starts.
    """
    out = []
    for step in workflow.steps.values():
        if step.ask and step.ask.delegate.candidates:
            out.append((step, "approval", step.ask.delegate))
        if step.is_select and step.select.delegate is not None:
            if step.select.delegate.candidates:
                out.append((step, "branch", step.select.delegate))
    return out


def escalation_check(
    workflow: Workflow, *, mesh: str = "", cwd: Optional[str] = None
) -> Optional[dict]:
    """What this workflow's ``escalate`` declarations resolve to right now.

    Reported wherever a declaration is READ — at ``start``, at
    ``request_start``, and by ``cflow show`` — because that is where it is
    still cheap to be wrong about one. An escalation that turns out to be
    unusable at the moment it fires costs a whole round: the run is already
    finished, there is no step to go back to, and the fallback is the plain
    ending the escalation existed to avoid.

    Two things are held against each declaration, and neither is enforced
    here: whether the target workflow resolves to a file at all, and what its
    ``filter_roles`` says about the session that would be driving. The second
    is reported rather than enforced because the answer may legitimately
    change before the escalation fires (a session's mesh role is not fixed at
    parse time), and because a run that never reaches the escalating step is
    not wrong for declaring one.
    """
    declared = [s for s in workflow.steps.values() if s.escalate is not None]
    if not declared:
        return None
    session = state_mod.current_scope()
    if session == state_mod.DEFAULT_SCOPE:
        session = ""
    reach = responders.pool(session=session, mesh=mesh, cwd=cwd)
    steps: List[dict] = []
    for step in declared:
        escalate = step.escalate
        assert escalate is not None
        entry: dict = {"step": step.id, "workflow": escalate.workflow}
        try:
            composed = state_mod.load_workflow(escalate.workflow, cwd)
        except Exception as exc:
            entry["problem"] = f"does not resolve to a workflow: {exc}"
            steps.append(entry)
            continue
        # Resolved: the target exists and this is what it declares. Answered
        # from files alone, so it holds with no daemon in reach.
        target = composed.workflow
        entry["resolves"] = str(composed.path)
        entry["name"] = target.name
        role_filter = target.filter_roles
        entry["filter_roles"] = (
            role_filter.describe() if role_filter else "none — any role may drive it"
        )
        # A PREVIEW of the run-time check, kept in its own field because it is
        # answered from the daemon rather than from files: whether this
        # session's mesh role passes that filter. "The filter said no" and
        # "nothing could ask the filter" are different answers, and folding
        # them together is how an unchecked declaration comes to look checked.
        if role_filter is None:
            entry["role_check"] = "admits any role"
        elif reach.problem or not reach.me:
            entry["role_check"] = (
                "unchecked: "
                f"{reach.problem or 'the driving session has no mesh identity'}"
                " — the escalation would proceed and record that it went "
                "unchecked"
            )
        elif role_filter.allows(reach.me_role):
            entry["role_check"] = f"admits {reach.me_role!r}"
        else:
            entry["role_check"] = (
                f"would be declined: {reach.me} holds role "
                f"{reach.me_role!r}, which this filter turns away — the run "
                f"would end the ordinary way instead of handing over"
            )
        steps.append(entry)
    unresolved = [e for e in steps if e.get("problem")]
    refused = [e for e in steps if str(e.get("role_check", "")).startswith("would be")]
    note = f"{len(steps)} escalation(s) declared"
    if unresolved:
        note += f"; {len(unresolved)} name(s) no workflow"
    if refused:
        note += (
            f"; {len(refused)} would be declined by the target's filter_roles "
            f"as this session stands"
        )
    return {"note": note, "steps": steps}


def delegation_check(
    workflow: Workflow, *, mesh: str = "", cwd: Optional[str] = None
) -> Optional[dict]:
    """What this workflow's delegated steps resolve to right now.

    Reported at start time and never enforced there: a leader that has not
    spawned yet is legitimate, and the step may be an hour away. Its value is
    that a person asking for the run is standing right there and can fix the
    wiring before it matters — which is why ``request_start`` carries it too.
    """
    delegated = _delegations(workflow)
    if not delegated:
        return None
    session = state_mod.current_scope()
    if session == state_mod.DEFAULT_SCOPE:
        session = ""
    reach = responders.pool(session=session, mesh=mesh, cwd=cwd)
    steps = []
    for step, kind, delegate in delegated:
        entry = {
            "step": step.id,
            "decision": kind,
            "from": " -> ".join(c.describe() for c in delegate.candidates),
            "resolves": [],
            "otherwise": delegate.otherwise + (
                f":{delegate.default_option}" if delegate.default_option else ""
            ),
        }
        reasons = []
        for candidate in delegate.candidates:
            # No `autowire`: this reports what the run resolves to, and a
            # report that rewired the mesh to make itself come out better
            # would be a different thing than a report.
            found, reason = reach.match(candidate)
            if found:
                entry["resolves"] = [r.handle for r in found]
                break
            reasons.append(reason or "nobody matched")
        if not entry["resolves"]:
            entry["reason"] = "; ".join(reasons)
        steps.append(entry)
    missing = [e for e in steps if not e["resolves"]]
    note = f"{len(steps)} delegated decision(s)"
    if missing:
        to_human = sum(1 for e in missing if e["otherwise"] == model.OTHERWISE_HUMAN)
        note += (
            f"; {len(missing)} resolve(s) to no agent right now and would "
            f"{'reach a human' if to_human else 'proceed unanswered'}"
        )
    else:
        note += "; all have a responder right now"
    return {"steps": steps, "note": note}


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
def _archive_current(state: dict, by: str, cwd: Optional[str]) -> str:
    """Retire the loaded run into the scope's archive folder. A still-active
    run is aborted first so its final status is honest; the journal moves
    with the run, so the next run starts a fresh one."""
    if state.get("status") not in ("done", "aborted"):
        state["status"] = "aborted"
        state_mod.save_state(state, cwd)
        state_mod.journal(
            "aborted", {"run": state["run_id"], "by": by, "reason": "archived"}, cwd
        )
    state_mod.journal("archived", {"run": state["run_id"], "by": by}, cwd)
    return str(state_mod.archive_run(cwd))


def _enforce_role_filter(
    workflow: Workflow, *, mesh: str, cwd: Optional[str]
) -> Optional[str]:
    """Hold the workflow's ``filter_roles`` against the driving session.

    Raises :class:`CflowError` when the driver's mesh role is resolvable and
    the filter turns it away. Returns a note for the payload/journal when the
    filter could not be enforced (no managed session, no membership, no
    daemon) — the run proceeds, but the fact is recorded rather than silently
    passed: the filter is a guardrail on a fleet's division of labour, and a
    standalone run has no fleet to divide.
    """
    role_filter = workflow.filter_roles
    if role_filter is None:
        return None
    session = state_mod.current_scope()
    if session == state_mod.DEFAULT_SCOPE:
        session = ""  # not a managed session: it has no mesh identity
    reach = responders.pool(session=session, mesh=mesh, cwd=cwd)
    if reach.problem or not reach.me:
        return (
            f"filter_roles {role_filter.describe()} could not be enforced: "
            f"{reach.problem or 'the driving session has no mesh identity'}"
        )
    if role_filter.allows(reach.me_role):
        return None
    raise CflowError(
        f"workflow {workflow.name!r} declares filter_roles "
        f"{role_filter.describe()}, and {reach.me} holds role "
        f"{reach.me_role!r} in mesh {reach.mesh!r} — this session may not "
        f"drive it. Start it from a session whose role the filter admits, "
        f"or change the workflow's 'filter_roles'"
    )


@_locked_op
def start(
    workflow_ref: str,
    context: Optional[str] = None,
    *,
    force: bool = False,
    mesh: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Begin a run here.

    ``mesh`` names which mesh a delegated decision looks up its responders in,
    and is a property of the RUN rather than of the workflow: a workflow says
    "ask the leader above me", which is portable, while which mesh that leader
    is in is a fact about this deployment. It is only needed when the driving
    session belongs to more than one — with a single membership the run finds
    it, and with none the delegation falls to a human either way.

    The body is :func:`_start_impl`; this wrapper holds the slot lock, and the
    daemon performs the same body for a recurring run's own request through
    :func:`auto_start_next_round`.
    """
    return _start_impl(
        workflow_ref, context=context, force=force, mesh=mesh, cwd=cwd
    )


def _start_impl(
    workflow_ref: str,
    context: Optional[str] = None,
    *,
    force: bool = False,
    mesh: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Begin a run here, without the slot lock.

    :func:`start` and :func:`auto_start_next_round` both call this, each
    holding the lock themselves — the cross-process lock is O_EXCL, so a
    daemon-side start must never enter a locked :func:`start` from inside one.
    Otherwise this is the whole of the start: settle the old run, resolve
    layers, snapshot, journal, and read the first step's payload.
    """
    pending = state_mod.read_request(cwd)
    if state_mod.has_run(cwd):
        old = state_mod.load_state(cwd)
        active = old.get("status") not in ("done", "aborted")
        if active and not force:
            raise CflowError(
                f"a run of {old.get('workflow')!r} is already active here "
                f"(step {old.get('current')}); resume it via 'status'/'next'. "
                f"To start fresh the active run must be retired first: a human "
                f"archives it with 'claunch cflow archive' (or the dashboard's "
                f"Archive button), or pass force=true to abort+archive it — "
                f"only with the user's explicit go-ahead"
            )
        # Finished runs never block a new start; a forced start retires the
        # active run the same way. Either way the history is kept, not lost.
        _archive_current(old, "force" if active else "auto", cwd)
    located = state_mod.locate(workflow_ref, cwd)
    path = located.path
    # Layers first: a file that `extends` another IS its merge with that
    # base, and the snapshot below must be of the merge — the composed text,
    # not this file's half of it — or the run would re-read a base that has
    # moved under it.
    composed = state_mod.compose_located(located, cwd)
    text = composed.text
    workflow = composed.workflow
    role_filter_note = _enforce_role_filter(
        workflow, mesh=(mesh or "").strip(), cwd=cwd
    )
    checkout_note = checkout.check(cwd=cwd)

    fulfilled = bool(pending) and (
        pending.get("workflow") == workflow_ref
        or pending.get("resolved") == str(path)
    )
    # Fulfilling a request that carries a round count — a recurring run's own
    # next-round request, or the dashboard's skip — carries it into the new
    # run; every other start (a human's request, a fresh loop, a
    # non-recurring workflow) is round 1 again.
    round_no = 1
    if fulfilled:
        try:
            round_no = max(1, int(pending.get("round") or 1))
        except (TypeError, ValueError):
            round_no = 1

    state = {
        "run_id": f"run-{secrets.token_hex(4)}",
        "workflow": workflow.name,
        "source": str(path),
        # Recorded, not derived later: a name can start resolving to a
        # different layer the moment somebody adds or deletes a file, and this
        # run's answer must stay the one that was true when it started.
        "origin": located.origin,
        # The bases this file was composed with, recorded for the same reason
        # `origin` is: which files answered is a fact about this start, and a
        # layer added later must not rewrite the answer after the fact.
        **({"bases": [str(p) for p in composed.bases]} if composed.layered else {}),
        "context": context or "",
        "mesh": (mesh or "").strip(),
        "started_at": state_mod.utcnow(),
        "status": "running",
        "current": workflow.start,
        "delivered": False,
        "gate_approved": False,
        "gate_logged": None,
        "pending_select": None,
        "ask": None,
        "declined": None,
        "unanswered": None,
        "goto_request": None,
        "report": None,
        "completed": 0,
        "visits": {workflow.start: 1},
        "loop_extensions": {},
        "round": round_no,
    }
    # The start step is an arrival like any other: a workflow whose START is
    # a timed wait (gds-job — a run its driver has no tools to advance) must
    # be armed here, or the daemon has nothing to fire and the run sits at
    # its first step forever. `_move_to` arms every later arrival.
    _arrive_timer(workflow, state, workflow.start, None, cwd)
    state_mod.snapshot_workflow(text, cwd)
    state_mod.save_state(state, cwd)
    state_mod.register_run_dir(cwd)
    state_mod.journal(
        "started",
        {
            "run": state["run_id"],
            "workflow": workflow.name,
            "source": str(path),
            "origin": located.origin,
            "shadowed": [str(p) for p in located.shadows],
            **({"extends": [str(p) for p in composed.bases]} if composed.layered else {}),
            "context": context or "",
            "total_steps": workflow.step_count(),
            "warnings": workflow.warnings,
            **({"round": round_no} if round_no > 1 else {}),
            **({"role_filter": role_filter_note} if role_filter_note else {}),
            **({"checkout": checkout_note} if checkout_note else {}),
        },
        cwd,
    )
    # A start settles any pending request for this slot — a human's or a
    # recurring run's own — whether it fulfils it or (starting something
    # else) supersedes it. Either way the request must not survive to be
    # "fulfilled" a second time.
    if pending:
        state_mod.journal(
            "request_fulfilled" if fulfilled else "request_superseded",
            {
                "run": state["run_id"],
                "request": pending.get("id"),
                "requested": pending.get("workflow"),
                "started": workflow.name,
                "by": pending.get("by"),
            },
            cwd,
        )
        state_mod.clear_request(cwd)
    payload = _payload(workflow, state, cwd, mutate=True)
    if context:
        payload["context"] = context
    if state["mesh"]:
        payload["mesh"] = state["mesh"]
    if workflow.warnings:
        payload["workflow_warnings"] = workflow.warnings
    if role_filter_note:
        payload["role_filter"] = role_filter_note
    if checkout_note:
        payload["checkout"] = checkout_note
    check = delegation_check(workflow, mesh=state["mesh"], cwd=cwd)
    if check:
        payload["delegation_check"] = check
    escalations = escalation_check(workflow, mesh=state["mesh"], cwd=cwd)
    if escalations:
        payload["escalation_check"] = escalations
    return payload


@_locked_op
def auto_start_next_round(*, cwd: Optional[str] = None) -> Optional[dict]:
    """Start a recurring run's next round for it. Daemon-driven.

    The engine half of ``recur: {auto: true}``: when a run finished and its
    pending start request is its OWN next round (``by: recur`` + ``auto``),
    the daemon's clock (:class:`..daemon.cflow_clock.RoundStartClock`)
    performs the start instead of the driving agent — the loop continues
    without anyone looking. The single-writer rule is not bent: the request
    channel is single-use, so exactly one start consumes it. The same fact
    makes this idempotent and restart-safe — a request the clock did not get
    to between scans (daemon down, slot locked) survives and is performed by
    the next scan, and a request already consumed by a human's or the
    driver's own start is a no-op here.

    ``None`` is every no-op: no pending request, a human's request, or a
    plain ``recur: true`` request (those keep the driver-performs-start flow).
    """
    pending = state_mod.read_request(cwd)
    if (
        not pending
        or pending.get("by") != "recur"
        or not pending.get("auto")
    ):
        return None
    workflow_ref = str(pending.get("workflow") or pending.get("resolved") or "")
    if not workflow_ref:
        return None
    payload = _start_impl(
        workflow_ref,
        context=str(pending.get("context") or ""),
        mesh=str(pending.get("mesh") or ""),
        cwd=cwd,
    )
    round_no = int(payload.get("round") or 1)
    state_mod.journal(
        "round_auto_started",
        {
            "run": payload.get("run"),
            "request": pending.get("id"),
            "workflow": payload.get("workflow"),
            **({"round": round_no} if round_no > 1 else {}),
        },
        cwd,
    )
    return {
        "workflow": payload.get("workflow"),
        "round": round_no,
        "step": payload.get("step_id"),
    }


@_locked_op
def request_start(
    workflow_ref: str,
    context: Optional[str] = None,
    *,
    by: str = "web",
    round_no: Optional[int] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Ask this slot's agent to start ``workflow_ref`` — the human side of a
    start, without writing a run.

    The dashboard could write the run itself (and :func:`start` still lets it),
    but then two independent writers create runs the agent has not read: its
    next ``report``/``next`` lands in a run it never saw. Recording an intent
    instead keeps a single writer — the agent — and the agent learns of the
    request through the same ``status`` call the protocol already makes it do
    after any nudge.
    """
    if state_mod.has_run(cwd):
        old = state_mod.load_state(cwd)
        if old.get("status") not in ("done", "aborted"):
            raise CflowError(
                f"a run of {old.get('workflow')!r} is already active here "
                f"(step {old.get('current')}); retire it first "
                f"('claunch cflow archive' or the dashboard's Archive button)"
            )
    # Resolve now, so a typo or an invalid workflow fails in front of the
    # human who asked, not silently inside the agent's turn later.
    composed = state_mod.load_workflow(workflow_ref, cwd)
    path = composed.path
    workflow = composed.workflow
    # The filter too: a request this scope's agent could never start should
    # be refused in front of whoever filed it, not inside the agent's turn.
    role_filter_note = _enforce_role_filter(workflow, mesh="", cwd=cwd)
    request = {
        "id": f"req-{secrets.token_hex(3)}",
        "workflow": workflow_ref,
        "name": workflow.name,
        "resolved": str(path),
        "context": (context or "").strip(),
        "by": by,
        # A round count on a request is a claim of continuity: the start that
        # fulfils it joins the loop at this round instead of opening round 1.
        # Filed by the dashboard's skip; a plain human request never has one.
        **({"round": int(round_no)} if round_no and int(round_no) > 1 else {}),
        "at": state_mod.utcnow(),
    }
    state_mod.write_request(request, cwd)
    state_mod.register_run_dir(cwd)
    state_mod.journal("start_requested", dict(request), cwd)
    result = {
        "status": "start_requested",
        "request": request,
        "note": (
            "recorded; the scope's agent starts it itself (it sees the request "
            "in its next 'status' call) — nudge it if it is idle"
        ),
    }
    # Resolved against the scope this run will belong to, which the decorator
    # has already installed — so what a person sees here is what the agent
    # would see, not what the dashboard's own process can reach.
    check = delegation_check(workflow, cwd=cwd)
    if check:
        result["delegation_check"] = check
    escalations = escalation_check(workflow, cwd=cwd)
    if escalations:
        result["escalation_check"] = escalations
    if role_filter_note:
        result["role_filter"] = role_filter_note
    return result


@_locked_op
def cancel_request(*, by: str = "web", cwd: Optional[str] = None) -> dict:
    """Withdraw a pending start request (nothing has run yet)."""
    pending = state_mod.read_request(cwd)
    if not pending:
        raise CflowError("no pending start request here")
    state_mod.clear_request(cwd)
    state_mod.journal(
        "request_cancelled",
        {"request": pending.get("id"), "workflow": pending.get("workflow"), "by": by},
        cwd,
    )
    return {"status": "request_cancelled", "request": pending}


def _load(cwd: Optional[str]):
    state = state_mod.load_state(cwd)
    workflow = state_mod.load_snapshot(cwd)
    return workflow, state


def _blocked(workflow: Workflow, state: dict) -> Optional[str]:
    """Why the current step's content is withheld, if it is.

    Entry approvals only: a delegated *select* is the step rather than a lock
    on it, so it is not "blocked" — the step has been delivered and the run is
    waiting on the decision itself.
    """
    step = workflow.step(state["current"])
    visit = _visits(state, step.id)
    if visit > _limit(workflow, state, step.id):
        return "loop_limit"
    declined = state.get("declined")
    if declined and declined.get("step") == step.id and declined.get("visit") == visit:
        return "declined"
    if step.gate and not state["gate_approved"]:
        return "gate"
    if step.ask and not state["gate_approved"]:
        return "ask"
    return None


@_locked_op
def report(
    summary: str,
    details: Optional[str] = None,
    *,
    cwd: Optional[str] = None,
) -> dict:
    """File the completion report for the current step (required by 'next')."""
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        return _done_payload(state, cwd)
    step = workflow.step(state["current"])
    if _blocked(workflow, state) or not state["delivered"]:
        raise CflowError(
            f"step {step.id!r} has not been delivered yet — nothing to report; "
            f"call 'next' or 'status' first"
        )
    if step.is_select:
        raise CflowError(
            f"current step {step.id!r} is a decision point — use 'select' "
            f"(its reason is the record), not 'report'"
        )
    summary = (summary or "").strip()
    if not summary:
        raise CflowError("a report needs a non-empty 'summary'")
    entry = {
        "step": step.id,
        "visit": _visits(state, step.id),
        "summary": summary,
        "details": (details or "").strip() or None,
        "at": state_mod.utcnow(),
    }
    state["report"] = entry
    state_mod.save_state(state, cwd)
    state_mod.journal("step_report", {"run": state["run_id"], **entry}, cwd)
    return {
        **_base(state),
        "step_id": step.id,
        "status": "reported",
        "note": "report recorded; call 'next' to advance",
    }


#: "the caller named no target", kept apart from ``None`` — which is a real
#: target here, and means the run ends.
_UNSET = object()


def _fence(state: dict) -> tuple:
    """The identity of 'the position a long operation was started from'."""
    current = state.get("current") or ""
    return (
        state.get("run_id"),
        state.get("status"),
        current,
        _visits(state, current) if current else 0,
    )


def _advance(
    workflow: Workflow,
    state: dict,
    step: Step,
    filed: dict,
    cwd,
    *,
    target: Optional[str] = _UNSET,
) -> dict:
    """Journal the step's completion and move to its successor.

    ``target`` defaults to the step's own ``next``. A checklist step has no
    ``next`` — its exit is ``checklist.then`` — so that one passes the
    destination in rather than having a second copy of this journalling.
    """
    if target is _UNSET:
        target = step.next
    state_mod.journal(
        "step_completed",
        {
            "run": state["run_id"],
            "step": step.id,
            "visit": _visits(state, step.id),
            "summary": filed["summary"],
            "details": filed.get("details"),
        },
        cwd,
    )
    state["completed"] += 1
    _move_to(workflow, state, target, cwd)
    if state["status"] == "done":
        return _done_payload(state, cwd)
    return _payload(workflow, state, cwd, mutate=True)


@_scoped_op
def next_step(*, cwd: Optional[str] = None) -> dict:
    """Advance the run, first settling any step-change request standing on it.

    A live request HOLDS the run: advancing would carry the position out from
    under the question, and an approval landing afterwards would move a run
    that is no longer where the person answering was looking.

    A refused one does not hold anything — it is news the agent has to act on
    — so it rides along with this call's ordinary outcome and is then cleared.
    Deliberately not a stop of its own: the agent had already filed the
    report that earns this advance, and making the refusal cost an extra
    ``next`` would mean a refusal is more expensive than a grant.
    """
    with state_mod.run_lock(cwd):
        state = state_mod.load_state(cwd)
        if state.get("status") not in ("done", "aborted"):
            pending = _pending_goto(state)
            if pending:
                return _goto_payload(state, pending)
        refused = state.get("goto_request")
        if isinstance(refused, dict) and refused.get("decision") == "denied":
            state["goto_request"] = None
            state_mod.save_state(state, cwd)
        else:
            refused = None
    payload = _next_step_impl(cwd=cwd)
    if refused:
        payload["goto_request"] = refused
        payload["note"] = (
            f"your request to move to {refused.get('step')!r} was refused by "
            f"{refused.get('decided_by') or 'a human'}"
            + (
                f": {refused['decided_reason']}"
                if refused.get("decided_reason")
                else " (no reason given)"
            )
            + ". Continue on the route the workflow declares, and say in your "
            "next report that it was refused. "
            + str(payload.get("note") or "")
        ).strip()
    return payload


def _next_step_impl(*, cwd: Optional[str] = None) -> dict:
    with state_mod.run_lock(cwd):
        workflow, state = _load(cwd)
        if state["status"] in ("done", "aborted"):
            return _done_payload(state, cwd)
        step = workflow.step(state["current"])

        if not state["delivered"] or _blocked(workflow, state):
            # Nothing has been handed out yet (fresh arrival, or a gate/loop
            # guard is closed): (re)attempt delivery.
            return _payload(workflow, state, cwd, mutate=True)

        if step.checklist is not None:
            # No agent exit from a checklist gate. `next` here is not an
            # error either — it is the natural thing to try — so it answers
            # with the position, which says what is still false and who moves
            # the run.
            return _payload(workflow, state, cwd, mutate=False)

        if step.is_select:
            if _current_window(state, step.id, _visits(state, step.id)):
                # A held choice: due, it is released right here (the
                # no-daemon path); not yet, the hold is restated.
                return _payload(workflow, state, cwd, mutate=True)
            payload = _payload(workflow, state, cwd, mutate=False)
            if payload.get("status") == "select":
                # Only when the decision is actually this agent's to make; a
                # delegated one already explains that it is waiting on someone
                # else, and must not be told to call a tool it may not call.
                payload["note"] = (
                    "this is a decision point — use the 'select' tool, not 'next'"
                )
            return payload

        # Completing a delivered executable step: the report comes first, so
        # the journal/dashboard always carry an explicit account of what
        # happened.
        filed = _current_report(state, step.id)
        if filed is None:
            return {
                **_base(state),
                "step_id": step.id,
                "status": "report_required",
                "note": (
                    "no completion report filed for this step yet — call "
                    "'report' with {summary, details?} describing what actually "
                    "happened, then call 'next' again"
                ),
            }
        if not step.verify:
            return _advance(workflow, state, step, filed, cwd)
        fence = _fence(state)

    # Machine gate second: verify must confirm the reported outcome. It runs
    # UNLOCKED — a build/test command can take an hour, and holding the slot
    # that long would block every human control (approve, archive, goto) on
    # the dashboard. The commit below re-checks the position instead.
    #
    # Asked here rather than at the gate's result: what the command is about
    # to be run *in* decides what its exit code is worth, and a pass is the
    # case that needs saying — a red gate stops the run by itself, while a
    # green one from somebody else's tree is read as proof and is not.
    isolation = checkout.check(cwd=cwd)
    result = _run_verify(step, cwd, scope=verify_scope(cwd))

    with state_mod.run_lock(cwd):
        workflow, state = _load(cwd)
        if _fence(state) != fence:
            # A human moved (or retired) the run while the command ran. The
            # result describes a position that no longer exists, so it is not
            # applied — reporting that plainly beats advancing the wrong run.
            state_mod.journal(
                "verify_discarded",
                {"run": state["run_id"], "step": step.id, "was": fence[2]},
                cwd,
            )
            payload = _payload(workflow, state, cwd, mutate=False)
            payload["note"] = (
                f"the run moved while {step.verify.command!r} was running, so "
                f"its result was discarded — this is the current position; "
                f"re-read it and continue from here"
            )
            return payload
        if result is not None:
            state_mod.journal(
                "verify_failed",
                {"run": state["run_id"], "step": step.id, **result},
                cwd,
            )
            # The report described an outcome that did not survive verify;
            # after fixing, the (different) outcome must be re-reported.
            state["report"] = None
            state_mod.save_state(state, cwd)
            return {
                **_base(state),
                "step_id": step.id,
                "status": "verify_failed",
                "command": step.verify.command,
                **result,
                **({"checkout": isolation} if isolation else {}),
                "note": (
                    "the step's verify command failed; fix the problem, file a "
                    "new 'report', and call 'next' again (the command will be "
                    "re-run)"
                ),
            }
        state_mod.journal(
            "verify_passed",
            {
                "run": state["run_id"],
                "step": step.id,
                "command": step.verify.command,
                **({"checkout": isolation} if isolation else {}),
            },
            cwd,
        )
        filed = _current_report(state, step.id) or filed
        payload = _advance(workflow, state, step, filed, cwd)
        if isolation:
            payload["checkout"] = isolation
        return payload


def verify_scope(cwd: Optional[str]) -> str:
    """Whose session a verify command runs as, given where the run lives.

    A gate under ``tools/`` asks a question about a session -- "did MY branch
    land", "is MY report filed" -- and finds the session by reading
    ``CLAUNCH_SESSION``. The run's own scope IS that name whenever it has one,
    so this returns it and :func:`_run_verify` writes it in rather than
    letting the caller's environment supply it. Two callers, two reasons:

    * the in-session MCP server, where the ambient value already equals the
      scope, so writing it in changes nothing and closes the door on it ever
      *not* being equal;
    * anything else that advances a run -- the same door
      :func:`probe_env` shut for the daemon's probes (``claunch-04ru``).

    The fallback is the case that actually failed. A run's scope is taken from
    the ambient ``CLAUNCH_SESSION`` at ``start`` (:func:`.state.current_scope`),
    so a run started by a process that had none is keyed to
    :data:`.state.DEFAULT_SCOPE` and holds no identity to hand on. Measured
    (issue ``claunch-d7qp``): a worker's ``improv-worker`` round drove
    ``run-881710ab`` out of ``.cflow/runs/default/`` in its own worktree, and
    ``wrapup``'s ``tools/report_check.py`` answered ``exit 2 -- error: no
    session`` because the environment it inherited had none either. The same
    shape is in two other worktrees on this machine (sessions s305 and s362,
    three failures), and s362's round was blocked until a person moved it.

    So the identity comes from the daemon instead: exactly one live managed
    session standing in this directory is that session's checkout, and a gate
    run there is about its round (:func:`.checkout.occupant`, which declines
    to guess when it is not exactly one). Nothing is invented -- when the
    daemon cannot say, the answer is empty and :func:`probe_env` removes the
    variable, which is precisely the state the failing run was already in.
    """
    scope = state_mod.current_scope()
    if scope and scope != state_mod.DEFAULT_SCOPE:
        return scope
    return checkout.occupant(cwd)


def _run_verify(step: Step, cwd: Optional[str], *, scope: str) -> Optional[dict]:
    """Run the step's verify command; None on success, failure details otherwise.

    ``scope`` has no default for the same reason :func:`run_probe`'s has none:
    a default is a door for somebody else's environment to walk back through,
    and it would do it silently. See :func:`verify_scope`.
    """
    verify = step.verify
    try:
        completed = subprocess.run(
            verify.command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=verify.timeout,
            env=probe_env(scope),
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "output": f"timed out after {int(verify.timeout)}s"}
    if completed.returncode == 0:
        return None
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return {
        "exit_code": completed.returncode,
        "output": output[-VERIFY_OUTPUT_TAIL:],
    }


def probe_env(scope: Optional[str]) -> Dict[str, str]:
    """The environment a probe subprocess runs in, given whose run it is for.

    A probe is launched by the daemon, and the daemon's own environment is not
    the run's. It carries whatever ``CLAUNCH_SESSION`` the daemon inherited
    from the terminal it was started in -- one session's name, for every run
    on the machine -- read out of the live daemon's process environment while
    this was being fixed: ``CLAUNCH_SESSION=s127``, ``PWD`` the repository
    root, unchanged for that daemon's whole lifetime. Anything the probe calls
    that resolves "which session am I" then answers with that one name: :func:`.checkout.own_checkout` looks it
    up, gets that session's recorded directory, and returns it *instead of* the
    run's own -- so a gate under ``tools/`` measures a checkout the run never
    touched. Measured on this machine (issue ``claunch-04ru``): a worker's
    ``await-landing`` probe reported on the repository root, printing its basis
    as ``session``, while the same command in the worker's own worktree
    answered about the worker's branch.

    Writing the run's scope in is chosen over deleting the variable, and the
    difference matters because the answer must not depend on the lookup
    succeeding. ``own_checkout`` falls back to the run's ``cwd`` whenever the
    daemon cannot be asked, so with the right name loaded BOTH of its branches
    name the same checkout: the lookup answers with that session's directory,
    or it fails and the ``cwd`` the clock already chose stands. A probe that
    flips between the two therefore flips between two identical answers. That
    property is the point -- the flipping itself was observed (a worker saw the
    probe's exit code move 2 -> 1 -> 2 -> 1 over three minutes) and its cause
    was never measured, so the fix is built not to need it.

    An unmanaged run (``scope`` is :data:`.state.DEFAULT_SCOPE`, or empty) has
    no session to name, and there the variable is REMOVED rather than left:
    inheriting it would be the original defect with an extra step, and its
    absence is exactly what ``own_checkout`` reads as "fall back to cwd".
    """
    env = dict(os.environ)
    who = (scope or "").strip()
    if who and who != state_mod.DEFAULT_SCOPE:
        env[state_mod.SESSION_ENV] = who
    else:
        env.pop(state_mod.SESSION_ENV, None)
    return env


def run_probe(
    command: str, cwd: Optional[str], timeout: float, *, scope: Optional[str]
) -> Optional[dict]:
    """Measure a step's awaited condition once. ``None`` = could not measure.

    Called by the daemon's reminder clock, not by the run — nothing in a run's
    own lifecycle samples a probe. Lives here anyway because this is where
    cflow's subprocess policy is written, and a second copy of it in the
    daemon would drift from :func:`_run_verify` the first time either changed.

    Two answers, and keeping them apart is the point:

    * a dict — the probe ran, and its ``code`` is the fact. Nothing is judged
      here: 0 is not "good" and 1 is not "not yet", because only the workflow
      author knows what their own probe's codes mean. The caller compares this
      code with the previous one and cares about nothing but the difference.
    * ``None`` — the probe could not be run at all, or did not finish inside
      ``timeout``. That is *no answer*, not the answer "not yet", and the
      caller must fall back to its clock rather than read it as "unchanged".
      A broken probe silencing a run is the one failure this feature could
      introduce, and this return value is where it is refused.

    The timeout is the caller's (a step's ``awaits.timeout``, capped at
    :data:`model.MAX_AWAITS_TIMEOUT`), never a verify's — a probe nominated with
    ``awaits: verify`` runs under the probe's budget, so a heavy command
    nominated by mistake times out into ``None`` instead of being re-run on a
    loop.

    ``scope`` says whose run this probe is for and has no default, because
    every caller knows it and a wrong answer here is silent: see
    :func:`probe_env` for what it decides and why it is not optional.
    """
    kwargs = (
        {"start_new_session": True}
        if os.name == "posix"
        else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    )
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=probe_env(scope),
            **kwargs,
        )
    except (OSError, ValueError):
        return None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # `subprocess.run(timeout=...)` would not be enough here, and the
        # difference is the whole reason this is written out. With `shell=True`
        # the child is a shell; killing it leaves the grandchild it launched
        # alive, still holding the write end of these pipes, so the read that
        # follows the kill blocks until the *real* process finishes — and the
        # timeout that was supposed to cap a probe at ten seconds caps nothing.
        # A clock that can be held open by a slow probe is a clock a workflow
        # can hang the daemon with, which is exactly what the ceilings on
        # `awaits` exist to prevent. So the whole tree goes.
        _kill_tree(proc)
        return None
    except (OSError, ValueError):
        return None
    says = ((out or "") + (err or "")).strip()
    return {"code": proc.returncode, "says": says[-PROBE_OUTPUT_TAIL:]}


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill a probe and everything it started, then stop holding its pipes."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=KILL_GRACE,
            )
    except Exception:
        # A tree-kill that failed still leaves the direct child to kill, and a
        # probe already gone raises here on some platforms. Either way the
        # answer to the caller is the same one: None.
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=KILL_GRACE)
    except Exception:
        pass


@_locked_op
def select(
    option: str,
    reason: Optional[str] = None,
    *,
    by: str = "agent",
    cwd: Optional[str] = None,
) -> dict:
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        return _done_payload(state, cwd)
    step = workflow.step(state["current"])
    if not step.is_select:
        raise CflowError(f"current step {step.id!r} is not a decision point; use 'next'")
    if _blocked(workflow, state):
        return _payload(workflow, state, cwd, mutate=True)
    if option not in step.select.options:
        raise CflowError(
            f"unknown option {option!r} for step {step.id!r} "
            f"(options: {', '.join(step.select.options)})"
        )

    if by == "agent" and step.select.require_reason and not (reason or "").strip():
        raise CflowError(
            f"step {step.id!r} requires a reason with the agent selection"
        )

    if by == "agent" and _live_chooser(state, step) == "delegate":
        raise CflowError(
            f"step {step.id!r} delegates this decision — it is not yours to "
            f"make, and recording a proposal would not help. The responders "
            f"answer it with the 'answer' tool; stop your turn and wait to be "
            f"nudged (call 'status' to see who was asked)"
        )

    if by == "agent" and step.select.chooser == "user":
        state["pending_select"] = {
            "step": step.id,
            "option": option,
            "reason": (reason or "").strip(),
            "by": "agent",
            "at": state_mod.utcnow(),
        }
        state_mod.save_state(state, cwd)
        state_mod.journal(
            "select_proposed",
            {"run": state["run_id"], "step": step.id, "option": option,
             "reason": (reason or "").strip()},
            cwd,
        )
        return _payload(workflow, state, cwd, mutate=False)

    now = _utc_now()
    held = _current_window(state, step.id, _visits(state, step.id))
    if by == "agent":
        opens = _window_opens(state, step, option, cwd, now)
        if opens is not None:
            # Inside the option's interval: the choice is parked, not refused.
            # A different option held before this one is dropped — the driver
            # changed its mind, and the record says so.
            if held and held.get("option") != option:
                state_mod.journal(
                    "select_hold_cancelled",
                    {"run": state["run_id"], "step": step.id,
                     "was": held.get("option"), "now": option},
                    cwd,
                )
                state["window"] = None
            _hold(state, step, option, (reason or "").strip(), opens, cwd, now)
            return _payload(workflow, state, cwd, mutate=True)
    if held:
        # A take that goes through now — the driver past the window, or a
        # human confirming from the CLI/dashboard, which is not paced — makes
        # the hold moot; if it was for another option, that is a cancel.
        if held.get("option") != option:
            state_mod.journal(
                "select_hold_cancelled",
                {"run": state["run_id"], "step": step.id,
                 "was": held.get("option"), "now": option, "by": by},
                cwd,
            )
        state["window"] = None

    open_ask = _current_ask(state, step.id, _visits(state, step.id), "branch")
    if open_ask:
        # A human settling a delegated decision — from the dashboard, from the
        # CLI, or because the responders never got to it. Close the question
        # here so `_move_to` does not log it as one the run walked away from.
        state["ask"] = None
        _settled_by_person(state, open_ask, decision=option, by=by, cwd=cwd)
    state_mod.journal(
        "select_confirmed",
        {"run": state["run_id"], "step": step.id, "option": option,
         "reason": (reason or "").strip(), "by": by,
         "visit": _visits(state, step.id),
         **({"paced": "override"} if held and by != "agent" else {})},
        cwd,
    )
    _take(state, step, option, cwd, now)
    state["completed"] += 1  # the decision itself counts as a completed step
    _move_to(workflow, state, step.select.options[option].next, cwd)
    if state["status"] == "done":
        return _done_payload(state, cwd)
    if by == "agent":
        # The agent receives the next step directly in this tool result.
        return _payload(workflow, state, cwd, mutate=True)
    # A CLI confirmation must NOT deliver: the agent has not seen the step —
    # it fetches its own instructions on the next 'next'/'status' call.
    return {
        **_base(state),
        "status": "selected",
        "step_id": step.id,
        "option": option,
        "note": "selection confirmed; nudge the agent to continue",
    }


@_locked_op
def release_window(
    *, now: Optional[datetime] = None, cwd: Optional[str] = None
) -> Optional[dict]:
    """Confirm a held choice whose window has opened. Daemon-driven.

    Same shape as :func:`expire_ask`, for the same reason: the agent that
    would notice the window opening is the one that stopped its turn to wait
    for it. The daemon's clock (:class:`..daemon.cflow_clock.WindowClock`)
    calls this over every run and nudges the driver when it moved something.
    Without a daemon the hold is not lost — the driver's next ``next`` past
    the moment releases it (see :func:`_payload`) — it is only late.

    Returns what moved (for the nudge), or None when nothing was due.
    """
    if not state_mod.has_run(cwd):
        return None
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted") or not state.get("current"):
        return None
    step = workflow.step(state["current"])
    held = _current_window(state, step.id, _visits(state, step.id))
    if held is None:
        return None
    now = now or _utc_now()
    if not _window_due(held, now):
        return None
    window = _release(workflow, state, step, cwd, now)
    return {
        "run": state["run_id"],
        "workflow": state["workflow"],
        "step": step.id,
        "option": window["option"],
        "held_since": window.get("at"),
        "opens_at": window.get("opens_at"),
        "now_at": state.get("current"),
        "status": state["status"],
    }


@_locked_op
def fire_timer(*, cwd: Optional[str] = None) -> Optional[dict]:
    """A timed wait's next fire: the run moves on schedule, or nothing.

    Daemon-driven, the same shape as :func:`release_window`: the one agent
    that would notice the moment is the one that ended its turn to wait for
    it. The daemon's clock (:class:`..daemon.cflow_clock.TimerClock`) calls
    this over every run and nudges the driver when something moved. Without
    a daemon nothing fires — a timed wait holds, and the agent's own next
    ``next`` past the moment leaves the step normally.

    ``None`` is every "nothing to do yet" answer: no armed timer, the run
    left the step, or the window has not opened — the clock simply tries
    again next tick. A due fire moves the run to ``timer.then`` (a paid
    visit, delivered on the next read) and journals ``timer_fired``; the
    fire past the budget moves it to ``timer.after`` instead
    (``timer_budget_spent``) and clears the arm — the inner loop is closed.
    """
    if not state_mod.has_run(cwd):
        return None
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted") or not state.get("current"):
        return None
    step = workflow.step(state["current"])
    armed = state.get("timer_armed")
    if not armed or armed.get("step") != step.id or step.timer is None:
        return None
    opens = _parse_at(armed.get("opens_at"))
    if opens is not None and opens > _utc_now():
        return None
    fires = int(armed.get("fires") or 0) + 1
    if fires > step.timer.max:
        # `after` is kept as written in the workflow (`end` included), so the
        # move target is normalized the way every other engine edge is.
        target = None if step.timer.after == model.END else step.timer.after
        state_mod.journal(
            "timer_budget_spent",
            {"run": state["run_id"], "step": step.id,
             "fires": fires, "max": step.timer.max},
            cwd,
        )
        _move_to(workflow, state, target, cwd)
        return {
            "run": state["run_id"],
            "workflow": state["workflow"],
            "step": step.id,
            "fires": fires,
            "max": step.timer.max,
            "moved_to": "end" if target is None else target,
            "opens_at": armed.get("opens_at"),
        }
    fires_by_step = dict(state.get("timer_fires") or {})
    fires_by_step[step.id] = fires
    state["timer_fires"] = fires_by_step
    state_mod.journal(
        "timer_fired",
        {"run": state["run_id"], "step": step.id, "fires": fires,
         "max": step.timer.max},
        cwd,
    )
    _move_to(workflow, state, step.timer.then, cwd)
    return {
        "run": state["run_id"],
        "workflow": state["workflow"],
        "step": step.id,
        "fires": fires,
        "max": step.timer.max,
        "moved_to": step.timer.then,
        "opens_at": armed.get("opens_at"),
    }


@_locked_op
def claim_restart(*, platform: str, boot_id: str, cwd: Optional[str] = None) -> Optional[dict]:
    """Claim this checklist visit's external restart exactly once.

    The caller executes the returned command outside the run lock.  A record
    from another daemon boot is deliberately not re-run: a restart command
    may itself have restarted the cflow daemon, so repeating it is unsafe.
    """
    if not state_mod.has_run(cwd):
        return None
    workflow, state = _load(cwd)
    if state.get("status") in ("done", "aborted") or not state.get("current"):
        return None
    # Mutating status settles an `ask: {otherwise: self}` before this action.
    payload = _payload(workflow, state, cwd, mutate=True)
    if payload.get("status") != "waiting_checklist":
        return None
    step = workflow.step(state["current"])
    if step.restart is None:
        return None
    visit = _visits(state, step.id)
    previous = state.get("restart")
    if isinstance(previous, dict) and previous.get("step") == step.id and previous.get("visit") == visit:
        if previous.get("status") == "running" and previous.get("boot_id") != boot_id:
            previous["status"] = "interrupted"
            previous["completed_at"] = state_mod.utcnow()
            state_mod.save_state(state, cwd)
            state_mod.journal("restart_interrupted", {"run": state["run_id"], "step": step.id,
                              "visit": visit, "boot_id": previous.get("boot_id")}, cwd)
            return {"kind": "interrupted", "run": state["run_id"], "workflow": state["workflow"],
                    "step": step.id, "visit": visit}
        return None
    command = step.restart.command_for(platform)
    if not command:
        record = {"step": step.id, "visit": visit, "status": "unsupported",
                  "requested_at": state_mod.utcnow(), "boot_id": boot_id}
        state["restart"] = record
        state_mod.save_state(state, cwd)
        state_mod.journal("restart_unsupported", {"run": state["run_id"], "step": step.id,
                          "visit": visit, "platform": platform}, cwd)
        return {"kind": "unsupported", "run": state["run_id"], "workflow": state["workflow"],
                "step": step.id, "visit": visit, "platform": platform}
    record = {"step": step.id, "visit": visit, "status": "running", "command": command,
              "requested_at": state_mod.utcnow(), "boot_id": boot_id}
    state["restart"] = record
    state_mod.save_state(state, cwd)
    state_mod.journal("restart_requested", {"run": state["run_id"], "step": step.id,
                      "visit": visit, "platform": platform, "command": command}, cwd)
    return {"kind": "run", "run": state["run_id"], "workflow": state["workflow"],
            "step": step.id, "visit": visit, "command": command, "timeout": step.restart.timeout}


@_locked_op
def complete_restart(*, step_id: str, visit: int, exit_code: Optional[int], output: str,
                     cwd: Optional[str] = None) -> Optional[dict]:
    """Persist one external restart result for the claimed visit."""
    if not state_mod.has_run(cwd):
        return None
    workflow, state = _load(cwd)
    record = state.get("restart")
    if (not isinstance(record, dict) or record.get("step") != step_id
            or record.get("visit") != visit or record.get("status") != "running"):
        return None
    record.update({"status": "succeeded" if exit_code == 0 else "failed",
                   "exit_code": exit_code, "output": output[-4000:],
                   "completed_at": state_mod.utcnow()})
    state_mod.save_state(state, cwd)
    state_mod.journal("restart_completed", {"run": state["run_id"], "step": step_id,
                      "visit": visit, "exit_code": exit_code, "output": output[-4000:]}, cwd)
    return {"run": state["run_id"], "workflow": state["workflow"], "step": step_id,
            "visit": visit, "exit_code": exit_code, "output": output[-4000:]}


@_scoped_op
def check_checklist(*, cwd: Optional[str] = None) -> Optional[dict]:
    """Re-measure a checklist gate's items, and move the run if they are all true.

    Daemon-driven, the same shape as :func:`fire_timer`: the one agent that
    would notice the moment is the one that ended its turn to wait for it. The
    daemon's clock (:class:`..daemon.cflow_clock.ChecklistClock`) calls this
    over every run that reports ``waiting_checklist``. It is also what
    ``claunch cflow checklist --recheck`` calls, so a run with no daemon
    behind it is late rather than stuck.

    The commands run **unlocked**, exactly as a ``verify`` does in
    :func:`next_step` and for the same reason: holding the slot across a
    subprocess would block every human control on the dashboard. The commit
    below re-checks the position with :func:`_fence` and discards a
    measurement whose position moved underneath it.

    Two conditions gate the move, and both are stated in the workflow rather
    than decided here:

    * every item exited 0 — an unmeasurable item (could not be launched, or
      timed out) is ``None``, never true, so a broken command holds the gate
      shut instead of opening it;
    * the step's own report has been filed. The items are what a command can
      check; the report is the rest of ``done_when``, and a gate that moved
      without it would close a round with the human-checked half missing from
      the journal. A report never opens the gate on its own.

    Returns ``None`` for every "nothing happened" answer (no run, not at a
    checklist step, nothing measurable changed and nothing moved), a dict
    describing the measurement otherwise — with ``moved_to`` set only when
    the run actually left.
    """
    if not state_mod.has_run(cwd):
        return None
    with state_mod.run_lock(cwd):
        workflow, state = _load(cwd)
        if state["status"] in ("done", "aborted") or not state.get("current"):
            return None
        step = workflow.step(state["current"])
        if step.checklist is None:
            return None
        before = {
            entry["id"]: entry["ok"] for entry in _checklist_items(
                step, _checklist_record(state, step)
            )
        }
        fence = _fence(state)
        visit = _visits(state, step.id)
        checklist = step.checklist

    measured: Dict[str, dict] = {}
    for item in checklist.items:
        # The scope is read here rather than defaulted inside `run_probe`: the
        # ambient one is right only because `_scoped_op` installed this run's
        # scope for the duration of the call, and that is a fact about THIS
        # function, not about probes.
        probe = run_probe(
            item.check, cwd, checklist.timeout, scope=state_mod.current_scope()
        )
        measured[item.id] = {
            # `run_probe` returns None when the command could not be run at
            # all, which is no answer rather than the answer "false". It is
            # recorded as unknown so a reader can tell "this is not true" from
            # "nobody could tell", and both hold the gate.
            "ok": None if probe is None else probe["code"] == 0,
            "code": None if probe is None else probe["code"],
            "says": None if probe is None else probe["says"],
            "at": _iso(_utc_now()),
        }

    with state_mod.run_lock(cwd):
        workflow, state = _load(cwd)
        if _fence(state) != fence:
            # A human moved (or retired) the run while the items ran. The
            # measurement describes a position that no longer exists.
            state_mod.journal(
                "checklist_discarded",
                {"run": state["run_id"], "step": step.id, "was": fence[2]},
                cwd,
            )
            return None
        record = {
            "step": step.id,
            "visit": visit,
            "items": measured,
            "checked_at": _iso(_utc_now()),
        }
        state["checklist"] = record
        items = _checklist_items(step, record)
        after = {entry["id"]: entry["ok"] for entry in items}
        changed = sorted(k for k in after if before.get(k) is not after[k])
        green = _checklist_green(items)
        filed = _current_report(state, step.id)
        result = {
            "run": state["run_id"],
            "workflow": state["workflow"],
            "step": step.id,
            "visit": visit,
            "passed": sum(1 for entry in items if entry.get("ok") is True),
            "total": len(items),
            "all_true": green,
            "report_filed": filed is not None,
            "changed": changed,
            "items": items,
        }
        if changed:
            # Only on a change. A poll that measured the same thing again is
            # not news, and a journal that recorded every pass would bury the
            # entries that say something under the ones that do not.
            state_mod.journal(
                "checklist_changed",
                {
                    "run": state["run_id"],
                    "step": step.id,
                    "visit": visit,
                    "changed": changed,
                    "state": {
                        entry["id"]: {
                            "ok": entry["ok"],
                            "exit_code": entry["exit_code"],
                        }
                        for entry in items
                    },
                },
                cwd,
            )
        if not green:
            expires = _checklist_expires_at(state, step)
            if expires is not None and _utc_now() >= expires:
                # The gate's "no": the wait the workflow bounded is over and
                # the list is still not all true. The run leaves for the
                # declared step on the daemon's clock, with the evidence a
                # reader would otherwise reconstruct — and without a report,
                # because nothing was completed here.
                otherwise = checklist.otherwise
                target = None if otherwise.then == model.END else otherwise.then
                state_mod.journal(
                    "checklist_expired",
                    {
                        "run": state["run_id"],
                        "step": step.id,
                        "visit": visit,
                        "after": otherwise.after,
                        "then": otherwise.then,
                        "items": [
                            {
                                "id": entry["id"],
                                "describe": entry["describe"],
                                "ok": entry["ok"],
                                "exit_code": entry["exit_code"],
                                "output": entry["output"],
                                "measured_at": entry["measured_at"],
                            }
                            for entry in items
                        ],
                    },
                    cwd,
                )
                _move_to(workflow, state, target, cwd)
                result["expired"] = True
                result["after"] = otherwise.after
                result["moved_to"] = "end" if target is None else target
                result["status"] = state["status"]
                return result
        if not green or filed is None:
            state_mod.save_state(state, cwd)
            return result if changed else None
        state_mod.journal(
            "checklist_passed",
            {
                "run": state["run_id"],
                "step": step.id,
                "visit": visit,
                "then": checklist.then,
                # The evidence the agent no longer has to transcribe: what was
                # asked, what answered, with what code and when.
                "items": [
                    {
                        "id": entry["id"],
                        "describe": entry["describe"],
                        "check": entry["check"],
                        "exit_code": entry["exit_code"],
                        "output": entry["output"],
                        "measured_at": entry["measured_at"],
                    }
                    for entry in items
                ],
            },
            cwd,
        )
        target = None if checklist.then == model.END else checklist.then
        _advance(workflow, state, step, filed, cwd, target=target)
        result["moved_to"] = "end" if target is None else target
        result["status"] = state["status"]
        return result


@_locked_op
def expire_ask(*, now: Optional[datetime] = None, cwd: Optional[str] = None) -> Optional[dict]:
    """Move a timed-out ask on to the next candidate group. Daemon-driven.

    Nothing calls into a stopped run, so an expiry cannot be noticed lazily
    the way a gate is: the agent that would ask is the one waiting. The daemon
    owns this clock (see :mod:`..daemon.cflow_clock`), which is also why a
    workflow with no daemon behind it simply keeps waiting — the timeout
    lapses into "no timeout", never into "proceed unapproved".

    Returns what happened, or ``None`` when there was nothing to expire.
    """
    if not state_mod.has_run(cwd):
        return None
    try:
        workflow, state = _load(cwd)
    except state_mod.StateError:
        return None
    if state.get("status") in ("done", "aborted"):
        return None
    ask = state.get("ask")
    deadline = (ask or {}).get("deadline")
    if not ask or not deadline:
        return None
    step = workflow.step(state["current"])
    if ask.get("step") != step.id or ask.get("visit") != _visits(state, step.id):
        return None  # stale; the run moved and `_move_to` will have cleared it
    try:
        due = datetime.fromisoformat(str(deadline))
    except ValueError:
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if (now or datetime.now(timezone.utc)) < due:
        return None
    who = ", ".join(e.get("handle") or e["kind"] for e in ask.get("asked") or [])
    if ask.get("deferred"):
        # Not an expiry: this ask was never put to anyone, and its deadline is
        # the retry the deferral asked for. Re-resolve the same group.
        reopened = _retry_deferred(workflow, state, step, ask, cwd)
    else:
        reopened = _escalate(
            workflow, state, step, ask, cwd,
            why=f"{who or 'nobody'} did not answer by {deadline}",
        )
    moved_to = None
    if reopened is None and state.get("current") != step.id:
        # `otherwise: self:<option>`: the declared default took the decision
        # unanswered and the run is already elsewhere — the clock's caller
        # needs to know there is nobody to wait on because there is nothing
        # left to wait for.
        moved_to = state.get("current") or "end"
    return {
        "run": state["run_id"],
        "ask": ask["id"],
        "step": step.id,
        "expired": who,
        # Empty both when the question fell to a human and when the workflow
        # said to carry on unanswered — the daemon reports the expiry either
        # way, and the journal is where the difference is written down.
        "now_with": [
            e.get("handle") or e["kind"] for e in (reopened or {}).get("asked") or []
        ],
        **({"moved_to": moved_to} if moved_to else {}),
    }


def _ask_addresses(ask: Optional[dict], session: str) -> bool:
    return bool(ask) and any(
        e.get("kind") == "member" and e.get("session") == session
        for e in (ask or {}).get("asked") or []
    )


def open_asks(session: str) -> List[dict]:
    """Every open request waiting on ``session``, across this machine's runs.

    A responder has no idea which directory or scope the run asking it lives
    in — nor should it, since the whole transaction is "somebody above you
    needs a decision". So the ask id is the only handle it ever holds, and
    this is what turns one into a run: a scan of the same registry the
    dashboard lists runs from.

    Read-only and lock-free. A listing that raced a state write is stale by
    one transition, and :func:`answer` re-checks everything under the lock —
    taking every run's lock to render a list would be the expensive way to be
    exactly as correct.
    """
    session = str(session or "").strip()
    if not session:
        return []
    out: List[dict] = []
    for cwd, scope in state_mod.known_runs():
        token = state_mod.push_scope(scope)
        try:
            if not state_mod.has_run(cwd):
                continue
            state = state_mod.load_state(cwd)
            ask = state.get("ask")
            if not _ask_addresses(ask, session):
                continue
            if state.get("status") in ("done", "aborted"):
                continue
            out.append(
                {
                    "ask": ask["id"],
                    "kind": ask["kind"],
                    "prompt": ask["prompt"],
                    "options": ask["options"],
                    "deadline": ask.get("deadline"),
                    "opened_at": ask.get("opened_at"),
                    "from_session": scope,
                    "workflow": state.get("workflow"),
                    "step": ask["step"],
                    "context": state.get("context") or "",
                    "cwd": cwd,
                }
            )
        except (state_mod.StateError, KeyError, TypeError):
            continue  # a half-written or foreign run is not this list's problem
        finally:
            state_mod.pop_scope(token)
    return out


def answer_ask(
    ask_id: str,
    decision: str,
    reason: Optional[str] = None,
    *,
    by_session: str = "",
) -> dict:
    """Answer by id alone: find the run it belongs to, then :func:`answer` it.

    The lookup is deliberately restricted to asks addressed to this session,
    so an id learned some other way is not a way into a run that never asked.
    """
    for entry in open_asks(by_session):
        if entry["ask"] == ask_id:
            receipt = answer(
                ask_id,
                decision,
                reason,
                by_session=by_session,
                cwd=entry["cwd"],
                scope=entry["from_session"],
            )
            # Outside the lock deliberately: the asking session is stopped
            # waiting for this, and waking it is a message to a third process
            # that has no business being inside the run's state transition.
            woken = responders.nudge(
                entry["from_session"], NUDGE_ANSWERED, cwd=entry["cwd"]
            )
            receipt["nudged"] = woken or None
            if not woken:
                receipt["note"] = (
                    f"{receipt['note']} — but {entry['from_session']!r} could "
                    f"not be nudged (no daemon, or the session is gone), so it "
                    f"may not notice until someone prompts it"
                )
            return receipt
    raise CflowError(
        f"no open request {ask_id!r} is waiting on you — it was answered, it "
        f"escalated past you, or its run moved on. Call 'asks' for what is "
        f"actually open"
    )


def _asked_handle(ask: dict, session: str) -> str:
    """The handle the asked list recorded for ``session`` (else the session)."""
    for entry in ask.get("asked") or []:
        if entry.get("session") == session:
            return str(entry.get("handle") or session)
    return session


def _closed(ask_id: str, state: dict) -> "CflowError":
    return CflowError(
        f"request {ask_id!r} is not open any more — it was answered, it timed "
        f"out and moved on, or the run left the step it belonged to. Nothing "
        f"was applied. Call 'asks' to see what is actually waiting on you"
    )


@_locked_op
def answer(
    ask_id: str,
    decision: str,
    reason: Optional[str] = None,
    *,
    by_session: str = "",
    cwd: Optional[str] = None,
) -> dict:
    """Record another session's decision on an open ask, and act on it.

    ``by_session`` is the answering session's name and is NOT the caller's to
    choose: the MCP layer reads it from the environment the daemon exported
    into that session, so it identifies the process rather than the claim. All
    the authority checking there is, is that this identity appears in the list
    frozen into the ask when it was opened.

    Nothing here delivers the asking step. A responder that received the
    asker's instructions would be a second agent working the run, which is the
    opposite of what a reviewer is for — it gets a receipt, and the asking
    session picks the run up through its own ``status``/``next``.
    """
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        raise _closed(ask_id, state)
    step = workflow.step(state["current"])
    visit = _visits(state, step.id)
    ask = state.get("ask")
    if (
        not ask
        or ask.get("id") != ask_id
        or ask.get("step") != step.id
        or ask.get("visit") != visit
    ):
        raise _closed(ask_id, state)

    session = str(by_session or "").strip()
    if not session:
        raise CflowError(
            "this call carries no session identity, so it cannot be attributed "
            "to anyone — a delegated decision has to be recorded against the "
            "session that made it. Answer from a managed session"
        )
    if session == state_mod.current_scope():
        # Unreachable through `responders.pool` (which excludes the asking
        # session), and checked anyway: this is the one invariant the whole
        # feature rests on, and it costs a comparison to prove rather than
        # assume.
        raise CflowError(
            "this is your own run — a step cannot approve itself. That is the "
            "entire point of delegating the decision"
        )
    if not any(
        e.get("kind") == "member" and e.get("session") == session
        for e in ask.get("asked") or []
    ):
        who = ", ".join(
            e.get("handle") or e["kind"] for e in ask.get("asked") or []
        ) or "nobody"
        raise CflowError(
            f"{session!r} was not asked this — it is with {who}. If it "
            f"escalated past you, it is no longer yours to answer"
        )

    choice = str(decision or "").strip().lower()
    allowed = [o["name"] for o in ask.get("options") or []]
    if choice not in allowed and choice != ABSTAIN:
        raise CflowError(
            f"unknown decision {decision!r} — answer with one of: "
            f"{', '.join(allowed)}, or {ABSTAIN!r} if you have no basis to "
            f"decide (which passes it on rather than guessing)"
        )
    note = (reason or "").strip()
    handle = _asked_handle(ask, session)
    # The question goes back with the answer. A responder working several of
    # these has only its own turn to tell them apart, and an id plus a verb is
    # too thin to check against: the receipt should be readable as a record of
    # what was decided, not just that something was.
    receipt = {
        **_base(state),
        "status": "answered",
        "step_id": step.id,
        "step_title": step.title or step.id,
        "ask": ask_id,
        "prompt": ask["prompt"],
        "decision": choice,
        "reason": note,
        "as": handle,
    }

    if choice == ABSTAIN:
        state_mod.journal(
            "ask_abstained",
            {"run": state["run_id"], "ask": ask_id, "step": step.id,
             "by": handle, "by_session": session, "reason": note},
            cwd,
        )
        reopened = _escalate(
            workflow, state, step, ask, cwd,
            why=f"{handle} abstained" + (f": {note}" if note else ""),
        )
        if reopened is not None:
            receipt["note"] = (
                "recorded as an abstention; the decision moved on to the next "
                "candidate group (or to a human if there is none)"
            )
        elif state.get("current") != step.id or state["status"] in ("done", "aborted"):
            # The branch's `otherwise: self:<option>` fired on this
            # abstention: the declared default took it, by nobody.
            receipt["note"] = (
                "recorded as an abstention; nobody else was left to ask, and "
                "the workflow's declared default took the decision unanswered "
                f"— the run moved to {state.get('current') or 'end'}"
            )
        else:
            receipt["note"] = (
                "recorded as an abstention; nobody else was left to ask, and "
                "this workflow says to carry on without an answer — the run has "
                "moved past it unapproved"
            )
        return receipt

    state["ask"] = None
    state_mod.journal(
        "ask_answered",
        {"run": state["run_id"], "ask": ask_id, "step": step.id, "visit": visit,
         "kind": ask["kind"], "decision": choice, "reason": note,
         "by": handle, "by_session": session, "in_group": True},
        cwd,
    )

    if ask["kind"] == "branch":
        state["completed"] += 1  # the decision itself counts as a step
        _move_to(workflow, state, step.select.options[choice].next, cwd)
        receipt["note"] = (
            "decision recorded and the run routed; the asking session is "
            "nudged to continue"
        )
        return receipt

    if choice == APPROVE:
        state["gate_approved"] = True
        state_mod.save_state(state, cwd)
        receipt["note"] = (
            "approval recorded; the asking session is nudged to continue"
        )
        return receipt

    # A refusal. Where it goes was declared (or deliberately not) by the
    # workflow, never chosen here — a responder decides the answer, not the
    # shape of the run.
    target = step.ask.on_decline
    state_mod.journal(
        "ask_declined",
        {"run": state["run_id"], "ask": ask_id, "step": step.id, "by": handle,
         "by_session": session, "reason": note, "route": target or "hold"},
        cwd,
    )
    if target is None:
        state["declined"] = {
            "step": step.id,
            "visit": visit,
            "by": handle,
            "by_session": session,
            "reason": note,
            "at": state_mod.utcnow(),
        }
        state_mod.save_state(state, cwd)
        receipt["note"] = (
            "refusal recorded; the workflow declares no decline route, so the "
            "run is held for a human"
        )
        return receipt
    _move_to(workflow, state, None if target == model.END else target, cwd)
    receipt["note"] = f"refusal recorded; the run was routed to {target!r}"
    return receipt


@_locked_op
def goto(
    step_id: str,
    *,
    by: str = "user",
    reason: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Force the run's position to an arbitrary step — a human override for
    when the graph and reality disagree (a step never got delivered, work
    must be redone, or a finished run needs reopening). ``end`` force-
    finishes. The move is journaled; the step itself is NOT delivered here —
    the agent fetches it with 'next' (so per-visit gates re-apply), which is
    why callers pair this with a session nudge.

    'next' and not 'status': a step whose entry is delegated only *becomes*
    a question somebody holds when the ask is opened, and opening one is a
    write. ``status`` reads with ``mutate=False`` and therefore cannot do it
    — by design, since a run must not change because somebody looked at it.
    So a run left here reports an approval "not put to anyone yet" until the
    agent calls 'next', and nothing but 'next' ends that.
    """
    workflow, state = _load(cwd)
    return _force_position(workflow, state, step_id, by=by, reason=reason, cwd=cwd)


def _force_position(
    workflow: Workflow,
    state: dict,
    step_id: str,
    *,
    by: str,
    reason: Optional[str],
    cwd: Optional[str],
    granted: Optional[dict] = None,
) -> dict:
    """The move itself, shared by the human's :func:`goto` and by the approval
    of an agent's :func:`request_goto`.

    ``granted`` is the request this move answers, when it answers one: it is
    journaled with the move, so the record says the position was *asked for*
    and by whom rather than reading as a bare human override.
    """
    target = None if step_id == model.END else step_id
    if target is not None:
        workflow.step(target)  # unknown id -> WorkflowError
    superseded = state.get("goto_request")
    if superseded is not None and superseded.get("decision"):
        superseded = None  # already answered; only a live one can be superseded
    state_mod.journal(
        "state_forced",
        {
            "run": state["run_id"],
            "from": state.get("current"),
            # Both spellings: 'to' is what this event has always carried, and
            # 'step' is the key every other event names its step with (the
            # dashboard's journal reader keys on that one).
            "to": step_id,
            "step": step_id,
            "by": by,
            "reason": (reason or "").strip(),
            **(
                {"granted": granted.get("id"), "asked_by": granted.get("by")}
                if granted
                else {}
            ),
        },
        cwd,
    )
    if granted is not None:
        state["goto_request"] = None
    elif superseded is not None:
        # A human forced the position while a request was pending. The move
        # answers the request whether or not it named this step, so it must
        # not stay behind and stop the run a second time.
        state["goto_request"] = None
        state_mod.journal(
            "goto_superseded",
            {
                "run": state["run_id"],
                "request": superseded.get("id"),
                "step": superseded.get("step"),
                "forced_to": step_id,
                "by": by,
            },
            cwd,
        )
    if state["status"] in ("done", "aborted"):
        state["status"] = "running"  # a forced goto can reopen a finished run
    _move_to(workflow, state, target, cwd)
    if state["status"] == "done":
        return _done_payload(state, cwd)
    return {
        **_base(state),
        "status": "state_set",
        "step_id": target,
        "visit": _visits(state, target),
        "note": (
            "position forced; the agent picks the step up via 'next' "
            "- nudge it to continue"
        ),
    }


@_locked_op
def request_goto(
    step_id: str,
    reason: str,
    *,
    by: str = "agent",
    via: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """The agent's side of an off-graph move: ask for a position the workflow
    declares no transition to, and let a person grant or refuse it.

    Why a request and not a move. The graph is the run's account of what may
    happen, and an agent that could re-position itself could also walk out of
    any gate the graph puts in front of it -- which is why :func:`goto` is a
    human command and why the harness deny rules keep the agent's shell off
    it. But reality does out-run the graph: a merge turns up work belonging to
    a step already passed. Until now that ended as a permission failure with
    nothing recorded and nobody asked. This is the missing half -- the agent
    states where it must go and why, the run stops instead of advancing, and a
    human answers with :func:`resolve_goto` (or overrides with :func:`goto`,
    which supersedes the request).

    Deliberately NOT a case in :func:`_blocked`: a gate withholds a step's
    content on entry, while this holds a run whose step has already been
    delivered and reported on. Conflating the two would re-deliver the step on
    every poll and drop the report already filed against it.

    ``via`` names the door the request came through when it is not the
    driving agent's own: ``"leader"`` is a parent session asking for this
    run to move (see daemon/goto_gate.py), filed here so the record, the
    hold, and the settlement reuse this one path. It rides on the request
    and the journal, and changes what the held run tells its driver.
    """
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        raise CflowError(
            f"this run is {state['status']}; there is no position to move. A "
            f"finished run is reopened by a human with 'claunch cflow goto "
            f"<step>'{_t_hint()}"
        )
    target = None if step_id == model.END else step_id
    if target is not None:
        workflow.step(target)  # unknown id -> WorkflowError
    note = (reason or "").strip()
    if not note:
        raise CflowError(
            "'reason' is required: the person answering has not watched you "
            "work, and a step id on its own is not something anyone can "
            "approve or refuse"
        )
    if target is not None and target == state.get("current"):
        raise CflowError(f"the run is already at {target!r} - nothing to request")
    previous = state.get("goto_request")
    request = {
        "id": f"gr-{secrets.token_hex(3)}",
        "step": step_id,
        "reason": note,
        "by": by,
        "from": state.get("current"),
        "visit": _visits(state, state["current"]) if state.get("current") else 0,
        "at": state_mod.utcnow(),
        **({"via": via} if via else {}),
    }
    state["goto_request"] = request
    state_mod.save_state(state, cwd)
    state_mod.journal(
        "goto_requested",
        {
            "run": state["run_id"],
            "request": request["id"],
            "step": step_id,
            "from": request["from"],
            "by": by,
            "reason": note,
            **({"via": via} if via else {}),
            **(
                {"replaces": previous.get("id")}
                if previous and not previous.get("decision")
                else {}
            ),
        },
        cwd,
    )
    return {
        **_base(state),
        "status": "goto_requested",
        "step_id": state.get("current"),
        "goto_request": request,
        "note": (
            "recorded; the run will not advance until a person answers. Stop "
            "your turn and write them the decision brief -- where the run has "
            "to go, what you found that the workflow declared no route for, "
            "what redoing that step costs, and what continuing on the declared "
            "route costs. They answer with 'claunch cflow goto --approve' or "
            f"'claunch cflow goto --deny'{_t_hint()}, or from the dashboard's "
            "workflow panel; they may also send the run somewhere else "
            "entirely with 'claunch cflow goto <step>'"
        ),
        "how_to_unblock": _asking_well(
            f"They are being asked to let this run leave the route its "
            f"workflow declares, for {step_id!r}."
        ),
    }


@_locked_op
def cancel_goto_request(*, by: str = "agent", cwd: Optional[str] = None) -> dict:
    """Withdraw a pending step-change request -- the agent found its own way
    forward, or the reason stopped being true, and nobody should be left
    holding a question no answer is worth giving to."""
    _workflow, state = _load(cwd)
    pending = state.get("goto_request")
    if not pending or pending.get("decision"):
        raise CflowError("no pending step-change request on this run")
    state["goto_request"] = None
    state_mod.save_state(state, cwd)
    state_mod.journal(
        "goto_withdrawn",
        {
            "run": state["run_id"],
            "request": pending.get("id"),
            "step": pending.get("step"),
            "by": by,
        },
        cwd,
    )
    return {
        **_base(state),
        "status": "goto_withdrawn",
        "step_id": state.get("current"),
        "note": "request withdrawn; the run continues from where it stands",
    }


@_locked_op
def resolve_goto(
    decision: str,
    *,
    by: str = "user",
    reason: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Answer a pending step-change request. CLI / dashboard only.

    ``approve`` performs the move that was asked for; ``deny`` leaves the
    position alone and hands the refusal back to the agent, which reads it on
    its next ``status``/``next`` and carries on down the declared route. Not
    exposed over MCP for the same reason ``approve`` is not: an
    agent-callable grant is not a grant.
    """
    verdict = (decision or "").strip().lower()
    if verdict not in ("approve", "deny"):
        raise CflowError(f"decision must be 'approve' or 'deny', not {decision!r}")
    workflow, state = _load(cwd)
    pending = state.get("goto_request")
    if not pending or pending.get("decision"):
        raise CflowError("no pending step-change request on this run")
    note = (reason or "").strip()
    if verdict == "approve":
        state_mod.journal(
            "goto_approved",
            {
                "run": state["run_id"],
                "request": pending.get("id"),
                "step": pending.get("step"),
                "by": by,
                "reason": note,
            },
            cwd,
        )
        payload = _force_position(
            workflow,
            state,
            pending["step"],
            by=by,
            reason=note or pending.get("reason") or "",
            cwd=cwd,
            granted=pending,
        )
        payload["goto_request"] = {**pending, "decision": "approved"}
        return payload
    decided = {
        **pending,
        "decision": "denied",
        "decided_by": by,
        "decided_reason": note,
        "decided_at": state_mod.utcnow(),
    }
    state["goto_request"] = decided
    state_mod.save_state(state, cwd)
    state_mod.journal(
        "goto_denied",
        {
            "run": state["run_id"],
            "request": pending.get("id"),
            "step": pending.get("step"),
            "by": by,
            "reason": note,
        },
        cwd,
    )
    return {
        **_base(state),
        "status": "goto_denied",
        "step_id": state.get("current"),
        "goto_request": decided,
        "note": (
            "refusal recorded; the run stays where it is and the agent is told "
            "on its next 'status' or 'next' - nudge it to continue"
        ),
    }


def _pending_goto(state: dict) -> Optional[dict]:
    """The live (unanswered) step-change request on this run, if any."""
    request = state.get("goto_request")
    if isinstance(request, dict) and not request.get("decision"):
        return request
    return None


def _goto_payload(state: dict, request: dict) -> dict:
    """The stop a pending request puts the run in."""
    if request.get("via") == "leader":
        # Filed by a parent session through the daemon's goto gate, not by
        # this run's driver: the note names who asked, and withdrawal is the
        # driver's visible act (journaled, attributed), not a quiet escape.
        asker = request.get("by") or "your leader"
        reason = request.get("reason") or "no reason recorded"
        note = (
            f"{asker} asked for this run to be moved to "
            f"{request.get('step')!r} ({reason}) and a person has not "
            f"answered yet; the run does not advance until they do. Stop "
            f"your turn. If the reason stopped being true, answer {asker} "
            f"— withdrawing the request yourself is journaled under your "
            f"name, and it is their call to re-file"
        )
    else:
        note = (
            f"you asked for this run to be moved to {request.get('step')!r} and "
            f"nobody has answered yet; it does not advance until they do. Stop "
            f"your turn. If the reason stopped being true, withdraw the request "
            f"('request_goto' with cancel) rather than leaving a question no "
            f"answer helps"
        )
    return {
        **_base(state),
        "status": "waiting_goto",
        "step_id": state.get("current"),
        "goto_request": request,
        "note": note,
        "how_to_unblock": _asking_well(
            f"They are being asked to let this run leave the route its "
            f"workflow declares, for {request.get('step')!r}."
        ),
    }


@_locked_op
def approve(*, by: str = "user", cwd: Optional[str] = None) -> dict:
    """Unblock the current entry approval or loop guard. CLI-only.

    Covers all four ways a step can be held shut — a ``gate``, a delegated
    ``ask`` that reached a human, one that reached nobody, and a decline the
    workflow declared no route for — because they are one thing to the person
    looking at them: the run is stopped and they have decided it may proceed.
    Still not exposed over MCP: an agent-callable approval is not an approval.
    """
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        return _done_payload(state, cwd)
    step = workflow.step(state["current"])
    blocked = _blocked(workflow, state)
    if blocked == "loop_limit":
        state["loop_extensions"][step.id] = (
            int(state["loop_extensions"].get(step.id, 0)) + 1
        )
        state_mod.save_state(state, cwd)
        state_mod.journal(
            "loop_extended",
            {"run": state["run_id"], "step": step.id, "by": by,
             "new_limit": _limit(workflow, state, step.id)},
            cwd,
        )
        return {
            **_base(state),
            "status": "approved",
            "step_id": step.id,
            "note": (
                f"loop limit extended to {_limit(workflow, state, step.id)} "
                f"visits; nudge the agent to continue"
            ),
        }
    if blocked in ("gate", "ask", "declined"):
        visit = _visits(state, step.id)
        open_ask = _current_ask(state, step.id, visit, "approval")
        if open_ask:
            # The human is answering the delegated question, whether or not
            # they were one of the candidates — see `_settled_by_person`, which
            # records which of the two that was and tells anyone it overrode.
            _settled_by_person(state, open_ask, decision=APPROVE, by=by, cwd=cwd)
        state["ask"] = None
        overridden = state.get("declined") if blocked == "declined" else None
        state["declined"] = None
        state["gate_approved"] = True
        state_mod.save_state(state, cwd)
        state_mod.journal(
            "approved",
            {"run": state["run_id"], "step": step.id, "by": by,
             "visit": visit,
             **({"overrode_decline": overridden} if overridden else {})},
            cwd,
        )
        # Do NOT deliver here: the agent must fetch its own instructions via
        # 'next'/'status', otherwise the step would count as handed out unseen.
        return {
            **_base(state),
            "status": "approved",
            "step_id": step.id,
            "note": "gate approved; nudge the agent to continue",
        }
    raise CflowError(f"current step {step.id!r} has nothing waiting for approval")


#: Told to an agent whose 'status' turns up a human's start request. The
#: agent still performs the start, which is the whole point: it cannot end up
#: driving a run it never read.
REQUEST_NOTE = (
    "a human asked for this workflow to be started here — confirm it is what "
    "you should be doing, then call 'start' with {workflow, context} using "
    "exactly this workflow (its context is the requester's own words). If it "
    "looks wrong, do not start it: say so and stop"
)


@_scoped_op
def status(cwd: Optional[str] = None) -> dict:
    pending = state_mod.read_request(cwd)
    if not state_mod.has_run(cwd):
        payload = {"status": "idle", "note": "no active cflow run in this directory"}
        if pending:
            payload["pending_start"] = pending
            payload["note"] = REQUEST_NOTE
        return payload
    workflow, state = _load(cwd)
    payload = _payload(workflow, state, cwd, mutate=False)
    # Said by every status, not only the graph payload: the runs list, the
    # session panel and the run page all read this, and "this run loops"
    # changes what its controls should offer (the dashboard's Reset).
    if workflow.recur:
        payload["recur"] = True
    payload["visits"] = dict(state["visits"])
    payload["started_at"] = state.get("started_at")
    # Which file this run is a snapshot of. The run itself reads the snapshot
    # from here on, so the source is history, not a live dependency — but it
    # is the only thing that answers "the project one or the global one?"
    # once two layers declare the same name.
    if state.get("source"):
        payload["source"] = state["source"]
        payload["origin"] = state.get("origin") or ""
        # And, when the source was a layer over something, what it was a layer
        # OVER: "the project one" stops being an answer the moment the project
        # file is four lines of verify on top of the packaged workflow.
        if state.get("bases"):
            payload["extends"] = list(state["bases"])
    if state.get("context"):
        payload["context"] = state["context"]
    if state.get("current") and _current_report(state, state["current"]):
        payload["report"] = state["report"]
    if pending:
        # Only reachable when the run finished after the request was filed
        # (an active run refuses one) — the next start will consume it.
        payload["pending_start"] = pending
    if state.get("reminder"):
        # The per-run override only — the machine defaults are the daemon's
        # (store.daemon_config) and are reported by its API, not by a run.
        payload["reminder"] = dict(state["reminder"])
    request = state.get("goto_request")
    if isinstance(request, dict):
        payload["goto_request"] = request
        if not request.get("decision"):
            # The stop overrides whatever position status would otherwise
            # report: the dashboard and the agent both dispatch on this word,
            # and "step" here would read as "nothing needs a human".
            payload.update(_goto_payload(state, request))
    return payload


@_scoped_op
def recall(digest: str = "", cwd: Optional[str] = None) -> dict:
    """The instructional text behind a content id, for an agent that lost it.

    The pull half of the reminder's push. A repeat reminder names the id of
    the text it is NOT re-pasting; an agent that cannot find that id in its
    own context calls this, and gets the text back.

    Read-only, and deliberately so: this is called by an agent that has just
    discovered it lost something, which is the worst moment to also advance
    a run as a side effect. It does not mark delivery and it does not open a
    delegated ask — ``status`` at least reports a position, and this reports
    only text.

    Three answers, and the middle one is the reason this is not just
    ``status``:

    * the id names this run's current position — its text, with the id, so
      the agent can see it matched.
    * the id names something else — said plainly. An id from a step the run
      has since left is not an error on the agent's part (it was told that
      id once, truthfully), and the honest answer is that the position moved
      and ``status`` holds what is true now. Serving the old text here would
      be worse than the silence: the agent would work from a step it is no
      longer on.
    * there is no run here at all — the ordinary idle answer.
    """
    digest = (digest or "").strip().lower()
    if not digest:
        raise CflowError("'id' is required -- pass the id from the block header")
    if not state_mod.has_run(cwd):
        return {"status": "idle", "note": "no active cflow run in this directory"}
    # Loaded here rather than through `status`: this needs the position and
    # nothing status wraps around it (pending starts, the reminder override,
    # loop bookkeeping), and reading the slot once is the point of a tool an
    # agent calls when it is already behind.
    workflow, state = _load(cwd)
    payload = _payload(workflow, state, cwd, mutate=False)
    current = payload.get("digest") or ""
    # Journalled, though this is otherwise read-only, and the exception is
    # deliberate: the whole push-to-pull design is priced on how often agents
    # actually make this call, and until it is recorded that number can only
    # be guessed at. `journal` is a lock-free append and touches no run state,
    # so recording the question does not answer it differently.
    state_mod.journal(
        "recall",
        {"run": state.get("run_id"), "id": digest, "step": payload.get("step_id"),
         "hit": bool(current and current == digest)},
        cwd,
    )
    if current and current == digest:
        out = {
            "status": "recalled",
            "id": digest,
            "run": payload.get("run"),
            "workflow": payload.get("workflow"),
            "step_id": payload.get("step_id"),
            "visit": payload.get("visit"),
            "note": (
                "this is the text you were given under this id, unchanged. "
                "The position has not moved; carry on with it."
            ),
        }
        for key in ("instructions", "done_when", "verify", "prompt", "options"):
            if payload.get(key):
                out[key] = payload[key]
        return out
    return {
        "status": "stale_id",
        "id": digest,
        "current_id": current,
        "run": payload.get("run"),
        "step_id": payload.get("step_id"),
        "note": (
            f"id {digest} is not this run's current position -- the run moved "
            "on after you were given it. Nothing here can hand you that text, "
            "and the text you want is the position you are on now: call "
            "'status'. Do not act on whatever you remember of the old step."
        ),
    }


#: The floor for a per-run reminder interval. Below this a reminder is not a
#: reminder, it is the daemon talking over the agent's own typing.
REMINDER_MIN_INTERVAL = 30.0


@_locked_op
def set_reminder(
    enabled: Optional[bool] = None,
    interval: Optional[float] = None,
    *,
    by: str = "user",
    cwd: Optional[str] = None,
) -> dict:
    """Set (or clear) this run's reminder override.

    The reminder itself is the daemon's clock (see
    :class:`daemon.cflow_clock.ReminderClock`): while a run sits on an
    agent-actionable position with no progress, the current step's
    instructions are re-typed into the driving session every ``interval``
    seconds. What is stored here is only this run's departure from the
    machine defaults — ``enabled`` and/or ``interval``, each optional, merged
    over whatever was set before. Both ``None`` clears the override, so the
    run follows the defaults again.

    Kept in run state rather than config because the setting is *this run's*:
    it is archived with the run, and the next run in the slot starts back on
    the defaults instead of inheriting a tuning nobody remembers making.
    """
    if not state_mod.has_run(cwd):
        raise CflowError("no active cflow run here to set a reminder on")
    _, state = _load(cwd)
    if enabled is None and interval is None:
        state.pop("reminder", None)
    else:
        override = dict(state.get("reminder") or {})
        if enabled is not None:
            override["enabled"] = bool(enabled)
        if interval is not None:
            interval = float(interval)
            if interval < REMINDER_MIN_INTERVAL:
                raise CflowError(
                    f"reminder interval must be at least "
                    f"{REMINDER_MIN_INTERVAL:.0f}s, got {interval:g}"
                )
            override["interval"] = interval
        state["reminder"] = override
    state_mod.save_state(state, cwd)
    state_mod.journal(
        "reminder_set",
        {"run": state["run_id"], "by": by,
         "reminder": state.get("reminder")},
        cwd,
    )
    return {**_base(state), "reminder": state.get("reminder")}


def current_run_id(
    cwd: Optional[str] = None, scope: Optional[str] = None
) -> Optional[str]:
    """The run id on disk for a slot, or None when it holds no run.

    Read-only and cheap: the MCP server calls it before every mutating tool to
    notice that the run it has been driving was replaced underneath it.
    """
    token = state_mod.push_scope(scope)
    try:
        if not state_mod.has_run(cwd):
            return None
        return str(state_mod.load_state(cwd).get("run_id") or "") or None
    except state_mod.StateError:
        return None
    finally:
        state_mod.pop_scope(token)


@_locked_op
def abort(*, by: str = "user", cwd: Optional[str] = None) -> dict:
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        return _done_payload(state, cwd)
    state["status"] = "aborted"
    state_mod.save_state(state, cwd)
    state_mod.journal("aborted", {"run": state["run_id"], "by": by}, cwd)
    return _done_payload(state, cwd)


@_locked_op
def archive(*, by: str = "user", cwd: Optional[str] = None) -> dict:
    """Retire the current run — finished or not — into the scope's archive
    folder, freeing the slot for a new ``start``. An active run is aborted
    first; state, workflow snapshot, and journal all move together."""
    state = state_mod.load_state(cwd)
    was = state.get("status")
    dest = _archive_current(state, by, cwd)
    return {
        "run": state["run_id"],
        "workflow": state.get("workflow"),
        "status": "archived",
        "was": was,
        "archived_to": dest,
    }


@_scoped_op
def reset(cwd: Optional[str] = None) -> None:
    """Clear run state (the journal is kept for the record)."""
    state_mod.clear_state(cwd)
