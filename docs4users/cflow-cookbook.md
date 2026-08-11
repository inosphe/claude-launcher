# cflow cookbook — workflow patterns that work well

The [README](../README.md#cflow-declarative-agent-workflows) explains what
cflow *is*; this document shows how to *use* it well. Each recipe is a
complete, copy-pasteable workflow YAML that teaches one pattern. Drop any of
them into `.claunch/workflows/<name>.yaml` in your project (or
`~/.claude-launcher/workflows/` to share across projects) and run it with:

```
/cflow <name> <one line describing the concrete task>
```

Setup, once per profile or project:

```sh
claunch cflow install --profile work    # or: claunch cflow install --project
```

---

## Recipe 1 — linear bugfix (instructions + verify)

The smallest useful shape: a straight line where the machine, not the agent,
decides whether the fix step is finished. `verify` runs **server-side** after
the agent calls `next`; a non-zero exit sends the output back and keeps the
agent on the step. The agent cannot talk its way past a failing test.

```yaml
name: bugfix
description: reproduce -> fix -> prove it stays fixed
steps:
  reproduce:
    title: Reproduce
    instructions: |
      Reproduce the reported bug. Write a failing test that captures it
      BEFORE touching the implementation. In your report, name the test
      and paste the failure line.
    next: fix

  fix:
    title: Fix
    instructions: |
      Make the failing test pass with the smallest correct change.
      Do not refactor surrounding code in this step.
    verify: "uv run pytest -q"
    next: writeup

  writeup:
    title: Write-up
    instructions: |
      Summarize root cause and fix in 3-5 sentences, suitable for a
      commit message body. Do not commit yet.
    # no 'next' -> the workflow ends here
```

**Why it works:** the failing-test-first instruction gives `verify` teeth —
if the agent skipped step 1, step 2's verify passes vacuously; because step 1
demands the failure line in its report, the journal shows whether that
happened. Every step ends with a mandatory `report` (the server refuses to
advance without one, and shows them live on the web dashboard) — that report
stream is your audit trail; ask for evidence in it.

---

## Recipe 2 — risk triage (agent-chooser select, shared nodes)

A `select` step branches the graph. With `chooser: agent` the agent decides
alone — appropriate when both branches are safe and the choice is about
*effort*, not *permission*. Note that both options point at the **same**
`impl` step: structure lives in `next` pointers, so shared sequences are
defined once, never duplicated.

```yaml
name: task
description: triage size, then implement with or without a design pass
steps:
  triage:
    select:
      prompt: |
        Estimate the change. "small" = one or two files, mechanical,
        existing tests cover the area. Anything else is "large".
      chooser: agent
      options:
        small:
          description: skip the design note, implement directly
          next: impl
        large:
          description: write a design note first
          next: design

  design:
    title: Design
    instructions: |
      Write a short design note: goal, approach, files to touch, risks.
    next: impl

  impl:
    title: Implement
    instructions: |
      Implement the change (following the design note if one was written).
    verify: "uv run pytest -q"
```

**Choosing the chooser:** `chooser: agent` for judgment calls where a wrong
pick costs time, not damage. `chooser: user` (next recipe) when the pick
grants authority — the agent's `select` call then only records a
*recommendation*, and the run blocks until you confirm with
`! claunch cflow select <option>` (you may pick differently).

---

## Recipe 3 — review loop (cycle + user verdict as the exit)

Cycles are how cflow models iteration. The rule: **a cycle is fine as long
as its exit is real** — here the exit is a human verdict, so the loop cannot
spin without you. `cflow show` will print a `cycle detected` warning for
this file; that warning is a prompt to check the exit, not a defect.

Gates and selects apply on **every visit**: pass two of the review loop hits
the same gate again. One approval never covers a whole loop.

```yaml
name: reviewed-change
description: implement -> test -> human review, looping until accepted
max_visits: 10        # safety net; see "loop guards" below
steps:
  impl:
    title: Implement
    instructions: |
      Implement the change, or address every point of the latest review
      feedback (see the journal for the previous review's summary).
    next: test

  test:
    title: Test
    instructions: Run and extend the tests covering the change.
    verify: "uv run pytest -q"
    next: review

  review:
    title: Human review
    gate: present the current diff and wait for review
    instructions: |
      Restate the review feedback you received as a concrete checklist.
    next: verdict

  verdict:
    select:
      prompt: Is the change accepted, or does it need another pass?
      chooser: user
      options:
        accept:
          description: reviewer satisfied
          next: ship
        rework:
          description: another implement/test/review pass
          next: impl          # the cycle; 'accept' is the exit

  ship:
    title: Ship
    gate: approve committing this change
    instructions: Commit with a clear message built from the run journal.
    next: end
```

Driving a review pass from the chat:

```
! claunch cflow approve            # open the review gate
  (review the diff...)
! claunch cflow select rework      # or: select accept
```

**Loop guards.** Independent of your exits, every step has a per-run visit
cap (`max_visits`, default 25). Reaching it pauses the run like a gate
(`status: waiting_approval`, `reason: loop_limit`); `claunch cflow approve`
extends the cap by another window. So even a `chooser: agent` cycle — say,
an autonomous fix-and-retest loop — degrades into "ask a human" instead of
spinning forever. For tight loops you expect to run a few times, set
`max_visits` low on purpose, as above.

**YAML footgun:** option names like `yes`/`no`/`on`/`off` parse as booleans
in YAML — quote them (`"yes":`) or pick other names (`accept`/`rework`).

---

## Recipe 4 — gates before irreversible actions

Put a `gate` on the step that *performs* an irreversible action, not after
it. The gate withholds the step's instructions until approved, so the agent
cannot even see "publish" work early. Use several small gates rather than
one big one — each approval then means exactly one thing.

```yaml
name: release
description: prep -> human checks -> tag -> publish, each irreversible step gated
steps:
  prep:
    title: Prepare
    instructions: |
      Update the changelog and version number. Build the package locally.
    verify: "uv build"
    next: tag

  tag:
    title: Tag
    gate: version and changelog look right — allow tagging
    instructions: Create the release tag and push it.
    next: publish

  publish:
    title: Publish
    gate: tag pushed and CI green — allow publishing to the index
    instructions: Publish the built package.
    next: announce

  announce:
    title: Announce
    instructions: Draft the release notes from the journal; do not send.
```

---

## Running unattended (cflow × managed sessions)

cflow blocks *inside* one agent session; the [session
daemon](../README.md#managed-sessions-tmux-style-daemon) lets a script or
orchestrator drive that session from outside. The division of labor:

- the **workflow** decides *what* happens and *where* it must pause,
- the **script** watches for pauses and supplies the human-side keystrokes
  (or forwards them to an actual human).

Run state is keyed per session (`CLAUNCH_SESSION`, exported by the daemon),
so several workers in the same project directory drive independent runs;
from outside a session, target one with `-t <session>`.

```sh
claunch new-session -s worker -- claude
claunch send-keys -t worker '/cflow bugfix fix issue #123' Enter

while true; do
  claunch wait-for -t worker --idle-threshold 5      # agent stopped typing
  status=$(claunch cflow status -t worker --json | python -c \
    "import json,sys; print(json.load(sys.stdin)['status'])")
  case "$status" in
    done|aborted|idle) break ;;
    waiting_approval)                 # a gate (or loop limit)
      claunch cflow status -t worker  # show a human what's being asked
      exit 2 ;;                       # ... or approve here if policy allows
    waiting_selection)
      claunch cflow status -t worker; exit 3 ;;
    *) claunch send-keys -t worker 'continue per /cflow protocol' Enter ;;
  esac
done
claunch cflow journal -t worker
```

Exiting on `waiting_approval` (rather than auto-approving) keeps the gate
meaningful: the script escalates to a person, who runs `claunch cflow
approve` and restarts the loop. Auto-approving in the script would make the
gate decorative — if a pause point never needs a human, it should not be a
gate in the first place.

---

## Authoring guidelines

**Write instructions for a stranger.** Each step is delivered alone — the
agent never sees the whole YAML. An instruction that says "continue from the
previous step" is meaningless; say *what* to produce and *what evidence* to
put in the step's completion report (the server refuses to advance without
one). The journal (`.cflow/journal.jsonl`) is built from those reports and
becomes your PR text — demand specifics (test names, failure lines, files
touched). Watch reports arrive live in the daemon web dashboard's Workflows
panel (`claunch web --open`).

**Let `verify` do the judging.** Anything checkable by a command — tests,
type checks, linters, builds — belongs in `verify`, not in instructions like
"make sure tests pass". Long commands take
`verify: {command: "...", timeout: 1800}`.

**Terminate implicitly, end cycles explicitly.** Omitting `next` ends the
run; that is the normal way to finish. `next: end` exists for when you want
the termination visible — and once your graph has a cycle, at least one
termination must be *reachable from start* or the file is rejected outright.

**Read the warnings.** `cflow show <name>` prints three kinds:
`cycle detected` (fine if the exit is real — recipe 3), `once entered, these
steps can never reach a termination` (a trap: some path enters a loop with
no exit — fix the graph), and `defined but unreachable from start` (dead
steps: a typo in some `next`, or leftovers to delete).

**Edit, re-show, restart.** A run snapshots the YAML at `start`, so editing
the file never corrupts an active run — but it also means edits only apply
to the *next* run (`claunch cflow archive` to retire the active run —
finished or not — into `.cflow/.../archive/`, then `/cflow <name> ...`
again; the web dashboard's Archive button and start picker do the same).

**Start it from outside the chat.** `claunch cflow request <name> -c "..."`
(or the dashboard's session page) asks a running session's agent to start a
workflow: the request is recorded, the session is nudged, and the *agent*
performs the `start` it sees in its next `status`. That keeps the agent the
only writer of run state — useful for scripts and phones alike, where typing
`/cflow` into someone else's terminal is not an option.
Iterate on a workflow by running `claunch cflow show <name>` after each edit
until the graph and warnings look right.
