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
        tag = f"  (mirror of {m['primary']})" if m.get("primary") else ""
        print(
            f"{m['name']:<16} {len(m['members'])} member(s), "
            f"{m['messages']} message(s)"
            + tag
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
    if args.code:
        body["code"] = args.code
    member = client.post(f"/api/mesh/{args.mesh}/members", body)
    if member.get("pending"):
        # codeless remote join: the primary's operator has to approve it
        print(
            f"requested to join mesh {member['mesh']!r} on {member['primary']!r} "
            f"-- waiting for approval (request {member['request_id']})"
        )
        print(
            "the operator there approves with: claunch mesh approve "
            f"{member['mesh']} <id>   |   track: claunch mesh requests"
        )
        _print_relay(client.get("/api/daemon").get("relay"))
        return 0
    local = args.mesh.split("@")[0]  # 'dev@pca' is mounted locally as 'dev'
    print(
        f"joined mesh {local!r} as {member['handle']!r} "
        f"(role: {member['role']}, session: {member['session']})"
    )
    print(
        f"send: claunch mesh send {local} '*' \"...\"  |  "
        f"members: claunch mesh members {local}"
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
    if result.get("queued"):
        # mirror with its primary unreachable: durably queued, not yet sent
        print(f"queued {result.get('id')} -- primary daemon unreachable; "
              "will forward on reconnect")
        _print_relay(result.get("relay"))
        return 0
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
    if args.revoke:
        result = client.delete(f"/api/mesh/{args.mesh}/invites/{args.revoke}")
        print(f"revoked {result.get('revoked')} ticket(s) matching {args.revoke!r}")
        return 0
    if args.ls:
        tickets = client.get(f"/api/mesh/{args.mesh}/invites").get("invites", [])
        if not tickets:
            print(f"mesh {args.mesh!r} has no outstanding invite tickets")
        for t in tickets:
            print(
                f"{t['prefix']:<10} minted {t['created_at']}  "
                f"expires in {int(t['expires_in'] // 60)}m"
            )
        return 0
    result = client.post(f"/api/mesh/{args.mesh}/invite", {})
    print(
        f"invite ticket for mesh {args.mesh!r} (machine {result.get('machine')!r}), "
        f"single-use, valid {int(float(result.get('expires_in') or 0) // 3600)}h:"
    )
    print(result.get("code"))
    print(
        "a ticket only pre-approves the join -- redeem it on the other machine "
        f"with: claunch mesh join {args.mesh}@{result.get('machine')} "
        "--code <code>",
        file=sys.stderr,
    )
    _print_relay(result.get("relay"))
    return 0


def _pick(prompt: str, items: list) -> Optional[str]:
    """Numbered picker on stdin. Returns None on EOF/blank/invalid."""
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    try:
        raw = input(f"{prompt} [1-{len(items)}]: ").strip()
    except EOFError:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return items[int(raw) - 1]
    if raw in items:  # typing the name itself also works
        return raw
    return None


def _cmd_add(args: argparse.Namespace) -> int:
    """Owner-side wizard: enrol a session from another relay daemon."""
    client = daemon_client.ensure_running()
    machine = args.machine
    if not machine:
        if not sys.stdin.isatty():
            print("error: give MACHINE and SESSION, or run interactively",
                  file=sys.stderr)
            return 2
        peers = client.get("/api/relay/peers").get("peers", [])
        if not peers:
            print("no other daemons are registered on the relay")
            _print_relay(client.get("/api/daemon").get("relay"))
            return 1
        machine = _pick("machine", peers)
        if not machine:
            print("cancelled")
            return 1
    session = args.session
    if not session:
        if not sys.stdin.isatty():
            print("error: give SESSION as well (no prompt without a tty)",
                  file=sys.stderr)
            return 2
        listed = client.get(
            f"/api/relay/peers/{machine}/sessions"
        ).get("sessions", [])
        if not listed:
            print(f"daemon {machine!r} has no live sessions to enrol")
            return 1
        session = _pick(
            "session", [s["name"] for s in listed]
        )
        if not session:
            print("cancelled")
            return 1
    handle = args.handle
    if handle is None and sys.stdin.isatty() and not args.machine:
        # only prompt inside the full wizard flow; flags stay scriptable
        try:
            handle = input(f"handle [{session}]: ").strip() or ""
        except EOFError:
            handle = ""
    body = {"machine": machine, "session": session,
            "handle": handle or "", "role": args.role or ""}
    result = client.post(f"/api/mesh/{args.mesh}/invitations", body)
    member = result.get("member", {})
    print(
        f"added {member.get('handle')!r} (role: {member.get('role')}, "
        f"{machine}/{session}) to mesh {args.mesh!r}"
    )
    print("its daemon now mirrors the mesh; the member was briefed in its terminal")
    _print_relay(client.get("/api/daemon").get("relay"))
    return 0


def _cmd_peers(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    if not getattr(args, "mesh", None):
        payload = client.get("/api/relay/peers")
        peers = payload.get("peers", [])
        if not peers:
            print("no other daemons are registered on the relay")
        for name in peers:
            print(name)
        _print_relay(payload.get("relay"))
        return 0
    info = client.get(f"/api/mesh/{args.mesh}")
    peers = info.get("peers", [])
    if not peers:
        print(f"mesh {args.mesh!r} is local to this daemon -- no peers")
        _print_relay(info.get("relay"))
        return 0
    print(f"rank  machine       role       members")
    for p in peers:
        marks = []
        if p.get("self"):
            marks.append("this daemon")
        if p.get("ok") is False:
            marks.append(f"unreachable ({p.get('error')})")
        if p.get("queued"):
            marks.append(f"{p['queued']} queued")
        if p.get("linked") and not p.get("enabled"):
            marks.append("cut")
        print(
            f"{p['rank']:<5} {p['machine']:<13} {p['role']:<10} "
            f"{', '.join(p.get('members') or []) or '-'}"
            + (f"   [{'; '.join(marks)}]" if marks else "")
        )
    print(
        f"\nauthority: {info.get('authority')} (rank 0, epoch "
        f"{info.get('epoch', 0)}) -- move it with 'claunch mesh rank "
        f"{args.mesh} <machine> 0'"
    )
    _print_relay(info.get("relay"))
    return 0


def _cmd_rank(args: argparse.Namespace) -> int:
    """Move one peer to a position; everyone else keeps their relative order."""
    client = daemon_client.ensure_running()
    info = client.get(f"/api/mesh/{args.mesh}")
    order = [p["machine"] for p in info.get("peers", [])]
    if args.machine not in order:
        print(
            f"{args.machine!r} is not a peer of mesh {args.mesh!r} "
            f"(peers: {', '.join(order) or 'none'})",
            file=sys.stderr,
        )
        return 1
    position = max(0, min(args.position, len(order) - 1))
    order.remove(args.machine)
    order.insert(position, args.machine)
    result = client.put(
        f"/api/mesh/{args.mesh}/peers",
        {"order": order, "force": bool(args.force)},
    )
    print("rank order: " + " > ".join(result.get("peers") or order))
    if result.get("handover"):
        print(
            f"authority moved to {result.get('authority')} "
            f"(epoch {result.get('epoch')})"
        )
    return 0


def _cmd_cut(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    enabled = args.func is _cmd_uncut
    result = client.patch(
        f"/api/mesh/{args.mesh}/links/{args.a}/{args.b}", {"enabled": enabled}
    )
    verb = "restored" if result.get("enabled") else "cut"
    print(f"{verb} the direct link {result['a']} <-> {result['b']}")
    if not result.get("enabled"):
        print(
            "their traffic now always goes through the authority -- there is "
            "no direct hop while the link is cut"
        )
    return 0


def _cmd_uncut(args: argparse.Namespace) -> int:
    return _cmd_cut(args)


def _cmd_requests(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    if args.cancel:
        result = client.delete(f"/api/mesh/outgoing/{args.cancel}")
        print(f"cancelled outgoing join request {result.get('request_id')}")
        print(
            "note: the primary's operator still sees the request -- ask them "
            "to deny it",
            file=sys.stderr,
        )
        return 0
    payload = client.get("/api/mesh")
    shown = 0
    for m in payload.get("meshes", []):
        if args.mesh and m["name"] != args.mesh:
            continue
        for r in m.get("requests") or []:
            shown += 1
            print(
                f"in   {m['name']:<12} {r['id']:<10} {r['handle']!r} "
                f"({r['role']}) from {r['machine']}/{r['session']}  "
                f"{r['requested_at']}"
            )
    for r in payload.get("outgoing", []):
        if args.mesh and r["mesh"] != args.mesh:
            continue
        shown += 1
        print(
            f"out  {r['mesh']:<12} {r['request_id']:<10} as {r['handle']!r} "
            f"-> {r['primary']}  {r['requested_at']}"
        )
    if not shown:
        print("no pending join requests")
    else:
        print(
            "approve/deny an inbound request: claunch mesh approve|deny MESH ID",
            file=sys.stderr,
        )
    _print_relay(payload.get("relay"))
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    result = client.post(f"/api/mesh/{args.mesh}/requests/{args.request_id}/approve")
    state = "granted" if result.get("delivered") else "granted (grant queued -- " \
                                                       "retried until the guest is reachable)"
    print(
        f"approved {result['id']}: {result['handle']!r} on "
        f"{result['machine']} is now a member -- {state}"
    )
    return 0


def _cmd_deny(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    result = client.post(f"/api/mesh/{args.mesh}/requests/{args.request_id}/deny")
    print(f"denied join request {result['id']}")
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    result = client.delete(f"/api/mesh/{args.mesh}/guests/{args.machine}")
    removed = result.get("removed_members") or []
    print(
        f"revoked guest {result['machine']!r} from mesh {args.mesh!r} "
        f"({len(removed)} member(s) removed: {', '.join(removed) or '-'})"
    )
    print("its mirror is dropped as soon as that daemon is reachable")
    return 0


def _cmd_members(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    info = client.get(f"/api/mesh/{args.mesh}")
    primary = info.get("primary")
    if primary:
        print(f"(mirror of primary daemon {primary!r})")
    members = info.get("members", [])
    if not members:
        print(f"mesh {args.mesh!r} has no members yet")
    for m in members:
        # the roster is absolute: '' = the primary daemon's own member
        where = m.get("machine") or (primary if primary else "local")
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
        label = "primary daemon" if p.get("role") == "primary" else "guest daemon"
        print(f"{label} {p['machine']:<12} [{state}]{queued}")
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


def _cmd_roles(args: argparse.Namespace) -> int:
    """Show, upload or reset a mesh's role set."""
    client = daemon_client.ensure_running()
    if args.reset:
        payload = client.put(f"/api/mesh/{args.mesh}/roles", {"yaml": None})
        print(f"mesh {args.mesh!r} is back on the packaged role set")
    elif args.file:
        from pathlib import Path

        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return 1
        payload = client.put(f"/api/mesh/{args.mesh}/roles", {"yaml": text})
        print(f"mesh {args.mesh!r} role set updated (version "
              f"{payload.get('version')})")
    else:
        payload = client.get(f"/api/mesh/{args.mesh}/roles")
    if args.yaml:
        # Exactly what --file would accept back: `roles <m> --yaml > r.yaml`,
        # edit, `roles <m> --file r.yaml`.
        print(payload.get("yaml", "").rstrip())
        return 0
    source = "custom" if payload.get("custom") else "packaged default"
    print(f"role set: {source} (version {payload.get('version')}), "
          f"default role: {payload.get('default')}")
    if not payload.get("is_authority"):
        print(f"  owned by {payload.get('authority')} — an edit here is "
              f"forwarded there, then comes back to every daemon")
    print()
    print(f"{'role':<12} {'aliases':<44} members")
    for role in payload.get("roles", []):
        aliases = ", ".join(role.get("aliases") or []) or "-"
        held = ", ".join(role.get("members") or []) or "-"
        flag = " *" if role.get("stall_watch") else ""
        print(f"{role['name'] + flag:<12} {aliases[:43]:<44} {held}")
    if payload.get("orphans"):
        print()
        print("roles held by a member but no longer defined (uploads are not "
              "retroactive):")
        print("  " + ", ".join(payload["orphans"]))
    return 0


def _cmd_stance(args: argparse.Namespace) -> int:
    """Print the stance for a handle's role — the post-compaction recovery."""
    client = daemon_client.ensure_running()
    info = client.get(f"/api/mesh/{args.mesh}")
    handle = args.handle
    if not handle:
        session = _own_session(args)
        if not session:
            print("error: no session — pass --as HANDLE, or run inside a "
                  "claunch session (where $CLAUNCH_SESSION is set)",
                  file=sys.stderr)
            return 1
        for m in info.get("members", []):
            if m.get("session") == session and not m.get("machine"):
                handle = m.get("handle")
                break
        if not handle:
            print(f"error: session {session!r} is not a member of "
                  f"{args.mesh!r}", file=sys.stderr)
            return 1
    member = next(
        (m for m in info.get("members", []) if m.get("handle") == handle), None
    )
    if member is None:
        print(f"error: no member {handle!r} in mesh {args.mesh!r}",
              file=sys.stderr)
        return 1
    payload = client.get(f"/api/mesh/{args.mesh}/roles")
    role = next(
        (r for r in payload.get("roles", [])
         if r.get("name") == member.get("role")), None
    )
    print(f"# {handle} — role {member.get('role')} on mesh {args.mesh}")
    if role is None:
        print(f"\nThis mesh's role set no longer defines "
              f"{member.get('role')!r}, so there is no stance for it. You "
              f"keep the role you joined with (uploads are not retroactive); "
              f"ask the mesh's owner to reassign you.")
        return 0
    print()
    print((role.get("stance") or "(this role declares no stance)").rstrip())
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from . import mesh_mcp

    return mesh_mcp.serve()


def _cmd_install(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import mesh_install, profile as profile_mod

    if args.profile and args.project is not None:
        print("error: choose --profile or --project, not both", file=sys.stderr)
        return 1
    if args.profile:
        p = profile_mod.require(args.profile)
        done = mesh_install.install_into_profile(p)
    else:
        done = mesh_install.install_into_project(Path(args.project or ".").resolve())
    for line in done:
        print(f"installed: {line}")
    print("note: restart claude for the MCP server to be picked up")
    return 0


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
        "join",
        help="join a mesh -- MESH here, or MESH@MACHINE on another daemon "
             "(defaults to the current $CLAUNCH_SESSION)",
    )
    p.add_argument("mesh", metavar="MESH[@MACHINE]",
                   help="a local mesh, or 'mesh@machine' to join the mesh "
                        "owned by that machine's daemon (relay name)")
    p.add_argument("--as", dest="handle", metavar="HANDLE",
                   help="handle inside the mesh (default: the session name)")
    p.add_argument("--role", help="member role (default: inferred from the handle)")
    p.add_argument("--session", help="session to enrol (default: $CLAUNCH_SESSION)")
    p.add_argument("--code", help="invite ticket from 'claunch mesh invite' -- "
                                  "pre-approves the join; without one the "
                                  "request waits for the owner's approval")
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
        help="mint a single-use ticket that pre-approves one "
             "'mesh join MESH@THIS-MACHINE --code ...'",
    )
    p.add_argument("mesh")
    p.add_argument("--ls", action="store_true",
                   help="list outstanding tickets instead of minting one")
    p.add_argument("--revoke", metavar="PREFIX",
                   help="revoke outstanding tickets by the prefix shown in --ls")
    p.set_defaults(func=_cmd_invite)

    p = msub.add_parser(
        "add",
        help="wizard: enrol a session from another relay daemon into this "
             "mesh (owner side; no codes to carry)",
    )
    p.add_argument("mesh")
    p.add_argument("machine", nargs="?",
                   help="target daemon's relay name (omit to pick from a list)")
    p.add_argument("session", nargs="?",
                   help="session on that daemon (omit to pick from a list)")
    p.add_argument("--as", dest="handle", metavar="HANDLE", default=None,
                   help="handle inside the mesh (default: the session name)")
    p.add_argument("--role", help="member role (default: inferred from the handle)")
    p.set_defaults(func=_cmd_add)

    p = msub.add_parser(
        "peers",
        help="with a mesh: its daemons in rank order (rank 0 = the "
             "authority); without: the daemons registered on the relay",
    )
    p.add_argument("mesh", nargs="?", help="show this mesh's ranked graph")
    p.set_defaults(func=_cmd_peers)

    p = msub.add_parser(
        "rank",
        help="move a peer to a rank; position 0 hands it the authority",
    )
    p.add_argument("mesh")
    p.add_argument("machine")
    p.add_argument("position", type=int,
                   help="0 = authority, 1 = next, ... (clamped to the list)")
    p.add_argument("--force", action="store_true",
                   help="take the authority over from a daemon that is gone "
                        "for good (bumps the epoch; run this on the daemon "
                        "that should hold rank 0)")
    p.set_defaults(func=_cmd_rank)

    p = msub.add_parser(
        "cut",
        help="cut the direct link between two peers: their traffic falls "
             "back to the authority's fanout",
    )
    p.add_argument("mesh")
    p.add_argument("a", metavar="MACHINE-A")
    p.add_argument("b", metavar="MACHINE-B")
    p.set_defaults(func=_cmd_cut)

    p = msub.add_parser("uncut", help="restore a cut link between two peers")
    p.add_argument("mesh")
    p.add_argument("a", metavar="MACHINE-A")
    p.add_argument("b", metavar="MACHINE-B")
    p.set_defaults(func=_cmd_uncut)

    p = msub.add_parser(
        "requests",
        help="pending join requests: inbound (awaiting your approval) and "
             "outbound (awaiting theirs)",
    )
    p.add_argument("mesh", nargs="?", help="only this mesh (default: all)")
    p.add_argument("--cancel", metavar="ID",
                   help="forget one of our outbound requests")
    p.set_defaults(func=_cmd_requests)

    p = msub.add_parser("approve", help="admit a pending join request")
    p.add_argument("mesh")
    p.add_argument("request_id", metavar="ID")
    p.set_defaults(func=_cmd_approve)

    p = msub.add_parser("deny", help="reject a pending join request")
    p.add_argument("mesh")
    p.add_argument("request_id", metavar="ID")
    p.set_defaults(func=_cmd_deny)

    p = msub.add_parser(
        "revoke",
        help="unlink a guest daemon: drop its members and its mirror "
             "(machines are listed by 'mesh members')",
    )
    p.add_argument("mesh")
    p.add_argument("machine")
    p.set_defaults(func=_cmd_revoke)

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

    p = msub.add_parser(
        "roles",
        help="show, upload or reset the mesh's role set (the vocabulary its "
             "handles resolve into)",
    )
    p.add_argument("mesh")
    p.add_argument("--file", metavar="ROLES.YAML",
                   help="upload this YAML as the mesh's role set")
    p.add_argument("--reset", action="store_true",
                   help="drop the override and go back to the packaged roles")
    p.add_argument("--yaml", action="store_true",
                   help="print the set as YAML (round-trips into --file)")
    p.set_defaults(func=_cmd_roles)

    p = msub.add_parser(
        "stance",
        help="print your role's stance on a mesh (re-read it after a "
             "compaction or restart)",
    )
    p.add_argument("mesh")
    p.add_argument("--as", dest="handle",
                   help="whose stance (default: resolved from $CLAUNCH_SESSION)")
    p.add_argument("--session", help="session to resolve (default: "
                                     "$CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_stance)

    p = msub.add_parser("history", help="print recent mesh messages")
    p.add_argument("mesh")
    p.add_argument("-n", type=int, default=50, help="how many (default 50)")
    p.set_defaults(func=_cmd_history)

    p = msub.add_parser(
        "mcp", help="run the stdio MCP server (send/members/history tools)"
    )
    p.set_defaults(func=_cmd_mcp)

    p = msub.add_parser(
        "install",
        help="register the mesh MCP server + /mesh skill (project or profile)",
    )
    p.add_argument("--profile", help="install into this profile's config dir")
    p.add_argument("--project", nargs="?", const=".", default=None,
                   metavar="DIR",
                   help="install into a project (.mcp.json + .claude/skills; "
                        "default: current directory)")
    p.set_defaults(func=_cmd_install)
