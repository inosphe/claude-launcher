---
title: MCP — cflow watch
description: How to use the runs/subscribe/unsubscribe/subscriptions tools — follow another session's cflow run by cursor instead of asking it for a status report, and recover what a missed delivery carried.
---
## Watching another session's run (runs / subscribe family)

These four tools answer the question a supervisor asks after delegating:
*where is that run now, and what happened while I was doing something else.*
They read the run's own journal (the same events its session drives), so
nothing has to be typed into the other terminal and no workflow text has to
be changed on either side.

- `runs` — where the target's run stands right now: version (1|2), workflow,
  run id, status, current nodes, what a person owes (`owed`), and a cursor.
  Omit `targets` to read every session you subscribe to; with nothing
  subscribed it answers `targets is required when nothing is subscribed`.
  A target you may not read comes back under `refused` with the reason
  (`not in your subtree or mesh`), and so does one with no live run
  (`no live cflow run`).
- `runs(since=<cursor>)` — **backfill**: the same answers plus the events
  after that cursor and the cursor that follows them. Use it after a restart,
  a compaction, or when a delivery did not land.
- `subscribe` — follow a target from now on. Every tick reads its journal from
  your stored cursor and types **one compressed fyi block** into your
  terminal: a report was filed, a gate opened, an ask moved, the run ended.
- `subscriptions` — your rows, with the event kinds, the stored cursor, since
  when, and the events the next delivery will carry (backfill, read-only).
  `unsubscribe` stops one and returns `removed: false` when there was none.

### What you ever have to do

Nothing, for the delivery itself: a block arrives like any other
machine-generated message and needs no reply. Reading it is the whole job —
then decide with *your own* workflow what it means (a gate is not permission,
a finished run is not an answer, a session exit is not a failure by proof).
If the block shows a gap, `runs(since=...)` reads the missing stretch; the
journal is the durable copy.

### The block, line by line

    ---
    # claunch cflow: watch -- machine-generated, not typed by the user
    watcher: <you>
    events: <n>
    - <target> [<time>] <event> <key=value ...>
    protocol: ...
    ---

Each line starts with the target's session name, so one block carrying
several watches stays attributable. Forms you will see, and what each means:

- `<target> [<time>] reported node=... outcome=...` — a task's report was
  filed. The run moved.
- `<target> [<time>] waiting kind=choose` (or `kind=ask`) — a gate: a person
  or a peer must decide.
- `<target> gate: choose: <title>` — a v2 node whose chooser is `user`;
  `<target> gate: approval: <label>` — a v2 approval in state. v1 journals no
  gate on entry, so its gates come the same way: `gate: waiting_selection:
  <title>` or `gate: waiting_approval: <title>`. This line is a state diff,
  not an event, and repeats only when the owed set changes.
- `<target> [<time>] done status=<s>` / `aborted` / `verify_failed` /
  `delegate_failed` — the run, or a branch of it, ended badly.
- `<target> run ended: run <id> status <s>` — the one-time end line, for every
  ending, reported or not. It appears once per run id; `status` is the run's
  final word.
- `<target> session exited: it stopped without finishing its run (read `runs`
  for where the run stands)` — the session behind the run went away. This is
  the case the family exists for: treat it as *your* follow-up to open, not as
  an answer that arrived.
- `<target> is gone: the session no longer exists — its run stopped without
  reporting (<Exception>)` — the session was removed outright.
- `<target> run is gone: the slot holds no live run` — the session lives but
  its run slot emptied (a new round was started elsewhere, or the run was
  cleared).
- `<target> (more events pending; the next tick carries them)` — the batch hit
  its ceiling (40); the rest comes next tick, or now through `runs(since=...)`.

### Choosing the kinds

By default you hear reports, gates, asks, and ends — not node transitions
(`entered`/`left`), because a long run would fill your terminal with turns;
those are a pull question (`runs` returns `current`). The default set is
`reported`, `done`, `aborted`, `select_held`, `waiting`, the `ask_*` family
(`ask_routed`/`ask_timeout`/`ask_answered`/`ask_withdrawn`), `round_requested`,
`verify_failed`, `delegate_failed`, `started`, `step_report`/`step_completed`,
and the v1 names `gate_wait`, `select_presented`, `checklist_presented`,
`timer_presented`, `ask_escalated`, `ask_declined`, `ask_deferred`,
`sub_ended`, `published`.

"Gates" means the `waiting` event whose `kind` is `choose` or `ask`; pacing
waits (`pause`, `loop_limit`, a held visit) are filtered out. Name
`kinds=[...]` when subscribing to widen or narrow that set — naming `waiting`
explicitly delivers every pacing pause too. `subscribe` records the set on the
row, so widening later means subscribing to that target again.

### Cursors and loss

The cursor looks like `"<run id>:<journal position>"`. The run id fences it:
a cursor from a finished run never hides the next run in the same slot. You do
not have to store it — the daemon does, in your own `loops.json` (`watch`
rows), and moves it **only after the block was delivered**. A refused or
cancelled delivery keeps the cursor and is retried, so a missed block is not a
lost event. `subscriptions` shows both the stored cursor and what the next
delivery will carry, so a long absence is recoverable without waiting for the
next tick. The board reads watched journals every 10 seconds; one block
carries everything that moved in between, so a slow cadence costs latency,
not events.

### Who you may watch

A session in your own subtree, or one you are connected to in the mesh.
Everything else is refused with the reason (`refused` in `runs`, 403 on
`subscribe`); watching yourself is refused too — that is `flow_status`.
Limits: 16 subscriptions per watcher, 8 targets per tick, 40 events per
delivery.

### What this family is not

- Not mesh mail: nothing here is addressed or accounted as `owed`, so never
  answer a watch block *as if* the run had asked you something. If you need an
  answer from that session, send a mesh message.
- Not a replacement for `flow_status` (your own run) or `children` (your
  team's roster): it watches *another session's* run.
- Not a scheduler: it does not start, stop, or resume anything it watches.
  A run that ended stays ended; a session that exited stays exited.
