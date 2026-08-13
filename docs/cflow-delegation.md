# Delegated decisions in cflow — implementation spec

Branch `ask-gating`. This is the contract the remaining work builds to, and
the record of what was decided against, so the settled questions stay settled.

## The idea in one paragraph

A cflow run can put a decision to a **different session**: an approval to
enter a step (`ask`), or which branch to take (`select.chooser`). The asking
agent cannot see, forge, or record the answer — the responder writes it into
the run itself. What makes that an approval rather than a formality is that
all three things deciding *who may answer* are outside the asking agent's
control: the mesh member graph (who it may reach), the spawn tree (what it
could have created), and the role a member was admitted under.

## Status

Eight commits are on the branch and the tree is green (634 passed, 1 skipped;
`uv run pytest -q --basetemp=.pytest-tmp` — the shared temp dir is
permission-denied on this machine, hence the flag).

| commit | what |
| --- | --- |
| `457898a` | schema + state machine: open/escalate/answer/decline/abstain, per-visit keying, `goto` invalidation, human fallbacks |
| `a328c5b` | `up: {min,max}`, receipt echoes the question |
| `a9ebbd5` | responder resolution against the mesh roster, one read per ask, delivery |
| `415cc15` | MCP `asks`/`answer`, fence isolation, `/cflow` skill responder protocol |
| `686000a` | the answer nudges the waiting run; shared nudge helper |
| `475c48f` | daemon `AskClock` — timeout expiry |
| `12ae367` | dashboard `waiting_answer` state + `claunch cflow asks` |
| `5f6ad80` | README / cookbook / mesh-design |

The design then changed in three ways that are **not yet implemented** (§7).
Everything below describes the target, not the current code.

## 1. Schema

```yaml
steps:
  ship:
    ask:                                   # approval to ENTER; per visit
      prompt: commit and push?             # required
      from:                                # optional; omit = ask no agent
        - {role: reviewer}
        - {role: leader, scope: ancestor}
      otherwise: human                     # human (default) | self
      timeout: 900                         # optional, seconds, per group
      on_decline: impl                     # optional step id | end
    instructions: ...
    next: end

  verdict:
    select:
      prompt: ready, or another pass?
      chooser:                             # "agent" | "user" | this mapping
        from: [{role: reviewer}]
        otherwise: human
        timeout: 1800
      options:
        ready:  {description: ..., next: ship}
        rework: {description: ..., next: impl}
```

**Two independent axes.** `from` is *who is asked*; `otherwise` is *what
happens when none of them answers*. A human is not a candidate — nothing
resolves them, nothing notifies them, and they answer through the CLI — so
`human` never appears in `from`. Writing it there would also mean the list
never runs out, making the two spellings say the same thing twice.

**Candidate** = `{role: <name>}` with optional `scope`. `role` is required: a
delegation is to a *function*, and "whoever happens to be connected" is not
one. `scope` is `any` (default — anything reachable that is not this run or
its descendant) or `ancestor` (the chain of command only).

**`from` is optional.** Omitted, no agent is asked and `otherwise` decides
immediately. With the default that is exactly a human gate, which is what
`gate:` deprecates into: `gate: <msg>` → `ask: {prompt: <msg>}`.

**The existing chooser strings are special cases of the same pair** —
`chooser: user` ≡ `{otherwise: human}`, `chooser: agent` ≡ `{otherwise: self}`,
both with no `from`. They stay as sugar.

Validation: unknown keys rejected; `prompt` required; `timeout > 0`;
`otherwise ∈ {human, self}`; `scope ∈ {any, ancestor}`; `on_decline` must name
a real step (or `end`) and counts as a graph edge for reachability, cycle and
can-finish analysis. `gate` and `ask` on one step is an error.

## 2. Resolution

Runs when a question is opened or escalated, against one reading of the mesh.
The result is **frozen into the run**; authorization afterwards is a
membership test against that record, never a re-resolution.

```
candidates(entry) =
      mesh members this session may reach      # connected(me, them)
    ∩ not me, not my descendant                # spawn tree
    ∩ ancestors only, if scope: ancestor       # spawn tree
    ∩ member role == entry.role
    ∩ session is local to this daemon and alive
```

Groups are tried in order; every one that matches nobody is recorded in
`skipped` with a reason a human can act on, and the next is tried. When the
list is exhausted, `otherwise` applies.

**Why "not my descendant" and not "ancestors only".** The boundary is what
the asking agent could have *manufactured*. It can spawn descendants, and it
can wire itself to them (`SessionManager.commands` runs strictly down the
tree). It cannot spawn a sibling, and it cannot wire itself to one — for edge
`(me, sib)` neither handle is commanded by it, so
`MeshManager._require_member_authority` refuses. Siblings, uncles, roots and
ancestors are therefore all equally safe, and a sibling reviewer spawned by
the same parent is the *common* shape for this feature. Only descendants must
go.

**Why the member graph is trustworthy.** A run cannot widen its own candidate
set: `commands(actor, target)` requires `actor != target` and `actor ∈
ancestors(target)`, so `connect me <anyone>` is refused. Only a session above
it, or a human, can add an edge.

**Reach in practice.** A spawned member is `wired`, so its join records the
parent edge and nothing else — by default the only reachable candidate is the
direct parent. A sibling or a grandparent becomes reachable when the session
above them wires it (`claunch mesh connect a b`, the `connect` MCP tool, or an
`auto_link` rule). That is a deliberate, operator-controlled act, and the skip
reason names it.

**Failure reasons must be specific**, because they are what a human reads when
a question lands on them: no mesh membership / no member holds that role among
those reachable / matched but not reachable (name the `connect` command) /
matched but the session has exited / this session is in several meshes and the
run does not say which.

## 3. Runtime

State lives in `state["ask"]`, keyed by `(step, visit)` exactly like a step
report, so an answer to a previous loop pass cannot land on this one.

```
{id, step, visit, kind: approval|branch, prompt, options,
 group, asked: [{kind: member, session, handle, role}], skipped: [...],
 opened_at, deadline, undelivered?}
```

- **Answering** (`answer {ask, decision, reason}`) is refused unless the
  answering session appears in `asked`. The identity comes from
  `$CLAUNCH_SESSION` in the responder's MCP process, never from arguments.
  Answering one's own run is refused separately — one comparison, and the
  invariant the feature rests on.
- **Decisions are closed**: the declared options, plus `abstain`. Nothing is
  parsed out of prose.
- **`abstain`** and **timeout expiry** both escalate to the next group. So
  does a group resolving to nobody. One path, three triggers — which is why
  the preference list is one declaration rather than separate "fallback" and
  "escalation" settings.
- **Decline** routes to `on_decline`, or parks the run for a human.
- **Exhaustion** applies `otherwise`:
  - `human` — the run holds; `claunch cflow approve` / `select <option>` (and
    the dashboard buttons) settle it, journaled with whether the human was in
    the asked group.
  - `self` — the run carries on alone. For an `ask` the step is entered
    unapproved; for a `select` the driving agent chooses (the payload becomes
    an ordinary `chooser: agent` decision). Journaled as
    `ask_unanswered_proceeded`, **never** as an approval.
- The responder never receives the asking step's instructions. Its receipt
  carries the question, the step title, its own decision and reason.
- A human moving the run (`goto`) discards an open ask, journaled.

Timeout expiry is owned by the daemon (`daemon/cflow_clock.py`), because the
one agent that would notice is the one stopped waiting. Without a daemon a
timeout does not fire and the question keeps waiting — a timeout lapses into
"no timeout", never into "proceed".

## 4. Delivery

The question is a **doorbell**, deliberately not load-bearing: it is recorded
and answerable (`asks`) whether or not it was announced, and a send failure is
journaled and shown rather than taking the ask down.

It goes over the mesh as an ordinary peer send — all session-to-session
communication does, and adding a second channel (a direct
`/api/sessions/{name}/deliver`) would promote an ACL bypass into a feature.

**A new mesh intent type, `decide`**, is added for it:

- joins `REPLY_OPTIONAL_TYPES` — the mesh's owed ledger must not claim it.
  `Mesh.owed` closes a debt on *any* message from the member, so an unrelated
  reply would read as "handled" on the dashboard's Unanswered box.
- joins `INTENT_TYPES` so it draws no unknown-type advisory.
- meaning, stated at the mesh level rather than as a cflow hook: *a decision
  is required of you, and it is recorded somewhere other than this thread.*
- carries an opaque `ref` (`{kind: cflow.ask, id, step}`) the mesh does not
  interpret, so a reader can link to the run without the mesh learning cflow's
  schema. Nothing further goes in fields — the responder gets the full context
  from `asks`.

It federates: an older daemon reads an unknown type as reply-expected and puts
it in its owed ledger. Mildly noisy, not broken.

The **answer** does not travel over the mesh. It is a local state transition
under the run's lock. Routing it as a message would make the decision a
parseable reply again, which is the thing this design exists to avoid.

## 5. Start-time check

`start` and `request_start` both report what the workflow's delegated steps
resolve to *right now*. It never blocks — a leader that has not spawned yet is
legitimate, and the step may be an hour away.

```
delegation_check:
  verdict: {from: "reviewer", resolves: ["sib1"]}
  ship:    {from: "leader (ancestor)", resolves: [],
            reason: "no reachable member holds 'leader'",
            otherwise: human}
note: 2 delegated steps; 1 has no agent responder and will reach a human
```

`request_start` matters most: the person asking for the run is right there and
can fix the wiring before it starts.

## 6. Human channels

Unchanged and deliberately outside the agent's reach: `claunch cflow approve`
(also: override a decline, or take a stuck question away from an agent) and
`claunch cflow select <option>`. `claunch cflow asks [--session S]` reads what
is waiting on someone and cannot answer it — a human settles these through the
approve/select doors, so an override is recorded as one.

There is no MCP approve tool. `asks`/`answer` act on *other* sessions' runs and
refuse both a request not put to this session and one from its own run, so no
arrangement of tool calls unblocks a step gated on the agent making them.

## 7. Remaining work

Everything below is a change to code already on the branch.

**A. `cflow/model.py`** — drop `HUMAN`, `MAX_UP`, `_parse_up`, `_hops`, and
`Candidate.up`/`.human`. `Candidate` becomes `{role: str, scope: str}` with
`role` required. `Delegate` gains `otherwise` and allows an empty
`candidates`. `_parse_delegate` accepts `from` optional. Deprecation text for
`gate:` becomes `ask: {prompt: ...}`.

**B. `cflow/responders.py`** — replace `Ancestry`/`ancestry()` (a lineage walk
with hop ranges) with a candidate-pool read: from `GET /api/mesh`, take the
asking session's handle, its neighbours (`member_links`), each member's
`parent` for the descendant/ancestor tests, `role`, `local`, and session
liveness. `deliver()` sends `type: decide` with `ref`. Keep `nudge()`.

**C. `daemon/mesh.py`** — add `decide` to `REPLY_OPTIONAL_TYPES` and
`INTENT_TYPES`; let a send carry `ref` through to the log and the delivery
block. `daemon/api.py` `h_mesh_send` passes it.

**D. `cflow/engine.py`** — `_open_ask` uses the new pool; exhaustion applies
`otherwise` instead of always holding; add the `self` path for both kinds
(journal `ask_unanswered_proceeded`); `_awaits_human` becomes "exhausted and
`otherwise: human`". Add the start-time check to `start`/`request_start`.

**E. `cflow/mcp.py`** — `start` keeps `mesh`; surface `delegation_check` in the
`start` payload.

**F. Tests** — `tests/test_cflow.py`: replace the `up`-range cases with
`scope`/`otherwise` cases; `_mesh` helper becomes a member-pool stub;
add sibling-reachable, descendant-excluded, not-connected, and
`otherwise: self` cases. Keep every existing safety test.

**G. Docs** — README control-point table, `docs4users/cflow-cookbook.md`
recipes 4–5, `docs/mesh-design.md` §"Delegated decisions ride the lineage",
and this file.

**H. End-to-end** — `tests/test_daemon_e2e.py` (or a sibling): a real daemon,
a parent session and a child, both in a mesh; the child starts a workflow with
a delegated step; assert the question is delivered as `decide`, that the
parent's `answer` is refused from the wrong session and accepted from the
right one, that the child's run advances, and that the child cannot answer its
own ask.

## 8. Decided against

- **Resolving on the session tree instead of the mesh.** The session tree does
  carry `parent` and `role` (`SessionDef`), and `/api/sessions/{name}/deliver`
  would reach anybody — but session-to-session communication goes through the
  mesh by design, and a second path would make the bypass a feature.
- **Hop counts (`up: 1`, `up: {min,max}`).** Replaced by the member graph:
  topology decides reach, role decides which, and the default wiring already
  yields "the direct parent". "Prefer the parent, else the grandparent" comes
  from what is wired rather than from a declared range.
- **External/system sends** to reach past the member graph. Needed only while
  reach was hop-counted; the graph is the authorization layer and is honored.
- **`self` as a candidate in `from`.** It would require deleting the
  "cannot answer your own run" check, which is one code path shared by every
  workflow. It lives on the `otherwise` axis instead, where it decides a
  terminal behaviour rather than posing as an answerer.
- **Requiring a mesh to spawn.** `onboard.inherit_mesh` already puts a child in
  its parent's mesh, opening one when the parent is in none. And a spawn-time
  check cannot establish the invariant anyway: `mesh leave`, `disconnect` and
  spawning into another mesh all break it afterwards. The check belongs where
  the question is asked.
- **Quorum (n-of-m).** Partial answers and tie-breaks multiply the failure
  modes; a group asks everyone who matches and the first valid answer wins.
- **Remote responders.** Answering writes the asking run's files, which live on
  the asking machine. A remote match is skipped *as remote* — a different
  problem, with a different fix, from "nobody holds that role".
- **Failing open.** No configuration turns "nobody answered" into "approved".
  `otherwise: self` is the one escape, it is explicit, and it is journaled as
  unanswered rather than approved.
