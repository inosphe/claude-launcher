"""The ``/cflow`` skill: the execution protocol, and where it is written.

Registering the MCP server that backs it is :mod:`claude_launcher.install`'s
job — the tools ship in one merged server, so there is no cflow-only
registration to do here. This module owns the skill text (what ``/cflow
<workflow> [context]`` primes the agent with) and the copy of the packaged
workflows into the global layer that ``claunch install --global`` (and the
profile install) performs.

The workflows themselves are files under ``claude_launcher/workflows/``, not
strings in here. They were both once: an ``EXAMPLE_WORKFLOW`` literal that
``cflow example`` wrote, and a ``.claunch/workflows/feature-dev.yaml`` in
this checkout — and the two drifted, one teaching the deprecated ``gate:``
the other teaching ``ask:``. One file, read by everything, is the fix.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path
from typing import List, Tuple

from . import state

SKILL_MD = """\
---
name: cflow
description: >-
  Run a declared claunch workflow (cflow) step by step via the cflow MCP
  tools. Use when the user types /cflow <workflow> or asks to run/resume a
  cflow workflow in this project.
---

# cflow — run a declared workflow step by step

Argument form: `/cflow <workflow-name> [extra context...]`. With no name:
call the `status` tool first — if a run is active, resume it; if `status`
carries a `pending_start` (a human asked from the dashboard/CLI for a
workflow to be started here), that is the answer; otherwise list candidates
(`claunch cflow ls`) and ask which one to run.

## Protocol

1. Call the MCP tool `start` with `{workflow, context}` (context = the extra
   arguments plus anything relevant the user said about the task).
2. The server feeds ONE step at a time. Execute the returned `instructions`
   completely, then file the step's completion report — `report {summary,
   details}` — and advance with `next {}`. The summary is 2–4 honest
   sentences on what actually happened; put evidence in `details` (commands
   run, test names, failure lines, files touched). Reports are journaled,
   shown live on the daemon web dashboard, and become the PR text. Failures
   belong in the report too.
3. Act on the returned `status`:
   - `step` — do the work, then `report {summary, details}`, then `next {}`.
   - `report_required` — you called `next` without filing the step's
     report; call `report` first.
   - `verify_failed` — the step's verify command failed; its output is
     included and your report was discarded (the outcome it described did
     not survive). Fix the underlying problem, file a new `report`, and
     call `next` again. Never claim success: the server re-runs the command
     itself.
   - `select` with `chooser: agent` — decide per the prompt's criteria and
     call `select {option, reason}`.
   - `select` with `chooser: user`, or `waiting_selection` — call `select`
     once to record your RECOMMENDATION with reasoning, then STOP your turn:
     present the options and your pick, and tell the user to confirm with
     `! claunch cflow select <option>` or from the daemon web dashboard
     (they may pick a different one).
   - `waiting_approval` — a human gate (or the loop guard, when `reason` is
     `loop_limit`; or a delegated decision that reached a human, when it is
     `ask`, or that nobody refused but nobody could take, when the payload's
     `ask.skipped` is non-empty). STOP your turn: present the work so far
     (for a loop limit, explain why the loop keeps repeating; for an `ask`,
     say who was meant to decide it and why they could not — that is what
     `ask.skipped` is), and tell the user to approve with `! claunch cflow
     approve` or the web dashboard's Approve button. You cannot approve and
     must not simulate approval.
   - `waiting_answer` — the workflow delegated this decision to another
     session, and `ask.asked` says who. STOP your turn: present what you
     have so far and say who is deciding. You cannot answer it — not with
     `select`, not with `answer`, not by asking them over the mesh and
     acting on the reply. They record it themselves; you will be nudged.
   - `status: step` or `select` where the payload says the decision was
     meant to be somebody else's — the workflow declared `otherwise: self`
     and nobody could be reached, so it is yours by default. Say so plainly
     when you report: nobody approved this, and reporting it as approved
     would be false.
   - `waiting_approval` with `reason: declined` — a responder refused, and
     the workflow declared nowhere for a refusal to go. Relay the refusal
     and its reason (`declined.by`, `declined.reason`) to the user and stop:
     a human decides whether to override (`! claunch cflow approve`) or send
     the run elsewhere (`! claunch cflow goto <step>`).
   - `done` — report the run using the returned journal and finish. If the
     payload carries a `pending_start` filed `by: "recur"`, this workflow is
     a service loop: report this round's journal, then immediately start the
     next round (`start` with exactly the requested workflow and context).
     Never decide to stop the loop yourself — only a human ends it
     (`! claunch cflow request --cancel`, or archiving the run).
4. Resuming after a stop: when nudged (any user message), call `status`
   first to see whether the gate/selection was granted, then continue with
   `next`.
5. `pending_start` in a `status` payload = a request for a workflow to be
   started in this session — filed by a human (from the dashboard or
   `claunch cflow request`), or `by: "recur"` when a recurring workflow's
   previous round finished. You perform the start: check it makes sense for
   what you are doing, tell the user you are starting it, then call `start
   {workflow, context}` with exactly that workflow (its `context` is the
   requester's own words — carry it through, adding anything relevant from
   the chat). If a human request is clearly wrong, do NOT start it: say why
   and stop. A recur request is never wrong to fulfil — it is the loop the
   workflow declared. The request clears once you start.

## Answering for someone else

Other sessions' runs may delegate a decision to your role — anything you are
wired to in the mesh except your own descendants. That is independent of
whether you are running a workflow yourself. A `decide` message on the mesh
is the doorbell; `asks` is where the question actually lives.

1. Call `asks {}` when a mesh message says a run needs a decision, whenever
   you are nudged, and before going idle. It lists what is waiting on you:
   the question, the options you may answer with, and the deadline.
2. Investigate before deciding. You were asked *because the run does not get
   to decide this one* — so check the actual code, tests and diff, not the
   asking agent's account of them. An approval you granted on the strength
   of the request text is worth nothing.
3. Call `answer {ask, decision, reason}`. The decision must be one of that
   request's options, or `abstain`. Put what you actually checked in
   `reason`; it is journaled and a human reads it.
4. `abstain` when you have no basis to decide — it passes the question to
   whoever is next, which is strictly better than a guess. Do not abstain
   to avoid the work of looking.
5. You never receive the asking step's instructions, and must not do its
   work, take over its run, or tell it what you would have implemented.
   Decide, say why, and stop.

You cannot answer a request that was not put to you, nor one from your own
run. Both are refused; neither is a thing to work around.

## Rules

- One step at a time. Do not skip ahead, merge steps, or invent steps.
- Workflows may loop (a `visit` counter > 1 means you are on another pass).
  Gates and selects apply on EVERY visit — a previous approval does not
  carry over.
- Reports must reflect reality, including what failed or was skipped. They
  are watched live by humans — write them as status updates for a reviewer,
  not as praise for yourself.
- Approvals and user selections happen OUTSIDE your tools (CLI / `!`
  commands / the web dashboard); nothing you can call grants them. The same
  holds for a delegated decision: `answer` acts on OTHER sessions' runs and
  refuses your own, so there is no arrangement of tool calls that unblocks
  a gate on you.
- Gates and selects apply on every visit, and so do delegated decisions: a
  loop that passes an `ask` twice asks twice, and the second answer may
  differ from the first.
- A human may force the run's position while you are stopped
  (`claunch cflow goto <step>`). Whatever `status` serves after a nudge IS
  the current truth — even if it revisits a step you already finished.
- If a tool returns an error about no active run, `start` one. If `start`
  errors because a run is ALREADY ACTIVE, do not retry and do not force:
  call `status` and resume that run — unless the user explicitly asked for
  a new/different workflow. In that case the active run must be retired
  first: ask the user to archive it (`! claunch cflow archive`, or the
  Archive button on the daemon web dashboard), or — only with the user's
  explicit go-ahead — call `start` with `force: true`, which aborts the
  active run and archives it (state + journal are kept, not lost). Never
  pass `force` on your own initiative.
- Finished (done/aborted) runs never block: `start` archives them
  automatically and begins the new run.
- A run can be replaced under you (someone archived it and started another,
  or started one from the dashboard). Then a tool answers that the run you
  were driving *is not the run here any more* — nothing was applied. Do not
  retry: call `status`, tell the user the run changed, and continue from
  whatever position `status` reports.
"""


def write_skill(skills_dir: Path) -> Path:
    path = skills_dir / "cflow" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_MD, encoding="utf-8")
    return path


#: Outcomes of seeding one packaged workflow into the global layer.
SEEDED = "seeded"  #: nothing was there; the packaged copy is now
UNCHANGED = "unchanged"  #: what is there is byte-for-byte the packaged copy
KEPT = "kept"  #: something different is there, and it was left alone


def example_workflow() -> Path:
    """The packaged workflow ``claunch cflow example`` scaffolds from."""
    return state.bundled_workflows_dir() / "feature-dev.yaml"


def install_workflow(src: Path, dest: Path, force: bool = False) -> str:
    """Copy one workflow file into a layer; report what became of it.

    Bytes, not text: a workflow that arrives with different line endings than
    it left with is a workflow that will look modified forever after.
    """
    if dest.exists() and not force:
        return UNCHANGED if filecmp.cmp(src, dest, shallow=False) else KEPT
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return SEEDED


def seed_global_workflows(force: bool = False) -> List[Tuple[str, Path, str]]:
    """Copy the packaged workflows into the global layer, for ``install``.

    A seeded file is an ordinary file from that moment on — a human edits it,
    a project overrides it, ``claunch cflow add`` joins more to it. So a
    re-install must not undo an edit: a destination that differs from the
    package is reported and left alone unless ``force``. That is the whole
    price of copying rather than reading the package at resolve time, and it
    is the deliberate one: the global layer is meant to be yours.
    """
    dest_dir = state.global_workflows_dir()
    return [
        (name, dest_dir / src.name, install_workflow(src, dest_dir / src.name, force))
        for name, src in state.bundled_workflows()
    ]


