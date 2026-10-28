---
title: MCP — session hierarchy management
description: How to use the orphans·adopt·adopt_request·give tools — find orphaned sessions, move a session (or a whole subtree) under a new parent with a user approval card every time, and finish the move with the returned plan.
---
## Session hierarchy management (orphans · adopt · adopt_request · give)

These tools re-draw the session tree: adopt a session whose parent exited,
pull one child out of a live parent's subtree, hand your own child to
another session, or fold a parent-child pair into your own hierarchy. They
are the agent path for what only the operator CLI (`claunch reparent`)
could do before. The normative contract is the design doc
`docs/session-hierarchy-management.md` — when this pack and the doc
disagree, the doc wins and this pack should be corrected.

### Who may call

- `adopt` and `give`: a session holding the `coordinator` role whose mesh
  membership covers the target's project. Until the role split lands
  (issue `claunch-fnnrk.1`, still open), `coordinator` normalizes to `pm` —
  read the doc's §4 for the migration state before relying on the name.
- `orphans` and `adopt_request`: any session may read or ask.

### The tools

- `orphans(project?)` — read-only. Returns sessions whose parent has
  exited or vanished, each with its run state (workflow, node, what it
  waits on), issue, and branch. `children` shows only your own subtree, so
  this is how you see stranded sessions at all.
- `adopt(session, source?: keep|retire|adopt_parent, reason)` — moves the
  target and its whole subtree under you. `source` says what happens to the
  old parent: `keep` (default: leave it), `retire` (request its wind-down
  once it has no live children — the kill gate applies), `adopt_parent`
  (move the old parent along too). Returns `{session, parent, previous,
  depth, connected, run, plan, warnings}` — the `plan` lists what a person
  or a later phase must still do.
- `adopt_request(session, reason)` — no tree change. Files the approval
  request with the source parent or the user; use it when you are not the
  coordinator or want the move proposed rather than executed. (Phase 2 —
  see availability below.)
- `give(session, parent)` — moves your own descendant under another
  session. Same side effects as `adopt`; its refusals follow `reparent`,
  not `adopt` (the doc's §3 table). (Phase 2.)

### The approval gate (decision D1)

Every `adopt` call passes a user approval card — orphaned adoptions
included. The card is the same shape as the spawn·kill gate. There is no
automatic path: a target whose parent exited still waits for the card.
If the user declines, the refusal is `approval-denied`. Whether a live
source parent's consent is also collected alongside the card is decision
D2 and still open — when it settles, collect both.

### Refusals

`not-a-coordinator` (caller lacks the role), `not-in-scope` (target in a
different project), `no-consent`, `approval-denied`, `cycle`, `depth`
(`spawn.max_depth`), `parent-exited` (the *new* parent has exited),
`mid-landing` (a landing is in flight — do not move a session mid-landing),
`relation-conflict` (phase 1 refuses a stacked worker under a parent that
owns no stack). Treat a refusal as the answer: do not retry around it.

### After the move

The tree changes; most coordinates do not move by themselves. Landing
routing re-resolves from the live tree on the next request, but the run
coordinates are stale until handled:

1. Read the returned `plan` first — it names the branch rebase and its new
   base, the in-flight landing records to withdraw from the old parent's
   queue (`WITHDRAWN @ <tip>`), the board parent link to fix, and the run
   inputs to rewrite.
2. Phase 1 refuses `relation-conflict` rather than rewriting `relation`.
   When the relation rewrite lands (decision D3: the daemon rewrites run
   inputs conditionally, journaled as `rehome`), the plan carries it.
3. Register an adopted session's issue on the new coordinator's `jobs`
   collection (`flow_fork`) — without it the coordinator's accounting
   never sees that issue. (Phase 2 — the `jobs` collection arrives with
   the coordinator role split, `claunch-fnnrk.1`.)
4. Announce the move: the moved session, the new parent, and the old
   parent get one batch send; leave the move (who, from, to, why) as a
   comment on the board's parent issue.

The operating procedure in the doc's §6 is the checklist for a cleanup
round: read `orphans`, write the one-line move record, call `adopt`, run
the plan, announce, and leave the cleaned-up state on the board.

### Unsettled decisions (see the doc's §7)

Decisions D2, D4, D5 and D6 are not settled — the phase split below and
the `relation-conflict` rule are the design's proposal, not decided
fact. What changes in this pack when each settles:

- **D2** (live source-parent consent alongside the card): the approval
  gate section gains the second consent step.
- **D4** (coordinator role name and the pm/coordinator alias split): the
  "Who may call" wording above is rewritten with the settled name.
- **D5** (whether a coordinator may own a stack): the
  `relation-conflict` refusal either becomes a hard rule or is replaced
  by the stack-aware path.
- **D6** (phase-1 scope): the "Availability (phases)" section below is
  re-cut along whatever phase boundary is decided.

### Availability (phases)

As currently planned (decision D6 is still open — see above), phase 1
ships `orphans` and the orphan path of `adopt` only — no `relation`
rewrite, refused as `relation-conflict` where the stack cannot be
settled. `adopt_request` and `give` are phase 2 (live-tree adoption).
The stack-owning worker move with the `stack-v2` `rehome` procedure is
phase 3. Until a phase's issue (`claunch-fnnrk.2`, `.5`, `.3`, `.4`) is
assigned and landed, the corresponding calls above describe the contract,
not running code.
