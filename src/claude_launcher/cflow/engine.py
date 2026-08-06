"""The cflow state machine: start / next / select / approve / status / abort.

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
"""

from __future__ import annotations

import secrets
import subprocess
from typing import Optional

from . import model, state as state_mod
from .model import Step, Workflow

#: Kept from a verify command's combined output when reporting failure.
VERIFY_OUTPUT_TAIL = 4000


class CflowError(Exception):
    """Raised for protocol misuse (wrong tool for the current position)."""


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
                "a human must run 'claunch cflow approve' to extend the loop "
                "limit (inside a chat session: '! claunch cflow approve'). "
                "Stop your turn, explain why the loop keeps repeating, and "
                "wait to be nudged."
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
                "a human must run 'claunch cflow approve' in this directory "
                "(inside a chat session: '! claunch cflow approve'); the agent "
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
                    "(inside a chat session: '! claunch cflow select <option>'). "
                    "Stop your turn, present your recommendation and reasoning, "
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
def start(
    workflow_ref: str,
    context: Optional[str] = None,
    *,
    force: bool = False,
    cwd: Optional[str] = None,
) -> dict:
    if state_mod.has_run(cwd):
        old = state_mod.load_state(cwd)
        if old.get("status") not in ("done", "aborted") and not force:
            raise CflowError(
                f"a run of {old.get('workflow')!r} is already active here "
                f"(step {old.get('current')}); resume it via 'status'/'next', "
                f"or abort with 'claunch cflow abort' (or pass force=true)"
            )
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
    payload = _payload(workflow, state, cwd, mutate=True)
    if context:
        payload["context"] = context
    if workflow.warnings:
        payload["workflow_warnings"] = workflow.warnings
    return payload


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


def next_step(*, cwd: Optional[str] = None) -> dict:
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        return _done_payload(state, cwd)
    step = workflow.step(state["current"])

    if not state["delivered"] or _blocked(workflow, state):
        # Nothing has been handed out yet (fresh arrival, or a gate/loop guard
        # is closed): (re)attempt delivery.
        return _payload(workflow, state, cwd, mutate=True)

    if step.is_select:
        payload = _payload(workflow, state, cwd, mutate=False)
        payload["note"] = "this is a decision point — use the 'select' tool, not 'next'"
        return payload

    # Completing a delivered executable step: the report comes first, so the
    # journal/dashboard always carry an explicit account of what happened.
    filed = _current_report(state, step.id)
    if filed is None:
        return {
            **_base(state),
            "step_id": step.id,
            "status": "report_required",
            "note": (
                "no completion report filed for this step yet — call 'report' "
                "with {summary, details?} describing what actually happened, "
                "then call 'next' again"
            ),
        }

    # Machine gate second: verify must confirm the reported outcome.
    if step.verify:
        result = _run_verify(step, cwd)
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


def status(cwd: Optional[str] = None) -> dict:
    if not state_mod.has_run(cwd):
        return {"status": "idle", "note": "no active cflow run in this directory"}
    workflow, state = _load(cwd)
    payload = _payload(workflow, state, cwd, mutate=False)
    payload["visits"] = dict(state["visits"])
    payload["started_at"] = state.get("started_at")
    if state.get("context"):
        payload["context"] = state["context"]
    if state.get("current") and _current_report(state, state["current"]):
        payload["report"] = state["report"]
    return payload


def abort(*, by: str = "user", cwd: Optional[str] = None) -> dict:
    workflow, state = _load(cwd)
    if state["status"] in ("done", "aborted"):
        return _done_payload(state, cwd)
    state["status"] = "aborted"
    state_mod.save_state(state, cwd)
    state_mod.journal("aborted", {"run": state["run_id"], "by": by}, cwd)
    return _done_payload(state, cwd)


def reset(cwd: Optional[str] = None) -> None:
    """Clear run state (the journal is kept for the record)."""
    state_mod.clear_state(cwd)
