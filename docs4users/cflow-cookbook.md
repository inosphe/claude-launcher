# cflow cookbook — workflow patterns that work well

The [README](../README.md#cflow-declarative-agent-workflows) explains what
cflow *is*; this document shows how to *use* it well. Each recipe is a
complete, copy-pasteable workflow YAML that teaches one pattern. Drop any of
them into `.claunch/workflows/<name>.yaml` in your project — or install them
once into the global layer, where every project can run them:

```sh
claunch cflow add ./recipe.yaml         # -> ~/.claude-launcher/workflows/
```

Then run it with:

```
/cflow <name> <one line describing the concrete task>
```

Setup — pick the scope:

```sh
claunch install                         # this project (.mcp.json + .claude/skills)
claunch install --global                # you, everywhere (~/.claude/skills + user MCP)
claunch install --profile work          # one claunch profile
```

The `--global` (and `--profile`) install also seeds the global layer with the
workflows claunch ships, so `claunch cflow ls` is not empty on a fresh
machine. It never overwrites one you have edited. A project install writes
only inside the project.

That install also writes the **`/cflow-author` skill**, which is the same
material as this cookbook aimed at an agent rather than at you: ask a session
to write or review a workflow and it will apply those rules. This document is
the worked examples; the skill is the judgement — which control point to
reach for, and which decisions the driving agent must never be the one to
answer.

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

## Recipe 4 — approvals before irreversible actions

Put an `ask` on the step that *performs* an irreversible action, not after
it. It withholds the step's instructions until approved, so the agent cannot
even see "publish" work early. Use several small approvals rather than one
big one — each then means exactly one thing.

An `ask` with no `from` asks no agent, so it is exactly a human gate: the run
holds until somebody runs `claunch cflow approve` or presses the dashboard
button. Recipe 5 adds the `from` that puts the same question to another
session.

```yaml
name: release
description: prep -> checks -> tag -> publish, each irreversible step approved
steps:
  prep:
    title: Prepare
    instructions: |
      Update the changelog and version number. Build the package locally.
    verify: "uv build"
    next: tag

  tag:
    title: Tag
    ask:
      prompt: version and changelog look right — allow tagging?
    instructions: Create the release tag and push it.
    next: publish

  publish:
    title: Publish
    ask:
      prompt: tag pushed and CI green — allow publishing to the index?
    instructions: Publish the built package.
    next: announce

  announce:
    title: Announce
    instructions: Draft the release notes from the journal; do not send.
```

> `gate: <message>` is the old spelling of `ask: {prompt: <message>}`. It
> still works — `claunch cflow show` will tell you where you are still using
> it — but only `ask` can name an agent as the approver.

---

## Recipe 5 — let another agent decide

A run driven by one agent can put a decision to a *different* one: a reviewer
alongside it, or the leader above it. This is not the asking agent consulting
a peer and acting on the reply — it cannot see or record the answer at all.
The responder writes the decision into the run itself, which is what makes
the approval worth having.

A delegation says two separate things. **`from`** is who is asked. **`otherwise`**
is what happens when none of them answers.

```yaml
name: delegated-dev
steps:
  impl:
    instructions: Implement the design, or address the latest feedback.
    next: verdict

  verdict:
    title: Review verdict
    select:
      prompt: |
        The diff is on the branch and the suite is green. Ready to ship, or
        another pass? Answer against the code, not the description of it.
      chooser:
        from:
          - {role: reviewer}    # anyone reachable holding that role
        otherwise: human        # nobody there? a person decides
        timeout: 1800
      options:
        ready:  {description: satisfied, next: ship}
        rework: {description: another pass, next: impl}

  ship:
    title: Ship
    ask:
      prompt: Reviewer passed this. Approve the commit and push?
      from:
        - {role: leader, scope: ancestor}   # the chain of command only
      timeout: 900
      on_decline: impl        # a refusal has somewhere to go
    instructions: Commit and draft the PR description from the journal.
```

**How the list is read.** One group at a time, top down. A group that matches
nobody is skipped — with its reason — and the next is tried. A group that
matches several people is asked all at once; the first valid answer wins.
When a group runs out of `timeout`, or everyone in it answers `abstain`, the
question moves along the same way. When the list is exhausted, `otherwise`
decides: `human` (the default) holds the run for a person, and `self` lets
the driving agent carry on alone — recorded in the journal as *unanswered*,
never as an approval.

**Who can be a candidate, and why that is the safety property.** The pool is
everyone the asking session can message over the mesh, *minus itself and
everything it spawned*. That exclusion is the whole thing: an agent can spawn
a child and wire itself to it, so a child holding the right role would let a
run manufacture its own approver. It cannot spawn a sibling, and it cannot
wire itself to one — `connect` needs authority over both ends, which runs
strictly down the tree — so a sibling reviewer is exactly as trustworthy as a
parent, and is the shape most of these take. Add `scope: ancestor` when only
the chain of command will do.

**Reach is deliberate.** A spawned session is wired to its parent and nobody
else, so by default the direct parent is the only candidate. Making a sibling
reviewer reachable is somebody's decision: `claunch mesh connect dev1 rev1`,
the `connect` tool from a session above them, or an `auto_link` rule. When a
role matches but is not wired, the skip reason names the command that fixes
it — and `claunch cflow request` reports the whole picture before the run
even starts.

**What a responder does.** It sees the question through `asks`, investigates,
and calls `answer {ask, decision, reason}`. The decision must be one of the
options the workflow declared, or `abstain` — nothing is parsed out of prose,
so no wording can widen the answer set. It never receives the asking step's
instructions: it decides, says why, and stops.

**What can go wrong, and what happens.** No daemon, no mesh membership, an
ambiguous mesh, a role nobody holds, a match nobody wired you to, or a
candidate that only exists on another machine — all of them skip the group
with a stated reason, and the question ends up wherever `otherwise` says.
There is no setting that turns "nobody answered" into "approved": even
`otherwise: self` is journaled as unanswered, so a run that proceeded alone
never reads as one that was approved.

If the driving session belongs to more than one mesh, say which to look in
when the run starts — the workflow stays portable, and which mesh a leader
lives in is a fact about the deployment rather than about the process.

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
from outside a session, target one with `-t <session>`. That key is also the
run's *identity* everywhere it is shown: with three workers on one workflow in
one tree, the session name is the only thing telling their runs apart. It is
spelled like a session name (letters, digits, `.`, `_`, `-`) because it
becomes a directory under `.cflow/runs/`; anything else is refused.

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
    waiting_answer)                   # with another agent; not ours to clear
      sleep 60 ;;
    *) claunch send-keys -t worker 'continue per /cflow protocol' Enter ;;
  esac
done
claunch cflow journal -t worker
```

Exiting on `waiting_approval` (rather than auto-approving) keeps the
approval meaningful: the script escalates to a person, who runs `claunch
cflow approve` and restarts the loop. Auto-approving in the script would
make it decorative — if a pause point never needs a human, it should not be
an approval in the first place.

`waiting_answer` is the one pause the script should simply *wait out*: the
question is with another agent, and its own `timeout` will move it along (to
the next candidate group, and eventually to a human) without the script's
help. `claunch cflow asks --session <who>` shows what is sitting with whom
if a run has been quiet for a while.

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

**Delegate the decision, never the judgement.** An `ask` is worth having only
because the answer comes from somewhere the run cannot reach. So point it at
a role that will actually look — and write the `prompt` as a question about
the artifact ("does the test cover the classification path?"), not about the
run's own account of it ("the agent says it is done — ok?"). A responder that
can only read your prompt has nothing to check against.

**Give a refusal somewhere to go.** Without `on_decline`, a decline parks the
run for a human, which is safe but is a dead end at 3am. `on_decline: impl`
turns a refusal into another pass, which is usually what it means.

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
