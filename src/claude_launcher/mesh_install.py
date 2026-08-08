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
  typed into your terminal by the daemon. Usage: /mesh MESH [HANDLE] — join
  (or resume membership in) MESH as HANDLE (its leading word picks your role;
  omit to use your session name). Also use when a mesh delivery or briefing
  block tells you to activate the mesh skill, or after a compaction/restart
  to recover your membership.
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
  your session name; its leading word sets your role (`worker_1` -> worker,
  `moderator`/`leader` -> leader, `reviewer...` -> reviewer).

Then catch up: `claunch mesh history MESH -n 20`.

## Step 3 — conduct

Reading a delivery block:

- `needs_reply: false` — the batch is fyi/ack only: absorb it and continue
  your work; do NOT reply (that is the point of those intents).
- otherwise — act on it and/or answer the sender. Each entry shows `from`,
  its message `id`, a `type` when not `say`, and `reply_to` when threaded.
- Blocks with `kind: heartbeat` or `kind: task-poll` come from the daemon's
  policy engine, not a member: follow their note, never reply to them.

Sending — `claunch mesh send MESH <handle|*> "..."` (the sender is resolved
from your session automatically):

- **Intent, not label**: `--type say` (default) or `ask` invite an answer;
  `--type fyi` / `ack` explicitly do not. Never put your role or a topic in
  `--type` — an unknown type reads as reply-expected and invites reply-all.
- **Direct over broadcast**: address the peer(s) concerned; `'*'` is for
  genuinely shared announcements only.
- **Fan-out with per-peer instructions = ONE batch send**: shared preamble
  as the body plus `--section w1="..."` `--section w2="..."` — each peer is
  delivered only its own slice. Via MCP, `sections={handle: {text, type}}`;
  a section `type` overrides the intent per recipient (fyi for the peer who
  only needs to know, ask for the one who must act).
- **Thread answers**: `--reply-to <msgid>` (ids appear in delivery blocks
  and history).
- Acknowledge an assignment with a brief `--type ack`, then report the real
  outcome with a normal message when done.

Roles set stance, not permissions: a leader assigns and coordinates (prefer
batch sends for fan-outs); a worker executes, reports to the leader, asks
when blocked; reviewers/specialists engage within their specialty. A
`stall:` message from the `policy` sender tells leaders a member looks stuck
— check on that member; do not reply to the notice itself.

## Recovery (after compaction or restart)

Membership lives in the daemon and survives; your context may not. Recover:

1. SESSION = `CLAUNCH_SESSION` env var.
2. `claunch mesh ls` — the meshes on this daemon.
3. `claunch mesh members <mesh>` — the row whose session is SESSION is you
   (handle + role).
4. `claunch mesh history <mesh> -n 30` — re-read the recent conversation.

Undelivered messages are redelivered by the daemon automatically — never ask
peers to retransmit.

## Cross-machine notes

Members may live on other machines (`members` shows `machine/session` and
reachability). Address them by handle exactly like local peers. When the
relay is disconnected they show `remote-disconnected` and messages to them
queue durably — mention the delay if coordination depends on it, but do not
retry-spam.
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
