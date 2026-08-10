# Mesh: session-to-session messaging

Status: all seven phases implemented — local mesh, federation over the relay,
the nudge-policy layer, the agent conveniences (MCP wrapper, join briefing),
the primary/mirror redesign, membership-first joining, and the ranked peer
graph that replaced the star.

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

> **Note (phase 7)**: the *topology* of this section is superseded — the
> hub-and-spoke below is now the special case of a ranked peer graph where
> only the authority has links (see "Ranked peer graph"). What stays true:
> the mesh still has exactly one authority at a time, it still sequences THE
> log, and guests still hold mirrors with durable outboxes. "Primary" now
> means `peers[0]`, a position rather than an identity.

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

**Owner-initiated invitations (the wizard path).** The flows above are all
guest-initiated; the complement is the owner pulling a remote session in
with nothing carried by hand. `claunch mesh add <mesh>` lists the other
backends on the relay (a `PEER_LIST` tunnel message, capability bit
`CAP_PEER_LIST` — relays that predate it simply fail the listing), browses
the chosen daemon's live sessions (`/peer/sessions`), and POSTs
`/api/mesh/{mesh}/invitations {machine, session, handle?, role?}`. The
primary then pushes `/peer/mesh/invite` to the target daemon with an
embedded one-shot ticket (skipped entirely when the machine is already a
trusted guest); the target validates the session locally and performs the
ordinary join-by-address back at the inviter, so every existing validation
and the whole grant path are reused. Failure hygiene: an unreachable target
or a rejected invitation burns the unredeemed ticket. Trust model: one
relay = one operator (single backend token), so the target daemon accepts
without a local confirmation gate — both `/peer/sessions` (names only) and
`/peer/mesh/invite` lean on that assumption deliberately. On the web the
sidebar's mesh field recognises a pasted invite code (base64url JSON),
decodes the address out of it and submits the join directly, and the mesh
page carries the wizard itself: "Invite a remote session…" lists the relay's
daemons, then that daemon's live sessions (minus the ones already enrolled),
and POSTs the invitation — so the owner-side path needs no code, no CLI and
no second machine. Ticket minting stays beside it for unattended joins.

## Ranked peer graph (phase 7 — implemented)

Phases 5–6 gave the mesh a clear owner, but they also hard-wired a **star**:
`Mesh.primary` was a two-valued fact (own it, or mirror somebody else's), so
the only relationship a daemon could have was *toward the hub*. Two guests on
the same relay, whose members talk to each other all day, had no connection at
all — every message hairpinned through the primary, and a primary outage froze
conversations that never needed it.

Phase 7 keeps the single source of truth and removes the star. The mesh is a
**graph of individually configured, duplex peer links**, and authority is a
*position in an ordered list* rather than an identity.

**Aligned decisions.**

1. *Rank replaces ownership.* A mesh holds `peers: [machine, …]` — ordered,
   and the order **is** the rank. `peers[0]` is the mesh's authority; nothing
   has to be declared per link. The list is editable afterwards, so authority
   can be moved without recreating the mesh.
2. *Rank decides both layers.* Globally, `peers[0]` sequences messages, owns
   the roster and handle uniqueness, mints invites, approves joins and runs
   the policy engine. Per edge, the lower-ranked side is the **link owner**:
   it drives the handshake, issues the credential and re-establishes a broken
   link. Traffic itself is always **duplex** — every link carries both
   directions with its own queue and cursor.
3. *Complete graph by default.* A peer joining links with every existing peer,
   not just the authority. An operator may **cut** an individual edge
   (`links[m].enabled = false`); there is no multi-hop routing, so a cut edge
   simply loses its fast path and falls back to the authority's fanout.
4. *Authority moves only when a human says so.* No automatic failover, hence
   no split-brain and no merge rules. Reordering is performed by the current
   `peers[0]`; a `force` reorder exists for a permanently dead authority and
   bumps `authority_epoch` so late traffic from the old one is re-sequenced
   rather than silently interleaved.

**Two streams.** Decision 3 is only worth anything if an outage of `peers[0]`
does not stop conversations, so *delivery* and *log placement* are separated:

- **Sequencing stream** — a non-authority daemon forwards its members' sends
  to `peers[0]`, which stamps `(epoch, seq)`, appends to THE log and fans the
  tail out to every peer by cursor. Unchanged from v2.
- **Fast path** — when that forward fails at the transport level, the send
  queues in the outbox *and* is pushed straight to the daemons hosting its
  recipients (`/peer/mesh/deliver`). They inject it into the terminals
  immediately and park it in a **provisional** list; when the sequenced copy
  eventually arrives it is folded into the log at its authoritative position
  instead of being re-injected (per-member `delivered_ids` guards that).

The cost is explicit: with the fast path in play, "the order I saw" can differ
from "the order the log records". The log stays the record; a provisional
entry is shown with `seq: null` until it is sequenced. The fast path only
arms when the authority is actually unreachable, so the healthy case still
carries each message exactly once.

**Who holds the edge credentials.** A pair of peers that have never spoken
cannot authenticate a first contact between themselves, so the **authority
brokers every edge**: it already has an authenticated channel to both ends,
so it mints both halves of each peer-to-peer pair and ships each side its own
view (`token_out` to present, `token_in` to expect) on the ordinary sync.
There is no separate handshake to secure, and the two ends cannot disagree
about an edge because one daemon wrote both halves.

**Who may cut an edge.** The authority owns the edge table, but it is not the
only daemon with standing on an edge: an edge is duplex and its two ends are
equals, so **either end may cut or restore it**, and a peer's edit is
forwarded to the authority (`/peer/mesh/link`) which records it and fans the
result back out. An edge between two *other* daemons is not yours — that is
the authority's call, and the rule is enforced twice, once in the client's
own daemon and again at the authority, so calling the peer endpoint directly
buys nothing. Edges incident on the authority are never cuttable by anyone:
they carry the sequenced log, so removing one is a revoke, not a cut.

**The roster is absolute.** Before phase 7 the authority's own members
carried a blank `machine`, which was unambiguous only because authority never
moved. It moves now, so every member names its own daemon: left blank, a
member would be claimed by whoever holds rank 0 next, and delivery would
follow it to a daemon that does not host the session. Migration stamps
existing members on load.

**State.** `primary` / `link` / `guests` collapse into:

```
mesh.json   self: machine                    # who we were when this was written
            peers: [machine, …]              # order = rank; peers[0] = authority
            links: {machine: {token_in, token_out, created_at, enabled}}
            pair_links: {"m1|m2": {token_m1, token_m2, created_at}}
            edges: {"m1|m2": enabled}        # authority-owned cut state
            authority_epoch: N
cursors.json  links: {machine: index}        # was "guests"
```

`token_in` is what that peer presents to us, `token_out` what we present to
it — the same shape the v2 guest/primary credentials already had, which is
why the two token checks merge into one `_check_link_token`. v2 state
migrates automatically: the old primary becomes `peers[0]`, guests follow in
link order, and the token pairs move into `links` unchanged. Guests that were
never linked to each other are given brokered credentials on the next sync.
`self` is recorded because the relay resolves our name *after* `load_all`,
and rank — and therefore "am I the authority?" — cannot be read without it.

**A mesh is keyed by its own name, and deletion is durable.** Deleting a mesh
retires its directory to `<name>.deleted-<stamp>` rather than erasing the
history. `load_all` therefore has to skip those directories — it once mounted
every directory containing a `mesh.json`, **keyed by the directory name**
while the loaded object kept its own. A deleted mesh then came back on the
next daemon restart as a zombie: listed in the sidebar under its real name,
but absent from the lookup table, so opening it — or deleting it again —
answered `no mesh named 'mesh0'`. The invariant is that the key *is*
`mesh.name`, which makes "listed" and "openable" the same set by
construction; a second directory claiming a live name is ignored with a
warning rather than shadowing it.

**Shipped surface.**

- CLI: `mesh peers [<mesh>]` — with a mesh, its daemons in rank order with
  link state; without, the relay listing it always did. `mesh rank <mesh>
  <machine> <position> [--force]` moves one peer (position 0 = handover);
  `mesh cut|uncut <mesh> <a> <b>` toggles one edge.
- API: `PUT /api/mesh/{mesh}/peers {order, force}`,
  `PATCH /api/mesh/{mesh}/links/{a}/{b} {enabled}`. `mesh_info` gains
  `authority`, `self`, `epoch`, a `peers` list carrying each daemon's rank,
  role, reachability, queue depth and hosted members, and a `links` **edge
  table** (`{a, b, enabled, cuttable, editable}`) — per-edge state cannot be
  inferred from per-node state, and the diagram needs it. `cuttable` is a
  property of the edge, `editable` of *this daemon's* view of it, so the
  "who may cut" rule is answered once on the server rather than re-derived
  in every client.
- Peer: `/peer/mesh/deliver` (fast path) and `/peer/mesh/link` (a peer asks
  the authority to cut or restore an edge it terminates). `/peer/mesh/sync`
  additionally carries `peers`, `authority_epoch`, the brokered `links` for
  that peer and the whole `edges` table, so every daemon draws the same graph.

**Handover, concretely.** The outgoing authority is the only daemon that
knows the new order and the only one still holding every peer's credentials,
but the moment it rewrites `peers` it is no longer allowed to use the
authority push path. So it broadcasts once *as* the outgoing authority
(`_flush_guest(..., force=True)`) carrying the new order; each peer adopts
it, and the one that finds itself at rank 0 takes over the authority's
duties — brokering edges, raising the sequence floor above everything
already written, and opening a fanout cursor per peer (the existing resync
handshake pulls those to the truth). A peer that was unreachable during the
broadcast still believes the old order; the new authority's own syncs
correct it, because the credential pair it presents was brokered by the
same authority either way.
- Web: the mesh page leads with an SVG **topology diagram** — see below.

## Topology diagram (web)

The mesh page gains a diagram above the existing boxes (which stay: they own
the text-level detail and the enrolment forms). It is hand-rolled inline SVG
in `app.js`, consistent with the rest of the dashboard — no build step, no
vendored library, no external fetch.

- **Layout**: a rank ring. `peers[0]` sits at 12 o'clock wearing the authority
  mark, the rest follow clockwise by rank, so position reads as precedence
  and every edge of the complete graph stays visible. It is drawn 1:1 at a
  radius derived from the peer count — the smallest that still separates
  neighbouring *labels* — so the panel stays small and does not stretch.
- **Nodes** are peer daemons: rank badge and member count inside the disc,
  machine name below it (names are longer than any disc worth drawing).
- **Edges** are links, styled by state: solid = ok, dashed = queued, red =
  unreachable, grey = cut. A transparent fat line under each hairline does
  the hit-testing, or they would be unclickable.
- **Editing**: drag a node onto another rank slot to reorder (authority
  handover is confirmed explicitly), click an edge to cut or restore it.
  Peers are revoked from the *Peer daemons* box below, since removing a
  daemon is not an edge operation.
- **Links list**: the same edges as text under the diagram, each with its
  state and a Cut/Restore button. Aiming at a hairline is a poor way to run
  a network — and an edge passing behind a node is barely clickable at all —
  so the list, not the diagram, is the surface that is always usable. Rows
  the viewing daemon may not edit say *which* rule blocked them and where
  the edit can be made instead.

Both editing surfaces are driven by the edge table's `editable` flag, so a
peer's dashboard can cut its own links without the operator having to go find
the authority's browser.

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
claunch mesh add <mesh> [<machine> [<session>]] [--as HANDLE]   # owner wizard
claunch mesh peers
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
7. **Ranked peer graph** (done): the star becomes a graph of individually
   configured duplex links; authority is `peers[0]` of an editable ordered
   list; a fast path keeps peers talking while the authority is down; the
   web page leads with an editable SVG topology diagram. Scenario-derived
   tests in `tests/test_mesh_graph.py` (TDD). See "Ranked peer graph" and
   "Topology diagram" above.
