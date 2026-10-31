"""Progress overview of the active sessions (claunch-7jzp8.2): one row per
session that says where it is -- its cflow v2 run's current tasks and what
the run owes a person, its plan's progress and what keeps it from closing
-- and one word for the stage the rows are grouped by.

Stages, first match wins:

  user       a person is owed something: the run's ``owed`` (a user choice,
             a hold, a requested move) or the plan's ``waiting`` (decisions,
             verify items, followups)
  blocked    the plan has a blocked step
  closable   the plan says the session may close (nothing left on it)
  working    a run or a plan is moving
  idle       neither a run nor a plan

Pure: the caller passes the live sessions, the plan rail (PlanStore.rail)
and the v2 rail rows (cflow2_web.rail_entries); nothing here reads the
daemon.
"""

from __future__ import annotations

from typing import Iterable, Optional

STAGES = ("user", "blocked", "closable", "working", "idle")


def stage(plan: Optional[dict], run: Optional[dict]) -> str:
    owed = (run or {}).get("owed") or []
    if owed or int((plan or {}).get("waiting") or 0) > 0:
        return "user"
    if plan and int(plan.get("blocked") or 0) > 0:
        return "blocked"
    if plan and plan.get("closable"):
        return "closable"
    if plan or run:
        return "working"
    return "idle"


def row(session: dict, plan: Optional[dict], run: Optional[dict]) -> dict:
    out = {**session, "stage": stage(plan, run), "plan": None, "run": None}
    if plan:
        out["plan"] = {k: plan.get(k) for k in ("title", "issue", "steps", "done", "current", "waiting",
                                                 "closable", "verify", "followups", "blocked", "updated_at")}
    if run:
        out["run"] = {k: run.get(k) for k in ("workflow", "status", "round", "current", "owed", "entered_at",
                                               "updated")}
    return out


def overview(sessions: Iterable[dict], plans: dict, runs: dict) -> dict:
    """``sessions`` are ``{session, ...}`` dicts (status, project, parent,
    issue as the caller knows them). Rows sorted by stage, then by name;
    ``counts`` per stage."""
    rows = [row(s, plans.get(s["session"]), runs.get(s["session"])) for s in sessions]
    rows.sort(key=lambda r: (STAGES.index(r["stage"]), r["session"]))
    counts = {k: 0 for k in STAGES}
    for r in rows:
        counts[r["stage"]] += 1
    return {"rows": rows, "counts": counts}
