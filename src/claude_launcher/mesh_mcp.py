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

from . import daemon_client, mcp_rpc

TOOLS = [
    {
        "name": "send",
        "description": (
            "Send a message to mesh members. '*' broadcasts to every other "
            "member; delivery types the message into each recipient's "
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
                    "description": "'*', a member handle, or comma-separated handles",
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
                        "key must be in 'to' (or covered by '*'). A section "
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
            "~/.claunch.yaml."
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
                        "starts it in no mesh at all, unable to report back"
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
                        "where the pair would have given it one"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "context string for that workflow run",
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
                        "minted nor adopted. Without this a child with a task "
                        "gets one minted for it"
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
                        "child's first word. Needs the claude harness and a "
                        "conversation of your own ('children' lists 'fork' "
                        "in may_choose when you have one), and cannot be "
                        "combined with 'workspace', 'cwd' or 'worktree': "
                        "claude keeps transcripts per directory, so a child "
                        "started elsewhere would boot empty"
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
            "rather than provoking a refusal."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
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
            "you used to know."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "kill",
        "description": (
            "End a session you spawned (or one of its descendants) and give "
            "its slot back — the counterpart of 'spawn'. A peer is refused "
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
            "a SESSION ENDED comment, ready to reassign."
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
            "you wire up your own team, not somebody else's."
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
    if name == "rebrief":
        return _client().get(f"/api/sessions/{_session()}/rebrief")
    if name == "spawn":
        return _spawn(args)
    if name == "kill":
        child = str(args.get("session") or "")
        if not child:
            raise MeshMcpError("'session' is required")
        q = "?force=1" if args.get("force") else ""
        return _client().post(
            f"/api/sessions/{_session()}/children/{child}/kill{q}"
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
            "member_links": links,
            "peers": info.get("peers", []),
            "relay": _relay_summary(info.get("relay")),
        }
    if name == "history":
        limit = int(args.get("limit") or 50)
        return _client().get(f"/api/mesh/{mesh}/messages?limit={limit}")
    raise MeshMcpError(f"unknown tool {name!r}")


def _my_handle(members: list) -> str:
    """This session's handle in a mesh, from the roster it just fetched."""
    session = os.environ.get("CLAUNCH_SESSION") or ""
    for member in members:
        if isinstance(member, dict) and member.get("session") == session:
            return str(member.get("handle") or "")
    return ""


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
_SPAWN_KEYS = (
    "name", "mesh", "handle", "role", "connect", "workflow", "context",
    "task", "issue", "issue_text", "harness", "workspace", "worktree",
    "rebase_onto", "profile", "borrow", "null_token", "cwd", "args", "env",
    "fork",
)


#: Offered fields that reach the API under another name. The one case: the
#: API's board switch is ``beads: false``, which as a *falsey* value cannot
#: ride in the comprehension below — it would be dropped as if the caller had
#: never said it — and reads better to an agent as ``no_issue: true``.
_SPAWN_RENAMED = {"no_issue": ("beads", False)}


def _spawn(args: dict) -> dict:
    payload = {k: args[k] for k in _SPAWN_KEYS if args.get(k) not in (None, "")}
    for offered, (key, value) in _SPAWN_RENAMED.items():
        if args.get(offered):
            payload[key] = value
    return _client().post(f"/api/sessions/{_session()}/children", payload)


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
