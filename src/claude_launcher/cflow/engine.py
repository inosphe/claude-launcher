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
from typing import Optional

from . import model, state as state_mod
from .model import Step, Workflow

#: Kept from a verify command's combined output when reporting failure.
VERIFY_OUTPUT_TAIL = 4000

#: Typed into the run directory's managed sessions after a human unblocks the
#: run, so a stopped agent resumes without a manual nudge. Per the /cflow
#: skill, any user message makes the agent re-check 'status' first.
NUDGE_APPROVED = "cflow: approved - continue per the /cflow protocol"
NUDGE_SELECTED = "cflow: selection confirmed - continue per the /cflow protocol"
NUDGE_CONTINUE = "cflow: continue per the /cflow protocol"
NUDGE_STARTED = (
    "cflow: a new workflow run was started - continue per the /cflow protocol"
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
    return {
        "run": state["run_id"],
        "workflow": state["workflow"],
        "steps_completed": state["completed"],
    }


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


def _move_to(workflow: Workflow, state: dict, target: Optional[str], cwd) -> None:
    """Advance to ``target`` (None = termination)."""
    state["delivered"] = False
    state["gate_approved"] = False
    state["gate_logged"] = None
    state["pending_select"] = None
    state["report"] = None
    if target is None:
        state["current"] = None
        state["status"] = "done"
        state_mod.save_state(state, cwd)
        state_mod.journal("done", {"run": state["run_id"]}, cwd)
        return
    state["current"] = target
    state["visits"][target] = _visits(state, target) + 1
    state_mod.save_state(state, cwd)


def _done_payload(state: dict, cwd: Optional[str]) -> dict:
    entries = state_mod.read_journal(cwd, run_id=state["run_id"])
    summaries = [
        {"step": e.get("step"), "summary": e.get("summary"), "details": e.get("details")}
        for e in entries
        if e.get("event") == "step_completed"
    ]
    return {
        **_base(state),
        "status": state["status"],  # done | aborted
        "journal": summaries,
        "note": "workflow finished; report the journal to the user",
    }


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
                "a human must extend the loop limit: 'claunch cflow approve' "
                "(inside a chat session: '! claunch cflow approve') or the "
                "Approve button on the daemon web dashboard. Stop your turn, "
                "explain why the loop keeps repeating, and wait to be nudged."
            ),
        }
        if mutate and state.get("gate_logged") != f"loop:{step.id}:{visit}":
            state["gate_logged"] = f"loop:{step.id}:{visit}"
            state_mod.journal(
                "loop_limit", {"run": state["run_id"], "step": step.id, "visit": visit}, cwd
            )
            state_mod.save_state(state, cwd)
        return payload

    # Human entry gate: re-required on every visit.
    if step.gate and not state["gate_approved"]:
        payload = {
            **base,
            "status": "waiting_approval",
            "reason": "gate",
            "gate": step.gate,
            "how_to_unblock": (
                "a human must approve: 'claunch cflow approve' in this "
                "directory (inside a chat session: '! claunch cflow approve') "
                "or the Approve button on the daemon web dashboard; the agent "
                "cannot approve. Stop your turn, present your work so far, and "
                "wait to be nudged."
            ),
        }
        if mutate and state.get("gate_logged") != f"gate:{step.id}:{visit}":
            state["gate_logged"] = f"gate:{step.id}:{visit}"
            state_mod.journal(
                "gate_wait", {"run": state["run_id"], "step": step.id, "visit": visit}, cwd
            )
            state_mod.save_state(state, cwd)
        return payload

    if step.is_select:
        pending = state.get("pending_select")
        options = [
            {"name": o.name, "description": o.description}
            for o in step.select.options.values()
        ]
        if pending and pending.get("step") == step.id:
            return {
                **base,
                "status": "waiting_selection",
                "prompt": step.select.prompt,
                "options": options,
                "proposal": pending,
                "how_to_unblock": (
                    "a human must confirm with 'claunch cflow select <option>' "
                    "(inside a chat session: '! claunch cflow select <option>') "
                    "or an option button on the daemon web dashboard. Stop "
                    "your turn, present your recommendation and reasoning, "
                    "and wait to be nudged."
                ),
            }
        payload = {
            **base,
            "status": "select",
            "prompt": step.select.prompt,
            "chooser": step.select.chooser,
            "options": options,
            "note": (
                "decide and call the 'select' tool with {option, reason}"
                if step.select.chooser == "agent"
                else "call 'select' once with your recommendation; a human then "
                "confirms out-of-band ('claunch cflow select <option>')"
            ),
        }
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
    cwd: Optional[str] = None,
) -> dict:
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
    path = state_mod.find_workflow(workflow_ref, cwd)
    text = path.read_text(encoding="utf-8")
    workflow = model.parse(text, default_name=path.stem)

    state = {
        "run_id": f"run-{secrets.token_hex(4)}",
        "workflow": workflow.name,
        "source": str(path),
        "context": context or "",
        "started_at": state_mod.utcnow(),
        "status": "running",
        "current": workflow.start,
        "delivered": False,
        "gate_approved": False,
        "gate_logged": None,
        "pending_select": None,
        "report": None,
        "completed": 0,
        "visits": {workflow.start: 1},
        "loop_extensions": {},
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
            "context": context or "",
            "total_steps": workflow.step_count(),
            "warnings": workflow.warnings,
        },
        cwd,
    )
    # A start settles any pending human request for this slot — whether it
    # fulfils it or (starting something else) supersedes it. Either way the
    # request must not survive to be "fulfilled" a second time.
    if pending:
        fulfilled = pending.get("workflow") == workflow_ref or (
            pending.get("resolved") == str(path)
        )
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
    if workflow.warnings:
        payload["workflow_warnings"] = workflow.warnings
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
    return {
        "status": "start_requested",
        "request": request,
        "note": (
            "recorded; the scope's agent starts it itself (it sees the request "
            "in its next 'status' call) — nudge it if it is idle"
        ),
    }


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
    """Why the current step's content is withheld ('loop_limit'/'gate'), if so."""
    step = workflow.step(state["current"])
    if _visits(state, step.id) > _limit(workflow, state, step.id):
        return "loop_limit"
    if step.gate and not state["gate_approved"]:
        return "gate"
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
    """Unblock the current gate or loop guard. CLI-only — not exposed over MCP."""
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
    if blocked == "gate":
        state["gate_approved"] = True
        state_mod.save_state(state, cwd)
        state_mod.journal(
            "approved",
            {"run": state["run_id"], "step": step.id, "by": by,
             "visit": _visits(state, step.id)},
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
