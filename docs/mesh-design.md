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
- Roles are inferred from the handle's leading word (`worker_1`/`coder2` ->
  worker, `moderator`/`mod` -> leader) unless given explicitly — same
  convention as interconnect, aliases included. See **Roles** below.

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
- Peer: `/peer/mesh/deliver` (fast path), `/peer/mesh/link` (a peer asks
  the authority to cut or restore an edge it terminates) and
  `/peer/mesh/roles` (a peer asks it to change the role set).
  `/peer/mesh/sync` additionally carries `peers`, `authority_epoch`, the
  brokered `links` for that peer and the whole `edges` table, so every daemon
  draws the same graph — plus `roles` when, and only when, that peer's
  `roles_seen` is behind the mesh's `roles_version`.
- Roles: `GET/PUT /api/mesh/{mesh}/roles` (see **Roles**). The mesh view
  carries only a summary (`version`, `custom`, `default`, role `names`,
  `is_authority`); the stance prose lives on the dedicated endpoint so the
  dashboard's 2s poll never carries it.

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
claunch mesh roles <mesh> [--yaml | --file ROLES.YAML | --reset]
claunch mesh stance <mesh> [--as HANDLE]
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
  role-targeted poke. The text is the first of `bodies[role]` (this mesh's
  own override), the **role set's** `task_poll`, then a `{role}`-interpolated
  fallback — so a mesh that defines its own roles gets matching wording
  without a second edit here.
- **stall warning** — a member has held one state for `warn_secs`
  (idle+caught-up, or *behind*: deliveries pending but its session never goes
  idle): send a real mesh message from the external `policy` sender to every
  member whose role is marked `stall_watch` in the role set (the leader, by
  default) — so it is injected locally *and* crosses machines over
  federation.

interconnect's fourth tier — tmux send-keys escalation — has no port: every
delivery already is an injection, so there is nothing to escalate to. All
three policies default **off**: unlike a socket append, a nudge consumes the
recipient agent's turn, so enabling is a deliberate choice. Config persists
in `mesh.json` (`policy`); timers are in-memory and restart with the daemon.

## Roles

A role is what a member **is** on the mesh: its stance, and the handful of
behaviours the daemon keys off it. The vocabulary is a **document**, not a
table in the source.

**The packaged set.** `leader` / `operator` / `worker` / `reviewer` /
`specialist`, with interconnect's aliases (`coder`, `dev`, `engineer`, … ->
worker; `mod`, `lead`, `chair` -> leader; `qa`, `critic`, `peer` -> reviewer)
and `reviewer` as the default, so an unlabelled member audits rather than
rubber-stamps. It ships as YAML in `mesh_roles.DEFAULT_YAML` — parsed by the
same code every upload goes through, so the default is proven by the parser
it depends on. Aliases matter more than they look: before this, a fleet named
with interconnect's usual handles (`coder1`…`coder5`) resolved every member
to `member`, a label no policy targeted.

**Per-mesh override.** A mesh may upload its own YAML
(`PUT /api/mesh/<m>/roles`, `claunch mesh roles <m> --file r.yaml`, or the
web panel). Scope is the mesh, not the machine: a mesh is the unit a team
actually is, one machine hosts members of several meshes at once, and keying
it to the mesh means every daemon resolves handles through the *same*
vocabulary by construction. That is also why a role's stance is **inline
text** rather than a file path — the document federates, and a path means
nothing on the machine it lands on. Caps (8 KB of stance per role, 64 KB per
document) keep it small enough to cross a link without thought.

```yaml
version: 1
default: reviewer
roles:
  leader:
    aliases: [lead, moderator, mod, chair]
    stall_watch: true          # hears about stalled members
    stance: |                  # handed to the member on join
      You hold this mesh's direction and own its decisions. ...
  worker:
    aliases: [coder, dev, engineer]
    task_poll: >-              # this role's task-poll wording
      you are idle and caught up ...
    stance: |
      You are a producer: ...
```

**Overriding is per role, not per field.** A role named in the upload
replaces that role's whole definition; roles left unmentioned keep the
packaged one; `<name>: null` deletes one; `replace: true` makes the upload
the entire vocabulary. Merging *within* a role was rejected deliberately — a
half-overridden role (new aliases, old prose) reads as a bug, and there is no
sane way to merge two pieces of prose. A name or alias may only ever resolve
to one role; an upload that breaks that is refused whole.

**The authority owns it**, as it owns `policy` — one mesh, one set of role
names, or two daemons would read the same handle differently. A mirror's
upload is *forwarded* (`/peer/mesh/roles`) rather than refused, so the
dashboard a user happens to have open need not be the authority's. It rides
the ordinary sync but is **version-gated**: `roles_version` vs a per-peer
`roles_seen`, so the stance prose travels on a real change and never on the
back of a message flush. A joining guest gets it with its grant.

**Uploads are not retroactive.** A member's role is resolved once, at join,
and stored as a plain string; changing the vocabulary never rewrites it.
A member holding a role the new set dropped keeps it and simply matches no
rule — no migration code, and the CLI/web surface it as an *orphan* so the
state is visible rather than mysterious.

**Stance is delivered by pointer.** The join briefing names the role and tells
the agent to run `claunch mesh stance <mesh>`; it never pastes the prose.
Inlining was tried and is wrong twice over — it doubles a block that is typed
into a live terminal, and it freezes the stance into the agent's context at
join time, so every later upload would leave that member acting on a
vocabulary the mesh no longer has. The same command is the recovery path
after a compaction drops the briefing.

**What a role actually drives** today: the stance pointer in the join
briefing, `stall_watch` (who hears about a stuck member), and `task_poll`
wording. Role-based **routing bans** (worker↔worker, the operator pipe) are
still not implemented — the schema leaves room, but turning them on would make
a `send` that works today start failing on an upload alone, so that stays a
separate, explicit decision.

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

## Agent-spawned sessions (phase 8 — implemented)

Until now every session had a human behind it. An agent could talk to peers
an operator had assembled, but it could not assemble any — so a mesh's shape
was fixed before the work started, by whoever guessed at it. Phase 8 lets a
session **create further sessions**, enrol them, and set who they may talk
to. Three separate mechanisms, deliberately not one:

**1. The session tree.** `SessionDef.parent` names the session that spawned
this one. That is the whole model — the tree is derived from those edges on
every read, never cached, because there are tens of sessions and a cached
tree needs invalidating on create, kill, clear *and* restore, which is four
chances for the list and the tree to disagree about who exists.

The parent is a **name**, not a resolved reference: a parent that exits
leaves its children running, and a name is the only reference that survives
that. A child whose parent no longer resolves is a root. Every walk is
cycle-guarded — a cycle is unreachable through `spawn` (the parent must
already exist, so edges point at older sessions) but `parent` is a plain
field on a definition the API accepts and `sessions.json` persists, so a
hand-edited file can still produce one, and a guard turns a daemon hang into
a wrong-but-finite answer.

Authority runs **down** the tree only: a session commands its descendants,
never its parent and never its siblings. Two workers spawned by the same
lead are peers, and a peer that could kill its peer turns a coordination bug
into a lost session.

Children are never restored on daemon restart. A restored parent would
replay its whole subtree, and those children would come back with no agent
left to brief them — the arrangement is a runtime one, rebuilt by the
parent, not by the daemon.

**2. The spawn policy** (`spawn:` in `~/.claunch.yaml`, see
`claude_launcher/spawn.py`). A child **inherits** its parent's harness,
profile, cwd, args and env; the agent supplies only what makes it a
different worker — name, handle, role, opening task. Each inherited field
has its own unlock (`allow_profile`, `allow_cwd`, `allow_args`,
`allow_env`, `allow_harness`), plus `max_children` and `max_depth`.

> This is a **surface, not a sandbox.** An agent holds the daemon's API
> token — it reads the same token file the CLI does — so nothing here stops
> it calling `POST /api/sessions` directly. What the policy buys is what
> cflow's deliberately-absent `approve` tool buys: the *offered* action is
> the safe one, so an agent following its tools cannot wander into spawning
> under another profile, in another directory, with flags nobody chose.
> The numbers are blast-radius limits on honest mistakes — runaway
> recursion, a fan-out loop — not a boundary against a hostile session.

A refusal is **403, not 400**: the request was well formed and was refused,
and that distinction is what tells an agent to stop retrying and ask a
human. A malformed `spawn:` block reads as the defaults rather than raising
— it is consulted on a path an agent triggers, and a YAML typo that failed
every spawn would be diagnosed as a broken feature.

**3. The member graph.** `Mesh.member_edges`, `"h1|h2"` (sorted) ->
enabled, missing = connected. Same convention as the peer graph's `edges`
one layer down, so a mesh that never touches it stays the complete graph it
has always been and nothing migrates.

The two graphs are **not** the same kind of thing, and the difference is the
whole design:

| | peer graph (`edges`) | member graph (`member_edges`) |
|---|---|---|
| endpoints | daemons | members (agents) |
| a cut means | lose the fast path | cannot speak at all |
| fallback | the authority's fanout | none — the send is refused |
| who may cut | either end, or the authority | the authority, gated by the session tree |

Members are not routed, so a cut here has nowhere to fall back to. And
either-end-may-cut is wrong at this layer: the endpoints of a peer edge are
daemons with their own operators, mutually consenting adults; the endpoints
here are agents, and a member that could cut its own edges could quit the
supervision it was spawned under. So an agent may only edit an edge
touching a session it commands — a lead wires its own workers together, a
worker wires nothing. A human (no `actor`) owns the whole graph.

**Enforced in one place.** `_resolve_recipients()` is the single call every
send funnels through — local, MCP, relayed from a guest, sliced out of a
batch — so that is where the graph is applied. `'*'` narrows silently to the
sender's neighbours (a broadcast has always meant "everyone I can reach"),
while a handle named *explicitly* and unreachable is an error: the agent
asked for that peer by name and must not be told it was delivered.

`Mesh.addressed_to()` applies it too, and has to. The log stores the
*address* (`"*"`, or a handle list), not the recipients it resolved to, and
delivery, `pending` and `owed` all re-derive membership from that address —
so checking only on the way in would leave a broadcast reaching everyone on
the way out. An ACL with a second entrance is not an ACL. Re-derivation
makes this **current rather than historical**: a message already accepted
stops being delivered if the edge is cut before it lands, which is the same
direction the fast path takes and the safe one.

Two consequences worth stating:

- **Isolation is a snapshot, not a standing rule.** A spawned child is cut
  from everyone except its parent *at join time*; members who join later are
  connected by default. A rule that kept isolating a child against all
  future joins would be a second kind of state — the graph, plus a policy
  about the graph — and the parent is the thing that knows when a newcomer
  should reach its child.
- **Leaving prunes the edges that named you.** Handles are reusable, so a
  rejoining handle would otherwise inherit the isolation imposed on whoever
  wore it last: a member that mysteriously cannot reach anyone, with nothing
  in the roster to explain it.

**Surface.** `POST /api/sessions/{name}/children` does the whole thing in
one call — session, mesh join, isolation, optional cflow run scoped to the
child's own session, optional opening task typed in once it settles. One
call because the steps are not independently useful: a child spawned but not
enrolled is a terminal nobody is listening to, and one enrolled but not
briefed is an agent that does not know why it exists. Each optional step
reports separately, so a partial success is legible. `GET` on the same path
answers "what may I spawn" before a refusal has to be provoked.

`PATCH /api/mesh/{mesh}/members/{a}/links/{b}` rewires a pair (`actor` names
the session asking; omitted = a human). MCP gains `spawn`, `children`,
`connect`, `disconnect`; `members` now also answers `reachable` — who *you*
can message — because an agent left to derive its own reachability from an
edge list will eventually address a peer it cannot reach and read the
refusal as a bug. CLI: `claunch spawn`, `claunch mesh connect|disconnect`,
disconnected pairs in `mesh members`, and `claunch sessions` indented by
lineage. The join briefing lists only reachable peers, and says how many
members it is *not* showing.

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
8. **Agent-spawned sessions** (done): a session creates further sessions,
   enrols them and decides who they may talk to — the session tree, the
   `spawn` policy, and the member graph. Scenario-derived tests in
   `tests/test_spawn.py`, `tests/test_member_graph.py` and
   `tests/test_spawn_api.py` (TDD). See "Agent-spawned sessions" below.
