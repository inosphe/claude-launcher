"""The ``/cflow-author`` skill: how to WRITE a workflow, as opposed to run one.

Kept apart from :mod:`claude_launcher.cflow.install` (which owns the execution
protocol) for the reason that module's own docstring gives: a skill's body
loads whole, and the rules for authoring a graph are dead weight in a session
that is executing one — and the reverse. The two also trigger on different
things, which a merged ``description`` could not express.

The text is opinionated on purpose. A workflow's value is entirely in its
control points; a file with none is a checklist that has been spread across
several tool calls, and is worse than the checklist. Most of what is here is
about choosing the weakest control point that still holds, and about the one
question an agent must never be allowed to answer about its own work.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD = """\
---
name: cflow-author
description: >-
  Write or revise a claunch workflow file (.claunch/workflows/*.yaml) — the
  step graph, its gates, its delegated decisions, its verify commands and its
  done_when completion criteria. Use
  when asked to create a workflow, add or reshape a step, decide who approves
  what, convert a deprecated 'gate:', or review an existing workflow file.
  NOT for running one: that is the 'cflow' skill.
---

# cflow — writing a workflow

A workflow is mechanical logic wrapped around an agent. Its value is in the
**control points**, not in the prose: an agent that is told what to do next
will do something either way. So the whole job of this file is deciding, for
each decision the work contains, *who is allowed to answer it and how it is
enforced*.

If your draft is a chain of `instructions` + `next` with no `verify`, no
`select` and no `ask`, you have written a checklist and split it across
several tool calls. Put it in ONE step's instructions instead — it will be
followed just as well and cost nothing to run.

## The control points, weakest first

Always take the weakest one that actually holds. Each row costs more than the
one above it, and the bottom two cost a person's attention.

| Control | Who decides | Use it when |
| --- | --- | --- |
| `verify:` | a command | the answer is machine-checkable. Exit 0 or the run does not advance |
| `select` `chooser: agent` | the driving agent | the answer is a *fact about the artifact* it can read off (did the diff touch this surface?) |
| `select` `chooser: {from: [...]}` | another session | it is a judgment, and the driver has a stake in the answer |
| `ask:` | another session, else `otherwise` | entering the step is irreversible or expensive |
| `chooser: user` / `ask:` with no `from` | a person | nobody else can know (product intent, risk appetite, "is this what you meant") |

`verify` is the one to reach for hardest. "The tests pass" is not a question
for anybody — it is `verify: "pytest -q"`, and the server re-runs it, so no
report can talk its way past it.

## `done_when` — the stated completion criterion

Most steps have no command that can check them — "the design note is filed",
"the feedback is turned into follow-ups". For those, write `done_when`: one
or two lines stating what must be TRUE for the step to count as done,
phrased as a state of the world, not as an activity.

```yaml
impl:
  instructions: Implement the design; keep commits small and focused.
  done_when: The design is implemented and committed; the report names the files touched.
  next: test
```

It is not a control point — nothing enforces it, and the driver still
certifies its own way past it. Its value is placement: it rides in the step
payload and in the reminder a stalled run hears, so "may I advance?" gets
judged against a criterion you wrote instead of one the agent improvises
mid-drift. Write both where they split the work: `verify` checks what a
command can, `done_when` states the rest. If a command CAN check it, promote
it to `verify` — a `done_when` saying "the tests pass" is a verify that gave
up its teeth.

A select step takes neither: its completion is the choice. `cflow show`
lists the steps that declare no criterion at all, as advice, not an error —
a cheap step may honestly not need one.

## The rule that matters most

**Never let the agent decide something whose answer lets it skip work.**

Look at every `chooser: agent` and ask what each option costs the chooser. If
one branch skips a verification step, a spec, or a review, then the party
choosing and the party who benefits are the same party — and the option will
get chosen, not because the agent is dishonest but because the case for it is
always available. Delegate that one:

```yaml
netcheck:
  select:
    prompt: |
      Does this change need the full owner/server/non-owner trace?
      If it is ambiguous, it needs it.
    chooser:
      from: [{role: reviewer}]     # the driver must not answer this
      otherwise: human             # ...and if no reviewer is reachable, a person does
      timeout: 1800
    options:
      trace: {description: run the checklist, next: netverify}
      skip:  {description: does not touch the network surface, next: review}
```

`chooser: agent` stays correct when the answer is a fact the next `verify`
would catch anyway — "did this diff touch the TypeScript surface" is fine,
because guessing wrong makes the following `tsc` fail. Getting it wrong is
self-correcting; skipping a review is not.

## Delegated decisions

Two independent axes. `from` is **who is asked** — an ordered list of roles,
tried a group at a time, first valid answer wins. `otherwise` is **what
happens when that list is exhausted**: `human` (default — the run holds for
`claunch cflow approve|select`) or `self` (the driver proceeds alone,
journaled as unanswered and never as an approval).

- **`role` is required.** A delegation is to a function. "Whoever I happen to
  be wired to" would make the answer depend on topology alone.
- **`scope: ancestor`** narrows to the asking session's own chain of command.
  Use it for authority decisions (shipping, spending, releasing); leave the
  default `any` for competence decisions (reviewing), where a sibling is the
  normal and best answer.
- **A human is never an entry in `from`.** Nothing resolves or notifies them.
  An `ask:` with no `from` at all is exactly a human gate — which is what the
  deprecated `gate:` becomes.
- **Nobody can approve their own run**, and a session's own descendants are
  never candidates, so spawning a `reviewer1` to pass your own work does not
  work. Do not try to design around this; design with it.
- **Reach is deliberate.** A spawned session is wired to its parent and nobody
  else, so a sibling reviewer is reachable only once somebody connects them
  (`claunch mesh connect dev1 rev1`). Write the workflow for the shape you
  want and let the skip reason name the missing wire — do not weaken the role
  to whatever happens to be reachable today.

## Writing a prompt somebody else will answer

The responder **never receives the asking step's instructions**. It gets the
prompt, the options, and its own investigation — so the prompt has to stand
alone. Three rules:

- **Say what to judge against, and make it the artifact.** "Answer against the
  code, not the description of it" belongs in the prompt of every review
  decision, because the alternative is a reviewer rubber-stamping a summary
  the asking agent wrote.
- **Do not argue for an answer.** "This is a small change, ready to ship?" has
  already answered itself. State the situation and the criteria; let them
  decide.
- **Say which way to err.** "If it is ambiguous, require the trace" turns a
  coin flip into a rule, and is where past regressions get encoded.

Option `description`s are read by whoever chooses, so write them as
consequences ("another implement/test pass"), not as labels ("no").

## Cadence — `interval` on an option

Some options are not wrong to take, only wrong to take *too often*: "merge
what is waiting" chosen the moment each branch is ready gives one merge and
one full sweep per task. Put `interval: <seconds>` on the option and the
driver's take of it is **held** until that long has passed since the option
was last taken — across rounds of a `recur` workflow, since the record
outlives the run. The run reports `waiting_window` with `opens_at`, the
daemon's clock releases it then (moving the run and waking the driver), and
what accumulated during the wait is in the batch, because the driver may
re-select the same option while held to update its reason — the reason
confirmed at release is the latest one. Selecting another option cancels
the hold; the first take ever is immediate (nothing to pace against yet).

```yaml
standby:
  select:
    prompt: is there a branch with its evidence complete? then integrate.
    chooser: agent
    options:
      integrate:
        description: merge what is waiting — at most once per window
        next: merge
        interval: 300        # a 5-minute batch window
      wind-down: {description: the user ended the shift, next: end}
merge:
  instructions: merge every waiting branch with --no-ff; the sweep is the next step's.
  next: sweep
sweep:
  ask:                       # the gate between merge and sweep; who opens it
    prompt: master's tip is this batch's merge — sweep the merged tree?
    otherwise: self          # is this workflow's call: self, a role, or a person
  instructions: run the full suite over the merged tree.
  verify: "pytest -q"
  next: end
```

It is a cadence, not a permission: nobody is asked anything while a choice
is held, and a person confirming the option from the CLI or dashboard takes
it at once (journaled as an override). Put it on the option whose cost is
per-take — a merge, a deploy, a sweep — never on one whose delay loses
information.

## `awaits:` — what the step is WAITING for

`verify` and `done_when` both answer "may this step be left?". `awaits`
answers "what is it standing still *for*?", and it is the only one of the
three the driving agent never acts on — the daemon does.

Without it a stalled run is on a clock: the reminder repeats the step's
instructions every interval and has no opinion about whether what the step
waits for has arrived. That is how a run ends up quoting a `verify` that went
green ten minutes ago. With `awaits`, the daemon runs the probe every `poll`
seconds and speaks **once, when its exit code changes** — and says nothing at
all while it does not.

```yaml
reflect:
  instructions: restart the live server onto the merge, then confirm it serves it.
  verify: "python tools/deploy_check.py --branch master"
  awaits: verify             # re-measure THIS step's verify; signal on change
```

Three forms, and there is no bare-command shorthand — a command is always
written under `probe`, so the reserved word can never be mistaken for one:

```yaml
awaits: verify                                   # this step's own verify
awaits: {probe: verify, poll: 30}                # the same, with knobs
awaits:
  probe: "python tools/queue_depth.py --zero"    # something else entirely
  poll: 30                                       # seconds; floor 15
  timeout: 10                                    # seconds; hard cap 30
  describe: the review queue has drained         # one line, for the signal
```

The exit code is the fact. Output rides into the signal as evidence and is
**not** compared, so a probe may print a timestamp without "changing" every
sample. The first measurement at a position is a baseline and never fires:
the state a step arrives in is not news about it.

Write the probe as a cheap question about state something *else* changes —
read a file, ask git, poke a status endpoint. The ceilings enforce that
rather than asking for it: `timeout` may not exceed 30s and `poll` may not go
below 15s, both parse errors. "Wait until the suite is green" is the shape
this field invites and the one thing it must not become — a test run hung on
a probe is re-run for as long as the step sits there. Ask a cheap question
*about* the suite (did it run? is the marker newer than the tip?) instead.

`awaits: verify` is per step and never a default, deliberately. A `verify` is
contracted to run once, on the way out; sampling one every minute also demands
it be read-only and idempotent, and that is fair to ask of a command an author
nominated and unfair to impose on every `verify` already written. Nothing is
taken on trust either: the probe runs under `awaits.timeout`, never the
verify's own, so a suite nominated by mistake times out into "cannot measure"
instead of running on a loop.

What happens when it breaks is the safety story. A probe that cannot be
launched, or does not finish in time, is **no answer** — not the answer "not
yet" — so the ordinary clock reminder resumes for that step. Silence is only
ever granted while something is actually watching, and with no daemon running
nothing is: then `awaits` does nothing at all and the run behaves exactly as
it does today. The same is true of a run already in flight when the workflow
gains the field — a run reads the snapshot it started on, and an absent
`awaits` is today's clock.

## `ask:` gates ENTRY

An `ask` withholds the step's `instructions` until it is answered, so it goes
on the step that **performs** the irreversible action — not on one after it.
Several small approvals beat one big one: each then means exactly one thing,
and a refusal can route somewhere specific.

```yaml
ship:
  ask:
    prompt: reviewer passed this — approve the commit and push?
    from: [{role: leader, scope: ancestor}]
    otherwise: human
    timeout: 900
    on_decline: impl        # where a refusal goes; without it the run parks
  instructions: Commit and draft the PR from the run journal.
  next: end
```

Approvals are **per visit**: a loop that passes an `ask` twice asks twice, and
the second answer may differ. Do not design around that either — it is the
point.

## Loops

Cycles are legal and are warned about (`cflow show` prints them). Two rules:

- **A loop's exit must be a decision, and usually not the driver's.** A loop
  whose exit the agent chooses is a loop the agent will leave when it is tired
  of it. Make the exit a delegated or human `select`.
- **A loop needs a reachable end.** A start that can never reach a termination
  is a load error, not a warning. `max_visits` (default 25) is a backstop that
  parks the run for a human, not an exit.
- **A service loop is not a cycle.** A session that must run its rounds
  indefinitely (a leader's standing A -> B -> C -> A ...) declares top-level
  `recur: true` instead of a back edge: each round still reaches a real
  `end`, and the finished run files the start request for the next one — so
  the graph stays honest, each round gets its own journal, and `max_visits`
  keeps meaning the rework budget *within* a round. The driver cannot end
  the loop; a human stops it (`claunch cflow request --cancel`, or archive/
  abort mid-round). Use a back-edge cycle for work that must *converge*;
  use `recur` for work that must *keep happening*.

## Shape

- The file's own header is `name` and `description`. The description is the
  one line a person is given when something offers this workflow beside four
  others — `claunch cflow ls`, the dashboard's start panel, the spawn
  wizard's Workflow row — and every picker clips it to a line, so lead with
  what a round of it DOES and where it ends. Nothing enforces it: a workflow
  without one is offered as a bare name, which is how the wrong one gets
  started.
- Steps are defined **once** and wired by id, so a shared target (`next: impl`
  from three places) needs no duplication.
- Termination is omitting `next`, or `next: end`.
- A `select` routes only through its options — no `next` on the step. An
  option may carry `interval: <seconds>` — see its section above.
- `verify` is a gate to **leave** a step; `ask`/`gate` gate **entering** it.
  A select step takes neither.
- `done_when` states the leave criterion where no command can check it —
  see its section above.
- `awaits` states what a step WAITS for, so the daemon can signal the change
  instead of repeating the step on a clock — see its section above.
- Steps should say what **evidence** to file in the report. Reports are
  journaled, shown live on the dashboard, and become the PR text; a step whose
  report is "done" has taught the agent nothing about what to record.

## Where the file goes

Two layers answer a name, nearest first:

| `<cwd>/.claunch/workflows/<name>.yaml` | this project only; **wins** |
| `~/.claude-launcher/workflows/<name>.yaml` | every directory on the machine |

Write to the project layer by default. Reach for the global one only when the
workflow is genuinely project-independent — a workflow that names this repo's
build command, test framework or module layout belongs to this repo, and
putting it where every project sees it is how one copy silently becomes two.

If it IS general, do not copy it by hand: `claunch cflow add <name>` promotes
this project's copy (parsing it first) and tells you if the project copy will
keep shadowing it. Never write the same workflow into both layers "to be
safe": that is two files to keep in step, and nothing warns you when they
diverge except the shadow note in `claunch cflow ls`.

## Who may drive it

A workflow written for one function in a fleet can say so, instead of hoping
the right session picks it up:

    filter_roles:
      type: whitelist        # whitelist = only these; blacklist = all but these
      roles: [worker]

The filter is about the DRIVER — the session that starts the run — never
about who it delegates to (that stays each decision's `from`). It is enforced
at `start`, against the driver's mesh role: a leader cannot start a
`whitelist [worker]` workflow by accident, and the refusal names the role it
holds. A driver with no resolvable mesh identity (standalone shell, no
membership, daemon down) is admitted with the fact journaled — the filter
guards a fleet's division of labour, and a standalone run has no fleet.
Role names are matched against the mesh's vocabulary at start time, not at
parse time, for the same reason a delegation's `role` is.

A workflow can also volunteer itself for a role, instead of waiting to be
picked:

    default_role: worker     # pickers auto-select this workflow when that role is chosen
    priority: 10             # tie-breaker when several volunteer for the same
                             # role — higher wins, and pickers list candidates
                             # in this order (0 when unstated)

Advisory, not enforcement: `filter_roles` still decides who may drive, a
picker never volunteers a workflow whose filter turns the chosen role away,
and a `default_role` the workflow's own filter refuses is rejected at parse
time as a contradiction.

A workflow can also name the run its *children* get — the two halves of a
delegation declared as a pair, from the parent's side:

    default_child_cflow: worker-flow   # or: default-child-cflow

A session driving this workflow that spawns a child without naming a run
gives it that one; a workflow declaring no pair gives its children no run at
all. Write it on the parent's half only — the child's role does not decide
this, because a role travels across every workflow while the pairing is a
property of the procedure the parent is actually running. A spawn that names
a workflow outright always wins, and `workflow: '-'` refuses the pair for one
child.

## Before you hand it over

- `claunch cflow show <workflow>` — prints the graph, each step's control
  points, cycle/unreachable warnings, any deprecated spellings still in
  the file, and which steps declare no completion criterion (neither
  `verify` nor `done_when`).
- `claunch cflow ls` — confirms the name resolves to the file you just wrote,
  and not to an older copy of it in the other layer. It prints each
  workflow's `description` beside its name, which is the description read the
  way the person picking one reads it: next to its rivals.
- `claunch cflow request <workflow>` — reports `delegation_check`: what each
  delegated step resolves to *right now*, and which would fall to a human. It
  never blocks; a leader that has not spawned yet is legitimate.
- Read the file back and ask, for each step: *what stops this being skipped?*
  If the answer is "the agent will not skip it", that step has no control
  point.

## Anti-patterns

- **A checklist as a graph.** Ten linear steps, no gates. One step.
- **The self-serving `chooser: agent`** — see the rule above; this is the one
  that actually causes incidents.
- **A person asked what a command could answer.** If `verify` can decide it,
  a human waiting on it is pure latency.
- **One approval at the end covering everything.** Nobody can meaningfully
  approve "all of it"; approve the irreversible actions individually.
- **A prompt that argues.** See above.
- **`gate:`** — deprecated. Write `ask: {prompt: ...}`, which is the same
  human gate and can grow a `from` later. `cflow show` lists where it is
  still used.
- **`otherwise: self` as a default.** It is the one escape hatch, it is
  journaled as unanswered rather than approved, and it belongs only where
  proceeding unreviewed is genuinely better than stopping. Never put it on a
  step that ships, deletes or publishes.
"""


def write_skill(skills_dir: Path) -> Path:
    path = skills_dir / "cflow-author" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_MD, encoding="utf-8")
    return path
