# Mesh: session-to-session messaging

Status: all four phases implemented — local mesh, federation over the relay,
the nudge-policy layer, and the agent conveniences (MCP wrapper, join
briefing).

Mesh lets claunch sessions — the agents running inside them — exchange
messages. Sessions are grouped into a *mesh* (on the web dashboard or via the
CLI), each member gets a handle, and members send each other messages that the
daemon **types into the recipient's terminal**. With a relay uplink configured,
meshes can span machines on different networks.

The concept and several policies are ported from the `interconnect` project
(file-backed agent mesh). The transport is deliberately different — see below.

## Why delivery is always send-keys

interconnect delivers by appending to a per-member file ("socket") and then
needs five layers of workarounds because a file append cannot wake an agent
that is not listening: a Monitor/`tail -F` watcher, a doorbell-coalescing proxy
process the human must remember to run, in-band heartbeat pings, an
out-of-band tmux `send-keys` escalation ("nudge"), and Stop/SessionStart hooks
for harnesses without a Monitor tool. An agent whose turn ended — watcher dead,
no `recv` parked — never reads its socket again; that failure mode is the root
of most of interconnect's complexity.

claunch owns every member's PTY, so it can make the escalation tier the *only*
tier: the daemon injects the message into the recipient's terminal (bracketed
paste + Enter). Arrival **is** the wake-up. No Monitor tool, no doorbell, no
proxy process, no hooks, no `recv`, and no nudge feature — all removed by
design, not omitted.

Consequences:

- Receivers need zero cooperation and zero setup. Any harness works.
- The agent-facing surface shrinks to *sending* (`claunch mesh send`), which
  works from any session shell because `$CLAUNCH_SESSION` identifies the
  sender.
- Delivery is idle-aware: the daemon already samples each session's screen for
  idleness, so messages are held until the recipient's turn is over, and
  bursts are coalesced into one injection (interconnect's `settle`).

## Ownership: relay-optional federation

The relay is **not** a required component of claunch sessions, so it cannot be
a required component of mesh either. The model is a federation:

> **Note (v2)**: the equal-replica reading of this section is superseded —
> a mesh now has one authoritative *primary* daemon and guest daemons hold
> mirrors (see "Federation v2"). What stays true: the relay remains optional
> for purely-local meshes, every daemon still owns delivery into its own
> PTYs, and disconnection still degrades to durable queues, never data loss.

- Every daemon fully owns its local side: its sessions, their mesh
  memberships, delivery into their PTYs, its copy of the message log, and
  policy execution (heartbeats etc.) for its own members.
- A cross-machine mesh is an *agreement* (link) between daemons, negotiated
  over the relay tunnel while it is up. The link handshake exchanges
  mesh-scoped tokens, so daemons grant each other message delivery for that
  mesh only — never their full API tokens.
- If the relay drops, the mesh partitions gracefully: local members keep
  messaging each other, remote members are shown `unreachable`, and messages
  addressed to them queue on the sender's daemon for delivery on reconnect
  (the sender is told `queued — relay disconnected` immediately).
- Relay connectivity is surfaced constantly: a status line in session-related
  CLI output, a badge in the web header, per-member reachability in mesh
  views.

## Identity

- A session's global address is `<relay-name>/<session-name>` (e.g.
  `work-pc/s0`). Session names are unique per daemon; the relay registration
  name (default: hostname) is the machine-level qualifier and the only
  cross-network identifier in the system.
- Inside a mesh, members are addressed by **handle** (unique per mesh). A
  member record maps `handle -> (machine, session)`; machine is empty for
  local members.
- Agents self-identify via `$CLAUNCH_SESSION` (injected into every session's
  children). No pane ids, no pids, no cwd-keyed markers — those were
  interconnect's machine-local identifiers and do not survive a network hop.
- Roles are inferred from the handle's leading word (`worker_1` -> worker,
  `moderator` -> leader) unless given explicitly — same convention as
  interconnect.

## What is kept from interconnect, what is dropped

Kept (as concepts or ported logic):

- mesh/channel + membership, handle/role conventions
- message shape (`id`, `ts`, `from`, `to` = `*` | handle | list, `type`,
  `body`, `reply_to?`), append-only `log.jsonl` history
- batch `sections` (`{handle: text|{text, type}}`): the log stores ONE
  composite message (plus `shared` + `sections` fields); *delivery* slices
  per recipient — shared preamble + own section only — and a section's
  `type` overrides the top-level intent for that recipient. Section keys
  must be actual recipients of the send (never silently undeliverable).
  Slicing happens at delivery time, so a federated batch crosses machines
  as one message and the remote daemon slices for its own local members
- message intents: `say`/`ask` invite a reply, `fyi`/`ack`/`ping` do not
  (`expects_reply` is derived on read, never stored — interconnect's
  contract); unknown types are accepted but draw a reply-all advisory.
  A delivery batch of only no-reply intents is marked `needs_reply: false`
  ("no reply expected"), and such deliveries never arm the heartbeat
- per-member delivery cursors into the log (durable "what was delivered")
- settle-based burst coalescing before delivery
- (phase 3) heartbeat / task-poll / stall-warning *policies*, running in the
  daemon and edited on the web — the proxy TUI's feature set without the
  proxy process

Dropped (obsoleted by send-keys delivery):

- Monitor tool / `tail -F` doorbells, the coalescing proxy process and its
  locks, Stop/SessionStart hooks, MCP `recv`/`pending`, the nudge escalation
  tier, all tmux dependence (`panes.json`, `force_ping`, paste-buffer), the
  two interconnect TUIs (replaced by the web dashboard), turn caps and
  role-based routing bans (may return later if needed).

## Storage

Machine-local runtime state, next to the other daemon state:

```
~/.claude-launcher/daemon/mesh/<mesh>/
  mesh.json      {name, created_at, members: {handle: {session, machine, role, joined_at}},
                  links: {machine: {token_in, token_out, created_at}},
                  invites: {token: minted_at}}
  log.jsonl      append-only message log (one line per send or peer ingest)
  cursors.json   {members: {handle: delivered-index}, peers: {machine: forwarded-index}}
```

Message logs are per-daemon; federation exchanges messages, not files. Both
cursor maps index into the same log, so local delivery and peer forwarding
share one durability model (the phase-1 flat cursors format is migrated on
load).

## HTTP surface (phase 1)

All under the existing auth (Bearer token / web cookie):

- `GET  /api/mesh` — meshes + member counts (+ relay status)
- `POST /api/mesh` `{name}` — create
- `GET  /api/mesh/{mesh}` — members, per-member pending/reachability
- `DELETE /api/mesh/{mesh}`
- `POST /api/mesh/{mesh}/members` `{session, handle?, role?}` — join
- `DELETE /api/mesh/{mesh}/members/{handle}` — leave
- `POST /api/mesh/{mesh}/messages` `{from, to, body}` — send (`from` is a
  handle or a session name; the CLI passes `$CLAUNCH_SESSION`)
- `GET  /api/mesh/{mesh}/messages?limit=N` — history

Plus a prerequisite: `POST /api/sessions/{name}/keys` accepts
`{"paste": "multi\nline text", "enter": true}` — bracketed-paste injection, so
multiline content does not submit once per newline.

## Federation (phase 2) — retired, replaced by v2 below

> **Status: retired.** This symmetric-replica model shipped and worked,
> but field use showed its structural flaw: no daemon is authoritative, so
> handles can race across daemons (merge resolves by skip-and-warn), member
> knowledge is non-transitive (A-B + B-C links leave A blind to C), any
> linked daemon can mint invites, same-named meshes merge by accident, and
> each daemon holds only a partial history and its own policy engine. See
> "Federation v2" for the model that replaced it; this section is kept only
> to document what was retired and why. v1 on-disk federation state (links /
> peer cursors) is dropped on load, not migrated.

Linking two daemons' copies of a mesh is invite-based:

1. `POST /api/mesh/{mesh}/invite` on daemon A mints a single-use invite code
   (base64 of `{mesh, machine, token}`); the human carries it to machine B.
2. `POST /api/mesh/link {code}` on daemon B performs the handshake **over the
   relay's backend bridge**: B opens `PEER_OPEN` to A and POSTs
   `/peer/mesh/link {mesh, machine, token, reply_token, members}`. A consumes
   the invite, stores the link `{token_in: invite, token_out: reply_token}`,
   and replies with its local member list. Both sides now hold a mesh-scoped
   token pair — never each other's daemon Bearer tokens.

Peer traffic rides three daemon endpoints deliberately outside `/api/` (so the
daemon-token middleware does not apply; each handler checks the per-link mesh
token itself):

- `POST /peer/mesh/link` — invite redemption (above)
- `POST /peer/mesh/messages` — batch message forward, deduped by message id
- `POST /peer/mesh/members` — member-list fanout on join/leave

Transport: one raw HTTP/1.1 request per bridged stream (`Connection: close` +
`Content-Length`), through `RelayUplink.peer_http` — the relay's `PEER_OPEN`
extension (see mux-relay `docs/tunnel-protocol.md` §4.1; opt-in via
`allow_backend_peering`, capability-gated by the REGISTER_OK caps bit).

Forwarding rules:

- Each message carries an `origin` machine; only the originating daemon
  forwards it to peers, so broadcasts never echo between daemons.
- The per-peer cursor (`cursors.json` `peers`) marks what a peer has
  acknowledged; a failed flush backs off (5s doubling to 60s) and retries —
  relay down simply means messages queue durably until reconnect, and the
  sender is told `queued_remote` immediately.
- Remote members surface as `remote-connected` / `remote-disconnected`
  reachability depending on the live uplink state; linked peers (with queue
  depth and last error) appear in mesh views, the CLI and the web panel.

## Federation v2: primary/mirror (phase 5 — implemented)

The goal was always "sessions on different networks woven into ONE mesh";
v2 keeps that goal but gives the mesh a single source of truth.

**Roles.**

- **Primary daemon** — the mesh's creator and owner (unchanged from local
  meshes today). Holds the authoritative state: the member registry, THE
  message log (one id + sequence per message), the policy config and its
  only running engine, invite minting, and a delivery cursor per guest.
- **Guest daemon** — linked by its first approved join (phase 6; the
  original invite/link ceremony is retired). Holds a **mirror**: a
  synced copy of the roster + log (for its UI and agents to read), the
  terminal-injection delivery for its own local member sessions, and one
  durable upstream queue toward the primary.
- **Secondary guest member** — a session on a guest daemon. Its join, leave
  and send are *requests* forwarded to the primary; the primary decides,
  sequences, and fans out.

**Topology: hub-and-spoke.** Guests talk only to the primary; guest-to-guest
traffic routes through it (transitivity solved — no pairwise links). Only
the primary mints invites. Loop prevention (origin gate) becomes
unnecessary; message ids remain for idempotent redelivery.

**Aligned decisions.**

1. *Name collision on link*: if the guest already has a local mesh with the
   invite's name, `link` fails with an error — never merge. The user renames
   or removes the local mesh first.
2. *Local DMs on a guest still go through the primary*: every message —
   including one between two members of the same guest daemon — is
   sequenced by the primary, so every daemon's history is identical (modulo
   tail lag). Primary unreachable means those conversations queue too.
3. *Policy engine runs on the primary only.* Guests piggyback member
   activity/idle state onto their cursor acks; nudges arrive as ordinary
   deliveries and the guest applies the usual idle-gate at injection time.
4. *Joins fail fast when the primary is unreachable* — membership is an
   authoritative decision (central handle uniqueness), so no queued joins.

**Flows.**

- *Link*: established by the first approved join from that machine (phase
  6) — the guest receives a roster + log snapshot and a guest credential;
  the primary records the guest machine and its cursor. The mirror records
  `primary: <machine>`.
- *Join (guest member)*: guest daemon forwards the join to the primary,
  which enforces handle uniqueness, records the member, fans the roster out
  to all guests; the guest injects the join briefing locally.
- *Send (any member)*: guest forwards to primary; primary assigns id/seq,
  appends to the log, delivers to its local recipients, and fans out to
  each guest daemon by cursor; guests append to the mirror and inject into
  their local recipients' terminals. Batch sections slice at the delivering
  daemon, exactly as today.
- *Failure*: guest→primary down — sends queue durably at the guest (sender
  told `queued`), the mirror stays readable, joins fail fast. Primary→guest
  down — fanout queues at the primary per guest cursor. Primary process
  down — the mesh freezes for guests (read + queue only): the accepted
  trade-off of a clear owner.
- *Admin*: kick and policy edits are primary-only; a guest daemon may
  request leave for its own members.

**Retired from v1**: symmetric per-link token pairs (replaced by a
primary-issued guest credential), guest-side `peer_cursors`, the origin
gate, guest-minted invites, accidental same-name merge, per-replica policy
engines. Existing v1 federation state is dropped, not migrated (the feature
was experimental).

## Membership-first joining (phase 6 — implemented)

Field use of v2 showed the remaining conceptual bug is the *establishment*
layer: the invite targets a **daemon**, but what the user means to connect
is a **session**. The `invite → link → join` three-step exposes plumbing
(daemon pairing, empty mirrors) as a first-class ceremony, requires humans
to carry opaque codes between machines that already share an authenticated
relay namespace, and shipped with no lifecycle at all (no unlink/revoke, no
invite expiry, orphan mirrors after a primary-side delete). Phase 6 replaces
the ceremony; everything underneath (hub sequencing, mirror sync, outbox,
policy engine) is untouched.

**Aligned decisions.**

1. *Join is the only first-class verb.* A mesh has one global address,
   `name@machine` (the primary's relay name): `claunch mesh join dev@pca
   --as worker_b` from any relay-connected daemon. Daemon pairing and
   mirror creation happen underneath, on the first join from that machine.
2. *Codeless joins are pended for manual approval.* The primary's operator
   sees join requests (web box + `mesh requests`/`approve`/`deny` CLI) and
   decides; the subject of approval is the requesting **machine** (its
   relay identity) plus the proposed member. A machine allowlist can come
   later as a convenience; it is not the default.
3. *Invite codes survive only as pre-approval tickets.* `mesh invite`
   mints a ticket that lets `mesh join dev@pca --code X` skip the pending
   queue (unattended/automation path). Tickets get expiry plus list/revoke.
4. *Links are persistent with explicit revocation.* Once a machine's first
   member is approved, the link lives until revoked: `mesh guests` lists
   linked machines, `mesh revoke <mesh> <machine>` removes the guest and
   its members and notifies it (best-effort; a guest that can no longer
   authenticate marks its mirror orphaned). Deleting a mesh on the primary
   notifies guests so mirrors are dropped, not orphaned. Additional members
   from an already-linked machine join without re-approval (the machine is
   trusted; only handle uniqueness applies).

**Flow (codeless).** Guest daemon has no mirror for `dev@pca` → sends a
join *request* (mesh, machine, session, handle, role, guest reply-token) →
primary queues it pending → operator approves → primary registers the guest
link, mints the credential pair, and calls back over the relay with the
grant: tokens + roster/log/policy snapshot + the member record. The guest
builds the mirror, records the member and injects the join briefing. If the
guest is unreachable at approval time the grant retries with backoff; the
requester sees "requested — waiting for approval by pca" meanwhile. With a
`--code` ticket the same request is granted synchronously — no pending.

**UI.** The mesh page's "Invite peer…" box became a **Join requests**
approval box on the primary (approve/deny per request), with ticket minting
demoted to a secondary control and a per-guest revoke. The sidebar's mesh
field takes either a new name (create) or `mesh@machine` (join, with an
optional ticket), and lists our own outstanding outbound requests. Mirrors
show no establishment controls at all.

**Shipped surface.**

- CLI: `mesh join MESH[@MACHINE] [--code X]`, `mesh requests [MESH]
  [--cancel ID]`, `mesh approve|deny MESH ID`, `mesh revoke MESH MACHINE`,
  `mesh invite MESH [--ls|--revoke PREFIX]`. `mesh link` is gone.
- API: `POST /api/mesh/{mesh|mesh@machine}/members` (201 admitted / 202
  pending), `GET|DELETE /api/mesh/{mesh}/invites[/{prefix}]`,
  `POST /api/mesh/{mesh}/requests/{id}/approve|deny`,
  `DELETE /api/mesh/{mesh}/guests/{machine}`,
  `DELETE /api/mesh/outgoing/{id}`. `POST /api/mesh/link` is gone.
- Peer: `/peer/mesh/join_request`, `/peer/mesh/grant`, `/peer/mesh/unlink`
  replace `/peer/mesh/link`.
- Durability: the guest persists outstanding outbound requests in
  `outgoing_joins.json`; the primary persists pending requests and
  undelivered grants in `mesh.json` and retries grants from the worker.
- Hardening: the grant is addressed to the *claimed* machine name over the
  relay, so a spoofed request cannot receive credentials; the primary
  refuses to send *as* a member that is not its own without `external`.

## Delivery pipeline

One asyncio worker per mesh. For each local member with undelivered log
entries addressed to it:

1. **settle** — wait for the burst to go quiet (default 2s) so several rapid
   messages become one injection;
2. **idle-gate** — wait until the member's session is idle (screen quiet per
   the daemon's idle tracker), up to `busy_hold` (default 60s); after that,
   inject anyway (harnesses like claude queue typed input during a turn);
3. **inject** — one fenced YAML block, control-characters stripped and bodies
   clipped, sent via bracketed paste + Enter:

   ```yaml
   ---
   # claunch mesh: automated delivery — machine-generated, not typed by the user
   mesh: dev
   to: worker_1
   batch:
     - from: leader
       body: |-
         ...
   note: 'reply: claunch mesh send dev <to|*> "..."'
   ---
   ```

4. advance the member's cursor. An exited session simply holds its cursor;
   messages deliver when the session is respawned (same name). Cursors are
   persisted, so a daemon restart redelivers anything not yet injected.

## CLI (phase 1)

```
claunch mesh create <mesh>
claunch mesh ls
claunch mesh join <mesh> [--as HANDLE] [--role ROLE] [--session NAME]
claunch mesh leave <mesh> [--session NAME | --as HANDLE]
claunch mesh send <mesh> <to|*> <text...>       # sender = $CLAUNCH_SESSION
claunch mesh members <mesh>
claunch mesh history <mesh> [-n N]
```

Cross-machine verbs (phase 6, "Membership-first joining" above):

```
claunch mesh join <mesh>@<machine> [--code TICKET]
claunch mesh requests [<mesh>] [--cancel ID]
claunch mesh approve|deny <mesh> <request-id>
claunch mesh invite <mesh> [--ls | --revoke PREFIX]
claunch mesh revoke <mesh> <machine>
```

`join`/`send`/`leave` default the session to `$CLAUNCH_SESSION` so an agent
can run them bare inside its own session. Mesh and session commands print a
relay status trailer (`relay: connected as work-pc` / `relay: not configured`
/ `relay: disconnected`).

## Nudge policies (phase 3)

interconnect's proxy-TUI policy set, daemon-resident and per-mesh, edited on
the web (mesh view → "Nudge policy"), via
`GET/PUT /api/mesh/{mesh}/policy`, or `claunch mesh policy <mesh>
[--set section.key=value]`. The observable member state here is the session
idle tracker plus mesh activity (last delivery into the member vs. the
member's last send) — the injection-transport analogue of interconnect's
socket/recv state:

- **heartbeat** — a member had messages delivered but has sent nothing since:
  after `interval` (per-member doubling backoff to `max_interval`) inject a
  reminder block (`kind: heartbeat`). Fires only while the session is idle —
  a busy member is presumed to be working.
- **task-poll** — a member that is idle *and* caught up (nothing pending,
  nothing unanswered), whose role is in `roles` (default `worker`): inject a
  role-targeted poke (`bodies` per role, fallback interpolates `{role}`).
- **stall warning** — a non-leader member has held one state for `warn_secs`
  (idle+caught-up, or *behind*: deliveries pending but its session never goes
  idle): send a real mesh message from the external `policy` sender to every
  leader-role member — so it is injected locally *and* crosses machines over
  federation.

interconnect's fourth tier — tmux send-keys escalation — has no port: every
delivery already is an injection, so there is nothing to escalate to. All
three policies default **off**: unlike a socket append, a nudge consumes the
recipient agent's turn, so enabling is a deliberate choice. Config persists
in `mesh.json` (`policy`); timers are in-memory and restart with the daemon.

## Agent conveniences (phase 4)

- **Join briefing**: on join the daemon injects an idle-gated briefing block
  (mesh, handle/role, member list, how to send) into the new member's
  terminal — so a session enrolled from the web learns it joined something.
- **MCP wrapper**: `claunch mesh mcp` is a stdio MCP server exposing
  `send` / `members` / `history` (the caller is `$CLAUNCH_SESSION`, like the
  CLI). Deliberately no `recv`: receiving needs no tool by design.
- **/mesh skill + installer**: `claunch mesh install` registers the MCP
  server and writes `skills/mesh/SKILL.md` (profile or project), the agent
  protocol adapted from interconnect's — everything about *receiving*
  (recv loops, watches, parking, doorbell recovery) is gone by design, so
  the skill teaches only self-identification, idempotent join, delivery
  block reading, sending discipline (intents/sections/threading), role
  stances, and compaction recovery. The join briefing prompts the agent to
  activate the skill (`/mesh <name>`).

## Phases

1. **Local mesh MVP** (done): paste mode, mesh module + API + CLI +
   web panel, local delivery, relay status surfacing.
2. **Federation** (done): invite/link handshake over the relay's backend
   bridge (mesh-scoped tokens), remote member entries (machine/session),
   forwarding sends to peer daemons, durable queue with redelivery on relay
   reconnect, per-member reachability. See "Federation (phase 2)" above.
3. **Policy layer** (done): heartbeat, task-poll, stall warnings —
   daemon-resident, per-mesh config editable on the web. See "Nudge
   policies" above.
4. **Agent polish** (done): MCP wrapper for send/members/history, the join
   briefing injection, and the `/mesh` skill + `claunch mesh install`.
5. **Federation v2** (done): primary/mirror redesign — see "Federation v2"
   above. Retires the symmetric model of phase 2; scenario-derived tests in
   `tests/test_mesh_v2.py` (TDD) plus the multi-daemon e2e.
6. **Membership-first joining** (done): `mesh join name@machine` as the
   only establishment verb — codeless requests pend for approval, invite
   codes demoted to pre-approval tickets, persistent links with explicit
   revoke and guest management. Scenario-derived tests in
   `tests/test_mesh_join.py` (TDD). See "Membership-first joining" above.
