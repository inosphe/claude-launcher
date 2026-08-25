"""The two topology skills a lead reaches for once its team outgrows a star.

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
branch as a stacked pull request (the ``improv-mid`` workflow) and you
integrate once.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

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
owns the area, the area's workers report to it, and it hands you ONE branch.
The nested worker runs the `improv-mid` workflow — still a `worker` on the
mesh (you stay the only leader), but its run is a small control loop, not a
one-goal round: it lands the area's branches on its own branch as a
**stacked pull request** and requests integration from you once (see "The
stack" below). This skill is how you put an existing flat team into that
shape without respawning anybody.

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
3. **Spawn the nested worker** with `spawn`: role `worker`,
   `workflow: improv-mid`, its own `worktree` (prefixed with a session
   name, e.g. `<your session>-web-batch` — that branch becomes the stack
   base), and a `task` that names: the sessions it is about to receive; the
   shared area; that it runs the area as a stack (its branch is the base,
   each child branch lands on it with `--no-ff` in order, the rest restack
   after each landing), runs the reduced suite per landing, and requests
   integration from YOU once with the whole stack; and that it never
   touches master. A nested worker that has to ask what it exists for has
   already cost you a turn.
4. **Move each worker:** `reparent` (MCP: `{session: CHILD, parent: MID}`),
   one call per worker. Each call moves that worker's subtree, opens the
   worker↔MID edge in every mesh the two share, and leaves the worker↔you
   edge as it was. The worker keeps its terminal, conversation, handle,
   worktree and cflow run — only who it answers to changes.
5. **Brief, in ONE batch send** with a section each: to every moved worker
   ("your parent is now MID and your integration target is MID's branch
   `<stack base>`, not master: send completion reports and integration
   requests to it, not to me; measure your diff against that branch; a
   restack notice from MID means rebase onto it and re-request"), and to
   MID the roster it now owns. This step is not optional — a moved worker's
   own briefing still names you as its parent, and its `rebrief` will only
   show the new one from now on.
6. Optional: `disconnect` yourself from the moved workers once the handover
   has settled, if you want their traffic to stop reaching you. Keep the edge
   while it settles.
7. Record the new shape in your standby report: who, under whom, why.

## The stack

What MID runs is a stacked pull request with MID's branch as the base:

```
master ← MID branch (stack base; merge commits only)
             ↑ --no-ff, in order          landing order: bottom-up
             ├── w1 branch   (base: MID)
             ├── w2 branch   (base: MID)         ← independent: a fan
             └── w3 branch   (base: w2)          ← depends on w2: a chain
```

- Every child branch has a declared **base**: MID's branch, or a sibling's
  branch when it builds on that sibling's work. A child's request is
  measured against its base (two-way diff, `merge-tree`), not master, and
  goes to MID, not you.
- MID lands one child at a time with `--no-ff`, then sends the rest a
  **restack notice**: `git rebase <base>` — commits already on the base
  are skipped, so only the child's own commits replay. A chained child
  lands only after the branch it stands on.
- Children MID spawns itself start on the stack: `spawn` with
  `rebase_onto: <MID branch>` cuts a new worktree from that branch instead
  of the trunk. Workers you moved were cut from master — MID asks them to
  rebase onto the base if it has moved since.
- MID's base takes merge commits only; the area's code lives in children.
  When it hands off, it aligns the base on master with
  `git rebase --rebase-merges master` (a plain rebase would flatten the
  stack) and sends you ONE request carrying the stack table.

## After

- Integration: MID's branch arrives as ONE candidate carrying the stack
  table (order, child branch, base, merge commit, numbers) — review it like
  any other (two-way diff, `merge-tree`, one full sweep) and merge it with
  ONE `--no-ff`; never merge its children separately. A rebase re-request
  to MID is a restack: it re-aligns with `--rebase-merges` and re-requests.
- Landing gates are unchanged: each moved worker's `landing` (request/hold)
  is still the user's, and so is MID's; what MID decides alone is landing a
  child on ITS OWN branch (the way you decide master alone).
- Undo: `reparent` a worker back to yourself; `kill` MID once its branch has
  landed and its report is in (it kills itself at the end of its run).

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


def write_skills(skills_dir: Path) -> List[Path]:
    """Write both topology skills into ``skills_dir``; return their paths."""
    out: List[Path] = []
    for name, text in (
        ("mesh-wire", WIRE_SKILL_MD),
        ("mesh-delegate", DELEGATE_SKILL_MD),
    ):
        path = skills_dir / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        out.append(path)
    return out
