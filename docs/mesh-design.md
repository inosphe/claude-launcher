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

## Federation (phase 2)

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
claunch mesh invite <mesh>                      # mint a code for a peer daemon
claunch mesh link <code>                        # redeem it on the other machine
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
