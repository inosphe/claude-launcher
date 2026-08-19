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
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from . import model, responders, state as state_mod
from .model import Delegate, Step, Workflow

#: Kept from a verify command's combined output when reporting failure.
VERIFY_OUTPUT_TAIL = 4000

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
NUDGE_STARTED = (
    "cflow: a new workflow run was started - continue per the /cflow protocol"
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
# delegated decisions
# --------------------------------------------------------------------------- #
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
    approved it, and here nobody did. For a branch there is nothing to record
    at all — the select simply becomes the ordinary agent-chooses decision it
    would have been without a ``from``.
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
) -> Optional[dict]:
    """Put the decision to the first candidate group that resolves to anyone.

    Groups are tried in declared order and every one that resolves to nobody
    is recorded in ``skipped`` with its reason, so a question that reaches a
    human arrives with the account of why no agent took it. Running out of
    groups is not an error — it is where the second axis takes over:
    ``otherwise: human`` leaves the ask open with nobody in it (the hold state
    the CLI has always answered), and ``otherwise: self`` returns ``None``,
    having let the run carry on alone.

    ``ask_id`` is carried across an escalation on purpose — the decision is
    the same one, so a responder from an earlier group that answers late is
    told it is no longer theirs to answer rather than that the id is unknown.
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
    while group < len(candidates):
        candidate = candidates[group]
        found, reason = reach.match(candidate)
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

    if not asked and delegate.otherwise == model.OTHERWISE_SELF:
        _proceed_alone(
            state, step, kind=kind, prompt=prompt, skipped=skipped, cwd=cwd
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
        "deadline": _deadline(delegate.timeout) if asked else None,
    }
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
        "ask_unresolved" if not asked else "ask_opened",
        {
            "run": state["run_id"],
            "ask": ask["id"],
            "step": step.id,
            "visit": ask["visit"],
            "kind": kind,
            "group": group,
            "asked": [e.get("handle") or e["kind"] for e in asked],
            "skipped": [s["reason"] for s in skipped],
            **({"undelivered": ask["undelivered"]} if ask.get("undelivered") else {}),
        },
        cwd,
    )
    return ask


def _escalate(state: dict, step: Step, ask: dict, cwd, why: str) -> Optional[dict]:
    """Hand the same decision to the next candidate group.

    The one path that serves all three ways a group can fail to produce an
    answer — nobody resolved, everybody abstained, and nobody replying in
    time. That is the whole reason the preference list is one declaration
    instead of separate "fallback" and "escalation" settings: they are the
    same list read on different triggers.

    ``None`` back means the list ran out under ``otherwise: self`` and the run
    has already moved past the question.
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


def _ask_payload(base: dict, ask: dict) -> dict:
    """How an open ask is described to whoever reads the run.

    A question waiting on an *agent* is its own status, because it is a state
    no operator control resolves — the run is not stuck on a person. Once it
    falls to a human it is reported as the plain approval/selection it has
    become, so the CLI and the dashboard keep working on it unchanged.
    """
    payload = {**base, "ask": ask}
    if not _awaits_human(ask):
        who = ", ".join(e.get("handle", "?") for e in ask["asked"])
        payload["status"] = "waiting_answer"
        payload["reason"] = ask["kind"]
        payload["how_to_unblock"] = (
            f"{who} was asked to decide this and has not answered yet. You "
            f"cannot answer it yourself. Stop your turn, present what you have "
            f"so far, and wait to be nudged."
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
            f"an option button on the daemon web dashboard. Stop your turn, "
            f"present your recommendation and reasoning, and wait to be nudged."
        )
    else:
        payload["status"] = "waiting_approval"
        payload["reason"] = "ask"
        payload["gate"] = ask["prompt"]
        payload["how_to_unblock"] = (
            f"a human must approve: 'claunch cflow approve' (inside a chat "
            f"session: '! claunch cflow approve'{_t_hint()}) or the Approve "
            f"button on the daemon web dashboard; the agent cannot approve. "
            f"Stop your turn, present your work so far, and wait to be nudged."
        )
    if unresolved:
        payload["note"] = (
            "this was meant to be answered by another agent, but no candidate "
            "could be reached — it is in front of a human instead. Tell the "
            "user that, and why (see ask.skipped)."
        )
    return payload


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
    state["delivered"] = False
    state["gate_approved"] = False
    state["gate_logged"] = None
    state["pending_select"] = None
    state["ask"] = None
    state["declined"] = None
    state["unanswered"] = None
    state["report"] = None
    if target is None:
        state["current"] = None
        state["status"] = "done"
        state_mod.save_state(state, cwd)
        state_mod.journal("done", {"run": state["run_id"]}, cwd)
        if workflow.recur:
            _request_next_round(state, cwd)
        return
    state["current"] = target
    state["visits"][target] = _visits(state, target) + 1
    state_mod.save_state(state, cwd)


def _request_next_round(state: dict, cwd) -> None:
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
        "at": state_mod.utcnow(),
    }
    state_mod.write_request(request, cwd)
    state_mod.journal(
        "recur_requested",
        {"run": state["run_id"], "request": request["id"], "round": request["round"]},
        cwd,
    )


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
            payload["note"] = (
                "round finished; this workflow recurs — report this round's "
                "journal to the user, then start the next round: call 'start' "
                "with exactly the requested workflow and context. Do not "
                "invent a reason to stop: only a human ends the loop "
                "('claunch cflow request --cancel', or archiving the run)"
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


def _payload(workflow: Workflow, state: dict, cwd: Optional[str], *, mutate: bool) -> dict:
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
                f"or the Approve button on the daemon web dashboard. Stop your "
                f"turn, explain why the loop keeps repeating, and wait to be "
                f"nudged."
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
                f"to send the run somewhere else{_t_hint()}. Stop your turn, "
                f"relay the refusal and its reason to the user, and wait to be "
                f"nudged."
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
                f"cannot approve. Stop your turn, present your work so far, "
                f"and wait to be nudged."
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
                # question as a side effect of somebody watching.
                return {
                    **base,
                    "status": "waiting_answer",
                    "reason": "approval",
                    "prompt": step.ask.prompt,
                    "note": "this approval has not been put to anyone yet",
                }
            ask = _open_ask(
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
                    return {
                        **base,
                        "status": "waiting_answer",
                        "reason": "branch",
                        "prompt": step.select.prompt,
                        "options": options,
                        "note": "this decision has not been put to anyone yet",
                    }
                ask = _open_ask(
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
            chooser = "agent"  # nobody to ask; this run decides it after all
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
                    f"daemon web dashboard. Stop your turn, present your "
                    f"recommendation and reasoning, and wait to be nudged."
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
            "otherwise": delegate.otherwise,
        }
        reasons = []
        for candidate in delegate.candidates:
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
    text = path.read_text(encoding="utf-8")
    workflow = model.parse(text, default_name=path.stem)

    fulfilled = bool(pending) and (
        pending.get("workflow") == workflow_ref
        or pending.get("resolved") == str(path)
    )
    # Fulfilling a recurring run's own request carries its round count into
    # the new run; every other start — a human's request, a fresh loop, a
    # non-recurring workflow — is round 1 again.
    round_no = 1
    if fulfilled and pending.get("by") == "recur":
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
        "report": None,
        "completed": 0,
        "visits": {workflow.start: 1},
        "loop_extensions": {},
        "round": round_no,
    }
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
            "context": context or "",
            "total_steps": workflow.step_count(),
            "warnings": workflow.warnings,
            **({"round": round_no} if round_no > 1 else {}),
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
    check = delegation_check(workflow, mesh=state["mesh"], cwd=cwd)
    if check:
        payload["delegation_check"] = check
    return payload


@_locked_op
def request_start(
    workflow_ref: str,
    context: Optional[str] = None,
    *,
    by: str = "web",
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
    path = state_mod.find_workflow(workflow_ref, cwd)
    workflow = model.parse(path.read_text(encoding="utf-8"), default_name=path.stem)
    request = {
        "id": f"req-{secrets.token_hex(3)}",
        "workflow": workflow_ref,
        "name": workflow.name,
        "resolved": str(path),
        "context": (context or "").strip(),
        "by": by,
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


def _fence(state: dict) -> tuple:
    """The identity of 'the position a long operation was started from'."""
    current = state.get("current") or ""
    return (
        state.get("run_id"),
        state.get("status"),
        current,
        _visits(state, current) if current else 0,
    )


def _advance(workflow: Workflow, state: dict, step: Step, filed: dict, cwd) -> dict:
    """Journal the step's completion and move to its successor."""
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
    _move_to(workflow, state, step.next, cwd)
    if state["status"] == "done":
        return _done_payload(state, cwd)
    return _payload(workflow, state, cwd, mutate=True)


@_scoped_op
def next_step(*, cwd: Optional[str] = None) -> dict:
    with state_mod.run_lock(cwd):
        workflow, state = _load(cwd)
        if state["status"] in ("done", "aborted"):
            return _done_payload(state, cwd)
        step = workflow.step(state["current"])

        if not state["delivered"] or _blocked(workflow, state):
            # Nothing has been handed out yet (fresh arrival, or a gate/loop
            # guard is closed): (re)attempt delivery.
            return _payload(workflow, state, cwd, mutate=True)

        if step.is_select:
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
    result = _run_verify(step, cwd)

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
                "note": (
                    "the step's verify command failed; fix the problem, file a "
                    "new 'report', and call 'next' again (the command will be "
                    "re-run)"
                ),
            }
        state_mod.journal(
            "verify_passed",
            {"run": state["run_id"], "step": step.id, "command": step.verify.command},
            cwd,
        )
        filed = _current_report(state, step.id) or filed
        return _advance(workflow, state, step, filed, cwd)


def _run_verify(step: Step, cwd: Optional[str]) -> Optional[dict]:
    """Run the step's verify command; None on success, failure details otherwise."""
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

    open_ask = _current_ask(state, step.id, _visits(state, step.id), "branch")
    if open_ask:
        # A human settling a delegated decision — from the dashboard, or
        # because the responders never got to it. Close the question here so
        # `_move_to` does not log it as one the run walked away from.
        state["ask"] = None
        state_mod.journal(
            "ask_answered",
            {"run": state["run_id"], "ask": open_ask["id"], "step": step.id,
             "decision": option, "by": by, "by_session": None,
             "in_group": _awaits_human(open_ask)},
            cwd,
        )
    state_mod.journal(
        "select_confirmed",
        {"run": state["run_id"], "step": step.id, "option": option,
         "reason": (reason or "").strip(), "by": by,
         "visit": _visits(state, step.id)},
        cwd,
    )
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
    reopened = _escalate(
        state, step, ask, cwd, why=f"{who or 'nobody'} did not answer by {deadline}"
    )
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
            state, step, ask, cwd,
            why=f"{handle} abstained" + (f": {note}" if note else ""),
        )
        receipt["note"] = (
            "recorded as an abstention; the decision moved on to the next "
            "candidate group (or to a human if there is none)"
            if reopened is not None
            else "recorded as an abstention; nobody else was left to ask, and "
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
    the agent fetches it via 'next'/'status' (so per-visit gates re-apply),
    which is why callers pair this with a session nudge.
    """
    workflow, state = _load(cwd)
    target = None if step_id == model.END else step_id
    if target is not None:
        workflow.step(target)  # unknown id -> WorkflowError
    state_mod.journal(
        "state_forced",
        {
            "run": state["run_id"],
            "from": state.get("current"),
            "to": step_id,
            "by": by,
            "reason": (reason or "").strip(),
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
            "position forced; the agent picks the step up via 'next'/'status' "
            "- nudge it to continue"
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
            # they were one of the candidates. That is not a hole to close:
            # the CLI is the trusted channel by construction — anyone holding
            # it can already `goto` the run anywhere — so the honest thing is
            # to record whether the answer came from inside the asked group,
            # not to pretend the override is impossible.
            state_mod.journal(
                "ask_answered",
                {"run": state["run_id"], "ask": open_ask["id"], "step": step.id,
                 "decision": APPROVE, "by": by, "by_session": None,
                 "in_group": _awaits_human(open_ask)},
                cwd,
            )
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
    payload["visits"] = dict(state["visits"])
    payload["started_at"] = state.get("started_at")
    # Which file this run is a snapshot of. The run itself reads the snapshot
    # from here on, so the source is history, not a live dependency — but it
    # is the only thing that answers "the project one or the global one?"
    # once two layers declare the same name.
    if state.get("source"):
        payload["source"] = state["source"]
        payload["origin"] = state.get("origin") or ""
    if state.get("context"):
        payload["context"] = state["context"]
    if state.get("current") and _current_report(state, state["current"]):
        payload["report"] = state["report"]
    if pending:
        # Only reachable when the run finished after the request was filed
        # (an active run refuses one) — the next start will consume it.
        payload["pending_start"] = pending
    return payload


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
