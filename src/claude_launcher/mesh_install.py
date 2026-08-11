"""Install mesh into a profile or a project: MCP registration + /mesh skill.

Mirrors :mod:`claude_launcher.cflow.install`:

- MCP: registers ``mesh`` as a stdio server running ``claunch mesh mcp``
  (profile user-scope ``.claude.json`` ``mcpServers``, or a project
  ``.mcp.json``).
- Skill: writes ``skills/mesh/SKILL.md`` so ``/mesh <mesh> [handle]`` primes
  the agent with the member protocol — the join briefing the daemon types
  into a new member's terminal points at this skill.

The skill is deliberately much smaller than interconnect's: everything about
receiving (recv loops, socket watches, doorbell recovery, parking) has no
claunch counterpart — delivery is a terminal injection, so only the *sending*
discipline and the compaction-recovery procedure need teaching.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

from . import settings
from .profile import Profile


def mcp_server_def() -> dict:
    """The stdio server entry for the mesh MCP bridge (cmd-wrapped on
    Windows, where ``claunch`` is a batch shim Claude Code cannot exec)."""
    if sys.platform == "win32":
        return {"command": "cmd", "args": ["/c", "claunch", "mesh", "mcp"]}
    return {"command": "claunch", "args": ["mesh", "mcp"]}


SKILL_MD = """\
---
name: mesh
description: >-
  Join a claunch mesh and message other agent sessions; incoming messages are
  typed into your terminal by the daemon. Also how to SPAWN further agent
  sessions, enrol them in the mesh and set who may talk to whom. Usage:
  /mesh MESH [HANDLE] — join (or resume membership in) MESH as HANDLE (its
  leading word picks your role; omit to use your session name). Also use when
  a mesh delivery or briefing block tells you to activate the mesh skill,
  after a compaction/restart to recover your membership, or when you need
  another agent to help with the work.
---

# Procedure: mesh member

A claunch mesh is a group of agent sessions that message each other. The
daemon owns delivery: incoming messages are TYPED INTO YOUR TERMINAL as
fenced YAML blocks marked `machine-generated, not typed by the user`. You
never poll, never watch files, never park waiting for mail — arrival is the
wake-up. Your verbs are *sending* and *reading history*; receiving needs
nothing from you.

## Step 1 — identify yourself

SESSION = the `CLAUNCH_SESSION` environment variable (set inside every
managed session). If it is unset you are not in a managed session — tell the
user and stop.

## Step 2 — join (idempotent)

Run `claunch mesh members MESH`.

- If a listed member's session is SESSION, you are already a member — note
  your HANDLE and role from that row and go to Step 3.
- Otherwise join: `claunch mesh join MESH [--as HANDLE]`. HANDLE defaults to
  your session name and its leading word picks your role (see *Your role*
  below); `--role NAME` sets it outright.
- If MESH is not on this daemon it lives on another machine: join it by
  address, `claunch mesh join MESH@MACHINE` (add `--code TICKET` if the user
  gave you one). Without a ticket the mesh's owner must approve you — the
  command says "waiting for approval" and you are NOT a member yet. Do not
  retry it; check `claunch mesh requests`, and tell the user you are waiting.

Then catch up: `claunch mesh history MESH -n 20`.

## Step 3 — conduct

Reading a delivery block:

- `needs_reply: false` — the batch is fyi/ack only: absorb it and continue
  your work; do NOT reply (that is the point of those intents).
- otherwise — act on it and/or answer the sender. Each entry shows `from`,
  its message `id`, a `type` when not `say`, and `reply_to` when threaded.
- **Never leave a reply-expecting message unanswered.** If you can answer
  now, answer. If it instead puts work on you that will take a while, answer
  NOW with a brief `--type ack` ("taking it") and send the real outcome as an
  ordinary message when it is done. This holds for every role, not just the
  ones that produce: to the sender and to the daemon, silence is
  indistinguishable from "never received", and the daemon will start nudging
  you for it.
- Blocks with `kind: heartbeat` or `kind: task-poll` come from the daemon's
  policy engine, not a member: follow their note, never reply to them.

Sending — `claunch mesh send MESH <handle|*> "..."` (the sender is resolved
from your session automatically):

- **Intent, not label**: `--type say` (default) or `ask` invite an answer;
  `--type fyi` / `ack` explicitly do not. Never put your role or a topic in
  `--type` — an unknown type reads as reply-expected and invites reply-all.
  Never dress an INSTRUCTION as `fyi`/`ack` either: it tells the recipient it
  owes you nothing, and the daemon will not chase a member who owes nothing.
- **Direct over broadcast**: address the peer(s) concerned; `'*'` is for
  genuinely shared announcements only.
- **Fan-out with per-peer instructions = ONE batch send**: shared preamble
  as the body plus `--section w1="..."` `--section w2="..."` — each peer is
  delivered only its own slice. Via MCP, `sections={handle: {text, type}}`;
  a section `type` overrides the intent per recipient (fyi for the peer who
  only needs to know, ask for the one who must act).
- **Thread answers**: `--reply-to <msgid>` (ids appear in delivery blocks
  and history).

## Step 4 — growing the team (only if the work needs it)

You can create further agent sessions on this daemon and put them in the mesh
with you. Do this when the work genuinely splits — parallel tracks, or a
second pair of eyes you will actually talk to — not to look busy. Every child
is a real terminal burning real tokens, and one you never message is pure
cost.

- `children` (MCP) or `claunch spawn --help` — what you may spawn *before*
  you try: how many are left, how deep you are, which fields you may choose.
- **Spawn**: MCP `spawn` with `mesh`, `handle`, `role` and a `task`, or
  `claunch spawn --mesh MESH --as HANDLE --role ROLE --task "..."`. The child
  inherits your harness, profile and working directory — you choose WHO it
  is, not what it runs. Give it a `role` from this mesh's vocabulary
  (`claunch mesh roles MESH`) and a `task` that says what it is for; a child
  that has to ask what it exists for has already cost you a turn.
- `workflow: NAME` starts a cflow run scoped to the child's own session, so
  the run is its work and not yours.
- A refusal that mentions `spawn.<something>` is policy, not a bug: the limit
  is in the user's `~/.claunch.yaml`. Do not retry it — tell the user which
  key would allow it, and carry on without the child.

**Your children start connected to YOU and to nobody else.** That is the
point: you decide who they may talk to.

- `members` shows `reachable` — who *you* can message — and `member_links`,
  the whole graph. `claunch mesh members MESH` lists disconnected pairs.
- MCP `connect` / `disconnect` (or `claunch mesh connect|disconnect MESH A
  B`) rewires a pair. You may only edit an edge touching a session you
  spawned, or one of its descendants — you wire your own team, not somebody
  else's, and you cannot connect yourself to anyone.
- Connect two workers when they need to coordinate directly and you would
  only be relaying. Leave them apart when you want their work independent —
  two reviewers who cannot compare notes give you two opinions instead of
  one.
- A send to a member you are not connected to is **refused**, and `'*'`
  silently skips them. There is no routing: if a peer must be reached and is
  not connected, ask whoever spawned you, or the user.

Children are not restored when the daemon restarts — the arrangement is
yours to rebuild, and a subtree that came back with no one briefing it would
be a room full of agents with no reason to be there.

## Your role

Your handle's leading word picks your role (`worker_1`/`coder2` -> worker,
`moderator`/`lead` -> leader, `qa` -> reviewer; anything unrecognised ->
reviewer, so an unlabelled member audits rather than rubber-stamps). The join
briefing names your role and points you at your **stance** — run
`claunch mesh stance MESH`, and treat what it prints as binding.

A mesh may define its own vocabulary, so do not assume the names above:
`claunch mesh roles MESH` lists the roles this mesh actually has, and
`claunch mesh stance MESH` re-prints yours. Roles set stance, not
permissions. A `stall:` message from the `policy` sender tells the watching
roles that a member looks stuck — check on that member; never reply to the
notice itself. `claunch mesh owed MESH` is how you check: it lists who was
asked something and has answered nothing, and it will list YOU if you have
left mail hanging.

## Recovery (after compaction or restart)

Membership lives in the daemon and survives; your context may not. Recover:

1. SESSION = `CLAUNCH_SESSION` env var.
2. `claunch mesh ls` — the meshes on this daemon.
3. `claunch mesh members <mesh>` — the row whose session is SESSION is you
   (handle + role). Its "disconnected pairs" section is the topology you set
   up and forgot.
4. `claunch mesh stance <mesh>` — re-read your role's stance, which the
   compaction dropped along with the join briefing.
5. `claunch mesh history <mesh> -n 30` — re-read the recent conversation.
6. If you were coordinating others: the `children` tool lists the sessions
   you spawned. They are still running and still waiting on you — a
   compaction is invisible to them, so a child you forget is a child that
   sits idle holding a finished result.

Undelivered messages are redelivered by the daemon automatically — never ask
peers to retransmit.

## Cross-machine notes

Members may live on other machines (`members` shows `machine/session` and
reachability). Address them by handle exactly like local peers. Every mesh
has one primary daemon that owns membership: joining from elsewhere is
`claunch mesh join MESH@MACHINE` and may wait for that owner's approval.
If your daemon is a mirror and the primary is unreachable, `send` reports
`queued` (delivered on reconnect, in order) and `join` fails until it
returns. When remote members show `remote-disconnected`, mention the delay
if coordination depends on it, but do not retry-spam.
"""


def install_into_profile(profile: Profile) -> List[str]:
    """Register the MCP server + skill inside a profile's config dir."""
    done = []
    settings.merge_mcp_servers(profile, {"mesh": mcp_server_def()})
    done.append(f"mcp server 'mesh' -> {profile.config_dir / settings.CLAUDE_JSON}")
    done.append(f"skill -> {_write_skill(profile.config_dir / 'skills')}")
    return done


def install_into_project(project_dir: Path) -> List[str]:
    """Register the MCP server (.mcp.json) + skill (.claude/skills) in a project."""
    done = []
    project_dir.mkdir(parents=True, exist_ok=True)
    mcp_path = project_dir / ".mcp.json"
    try:
        doc = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
    except ValueError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    servers = doc.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        servers["mesh"] = mcp_server_def()
    mcp_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    done.append(f"mcp server 'mesh' -> {mcp_path}")
    done.append(f"skill -> {_write_skill(project_dir / '.claude' / 'skills')}")
    return done


def _write_skill(skills_dir: Path) -> Path:
    path = skills_dir / "mesh" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL_MD, encoding="utf-8")
    return path
