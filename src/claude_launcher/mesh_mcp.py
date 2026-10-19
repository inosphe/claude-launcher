"""The mesh half of claunch's MCP surface: talk to peers, and build the team.

The wire protocol lives in :mod:`claude_launcher.mcp_rpc`; this module is the
tools and what they do. Normally these are served alongside the cflow tools by
one ``claunch mcp`` process (see :mod:`claude_launcher.mcp_server`); the
standalone ``claunch mesh mcp`` entry point remains for installs written
before the servers were merged.

A convenience wrapper around the daemon's HTTP API for agents that prefer
tools over shell, in two groups:

- **Talking** — ``send``, ``members``, ``history``. Sending only: there is
  deliberately no ``recv``, because incoming messages are typed into the
  member's terminal by the daemon, so receiving needs no tool, no polling and
  no cooperation (the design's core invariant; see ``docs/mesh-design.md``).
- **Building** — ``spawn``, ``children``, ``kill``, ``connect``,
  ``disconnect``. A session can create further sessions, enrol them in its
  mesh, decide who they may talk to and end them when they are done. What it
  may spawn is capped by the ``spawn`` policy (see
  :mod:`claude_launcher.spawn`); which edges it may rewire and which sessions
  it may end are capped by the session tree — an agent touches only what it
  spawned.

The caller is identified by ``$CLAUNCH_SESSION`` — the env var every managed
session's children inherit — exactly like the CLI.
"""

from __future__ import annotations

import os
import time
from urllib.parse import quote

from . import daemon_client, mcp_rpc

#: How often ``await_connect`` asks the daemon again. There is no deadline:
#: the call returns when the peer answers, however long that takes.
CONNECT_POLL_SECONDS = 2.0

TOOLS = [
    {
        "name": "urgent_send",
        "description": (
            "EXCEPTION path: send ONE message to ONE member you are not "
            "connected to (a handle in `mesh`, or a session name in any mesh "
            "on this daemon). Only a leader may call it; anyone else is "
            "refused. A reason (12+ characters) is required and is written "
            "into the audit record of both meshes, along with who sent it. "
            "It opens no connection, files no wire request, and takes no "
            "'*', selector or list; 3 per hour per sender, 1 per target per "
            "10 minutes. Use it only when the normal rules cannot serve and "
            "waiting would cost more than the exception: for a normal need, "
            "ask for a connection ('connect', 'wire_requests') instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "your own mesh"},
                "to": {
                    "type": "string",
                    "description": "ONE member handle, or a session name",
                },
                "body": {"type": "string", "description": "message text"},
                "reason": {
                    "type": "string",
                    "description": "why the normal connection rules cannot serve",
                },
                "target_mesh": {
                    "type": "string",
                    "description": "only if the target session is in several meshes",
                },
            },
            "required": ["mesh", "to", "body", "reason"],
        },
    },
    {
        "name": "send",
        "description": (
            "Send a message to mesh members. '*' broadcasts to every other "
            "connected member (and the result carries a BROADCAST advisory: "
            "each recipient spends a turn on it); '@in_review' addresses the "
            "members whose issue is in_review on your board. Delivery types "
            "the message into each recipient's "
            "terminal (remote members receive it over the relay). The sender "
            "is this session ($CLAUNCH_SESSION) — join the mesh first with "
            "'claunch mesh join <mesh>'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "to": {
                    "type": "string",
                    "description": (
                        "'*', an audience selector ('@in_review'), a member "
                        "handle, or comma-separated handles"
                    ),
                },
                "body": {"type": "string", "description": "message text"},
                "type": {
                    "type": "string",
                    "description": (
                        "message INTENT (not your role or a label): 'say' "
                        "(default) or 'ask' invite a reply; 'fyi' and 'ack' "
                        "do not — use them for status/acknowledgements so "
                        "peers don't reply-all"
                    ),
                },
                "reply_to": {
                    "type": "string",
                    "description": (
                        "id of the message this answers (ids appear in "
                        "delivery blocks and history)"
                    ),
                },
                "sections": {
                    "type": "object",
                    "description": (
                        "BATCH send: {handle: text} or {handle: {text, type}} "
                        "per-recipient addenda. 'body' becomes the shared "
                        "preamble; each recipient is delivered only body + "
                        "its own section (never another's instructions), and "
                        "the log keeps one composite message. Every section "
                        "key must be in 'to' (or covered by '*'), but not "
                        "every recipient needs a section: one without gets "
                        "the preamble ALONE as its whole message, so either "
                        "the preamble stands on its own or those handles do "
                        "not belong in 'to'. A section "
                        "'type' overrides the top-level intent for that "
                        "recipient — e.g. fyi for the peer who only needs to "
                        "know, ask for the one who must act."
                    ),
                },
            },
            "required": ["mesh", "to", "body"],
        },
    },
    {
        "name": "members",
        "description": (
            "List a mesh's members: handle, role, machine/session, "
            "reachability, pending message count — plus linked peer daemons "
            "and the relay connection state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"mesh": {"type": "string", "description": "mesh name"}},
            "required": ["mesh"],
        },
    },
    {
        "name": "history",
        "description": "Recent messages in a mesh (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "limit": {"type": "number", "description": "max messages (default 50)"},
            },
            "required": ["mesh"],
        },
    },
    {
        "name": "peer_file",
        "description": (
            "Read one file from another member's working directory — on this "
            "machine or, over the relay, on theirs. The path is confined to "
            "that session's checkout; the reply carries the content (cut at "
            "max_bytes, default 64K), its size, whether it was truncated, and "
            "the sha256 of the WHOLE file, and the member/session/machine it "
            "resolved to. Name the member by handle OR by session name — "
            "the daemon to route to comes from the member row. Read-only. "
            "You must be connected to that member in the mesh (see "
            "'members' -> reachable)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "member": {
                    "type": "string",
                    "description": (
                        "whose checkout to read — a handle, a member's "
                        "session name, or <machine>/<session> when two "
                        "daemons run a session of that name"
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "file path, relative to that session's working directory",
                },
                "max_bytes": {
                    "type": "number",
                    "description": "cut the content after this many bytes (default 65536, max 1048576)",
                },
            },
            "required": ["mesh", "member", "path"],
        },
    },
    {
        "name": "peer_git",
        "description": (
            "One read-only git query in another member's checkout — on this "
            "machine or over the relay. op is one of status (porcelain, with "
            "branch line), diff (args: base, head, paths, stat, cached), log "
            "(args: n, range, paths — one line per commit: hash, author, "
            "date, subject), show (args: ref, stat), branch (list with HEAD "
            "marker and tip hash). Arguments are typed fields; there is no "
            "free option string and nothing that writes. A non-zero rc "
            "returns git's own message in output rather than failing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "member": {
                    "type": "string",
                    "description": (
                        "whose checkout to query — a handle, a member's "
                        "session name, or <machine>/<session> when two "
                        "daemons run a session of that name"
                    ),
                },
                "op": {
                    "type": "string",
                    "description": "status | diff | log | show | branch",
                },
                "args": {
                    "type": "object",
                    "description": (
                        "typed arguments for op: diff {base, head, paths[], stat, "
                        "cached}; log {n, range, paths[]}; show {ref, stat}; "
                        "status {paths[]}; branch takes none"
                    ),
                },
            },
            "required": ["mesh", "member", "op"],
        },
    },
    {
        "name": "lease",
        "description": (
            "Coordinate with members on other machines before touching a "
            "shared thing: acquire a named lease (key) for a TTL, renew it "
            "while you work, release it when done, or list who holds what. "
            "The mesh's authority daemon is the single grantor, so two "
            "members cannot both hold a key. acquire on a key somebody else "
            "holds is NOT an error: the reply has ok:false and held_by — "
            "wait, message them, or pick another key. Keys are plain strings "
            "by convention: 'path:<repo-relative file>', 'issue:<id>', "
            "'branch:<name>'. A lease expires on its own (default 15 min, "
            "max 4 h) so a dead holder never blocks the mesh; renew before "
            "it does. The holder is always you (your handle in the mesh)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "op": {
                    "type": "string",
                    "description": "acquire (default) | renew | release | list",
                },
                "key": {
                    "type": "string",
                    "description": "the lease key (for list: optional holder filter)",
                },
                "ttl": {
                    "type": "number",
                    "description": "seconds until the lease expires unless renewed (default 900)",
                },
                "note": {
                    "type": "string",
                    "description": "what you are doing under it (shown to whoever is refused)",
                },
            },
            "required": ["mesh"],
        },
    },
    {
        "name": "spawn",
        "description": (
            "Create a CHILD agent session on this daemon. THE way to make a "
            "session from inside one — 'claunch new-session' is the human's "
            "command and is refused here. The child inherits your harness, "
            "profile and working directory — you choose who it is, not what "
            "it runs — is recorded as yours, and lands in your mesh with you "
            "unless you say otherwise. It starts connected to YOU only: use "
            "'connect' to let it reach other members. Check your budget with "
            "'children' first (it also lists the workspaces you may send a "
            "child to, if any); limits come from the 'spawn' block in "
            "~/.claunch.yaml. The child cap is SOFT: at the limit the spawn "
            "still goes through and the answer carries a 'warnings' entry "
            "saying so, so a full parent is a thing to notice, not a wall to "
            "work around. The depth limit is hard and does refuse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "session name (auto-generated if omitted)",
                },
                "mesh": {
                    "type": "string",
                    "description": (
                        "mesh to enrol the child in. OMIT IT for the normal "
                        "case: the child joins the mesh you are in, and one "
                        "is opened for the two of you if you are in none, so "
                        "you can always reach each other. Only name a mesh to "
                        "put the child somewhere other than your own; '-' "
                        "skips a team mesh; the parent/child channel is still created"
                    ),
                },
                "handle": {
                    "type": "string",
                    "description": (
                        "the child's handle in that mesh (default: its "
                        "session name). Its leading word picks the role "
                        "unless 'role' is given"
                    ),
                },
                "role": {
                    "type": "string",
                    "description": (
                        "the child's mesh role, which sets its stance — see "
                        "'claunch mesh roles MESH' for this mesh's vocabulary"
                    ),
                },
                "subroles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "further roles the child also answers for besides "
                        "'role' — a workflow's delegated decision, a "
                        "filter_roles check and the policy engine find it "
                        "under any of them, and each subrole's stance is "
                        "briefed after the primary's"
                    ),
                },
                "connect": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "extra member handles the child may message, besides "
                        "you. Everyone else in the mesh is cut off from it"
                    ),
                },
                "workflow": {
                    "type": "string",
                    "description": (
                        "a cflow workflow to start for the child, scoped to "
                        "its own session so the run is its work, not yours. "
                        "OMIT IT for the normal case: the child starts on the "
                        "workflow YOUR OWN run pairs children with "
                        "('default_child_cflow'; 'children' reports it as "
                        "child_cflow), and on none when your workflow pairs "
                        "with nothing or you are driving no run. Name one to "
                        "override that pairing; '-' gives the child no run "
                        "where the pair would have given it one. A cflow v2 workflow "
                        "is named the same way (or as v2:<name> when a v1 one "
                        "shares the name, or a .cflow2/generated/*.yaml path)"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "context string for that workflow run",
                },
                "cflow_inputs": {
                    "type": "object",
                    "description": (
                        "start inputs of a cflow v2 workflow that declares "
                        "'inputs:' (name -> value; e.g. {\"goal\": \"...\", "
                        "\"invariants\": [{\"text\": \"...\", \"probe\": \"cmd\"}]}). "
                        "Read the declaration with 'claunch cflow2 show <name>'. "
                        "A missing required input or an unknown name refuses the spawn"
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "opening instruction typed into the child once it "
                        "has booted — what it is for, in its own words"
                    ),
                },
                "issue": {
                    "type": "string",
                    "description": (
                        "an EXISTING board issue for the child, instead of "
                        "the fresh one its task would otherwise mint. What "
                        "that means is the daemon's call, not yours: an issue "
                        "nobody holds is assigned to the child, and one a "
                        "RUNNING session holds is only joined — the "
                        "assignment stays where it is, both sessions are told "
                        "about each other, and they settle ownership between "
                        "them. Hand a worker the issue you wrote for it "
                        "rather than letting it mint a duplicate"
                    ),
                },
                "prompt_packs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "prompt pack ids to put into the child's opening: "
                        "the copy installed in the workspace it will run in "
                        "(Settings > Install), else claunch's own. Packs its "
                        "role or subroles require, and the workspace's own "
                        "prompt (its packs headed 'delivery: always', unless "
                        "no_workspace_prompt), are added whether listed or not; a listed pack that exists in neither place "
                        "refuses the spawn, and so does an on-demand pack "
                        "(retrospective), which is sent later with "
                        "'claunch retro', never in an opening"
                    ),
                },
                "no_daemon_prompt": {
                    "type": "boolean",
                    "description": (
                        "leave this daemon's own prompt (see 'daemon_prompt') "
                        "out of the child's opening. Without it every child "
                        "gets that prompt, whatever its role"
                    ),
                },
                "no_workspace_prompt": {
                    "type": "boolean",
                    "description": (
                        "leave the workspace's own prompt (its packs headed "
                        "'delivery: always') out of the child's opening. "
                        "Without it every child created in that workspace "
                        "gets it"
                    ),
                },
                "issue_text": {
                    "type": "string",
                    "description": (
                        "what the child's NEW issue says, written here "
                        "instead of being read off its task. First line "
                        "becomes the title, the whole of it the goal — so "
                        "the board holds the specification while 'task' "
                        "holds only the first instruction, and the child is "
                        "told to go read the record. Cannot be combined with "
                        "'issue' or 'no_issue': those are the other two "
                        "answers to the same question and the request is "
                        "refused rather than one of them quietly dropped"
                    ),
                },
                "no_issue": {
                    "type": "boolean",
                    "description": (
                        "give the child no board issue at all — neither "
                        "minted nor adopted — AND tell it not to go looking "
                        "for one: its goal is the task you give it, and with "
                        "no task it waits for its user. Without this a child "
                        "with a task gets one minted for it"
                    ),
                },
                "no_issue_auto": {
                    "type": "boolean",
                    "description": (
                        "the other half of 'no_issue': mint and adopt "
                        "nothing, but tell the child to go to the board, "
                        "pick the open issue that fits and assign itself. "
                        "Use it for a child you are sending to work a queue "
                        "rather than a task you wrote. One switch used to "
                        "mean both of these and the child had to guess which "
                        "you meant"
                    ),
                },
                "issue_pending": {
                    "type": "boolean",
                    "description": (
                        "the child works an EXISTING issue you have not "
                        "named yet: nothing is minted or assigned, and it "
                        "waits until you send it the id, then records it "
                        "(a v2 run's start input 'issue') and takes it. "
                        "Without this a child with a task gets one minted "
                        "for it"
                    ),
                },
                "fork": {
                    "type": "boolean",
                    "description": (
                        "hand the child a COPY of YOUR OWN conversation, so "
                        "it starts knowing everything you know instead of "
                        "from the task line alone — useful when the context "
                        "is long and re-explaining it would lose detail. "
                        "Yours is untouched; the copy diverges from the "
                        "child's first word. Needs the Claude or Codex harness and a "
                        "conversation of your own ('children' lists 'fork' "
                        "in may_choose when you have one), and cannot be "
                        "combined with 'workspace', 'cwd' or 'worktree': "
                        "claude keeps transcripts per directory, so a child "
                        "started elsewhere would boot empty"
                    ),
                },
                "project": {
                    "type": "string",
                    "description": (
                        "file the child under a project other than yours "
                        "('children' lists them). With no 'workspace', 'cwd' "
                        "or 'worktree' beside it, the child starts in that "
                        "project's default workspace when it has one"
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        "put the child in a different directory: one of the "
                        "registered workspaces 'children' lists. A name, not "
                        "a path — an unregistered directory is refused, and "
                        "if nothing is registered there is nowhere to send it "
                        "and yours is inherited"
                    ),
                },
                "worktree": {
                    "type": "string",
                    "description": (
                        "give the child a git worktree of ITS OWN, named: a "
                        "second checkout of the repository you are in, on its "
                        "own branch. Use it whenever you spawn more than one "
                        "child that will edit code — two agents in one "
                        "checkout overwrite each other's edits mid-edit. "
                        "Naming one that already exists returns to that "
                        "checkout with its branch and work intact"
                    ),
                },
                "rebase_onto": {
                    "type": "string",
                    "description": (
                        "put the child's 'worktree' ON this branch — usually "
                        "the branch you are on. A NEW worktree is cut from it "
                        "instead of from the trunk (a nested worker's branch "
                        "then begins on yours, like a stacked pull request); "
                        "a REUSED one is rebased onto it before the child "
                        "starts. If that rebase cannot be done cleanly the "
                        "spawn is refused and nothing is created, so the child "
                        "never wakes up in a conflicted checkout"
                    ),
                },
                "branch_from": {
                    "type": "string",
                    "description": (
                        "start a NEW 'worktree' at this branch, commit or "
                        "other worktree's name (that worktree's checked-out "
                        "branch, committed work only) instead of the trunk. "
                        "Refused when the named worktree already exists and "
                        "together with rebase_onto, which is the field that "
                        "updates a REUSED one"
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "children",
        "description": (
            "The sessions you spawned (and theirs), plus how many more you "
            "may spawn, which fields you are allowed to choose, the "
            "registered workspaces you may send a child to, and "
            "'child_cflow' — the workflow your next child starts on if you "
            "spawn without naming one ('' = none). Call this before spawning "
            "rather than provoking a refusal. 'blocked_by' is what actually "
            "refuses; 'soft_blocked_by' is the child cap, which warns and "
            "lets the spawn through. Its count is the RUNNING children — an "
            "ended one does not hold a slot, so it reads lower than the "
            "child list, which keeps the exited."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "daemon_prompt",
        "description": (
            "This daemon's own prompt: one text the daemon instance you run "
            "under gives EVERY session it starts, whatever the role, as the "
            "first prompt pack of the opening (a session's creator can untick "
            "it). It is daemon configuration, not source code: a file in this "
            "daemon's state directory that no other daemon reads. 'get' (the "
            "default) returns its path, text and size; 'set' with 'text' "
            "replaces it -- an empty text turns it off. Every set keeps the "
            "text it replaced beside the file and logs who wrote it, and it "
            "reaches only sessions created after it, so change it for a rule "
            "every future session should follow, not for this session's task"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["get", "set"]},
                "text": {
                    "type": "string",
                    "description": "the whole new prompt, for 'set'",
                },
            },
            "required": [],
        },
    },
    {
        "name": "rebrief",
        "description": (
            "Re-read this session's own briefing, composed fresh from the "
            "daemon: mesh memberships and current roster, replies you owe, "
            "the cflow run you drive, your parent and children, and your "
            "recorded opening task. Call it whenever your context was "
            "compacted or cleared and the state of your session is no longer "
            "in it — the result is the current truth, not a summary of what "
            "you used to know. Pass 'id' to pull back ONE block by the content "
            "id it was given with (your opening task, your binding stance): a "
            "session reminders name those ids, and an id you cannot find attached "
            "to its text anywhere in this conversation is the signal that your "
            "context no longer holds it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "content id of one block to recall, as printed next "
                        "to the text when you were given it. Omit for the "
                        "whole briefing."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "name": "kill",
        "description": (
            "Ask to end a session you spawned (or one of its descendants) "
            "and give its slot back — the counterpart of 'spawn'. Nothing is "
            "ended by this call: it files a request the user answers on the "
            "web UI's notification card (claunch-5aysr), with no deadline. "
            "The reply says pending with the request id; do not wait in a "
            "loop or call it again — when the user answers, a '[claunch "
            "kill-request <id>]' block is typed into your terminal. 'reason' "
            "is what the user decides on: say why the child is finished. A "
            "quick-fork (opened by the user from a session) is refused: it "
            "is the user's and ends when they merge it. Reviewers a workflow "
            "spawned are ended by the workflow itself, not by this tool. A "
            "peer is refused "
            "and so are you: authority runs down the tree only. Use it when "
            "a child's work is done, not to silence one you have stopped "
            "reading — a child you never message is cost either way, but "
            "ending one mid-task loses whatever it had not reported. Its "
            "mesh row stays, reading 'exited', and it can be respawned by an "
            "operator until somebody clears it. Calling it again on a child "
            "that has already exited changes nothing and says so, so a retry "
            "is safe; dropping the record is the operator's. A child holding "
            "active beads issues is not cut off at once: the daemon first "
            "types a wind-down block into it (settle the board — in_review "
            "with evidence, or a HANDOFF comment), waits for that turn (a "
            "bounded grace), then terminates; the reply says winding_down. "
            "Once it has exited, its in_progress issues go back to open with "
            "a SESSION ENDED comment, ready to reassign. A child whose cflow "
            "run already stands on its end decision (an ask or user choice "
            "with an 'end' option, e.g. improv-worker-v2's end-decision) is "
            "not filed as a kill: the user is asked on the dashboard to "
            "answer 'end', which ends it through its workflow; the reply "
            "says end_requested (claunch-g08xz.1). 'skip_end_decision: "
            "true' files the plain kill request instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": (
                        "the child's session name, as 'children' lists it "
                        "(the session name, not its mesh handle)"
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "skip the graceful terminate. Default false, which "
                        "lets the harness shut itself down; force only after "
                        "a graceful end has visibly failed to take"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "why the child should end (its work is reported, "
                        "where), shown to the user on the approval card"
                    ),
                },
                "skip_end_decision": {
                    "type": "boolean",
                    "description": (
                        "file a kill request even when the child's run "
                        "stands on its end decision (default false: the user "
                        "is asked to answer 'end' there instead)"
                    ),
                },
            },
            "required": ["session"],
        },
    },
    {
        "name": "resume_session",
        "description": (
            "Ask the user to bring an EXITED session back (claunch-yjhgx): "
            "the session is relaunched on its own conversation, as the "
            "operator's Respawn button does. Nothing is relaunched by this "
            "call. It files a request that the user answers on the web UI's "
            "notification card, the same kind of approval a daemon restart "
            "waits for, and there is no deadline: it waits until a person "
            "approves or rejects it. Do not wait in a loop or call it again "
            "to check. When the user answers, a '[claunch resume-request "
            "<id>]' block is typed into your terminal. 'reason' is the whole "
            "basis the user decides on: say what you need that session for "
            "and why a running session cannot do it. Any exited session may "
            "be named. A running or held session is refused (message it "
            "instead), and so is a target that already has a pending "
            "request. With 'request' alone it reads one of your requests; "
            "add 'withdraw: true' to take it back."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": (
                        "the exited session to resume, by session name "
                        "(not mesh handle)"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "why it should come back, shown to the user on the "
                        "approval card; required when filing"
                    ),
                },
                "request": {
                    "type": "string",
                    "description": (
                        "an existing request id: read it (or withdraw it "
                        "with 'withdraw') instead of filing a new one"
                    ),
                },
                "withdraw": {
                    "type": "boolean",
                    "description": "with 'request': take your pending request back",
                },
            },
            "required": [],
        },
    },
    {
        "name": "handoff",
        "description": (
            "Hand what THIS session did to another session, then end this "
            "session — the way back from a quick-fork, and the way out of "
            "any session whose work someone else should carry on. 'text' is "
            "the report, in your words: what changed, files and commits, "
            "decisions taken, what is still open, and (for a handoff) where "
            "it all is and how to pick it up. The daemon types it into the "
            "target as a fenced block and only THEN ends this session, so a "
            "delivery that fails keeps you alive and says so — call again "
            "or pick another target. A quick-fork (your opening carries a "
            "'--- forked from here ---' marker naming the origin) needs no "
            "'to': the wrap-up goes back to the origin, or to the nearest "
            "live session above it when the origin has ended. With none "
            "live the reply has 'undelivered': true -- the text was saved "
            "(its 'note' says where), this session was NOT ended, and you "
            "tell the user. A quick-fork calls this only after a 'MERGE "
            "requested' block from the operator arrives (unless its marker "
            "says it may merge on its own); before that the daemon refuses "
            "it. Any other session "
            "names its target with 'to'. Do not call this before the work is "
            "done: the text is the only thing of this session that survives."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "the wrap-up or handoff, as markdown",
                },
                "to": {
                    "type": "string",
                    "description": (
                        "the session to hand off to (its session name). "
                        "Omit in a quick-fork: the merge goes to the origin "
                        "(or the nearest live session above it)"
                    ),
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "deep_think",
        "description": (
            "Ask YOURSELF a question and have a copy of you answer it, so the "
            "exploration costs the copy's context instead of yours. 'ask' "
            "quick-forks this session (the copy holds your whole conversation "
            "up to now, so the question may lean on it), puts the question on "
            "top, and RETURNS AT ONCE with an id: the answer is NOT in the "
            "reply. It arrives LATER, typed into your terminal as a fenced "
            "'claunch deep-think: ANSWER from <id>' block that starts a new "
            "turn. So do not wait, sleep or poll for it, and do not redo the "
            "same exploration meanwhile: carry on with other work, or end "
            "your turn. Good for code exploration and deep reasoning whose "
            "path you do not need to read, only its conclusion. The copy "
            "stands in YOUR checkout and may edit; it tells you what it "
            "changes with notes (op 'notify', typed into your terminal) "
            "before the answer. Optional 'model'/'effort' run the copy "
            "differently (the spawn policy decides whether you may). Past "
            "its deadline you are asked to 'extend' or 'cancel'; say "
            "neither and it is asked for what it has, then ended. 'status' "
            "lists your running questions; 'cancel' ends one unanswered. "
            "Inside a copy, 'notify' is how you tell the asker, and the "
            "answer goes in with the 'handoff' tool, not here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["ask", "status", "cancel", "extend", "notify"],
                    "description": "what to do; default 'ask'",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "ask: the question, stated so it can be answered "
                        "without asking you back -- what you want to know, "
                        "where to look, and what shape the answer should take"
                    ),
                },
                "model": {
                    "type": "string",
                    "description": "ask: the model the copy runs on (default: yours)",
                },
                "effort": {
                    "type": "string",
                    "description": "ask: the reasoning effort the copy runs at (default: yours)",
                },
                "minutes": {
                    "type": "number",
                    "description": (
                        "ask: how long before you are asked to extend or "
                        "cancel (default daemon.deep_think_timeout, 30 min); "
                        "extend: how much more time from now"
                    ),
                },
                "id": {
                    "type": "string",
                    "description": "cancel/extend: the question's id, as 'ask' or 'status' gave it",
                },
                "text": {
                    "type": "string",
                    "description": "notify (inside a copy): the note for the asker -- which files and why",
                },
            },
            "required": [],
        },
    },
    {
        "name": "detach",
        "description": (
            "End the parent/child relation between a session you spawned (or "
            "a descendant of one) and its parent: it becomes a root. A direct "
            "parent and child may message each other whatever mesh or project "
            "they are in; after a detach they are an ordinary pair, so the "
            "private channel the daemon opened for them is cut. Its own "
            "children stay under it. There is no re-attach tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": (
                        "the session to detach, as 'children' lists it (the "
                        "session name, not its mesh handle)"
                    ),
                },
            },
            "required": ["session"],
        },
    },
    {
        "name": "reparent",
        "description": (
            "Move a session you spawned (or a descendant of one) — with its "
            "whole subtree — under another session you command, or under "
            "yourself. This is how a lead hands the workers of one crowded "
            "domain to a nested worker it spawned for that domain: that worker "
            "then collects their branches and requests integration once. The "
            "moved session keeps its terminal, conversation, mesh handle and "
            "cflow run; what changes is who it reports to — so TELL IT, its "
            "own briefing still names you. Its edge to the new parent is "
            "opened in every mesh the two share; its edge to you is left for "
            "you to cut (disconnect) or keep. Refused when it would make a "
            "cycle, hang a subtree from an exited session, or push any moved "
            "session past spawn.max_depth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": (
                        "the session to move, as 'children' lists it (the "
                        "session name, not its mesh handle)"
                    ),
                },
                "parent": {
                    "type": "string",
                    "description": (
                        "its new parent's session name — yourself, or a "
                        "session you spawned (or a descendant of one)"
                    ),
                },
            },
            "required": ["session", "parent"],
        },
    },
    {
        "name": "connect",
        "description": (
            "Let two members of a mesh message each other. At least one of "
            "them must be a session you spawned (or a descendant of one) — "
            "you wire up your own team, not somebody else's. This is also "
            "how a wire request is GRANTED: if either member was refused a "
            "send to the other, connecting them answers that and tells the "
            "requester ('wire_requests' lists what is waiting)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "a": {"type": "string", "description": "one member handle"},
                "b": {"type": "string", "description": "the other member handle"},
            },
            "required": ["mesh", "a", "b"],
        },
    },
    {
        "name": "disconnect",
        "description": (
            "Stop two members of a mesh from messaging each other. Same "
            "restriction as 'connect'. Sends across a cut pair are refused — "
            "there is no routing around it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "a": {"type": "string", "description": "one member handle"},
                "b": {"type": "string", "description": "the other member handle"},
            },
            "required": ["mesh", "a", "b"],
        },
    },
    {
        "name": "wire_requests",
        "description": (
            "Members that tried to message a peer they are not connected to "
            "and are waiting on your decision. Each row says who asked, for "
            "whom, and how many times. GRANT one with 'connect' — that is "
            "the whole answer, and the requester is told. Say no with "
            "'decline' here plus a 'reason': a decline is final and is "
            "carried into the refusal the requester gets if it asks again, "
            "so it costs no further messages. Leaving one open is the answer "
            "that costs you every later round, because the pair then has "
            "nowhere to settle their disagreement except through you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "state": {
                    "type": "string",
                    "description": "filter: open (default view), granted, declined",
                },
                "decline": {
                    "type": "string",
                    "description": (
                        "answer this pair with no: two handles, "
                        "comma-separated (e.g. 'w1,w2')"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "why you declined — the requester is shown it, so "
                        "say what to do instead of asking again"
                    ),
                },
            },
            "required": ["mesh"],
        },
    },
    {
        "name": "join",
        "description": (
            "Join this session to a mesh as a member. A session on this "
            "daemon joins at once; the handle defaults to the session name "
            "and the role to the handle's leading word. Joining a mesh that "
            "lives on another machine (a 'name@machine' address) without a "
            "'code' waits for that mesh's owner to approve — the answer then "
            "says so and carries the request id. Join first: 'connect_request' "
            "and 'await_connect' need you to be a member."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "handle": {"type": "string", "description": "optional handle"},
                "role": {"type": "string", "description": "optional role"},
                "code": {
                    "type": "string",
                    "description": "invite ticket for a mesh on another machine",
                },
            },
            "required": ["mesh"],
        },
    },
    {
        "name": "connect_request",
        "description": (
            "Ask to message a member you are not connected to, before you have "
            "anything to send. Files the same request a refused send files; the "
            "mesh's approver is told and answers by connecting you (granted) or "
            "declining with a reason. Returns the pair's standing at once. Both "
            "of you must be members. Follow it with 'await_connect'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "peer": {"type": "string", "description": "the member's handle"},
            },
            "required": ["mesh", "peer"],
        },
    },
    {
        "name": "await_connect",
        "description": (
            "Wait for the answer to a 'connect_request' to the member 'peer'. "
            "Blocks until the approver connects you ('connected'), declines "
            "('declined', with the reason) or cuts an edge that was granted "
            "('cut'). There is no deadline: a decision that takes a day keeps "
            "this call open for a day. Needs a request filed first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mesh": {"type": "string", "description": "mesh name"},
                "peer": {"type": "string", "description": "the member's handle"},
            },
            "required": ["mesh", "peer"],
        },
    },
    {
        "name": "loops",
        "description": (
            "What this session is waiting on -- its open loops. Stored "
            "entries (yours, and the mesh's own for a send it refused for a "
            "full inbox) plus the replies you are owed on messages you sent. "
            "A re-briefing after /compact or /clear carries the same list, "
            "so a wait recorded here survives your context; one that lives "
            "only in your conversation does not. STALE marks a loop past its "
            "horizon: renew it with loop_add or end it with loop_close."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "description": "include closed entries",
                },
            },
        },
    },
    {
        "name": "loop_add",
        "description": (
            "Record something you are waiting on, so it is handed back to "
            "you after a context loss: 'what' you wait for, 'resume_when' "
            "the condition that ends the wait, 'then' what you will do. Use "
            "it the moment a wait begins that a later turn must pick up -- "
            "a peer's answer, a window that must open, a re-send a refusal "
            "made you postpone. Same 'key' while open updates that entry "
            "instead of adding a second."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "what": {"type": "string", "description": "what is waited on"},
                "resume_when": {
                    "type": "string",
                    "description": "the condition that ends the wait",
                },
                "then": {
                    "type": "string",
                    "description": "what to do once it ends",
                },
                "refs": {
                    "type": "object",
                    "description": (
                        "ids the wait is about: issue, message, session, mesh"
                    ),
                },
                "key": {
                    "type": "string",
                    "description": "dedupe key: re-adding while open updates in place",
                },
                "expires_in": {
                    "type": "number",
                    "description": (
                        "seconds until the loop counts as stale (default 6h)"
                    ),
                },
            },
            "required": ["what"],
        },
    },
    {
        "name": "loop_close",
        "description": (
            "End one open loop by id, with an optional 'note' on how it "
            "resolved. Close a loop when its wait ends; a loop nobody closes "
            "turns STALE and keeps asking to be judged."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "the loop id"},
                "note": {"type": "string", "description": "how it resolved"},
            },
            "required": ["id"],
        },
    },
]


class MeshMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        reason = daemon_client.unreachable_reason(why)
        if not daemon_client.is_absent(why):
            # The agent reading this is the caller that matters most: told
            # "not running", it starts a daemon, and told nothing it retries
            # blind. A send that silently needed a retry once let a window
            # declaration land 22s late. So say which of the two it is, and
            # what the right next move actually is.
            raise MeshMcpError(
                f"the claunch {reason} -- this is NOT a report that the "
                f"daemon is gone, so do not start one; call again and act "
                f"only on what the retry returns"
            )
        raise MeshMcpError(
            f"the claunch {reason} (start it with any claunch "
            f"session command, or 'claunch daemon start')"
        )
    return client


def _session() -> str:
    """The calling session's name — the identity every write-side tool needs."""
    sender = os.environ.get("CLAUNCH_SESSION")
    if not sender:
        raise MeshMcpError(
            "no $CLAUNCH_SESSION — this tool only works inside a managed "
            "claunch session"
        )
    return sender


def call_tool(name: str, args: dict) -> dict:
    # The team-building tools are not scoped to a mesh (a child can be
    # spawned without one), so they are dispatched before the mesh check the
    # messaging tools share.
    if name == "children":
        return _client().get(f"/api/sessions/{_session()}/children")
    if name == "daemon_prompt":
        op = str(args.get("op") or "get").strip()
        if op == "get":
            return _client().get("/api/daemon-prompt")
        if op != "set":
            raise MeshMcpError("'op' is 'get' or 'set'")
        if not isinstance(args.get("text"), str):
            raise MeshMcpError("'set' needs 'text': the whole new prompt (empty turns it off)")
        return _client().put(
            "/api/daemon-prompt", {"text": args["text"], "by": _session()}
        )
    if name == "rebrief":
        ident = str(args.get("id") or "").strip()
        path = f"/api/sessions/{_session()}/rebrief"
        if ident:
            path += f"?id={quote(ident, safe='')}"
        return _client().get(path)
    if name == "spawn":
        return _spawn(args)
    if name == "loops":
        q = "?all=1" if args.get("all") else ""
        return _client().get(f"/api/sessions/{_session()}/loops{q}")
    if name == "loop_add":
        payload = {
            k: args[k]
            for k in ("what", "resume_when", "then", "refs", "key", "expires_in")
            if args.get(k) not in (None, "")
        }
        return _client().post(f"/api/sessions/{_session()}/loops", payload)
    if name == "loop_close":
        ident = str(args.get("id") or "").strip()
        if not ident:
            raise MeshMcpError("'id' is required")
        return _client().post(
            f"/api/sessions/{_session()}/loops/{quote(ident, safe='')}/close",
            {"note": str(args.get("note") or "")},
        )
    if name == "kill":
        child = str(args.get("session") or "")
        if not child:
            raise MeshMcpError("'session' is required")
        q = "?force=1" if args.get("force") else ""
        return _client().post(
            f"/api/sessions/{_session()}/children/{child}/kill{q}",
            {"reason": str(args.get("reason") or ""),
             "skip_end_decision": bool(args.get("skip_end_decision"))},
        )
    if name == "resume_session":
        return _resume_session(args)
    if name == "handoff":
        text = str(args.get("text") or "")
        if not text.strip():
            raise MeshMcpError("'text' is required: the wrap-up or handoff to deliver")
        body = {"text": text}
        if args.get("to"):
            body["to"] = str(args["to"])
        return _client().post(f"/api/sessions/{_session()}/handoff", body)
    if name == "deep_think":
        return _deep_think(args)
    if name == "detach":
        child = str(args.get("session") or "")
        if not child:
            raise MeshMcpError("'session' is required")
        return _client().post(
            f"/api/sessions/{child}/detach", {"actor": _session()},
        )
    if name == "reparent":
        child = str(args.get("session") or "")
        parent = str(args.get("parent") or "")
        if not child or not parent:
            raise MeshMcpError("'session' and 'parent' are both required")
        return _client().post(
            f"/api/sessions/{child}/parent",
            {"parent": parent, "actor": _session()},
        )

    mesh = str(args.get("mesh") or "")
    if not mesh:
        raise MeshMcpError("'mesh' is required")
    if name in ("connect", "disconnect"):
        a, b = str(args.get("a") or ""), str(args.get("b") or "")
        if not a or not b:
            raise MeshMcpError("'a' and 'b' are both required")
        return _client().patch(
            f"/api/mesh/{mesh}/members/{a}/links/{b}",
            {"enabled": name == "connect", "actor": _session()},
        )
    if name == "join":
        return _client().post(
            f"/api/mesh/{mesh}/join",
            {
                "session": _session(),
                "handle": str(args.get("handle") or ""),
                "role": str(args.get("role") or ""),
                "code": str(args.get("code") or ""),
            },
        )
    if name in ("connect_request", "await_connect"):
        peer = str(args.get("peer") or "").strip()
        if not peer:
            raise MeshMcpError("'peer' is required")
        me = _my_handle(_client().get(f"/api/mesh/{mesh}").get("members") or [])
        if not me:
            raise MeshMcpError(
                f"this session is not a member of mesh {mesh!r} — join it first"
            )
        if name == "connect_request":
            return _client().post(
                f"/api/mesh/{mesh}/wire-requests",
                {"requester": me, "target": peer},
            )
        return _await_connect(mesh, me, peer)
    if name == "wire_requests":
        decline = str(args.get("decline") or "").strip()
        if decline:
            pair = [p.strip() for p in decline.split(",") if p.strip()]
            if len(pair) != 2:
                raise MeshMcpError(
                    "'decline' takes exactly two handles, comma-separated"
                )
            return _client().post(
                f"/api/mesh/{mesh}/wire-requests/decline",
                {
                    "a": pair[0], "b": pair[1],
                    "reason": str(args.get("reason") or ""),
                    "actor": _session(),
                },
            )
        state = str(args.get("state") or "").strip()
        return _client().get(
            f"/api/mesh/{mesh}/wire-requests" + (f"?state={state}" if state else "")
        )
    if name == "urgent_send":
        result = _client().post(
            f"/api/mesh/{mesh}/urgent",
            {
                "from": _session(),
                "to": str(args.get("to") or ""),
                "body": str(args.get("body") or ""),
                "reason": str(args.get("reason") or ""),
                "target_mesh": str(args.get("target_mesh") or ""),
            },
        )
        relay = result.pop("relay", None)
        result["relay"] = _relay_summary(relay)
        return result
    if name == "send":
        sender = _session()
        to_raw = str(args.get("to") or "")
        to = to_raw if to_raw == "*" or "," not in to_raw else [
            t.strip() for t in to_raw.split(",") if t.strip()
        ]
        payload = {
            "from": sender,
            "to": to,
            "body": str(args.get("body") or ""),
            "type": str(args.get("type") or "say"),
        }
        if args.get("reply_to"):
            payload["reply_to"] = str(args["reply_to"])
        if isinstance(args.get("sections"), dict):
            payload["sections"] = args["sections"]
        result = _client().post(f"/api/mesh/{mesh}/messages", payload)
        relay = result.pop("relay", None)
        result["relay"] = _relay_summary(relay)
        return result
    if name == "members":
        info = _client().get(f"/api/mesh/{mesh}")
        # Who *you* can actually message, resolved here rather than left for
        # the agent to derive from the edge list — an agent that has to
        # compute its own reachability will sooner or later address a peer it
        # cannot reach and read the refusal as a bug.
        links = info.get("member_links") or []
        me = _my_handle(info.get("members") or [])
        reachable = sorted(
            (edge["b"] if edge["a"] == me else edge["a"])
            for edge in links
            if edge.get("enabled") and me in (edge.get("a"), edge.get("b"))
        )
        return {
            "you": me,
            "reachable": reachable if me else None,
            "members": info.get("members", []),
            "peers": info.get("peers", []),
            "relay": _relay_summary(info.get("relay")),
        }
    if name == "history":
        limit = int(args.get("limit") or 50)
        return _client().get(f"/api/mesh/{mesh}/messages?limit={limit}")
    if name == "peer_file":
        member = str(args.get("member") or "")
        path = str(args.get("path") or "")
        if not member or not path:
            raise MeshMcpError("'member' and 'path' are both required")
        payload = {"actor": _session(), "member": member, "path": path}
        if args.get("max_bytes"):
            payload["max_bytes"] = int(args["max_bytes"])
        return _client().post(f"/api/mesh/{mesh}/ops/file", payload)
    if name == "peer_git":
        member = str(args.get("member") or "")
        op = str(args.get("op") or "")
        if not member or not op:
            raise MeshMcpError("'member' and 'op' are both required")
        gargs = args.get("args")
        return _client().post(
            f"/api/mesh/{mesh}/ops/git",
            {"actor": _session(), "member": member, "op": op,
             "args": gargs if isinstance(gargs, dict) else {}},
        )
    if name == "lease":
        op = str(args.get("op") or "acquire")
        key = str(args.get("key") or "")
        if op != "list" and not key:
            raise MeshMcpError("'key' is required")
        payload = {"actor": _session(), "op": op, "key": key}
        if args.get("ttl"):
            payload["ttl"] = float(args["ttl"])
        if args.get("note"):
            payload["note"] = str(args["note"])
        return _client().post(f"/api/mesh/{mesh}/leases", payload)
    raise MeshMcpError(f"unknown tool {name!r}")


def _my_handle(members: list) -> str:
    """This session's handle in a mesh, from the roster it just fetched."""
    session = os.environ.get("CLAUNCH_SESSION") or ""
    for member in members:
        if isinstance(member, dict) and member.get("session") == session:
            return str(member.get("handle") or "")
    return ""


def _await_connect(mesh: str, me: str, peer: str) -> dict:
    """Poll the pair's standing until the approver answers, with no deadline.

    Refuses to wait on a pair nobody asked about: with no request filed the
    answer can never come, and a wait that never ends is worse than an error.
    """
    path = f"/api/mesh/{mesh}/connect-status?" + "&".join(
        (f"a={quote(me)}", f"b={quote(peer)}")
    )
    while True:
        status = _client().get(path)
        request = status.get("request")
        if request is None and not status.get("connected"):
            raise MeshMcpError(
                f"no connect request between {me!r} and {peer!r} in mesh "
                f"{mesh!r} — file one with 'connect_request' first"
            )
        if status.get("connected"):
            return {**status, "outcome": "connected"}
        state = (request or {}).get("state")
        if state == "declined":
            return {**status, "outcome": "declined"}
        if state == "granted":
            return {**status, "outcome": "cut"}
        time.sleep(CONNECT_POLL_SECONDS)


#: Keys passed straight through to the spawn endpoint. Listed rather than
#: forwarded wholesale so a mistyped argument is dropped here instead of
#: reaching the daemon as a field it silently ignores.
#:
#: A few of these are deliberately *not* in the tool schema: ``cwd``,
#: ``profile``, ``borrow``, ``null_token``, ``args`` and ``env`` are honoured
#: if a caller supplies them (the CLI does), but they are not offered,
#: because the offered way to move a child is ``workspace`` — a pick from
#: the registry rather than a path spelled from memory — and the auth
#: fields are the policy's to unlock, not the schema's to advertise.
#: ``tools`` (a harness's builtin-tool choice, e.g. Pi's ``full_read``) is
#: in the same honoured-not-offered group: it rides the ``args`` gate.
_SPAWN_KEYS = (
    "name", "mesh", "handle", "role", "subroles", "connect", "workflow", "context",
    "task", "issue", "issue_text", "harness", "workspace", "worktree",
    "rebase_onto", "branch_from", "profile", "borrow", "null_token", "cwd", "args", "env",
    "fork", "tools", "project", "prompt_packs", "workflow_version", "workflow_params",
    "cflow_inputs",
)


#: Offered fields that reach the API under another name. The case they share:
#: the API's board switch is one key, ``beads``, and the two "no issue"
#: answers are two values of it — ``False`` cannot ride in the comprehension
#: below at all (a falsey value is dropped as if the caller had never said
#: it), and both read better to an agent as switches of their own. Applied in
#: order, so a caller that sets both gets the auto answer: it is the one that
#: says what the child should DO, and the one it would have had to ask about.
_SPAWN_RENAMED = {
    "issue_pending": ("beads", "existing-pending"),
    "no_issue": ("beads", False),
    "no_issue_auto": ("beads", "none-auto"),
    "no_daemon_prompt": ("daemon_prompt", False),
    "no_workspace_prompt": ("workspace_prompt", False),
}


def _resume_session(args: dict) -> dict:
    """File, read or withdraw a request to resume an exited session. Filing
    returns at once: the user's answer arrives in the terminal, so the tool
    never holds a call open on a person (the request has no deadline)."""
    rid = str(args.get("request") or "").strip()
    if rid:
        path = f"/api/resume-requests/{quote(rid, safe='')}"
        if args.get("withdraw"):
            return _client().post(f"{path}/withdraw", {"session": _session()})
        return _client().get(path)
    if args.get("withdraw"):
        raise MeshMcpError("'withdraw' needs 'request': the id of your pending request")
    target = str(args.get("session") or "").strip()
    if not target:
        raise MeshMcpError("'session' is required: the exited session to resume")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise MeshMcpError(
            "'reason' is required: the user decides on it alone -- say what you "
            "need that session for"
        )
    reply = _client().post(
        "/api/resume-requests",
        {"session": _session(), "target": target, "reason": reason},
    )
    reply["next"] = (
        "filed and waiting on the user, with no deadline. End your turn or "
        "carry on with other work; the answer is typed into this terminal as "
        "a '[claunch resume-request <id>]' block."
    )
    return reply


def _spawn(args: dict) -> dict:
    payload = {k: args[k] for k in _SPAWN_KEYS if args.get(k) not in (None, "")}
    for offered, (key, value) in _SPAWN_RENAMED.items():
        if args.get(offered):
            payload[key] = value
    return _client().post(f"/api/sessions/{_session()}/children", payload)


def _deep_think(args: dict) -> dict:
    op = str(args.get("op") or "ask").strip()
    me = quote(_session(), safe="")
    base = f"/api/sessions/{me}/deep-think"
    minutes = args.get("minutes")
    seconds = None
    if minutes not in (None, ""):
        try:
            seconds = float(minutes) * 60.0
        except (TypeError, ValueError):
            raise MeshMcpError(f"'minutes' must be a number, not {minutes!r}") from None
    if op == "ask":
        question = str(args.get("question") or "").strip()
        if not question:
            raise MeshMcpError("'question' is required for op 'ask'")
        body = {"question": question}
        for key in ("model", "effort"):
            if args.get(key):
                body[key] = str(args[key])
        if seconds is not None:
            body["timeout"] = seconds
        return _client().post(base, body)
    if op == "status":
        return _client().get(base)
    if op == "notify":
        text = str(args.get("text") or "").strip()
        if not text:
            raise MeshMcpError("'text' is required for op 'notify'")
        return _client().post(f"{base}/notify", {"text": text})
    if op in ("cancel", "extend"):
        ident = str(args.get("id") or "").strip()
        if not ident:
            raise MeshMcpError(f"'id' is required for op {op!r}")
        body = {"timeout": seconds} if op == "extend" and seconds is not None else {}
        return _client().post(f"{base}/{quote(ident, safe='')}/{op}", body)
    raise MeshMcpError(
        f"unknown op {op!r}: one of ask, status, cancel, extend, notify"
    )


def _relay_summary(relay) -> str:
    from .cli_mesh import relay_line

    return relay_line(relay if isinstance(relay, dict) else None)


SERVER = mcp_rpc.Server(
    name="claunch-mesh",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(MeshMcpError, daemon_client.DaemonClientError),
)


def _handle(msg: dict):
    return SERVER.handle(msg)


def serve() -> int:
    return SERVER.serve()
