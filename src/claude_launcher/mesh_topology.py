"""The topology skills a lead reaches for once its team outgrows a star.

Every child a session spawns starts connected to that session and to nobody
else (see :mod:`mesh_install`), and every spawn hangs off the spawner — so a
team grows as a star around its lead until somebody says otherwise. That is
right for two or three workers and wrong for eight: the lead ends up relaying
questions between peers who could have asked each other, and merging six
branches from one file one at a time.

The tools to change the shape already exist — ``connect`` for the edges,
``reparent`` for the tree — and each is a single call. What was missing was
the *procedure*: when the shape has outgrown the star, which call, and what
to tell whom afterwards, because a moved worker's own briefing still names
the parent it was spawned under. These two skills are that procedure, kept
apart from the ``mesh`` skill the way ``cflow-author`` is kept apart from
``cflow``: a member reading how to send a message does not need to carry the
rules for re-drawing the team, and the lead who does needs them whole.

``mesh-wire`` — open an edge between two members who keep needing each
other through you.  ``mesh-delegate`` — spawn a nested worker for a crowded
area and move that area's workers under it, so their branches land on its
stack as a stacked pull request (the nested worker's ``stack`` sub run, beside
its ``improv-worker`` run) and you integrate one branch per landing.  ``mesh-retopology`` — every other re-drawing of the tree
that ``reparent`` does: take a finished tier's workers back, adopt the
children of a parent that exited, move a worker to the tier its work
belongs to, undo a delegation — and the briefing that has to follow each
move, because the moved session's own briefing still names its old parent.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from . import fsplan

WIRE_SKILL_MD = """\
---
name: mesh-wire
description: >-
  Connect two members of your mesh so they message each other directly
  instead of through you. Use it the moment you relay a question between the
  same two members for the second time, when two members report that they
  touch the same file, function or contract, or when a member says it cannot
  reach a peer it needs. A lead's skill: you may only wire sessions you
  spawned (or their descendants). Usage: /mesh-wire MESH A B — or decide the
  need is there and do it without being asked.
---

# Procedure: wire two peers

Your children start connected to you and to nobody else. That is right until
two of them need each other's answers — then every exchange costs you a turn
and them a delay, and the second relay is the signal, not the fifth.

## When

- You have forwarded a message between the same two members once already.
- Two members told you they edit the same file, function, form or contract
  (the usual "shared-file alert" in an intake report).
- A member says "I am connected only to you" while naming the peer it needs.

Do **not** wire members whose independence is the point — two reviewers who
cannot compare notes give you two opinions instead of one — or one you are
deliberately isolating.

## How

1. `members` (MCP) or `claunch mesh members MESH` — confirm both handles, and
   that at least one is in your subtree (`children` lists it).
2. `connect` (MCP: `{mesh, a, b}`) or `claunch mesh connect MESH A B`.
3. Tell both, in ONE batch send with a section each: who they can now reach
   and about what ("you can now message s72 directly about the restart
   signal; I stop relaying"). Skip this and they keep sending through you —
   nothing tells a member its reachability changed.
4. If you were holding an unanswered question between them, forward it one
   last time with the note that the reply goes direct now.

## Refusals

- "may not rewire": neither endpoint is in your subtree. Ask the session
  that spawned them, or the user (`claunch mesh connect` is the operator's
  form and is not scoped).
- To cut an edge again: `disconnect`, same authority. A send across a cut
  pair is refused — there is no routing around it.
"""

DELEGATE_SKILL_MD = """\
---
name: mesh-delegate
description: >-
  Spawn a nested worker for one crowded area of the work and move your
  existing workers in that area under it (re-parent), so it collects their
  branches and you integrate one branch instead of many. Use when three or
  more of your children edit the same file, module or UI surface, when your
  integration queue is serialising on one area (each merge re-runs the full
  sweep and re-bases the rest), or when your turns are going to relaying
  inside one group. A lead's skill: only your own subtree can be moved.
  Usage: /mesh-delegate — or decide the need is there and follow the
  procedure without being asked.
---

# Procedure: delegate an area to a nested worker

A team spawned by one session is a star: every worker reports to you, every
branch merges through you. That is right for three workers on three areas and
wrong for six on one file — you end up arbitrating hunks between siblings and
running the full sweep once per branch. The fix is a tier: one nested worker
owns the area, the area's workers report to it, and it hands you ONE branch
per landing. The nested worker is an ordinary worker — role `worker`, run
`improv-worker` (your pairing; you stay the only leader) — that stands a
`stack` **sub run** beside its main run: the sub run lands the area's
branches on a session-long stack branch as a **stacked pull request**, and
each time the worker lands, its round branch carries the stack cut so far
(see "The stack" below). This skill is how you put an existing flat team
into that shape without respawning anybody.

## Triggers (any one)

- `children` shows three or more live children whose reported scope touches
  the same file, module or surface (one `app.js`, one form, one API).
- You merged two branches from the same area in a row and a third is
  waiting.
- Two of them already overlap inside one function and you are the one
  keeping their hunks apart.

Do not delegate an area of one or two workers, and never to shorten your
roster: a nested worker is another terminal and another landing gate.

## Procedure

1. **Pick the members.** `children` plus the scopes they reported. Write the
   list and the shared area in one line — it becomes the nested worker's
   task.
2. **Check the room.** `children` → `depth` / `max_depth`. Every moved worker
   goes one level deeper, and its own children with it; `reparent` refuses a
   move that would put any of them past `spawn.max_depth`. With the default
   (3, root at 0) a lead at depth 0 can insert one tier for workers that have
   no children of their own.
3. **Spawn the nested worker** with `spawn`: role `worker`, no `workflow`
   (your pairing, `improv-worker`, is what it runs), its own `worktree`
   (prefixed with a session name, e.g. `<your session>-web-batch`), an
   issue for the area (`claunch beads create "area stack: <area>" ...`, then
   `issue: <id>`), and a `task` that names: the sessions it is about to
   receive; the shared area; that its round is a delegation round (its own
   feature commits may be zero — the area's code lives in the children);
   that in `work` it stands the `stack` sub run BEFORE you move anyone (the
   `start` tool with `workflow: stack`, `sub: stack`, `inputs: {issue,
   base}`), and that it lands each child on `<its session>-stack` with
   `--no-ff`, restacks the rest after each landing, and requests
   integration from YOU with the cut on its round branch; and that it never
   touches master. A nested worker that has to ask what it exists for has
   already cost you a turn.
4. **Move each worker:** `reparent` (MCP: `{session: CHILD, parent: MID}`),
   one call per worker. Each call moves that worker's subtree, opens the
   worker↔MID edge in every mesh the two share, and leaves the worker↔you
   edge as it was. The worker keeps its terminal, conversation, handle,
   worktree and cflow run — only who it answers to changes.
5. **Brief, in ONE batch send** with a section each: to every moved worker
   ("your parent is now MID and your integration target is MID's stack
   branch `<MID>-stack`, not master: send completion reports and
   integration requests to it, not to me; measure your diff against that
   branch; a restack notice from MID means rebase onto it and re-request"),
   and to MID the roster it now owns (its `stack` run's open step then
   tells each of them the base in its own words). This step is not
   optional — a moved worker's own briefing still names you as its parent,
   and its `rebrief` will only show the new one from now on.
6. Optional: `disconnect` yourself from the moved workers once the handover
   has settled, if you want their traffic to stop reaching you. Keep the edge
   while it settles.
7. Record the new shape in your standby report: who, under whom, why.

## The stack

What MID's `stack` sub run keeps is a stacked pull request on a
session-long branch, cut into MID's round branch at each of MID's landings:

```
master ← MID round branch ← --no-ff the cut (stack-merge, once per landing)
                                 ↑
                           MID-stack (merge commits only)
                                 ↑ --no-ff, in order     landing order: bottom-up
                                 ├── w1 branch   (base: MID-stack)
                                 ├── w2 branch   (base: MID-stack)  ← a fan
                                 └── w3 branch   (base: w2)         ← a chain
```

- Every child branch has a declared **base**: `<MID>-stack`, or a sibling's
  branch when it builds on that sibling's work. A child's request is
  measured against its base (two-way diff, `merge-tree`), not master, and
  goes to MID, not you.
- MID's stack run lands one child at a time with `--no-ff`, then sends the
  rest a **restack notice**: `git rebase <base>` — commits already on the
  base are skipped, so only the child's own commits replay. A chained child
  lands only after the branch it stands on.
- Children MID spawns itself start on the stack: `spawn` with
  `rebase_onto: <MID>-stack` cuts a new worktree from that branch instead
  of the trunk. Workers you moved were cut from master — the stack run's
  open step asks them to rebase onto the base.
- The stack branch takes merge commits only; the area's code lives in
  children. When MID lands, its run asks the stack for a **cut** (what has
  landed so far; children still working go to the next cut) and merges it
  once into its round branch — that branch is the ONE request you get,
  carrying the stack table. After MID lands on master the stack moves onto
  the new master with `git rebase --rebase-merges` (a plain rebase would
  flatten it).

## After

- Integration: MID's round branch arrives as ONE candidate carrying the
  stack table (order, child branch, base, merge commit, numbers) — review
  it like any other (two-way diff, `merge-tree`, one full sweep) and merge
  it with ONE `--no-ff`; never merge its children separately. A rebase
  re-request to MID re-aligns with `--rebase-merges` and re-requests.
- Landing decisions are unchanged: a moved worker's `landing` is its own
  when the machine checks are clean (it picks `request` and goes), and only
  an `escalate` puts `landing-review` (request/hold) in front of its parent
  -- which after a reparent is MID, not you. MID's own landing works the
  same way and escalates to you. What MID decides alone is landing a child
  on ITS OWN branch (the way you decide master alone).
- Undo: `reparent` a worker back to yourself; `kill` MID once its branch has
  landed and its report is in (its `settle-check` waits for the stack to be
  sealed, then its run ends).

## Refusals

- "does not command": the session is not in your subtree — you hand over
  only what you spawned (or what your children spawned).
- "would put a session N level(s) deep": the subtree does not fit under the
  new parent. Move fewer or shallower workers, or ask the user to raise
  `spawn.max_depth` in `~/.claunch.yaml`.
- "has exited": pick a live parent.
- "cannot move itself": a session is moved by the one that commands it, or
  by an operator (`claunch reparent SESSION PARENT`).
"""


RETOPOLOGY_SKILL_MD = """\
---
name: mesh-retopology
description: >-
  Re-draw your team's tree with the reparent tool: take a nested worker's
  members back once its stack has landed, adopt the live children of a
  parent that exited, move a worker to the tier its work actually belongs
  to, or undo a delegation that did not pay off. Use when `children` shows
  a mid-worker that has handed off and still holds members, a session
  marked exited that still has live children, or a worker reporting to the
  wrong parent for the branch it is on. A lead's skill: only sessions you
  spawned (or their descendants) can be moved. mesh-delegate is the one
  case of adding a tier; this is every other move. Usage: /mesh-retopology
  — or decide the need is there and follow the procedure without being
  asked.
---

# Procedure: re-draw the tree

A team's shape is a tree of who reports to whom, and `reparent` moves one
session — with everything under it — from one parent to another in a single
call. The call is cheap; what costs is everything the tree implied: which
branch a worker measures its diff against, who receives its integration
request, whose restack notices it must obey, and the briefing it was spawned
with, which still names the old parent and will keep naming it. This skill
is the moves a lead makes with `reparent` outside of delegating an area
(that one has its own skill, `mesh-delegate`), and the sentence that has to
follow each move.

## Triggers (any one)

- **A tier is done.** A mid-worker's stack is sealed and its last cut landed
  on master (you merged its branch) but `children` still shows members under it — they finished
  and are wrapping up, or they hold frozen branches. Take them back so their
  next round (if any) reports to you and their hold-branches sit in your
  queue, not a retired tier's.
- **A parent died.** `children` shows a session as `exited` with live
  children beneath it (a mid that crashed, a worker that spawned helpers and
  was killed). Those children are orphans: their requests go to a terminal
  nobody reads. Adopt them.
- **Wrong tier.** A worker's branch belongs to an area a mid owns (it edits
  the same surface, or was cut from that mid's stack branch), but it reports
  to you — or the reverse. Move it to where its integration target is.
- **Undo.** A delegation that did not pay off — the mid is idle, the area
  thinned out to one worker, or the stack is blocking more than it batches.
  Move the members back, then `kill` the mid once its report is in.

Do not move a session mid-landing (its `landing-review` is out with its
parent, or its request is on a mid's stack and about to land): let the
landing finish, then move. And do not move to "tidy" — every move costs a briefing.

## Procedure

1. **Read the tree.** `children` (MCP) — the subtree you command, each
   session's status, its cflow run and step, `depth`/`max_depth`. For a
   fuller view of who stands where: `claunch sessions`. Name the move in
   one line: *who*, from *whom*, to *whom*, and *why* — it becomes the
   briefing and the standby-report entry.
2. **Pick a live target you command.** Yourself, or a session in your
   subtree. An exited session cannot receive; a session cannot be moved
   under itself or its own descendant.
3. **Check the room.** The moved session and its whole subtree go to the
   target's depth + 1. `reparent` refuses a move that pushes any of them
   past `spawn.max_depth`; moving *up* (back to you) always fits.
4. **Read the moved session's run before touching it:** `claunch cflow
   status -t SESSION --json`. A run at `landing` (`waiting_selection`) or
   in `rebase`/`integration-request` against a specific base is mid-flight
   — wait, or accept that the briefing below must include a new base and a
   restack.
5. **Move:** `reparent` (MCP: `{session: SESSION, parent: TARGET}`), one
   call per session; each carries its subtree. What the daemon does for
   you: opens the SESSION↔TARGET edge in every mesh the two share. What it
   leaves: the edge to the old parent (cut it with `disconnect` if that
   parent is live and should stop hearing from the session), and every
   file, branch and run the session had. What it does not do: tell anyone.
6. **Brief, in ONE batch send** with a section each — this is the step
   that makes the move real:
   - to the moved session: "your parent is now TARGET; send completion
     reports and integration requests to it; your integration target is
     `<branch>` (master when TARGET is the lead; `<TARGET>-stack` when
     it is a mid); if your branch was cut from the old base, rebase onto
     the new one before requesting" — and, when it is mid-flight, what to
     do with the request it already sent;
   - to TARGET (unless it is you): the session it now owns, its branch,
     its base, and where it stands in its run;
   - to the old parent, if live: that the session left, so it stops
     waiting on it.
   Its own briefing still names the old parent and `rebrief` will only show
   the new one from now on; nothing else corrects it.
7. **The board.** The moved session's issue keeps its assignee. If TARGET
   is a mid with a stack issue, hang the issue under it (`claunch beads
   update <id> --parent <mid's issue>`); if the session came back to you
   from a mid, clear that parent link the same way. A frozen branch that
   moved with the session stays `in_progress` with its `HOLD:` comment —
   it is now on your `in_review` horizon, not the mid's.
8. **Record** the new shape in your standby report: who, under whom, why —
   and, for an adoption, which exited session they came from.

## The four moves, side by side

| move | reparent to | integration target after | extra |
|---|---|---|---|
| tier done | you | master (via you) | `kill` the mid once its report is in |
| parent died | you (or a live mid) | master / that mid's stack branch | check each orphan's run first — one may be at a human gate nobody is watching; surface it |
| wrong tier | the mid | the mid's stack branch | the worker rebases onto the base (`spawn`'s `rebase_onto` did this for children the mid spawned; a moved one does it by hand) |
| undo delegate | you | master | the mid's landed stack is already on master; unlanded child branches come back as your queue |

## Refusals (the daemon's words)

- "does not command": the session is not in your subtree — only what you
  spawned, or what your children spawned, is yours to move. The operator's
  form, `claunch reparent SESSION PARENT`, is not scoped.
- "cannot move itself": a session is moved by the one that commands it,
  never by itself.
- "has exited": the target must be live. Orphans are moved *from* an exited
  parent, never *to* one.
- "make a cycle": the target is the session itself or something under it.
- "would put a session N level(s) deep": the subtree does not fit under the
  target. Move fewer or shallower sessions, or ask the user to raise
  `spawn.max_depth` in `~/.claunch.yaml`.
"""


def write_skills(skills_dir: Path) -> List[Path]:
    """Write the topology skills into ``skills_dir``; return their paths."""
    out: List[Path] = []
    for name, text in (
        ("mesh-wire", WIRE_SKILL_MD),
        ("mesh-delegate", DELEGATE_SKILL_MD),
        ("mesh-retopology", RETOPOLOGY_SKILL_MD),
    ):
        path = skills_dir / name / "SKILL.md"
        fsplan.write_text(path, text)
        out.append(path)
    return out
