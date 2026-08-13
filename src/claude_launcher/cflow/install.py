"""The ``/cflow`` skill: the execution protocol, and where it is written.

Registering the MCP server that backs it is :mod:`claude_launcher.install`'s
job — the tools ship in one merged server, so there is no cflow-only
registration to do here. This module owns the skill text: what
``/cflow <workflow> [context]`` primes the agent with, plus the example
workflow ``claunch cflow example`` scaffolds.
"""

from __future__ import annotations

from pathlib import Path

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
   - `waiting_answer` — the workflow delegated this decision to somebody
     ABOVE you, and `ask.asked` says who. STOP your turn: present what you
     have so far and say who is deciding. You cannot answer it — not with
     `select`, not with `answer`, not by asking them over the mesh and
     acting on the reply. They record it themselves; you will be nudged.
   - `waiting_approval` with `reason: declined` — a responder refused, and
     the workflow declared nowhere for a refusal to go. Relay the refusal
     and its reason (`declined.by`, `declined.reason`) to the user and stop:
     a human decides whether to override (`! claunch cflow approve`) or send
     the run elsewhere (`! claunch cflow goto <step>`).
   - `done` — report the run using the returned journal and finish.
4. Resuming after a stop: when nudged (any user message), call `status`
   first to see whether the gate/selection was granted, then continue with
   `next`.
5. `pending_start` in a `status` payload = a human asked for a workflow to
   be started in this session (from the dashboard or `claunch cflow
   request`). You perform the start: check it makes sense for what you are
   doing, tell the user you are starting it, then call `start {workflow,
   context}` with exactly that workflow (its `context` is the requester's
   own words — carry it through, adding anything relevant from the chat).
   If it is clearly wrong, do NOT start it: say why and stop. The request
   clears once you start.

## Answering for someone else

Runs BELOW you in the spawn tree may delegate a decision to your role. This
is independent of whether you are running a workflow yourself.

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


EXAMPLE_WORKFLOW = """\
name: feature-dev
description: design -> (triage) implement -> test -> review loop -> ship
start: design
steps:
  design:
    title: Design
    instructions: |
      Analyze the request and the relevant code. Write a short design note
      (goal, approach, files to touch, risks) before changing anything.
      Keep it concise enough to paste into a PR description later.
    next: triage

  triage:
    title: Risk triage
    select:
      prompt: |
        Assess the scope and risk of the planned change. Consider: blast
        radius, reversibility, whether tests cover the touched area.
      chooser: user
      options:
        auto:
          description: low risk - implement autonomously, self-review only
          next: impl
        human:
          description: higher risk - a human reviews each pass
          next: impl

  impl:
    title: Implement
    instructions: |
      Implement the design (or address the latest review feedback).
      Keep commits small and focused.
    next: test

  test:
    title: Test
    instructions: |
      Run the test suite; add or extend tests covering the change.
    verify: "uv run pytest -q"
    next: review

  review:
    title: Review
    gate: present the diff summary and wait for human review
    instructions: |
      Relay the review feedback you received into concrete follow-ups.
    next: verdict

  verdict:
    title: Review verdict
    select:
      prompt: Is the change ready, or does it need another pass?
      chooser: user
      options:
        ready:
          description: reviewer satisfied - ship it
          next: ship
        rework:
          description: needs another implement/test/review pass
          next: impl        # intentional cycle; the verdict is the exit

  ship:
    title: Ship
    gate: approve committing and opening a PR
    instructions: |
      Commit the work with a clear message and prepare the PR description
      from the run journal (design note, decisions, review outcomes).
    next: end
"""
