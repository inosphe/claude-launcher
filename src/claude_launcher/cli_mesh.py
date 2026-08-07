"""``claunch mesh`` subcommands: group sessions and message between them.

Thin client of the daemon's ``/api/mesh`` surface. Inside a managed session
``$CLAUNCH_SESSION`` identifies the caller, so an agent can run
``claunch mesh join dev`` / ``claunch mesh send dev '*' "..."`` bare — no
pane ids, no explicit self-identification. Every command ends with a relay
status line so it is always visible whether the mesh can span machines.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from . import daemon_client


def relay_line(relay: Optional[dict]) -> str:
    """One-line relay connectivity summary, printed all over the CLI."""
    # ASCII only: this line goes through redirected stdio on cp949 consoles.
    if not relay or not relay.get("configured"):
        return "relay: not configured -- sessions/mesh reachable on this machine only"
    if relay.get("connected"):
        return f"relay: connected as {relay.get('name')!r}"
    return (
        f"relay: DISCONNECTED (registered name {relay.get('name')!r}) -- "
        "remote machines unreachable"
    )


def _print_relay(relay: Optional[dict]) -> None:
    print(relay_line(relay), file=sys.stderr)


def _own_session(args: argparse.Namespace) -> Optional[str]:
    """The session this command speaks for: --session, else $CLAUNCH_SESSION."""
    return getattr(args, "session", None) or os.environ.get("CLAUNCH_SESSION")


def _cmd_create(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    info = client.post("/api/mesh", {"name": args.mesh})
    print(f"created mesh {info['name']!r}")
    _print_relay(client.get("/api/daemon").get("relay"))
    return 0


def _cmd_ls(_args: argparse.Namespace) -> int:
    client = daemon_client.connect()
    if client is None:
        print("daemon is not running; no meshes")
        return 0
    payload = client.get("/api/mesh")
    meshes = payload.get("meshes", [])
    if not meshes:
        print("no meshes; create one with 'claunch mesh create <name>'")
    for m in meshes:
        print(
            f"{m['name']:<16} {len(m['members'])} member(s), "
            f"{m['messages']} message(s)"
        )
    _print_relay(payload.get("relay"))
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    client.delete(f"/api/mesh/{args.mesh}")
    print(f"mesh {args.mesh!r} removed (history retired on disk)")
    return 0


def _cmd_join(args: argparse.Namespace) -> int:
    session = _own_session(args)
    if not session:
        print(
            "error: no session — run inside a claunch session (where "
            "$CLAUNCH_SESSION is set) or pass --session NAME",
            file=sys.stderr,
        )
        return 1
    client = daemon_client.ensure_running()
    body = {"session": session, "handle": args.handle or "", "role": args.role or ""}
    member = client.post(f"/api/mesh/{args.mesh}/members", body)
    print(
        f"joined mesh {args.mesh!r} as {member['handle']!r} "
        f"(role: {member['role']}, session: {member['session']})"
    )
    print(
        f"send: claunch mesh send {args.mesh} '*' \"...\"  |  "
        f"members: claunch mesh members {args.mesh}"
    )
    _print_relay(client.get("/api/daemon").get("relay"))
    return 0


def _cmd_leave(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    handle = args.handle
    if not handle:
        session = _own_session(args)
        if not session:
            print(
                "error: pass --as HANDLE, or run inside the member session",
                file=sys.stderr,
            )
            return 1
        info = client.get(f"/api/mesh/{args.mesh}")
        mine = [
            m for m in info.get("members", [])
            if not m.get("machine") and m.get("session") == session
        ]
        if not mine:
            print(
                f"error: session {session!r} is not a member of mesh {args.mesh!r}",
                file=sys.stderr,
            )
            return 1
        handle = mine[0]["handle"]
    client.delete(f"/api/mesh/{args.mesh}/members/{handle}")
    print(f"left mesh {args.mesh!r} (handle {handle!r})")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.text == ["-"] else " ".join(args.text)
    sections = {}
    for item in args.section or []:
        handle, sep, sec_text = item.partition("=")
        if not sep or not handle or not sec_text:
            print(f"error: --section needs HANDLE=TEXT, got {item!r}",
                  file=sys.stderr)
            return 1
        sections[handle] = sec_text
    if not text.strip() and not sections:
        print("error: empty message", file=sys.stderr)
        return 1
    sender = args.sender or _own_session(args)
    if not sender:
        print(
            "error: no sender — run inside a claunch session, or pass "
            "--from HANDLE / --session NAME",
            file=sys.stderr,
        )
        return 1
    client = daemon_client.ensure_running()
    payload = {
        "from": sender,
        "to": args.to,
        "body": text if text.strip() else "",
        "external": bool(args.external),
        "type": args.type,
    }
    if args.reply_to:
        payload["reply_to"] = args.reply_to
    if sections:
        payload["sections"] = sections
    result = client.post(f"/api/mesh/{args.mesh}/messages", payload)
    recipients = result.get("recipients", [])
    queued = result.get("queued_remote", [])
    line = f"sent {result.get('id')} to {', '.join(recipients) or '(nobody)'}"
    if args.type != "say":
        line += f" [{args.type}]"
    if queued:
        line += f" -- queued for remote: {', '.join(queued)}"
    print(line)
    if result.get("notice"):
        print(f"notice: {result['notice']}", file=sys.stderr)
    _print_relay(result.get("relay"))
    return 0


def _cmd_invite(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    result = client.post(f"/api/mesh/{args.mesh}/invite", {})
    print(
        f"invite code for mesh {args.mesh!r} (machine {result.get('machine')!r}):"
    )
    print(result.get("code"))
    print(
        "redeem on the peer machine with: claunch mesh link <code>",
        file=sys.stderr,
    )
    _print_relay(result.get("relay"))
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    result = client.post("/api/mesh/link", {"code": args.code})
    print(
        f"linked mesh {result.get('mesh')!r} with peer {result.get('peer')!r} "
        f"({result.get('members')} member(s) total)"
    )
    _print_relay(result.get("relay"))
    return 0


def _cmd_members(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    info = client.get(f"/api/mesh/{args.mesh}")
    members = info.get("members", [])
    if not members:
        print(f"mesh {args.mesh!r} has no members yet")
    for m in members:
        where = m.get("machine") or "local"
        pending = m.get("pending")
        pending_s = f" pending:{pending}" if pending else ""
        print(
            f"{m['handle']:<16} {m['role']:<10} {where + '/' + m['session']:<28} "
            f"[{m['reachability']}]{pending_s}"
        )
    for p in info.get("peers", []):
        if p.get("ok") is False:
            state = f"unreachable ({p.get('error')})"
        elif p.get("ok"):
            state = "ok"
        else:
            state = "linked (no traffic yet)"
        queued = f" -- {p['queued']} message(s) queued" if p.get("queued") else ""
        print(f"peer daemon {p['machine']:<12} [{state}]{queued}")
    _print_relay(info.get("relay"))
    return 0


def _parse_policy_value(key: str, raw: str):
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if key == "roles":
        return [r.strip() for r in raw.split(",") if r.strip()]
    try:
        return float(raw)
    except ValueError:
        return raw


def _cmd_policy(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    if args.set:
        patch: dict = {}
        for item in args.set:
            path, sep, raw = item.partition("=")
            if not sep:
                print(f"error: --set needs section.key=value, got {item!r}",
                      file=sys.stderr)
                return 1
            parts = path.split(".")
            if len(parts) == 2:
                section, key = parts
                patch.setdefault(section, {})[key] = _parse_policy_value(key, raw)
            elif len(parts) == 3 and parts[1] == "bodies":
                section, _, role = parts
                patch.setdefault(section, {}).setdefault("bodies", {})[role] = raw
            else:
                print(f"error: bad policy path {path!r} (use section.key or "
                      "task_poll.bodies.<role>)", file=sys.stderr)
                return 1
        payload = client.put(f"/api/mesh/{args.mesh}/policy", patch)
    else:
        payload = client.get(f"/api/mesh/{args.mesh}/policy")
    import json as _json

    print(_json.dumps(payload.get("policy", {}), indent=2, ensure_ascii=False))
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from . import mesh_mcp

    return mesh_mcp.serve()


def _cmd_history(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    payload = client.get(f"/api/mesh/{args.mesh}/messages?limit={args.n}")
    for m in payload.get("messages", []):
        to = m.get("to")
        to_s = to if isinstance(to, str) else ",".join(to)
        body = str(m.get("body") or "")
        intent = str(m.get("type") or "say")
        tag = f" [{intent}]" if intent != "say" else ""
        if m.get("reply_to"):
            tag += f" [re {m['reply_to']}]"
        print(f"[{m.get('ts')}] {m.get('id')} {m.get('from')} -> {to_s}{tag}: {body}")
    return 0


def register(sub) -> None:
    p_mesh = sub.add_parser(
        "mesh", help="group sessions into a mesh and message between them"
    )
    msub = p_mesh.add_subparsers(dest="mesh_command", required=True)

    p = msub.add_parser("create", help="create a mesh")
    p.add_argument("mesh")
    p.set_defaults(func=_cmd_create)

    p = msub.add_parser("ls", aliases=["list"], help="list meshes")
    p.set_defaults(func=_cmd_ls)

    p = msub.add_parser("rm", aliases=["delete"], help="remove a mesh")
    p.add_argument("mesh")
    p.set_defaults(func=_cmd_rm)

    p = msub.add_parser(
        "join", help="join a mesh (defaults to the current $CLAUNCH_SESSION)"
    )
    p.add_argument("mesh")
    p.add_argument("--as", dest="handle", metavar="HANDLE",
                   help="handle inside the mesh (default: the session name)")
    p.add_argument("--role", help="member role (default: inferred from the handle)")
    p.add_argument("--session", help="session to enrol (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_join)

    p = msub.add_parser("leave", help="leave a mesh")
    p.add_argument("mesh")
    p.add_argument("--as", dest="handle", metavar="HANDLE",
                   help="handle to remove (default: resolved from the session)")
    p.add_argument("--session", help="session to resolve (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_leave)

    p = msub.add_parser(
        "send",
        help="send a message ('*' broadcasts); delivery types into recipients' terminals",
    )
    p.add_argument("mesh")
    p.add_argument("to", help="'*', a handle, or a handle to address")
    p.add_argument("text", nargs="+", help="message text ('-' reads stdin)")
    p.add_argument("--from", dest="sender",
                   help="sender handle (default: resolved from $CLAUNCH_SESSION)")
    p.add_argument("--session", help="sender session (default: $CLAUNCH_SESSION)")
    p.add_argument("--external", action="store_true",
                   help="send as a non-member (e.g. a human operator)")
    p.add_argument("--type", default="say",
                   help="message intent: say (default), ask, or the no-reply "
                        "fyi/ack -- recipients owe no answer to fyi/ack")
    p.add_argument("--reply-to", dest="reply_to", metavar="MSGID",
                   help="thread this message to an earlier message id "
                        "(ids are shown in delivery blocks and history)")
    p.add_argument("--section", action="append", metavar="HANDLE=TEXT",
                   help="batch send: per-recipient addendum; the main text "
                        "becomes the shared preamble and each recipient is "
                        "delivered only its own slice (repeatable)")
    p.set_defaults(func=_cmd_send)

    p = msub.add_parser(
        "invite",
        help="mint a code another machine's daemon can redeem to link this mesh",
    )
    p.add_argument("mesh")
    p.set_defaults(func=_cmd_invite)

    p = msub.add_parser(
        "link", help="redeem an invite code -- link this daemon into a peer's mesh"
    )
    p.add_argument("code")
    p.set_defaults(func=_cmd_link)

    p = msub.add_parser("members", help="list a mesh's members and reachability")
    p.add_argument("mesh")
    p.set_defaults(func=_cmd_members)

    p = msub.add_parser(
        "policy",
        help="show or edit the mesh's nudge policy (heartbeat/task-poll/stall-warn)",
    )
    p.add_argument("mesh")
    p.add_argument(
        "--set", action="append", metavar="SECTION.KEY=VALUE",
        help="e.g. --set heartbeat.enabled=true --set task_poll.roles=worker "
             "--set task_poll.bodies.worker='pull a task'",
    )
    p.set_defaults(func=_cmd_policy)

    p = msub.add_parser("history", help="print recent mesh messages")
    p.add_argument("mesh")
    p.add_argument("-n", type=int, default=50, help="how many (default 50)")
    p.set_defaults(func=_cmd_history)

    p = msub.add_parser(
        "mcp", help="run the stdio MCP server (send/members/history tools)"
    )
    p.set_defaults(func=_cmd_mcp)
