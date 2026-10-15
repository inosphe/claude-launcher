"""May this session run a simulation now? (claunch-efnbt.9)

A workflow step (cflow v1) or task (cflow v2) can carry ``sims: deny``: while
a session's run stands on such a step, starting a simulation campaign or
running a round is refused. The canonical workflows mark the goal-setting
steps -- the round's goal is not settled yet, so there is nothing to check a
model against. Building a model (create, patch) and reading stay open.

One predicate serves both callers:

* the sim API (``daemon/sim_api.py``) asks it for ``sim_start`` and
  ``sim_round`` with the calling session's checkout;
* a cflow v2 task's ``calls: [...]`` allow-list (the dynamic call engine,
  claunch-z17c9) refuses any procedure the task does not declare, and the
  checker refuses a ``sims: deny`` task that declares one of
  :data:`SIM_PROCEDURES`. A static ``call: sim-run`` reaches ``sim_start``
  and meets the sim API's check. :func:`v2_blocked` is pure, for a caller
  that already holds a chart and state.

Who is never asked: the user, the run session the daemon launched for a
simulation, and a session with no cflow run -- the callers leave those out
before asking; this module only reads runs.

The marker is read from the run's own snapshot (v1 workflow snapshot, v2
chart), so a run started before the marker existed is not restricted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

#: The step/task key and the one value that restricts.
KEY = "sims"
DENY = "deny"
VALUES = (DENY,)
#: Procedures that run a simulation: a task marked ``sims: deny`` may not
#: name one in its ``calls`` allow-list (cflow2 check ``sims-deny-call``).
SIM_PROCEDURES = ("sim-run",)


@dataclass(frozen=True)
class Blocked:
    """Where the run stands: the step that forbids a simulation run."""

    version: int  # 1 = cflow v1, 2 = cflow v2
    workflow: str
    step: str
    title: str

    def message(self, what: str = "simulation run") -> str:
        label = f"{self.step} ({self.title})" if self.title and self.title != self.step else self.step
        return (
            f"{what} refused: 목표 승인 후 실행 가능. 현재 cflow v{self.version} 단계 "
            f"{label}(워크플로 {self.workflow})는 시뮬레이션 실행 금지(sims: deny)로 표시되어 "
            f"있다. 모델 작성(sim_create, sim_patch)과 조회는 이 단계에서도 가능하다."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "workflow": self.workflow, "step": self.step,
                "title": self.title}


def v1_blocked(cwd: str, scope: str) -> Optional[Blocked]:
    """The v1 main run of ``scope`` in ``cwd`` stands on a ``sims: deny`` step."""
    from ..cflow import state as state_mod

    try:
        token = state_mod.push_scope(scope)
    except Exception:  # noqa: BLE001 -- not a valid scope: no run
        return None
    try:
        if not state_mod.has_run(cwd):
            return None
        state = state_mod.load_state(cwd)
        if state.get("status") in ("done", "aborted") or not state.get("current"):
            return None
        workflow = state_mod.load_snapshot(cwd)
        step = workflow.steps.get(state["current"])
    except Exception:  # noqa: BLE001 -- unreadable run: do not guess
        return None
    finally:
        state_mod.pop_scope(token)
    if step is None or getattr(step, "sims", None) != DENY:
        return None
    return Blocked(1, getattr(workflow, "name", "") or "", step.id, step.title or "")


def v2_blocked(chart, state: Dict[str, Any]) -> Optional[Blocked]:
    """An active task of this v2 run is marked ``sims: deny``. Pure: for a
    caller that already holds the run's chart and state."""
    from ..cflow2 import runtime

    if state.get("status") in runtime.FINISHED_STATUSES:
        return None
    inst = state.get("inst") or {}
    active = state.get("active")
    if active is None:
        active = [iid for iid, rec in inst.items() if rec.get("await")]
    for iid in sorted(active):
        rec = inst.get(iid) or {}
        try:
            node = chart.node(rec.get("node"))
        except Exception:  # noqa: BLE001
            continue
        if (node.props or {}).get(KEY) == DENY:
            return Blocked(2, getattr(chart, "name", "") or "", node.label or node.id,
                           str((node.props or {}).get("title") or ""))
    return None


def v2_run_blocked(cwd: str, scope: str) -> Optional[Blocked]:
    """The v2 run of ``scope`` in ``cwd`` stands on a ``sims: deny`` task."""
    from ..cflow2.store import RunStore

    try:
        st = RunStore(cwd, scope)
        if not st.exists():
            return None
        state = st.load_state()
        chart = st.load_chart()
    except Exception:  # noqa: BLE001 -- unreadable run: do not guess
        return None
    return v2_blocked(chart, state)


def blocked(cwd: str, scope: str) -> Optional[Blocked]:
    """The session ``scope``'s cflow v1 or v2 run in ``cwd`` forbids running a
    simulation now; ``None`` when it does not or there is no run."""
    if not cwd or not scope:
        return None
    return v1_blocked(cwd, scope) or v2_run_blocked(cwd, scope)
