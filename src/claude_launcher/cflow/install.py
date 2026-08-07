"""Install cflow into a profile or a project: MCP registration + /cflow skill.

- MCP: registers ``cflow`` as a stdio server running ``claunch cflow mcp``
  (into a profile's ``settings.json`` ``mcpServers``, or a project ``.mcp.json``).
- Skill: writes ``skills/cflow/SKILL.md`` so ``/cflow <workflow> [context]``
  primes the agent with the execution protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .. import settings
from ..profile import Profile

MCP_SERVER = {"command": "claunch", "args": ["cflow", "mcp"]}

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
call the `status` tool first — if a run is active, resume it; otherwise list
candidates (`claunch cflow ls`) and ask which one to run.

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
     `loop_limit`). STOP your turn: present the work so far (for a loop
     limit, explain why the loop keeps repeating) and tell the user to
     approve with `! claunch cflow approve` or the web dashboard's Approve
     button. You cannot approve and must not simulate approval.
   - `done` — report the run using the returned journal and finish.
4. Resuming after a stop: when nudged (any user message), call `status`
   first to see whether the gate/selection was granted, then continue with
   `next`.

## Rules

- One step at a time. Do not skip ahead, merge steps, or invent steps.
- Workflows may loop (a `visit` counter > 1 means you are on another pass).
  Gates and selects apply on EVERY visit — a previous approval does not
  carry over.
- Reports must reflect reality, including what failed or was skipped. They
  are watched live by humans — write them as status updates for a reviewer,
  not as praise for yourself.
- Approvals and user selections happen OUTSIDE your tools (CLI / `!`
  commands / the web dashboard); nothing you can call grants them.
- A human may force the run's position while you are stopped
  (`claunch cflow goto <step>`). Whatever `status` serves after a nudge IS
  the current truth — even if it revisits a step you already finished.
- If a tool returns an error about no active run, `start` one; if it says a
  run is already active, resume it instead of forcing a restart unless the
  user explicitly wants a fresh run.
"""


def install_into_profile(profile: Profile) -> List[str]:
    """Register the MCP server + skill inside a profile's config dir."""
    done = []
    settings.merge_mcp_servers(profile, {"cflow": dict(MCP_SERVER)})
    done.append(f"mcp server 'cflow' -> {profile.config_dir / 'settings.json'}")
    done.append(f"skill -> {_write_skill(profile.config_dir / 'skills')}")
    return done


def install_into_project(project_dir: Path) -> List[str]:
    """Register the MCP server (.mcp.json) + skill (.claude/skills) in a project."""
    done = []
    mcp_path = project_dir / ".mcp.json"
    try:
        doc = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
    except ValueError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    servers = doc.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        servers["cflow"] = dict(MCP_SERVER)
    mcp_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    done.append(f"mcp server 'cflow' -> {mcp_path}")
    done.append(f"skill -> {_write_skill(project_dir / '.claude' / 'skills')}")
    return done


def _write_skill(skills_dir: Path) -> Path:
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
