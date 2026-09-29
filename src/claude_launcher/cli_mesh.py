"""``claunch mesh`` subcommands: group sessions and message between them.

Thin client of the daemon's ``/api/mesh`` surface. Inside a managed session
``$CLAUNCH_SESSION`` identifies the caller, so an agent can run
``claunch mesh join dev`` / ``claunch mesh send dev '*' "..."`` bare — no
pane ids, no explicit self-identification. Every command ends with a relay
status line so it is always visible whether the mesh can span machines.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional
from urllib.parse import quote

from . import daemon_client, projects, stdio

#: The roster partitions ``--state`` accepts, spelled here rather than
#: imported: ``daemon.mesh`` pulls aiohttp in, and this module is loaded by
#: every ``claunch mesh`` invocation. ``test_mesh_cli_owed`` pins the two
#: lists equal, so a partition added there and not here is a test failure.
MEMBER_STATES = (
    "all", "current", "running", "remote",
    "killed", "paused", "archived", "missing",
)


def relay_line(relay: Optional[dict]) -> str:
    """One-line relay connectivity summary, printed all over the CLI.

    With several relays configured the line still answers the one question it
    always answered — can this machine reach the others right now — and adds
    the count, because "connected" there means at least one relay is up and
    the operator cannot see from the old wording that another is down.
    """
    # ASCII only: this line goes through redirected stdio on cp949 consoles.
    if not relay or not relay.get("configured"):
        return "relay: not configured -- sessions/mesh reachable on this machine only"
    total = int(relay.get("count") or 1)
    live = int(relay.get("connected_count") or (1 if relay.get("connected") else 0))
    if total > 1:
        suffix = f" [{live}/{total} relays: {_relay_names(relay)}]"
    else:
        suffix = ""
    if relay.get("connected"):
        return f"relay: connected as {relay.get('name')!r}{suffix}"
    return (
        f"relay: DISCONNECTED (registered name {relay.get('name')!r}) -- "
        f"remote machines unreachable{suffix}"
    )


def _relay_names(relay: dict) -> str:
    """``home=up, work=down`` — per-relay state behind the aggregate."""
    rows = relay.get("relays")
    if not isinstance(rows, list):
        return ""
    return ", ".join(
        f"{row.get('id')}={'up' if row.get('connected') else 'down'}"
        for row in rows
        if isinstance(row, dict)
    )


def _print_relay(relay: Optional[dict]) -> None:
    print(relay_line(relay), file=sys.stderr)


def _own_session(args: argparse.Namespace) -> Optional[str]:
    """The session this command speaks for: --session, else $CLAUNCH_SESSION."""
    return getattr(args, "session", None) or os.environ.get("CLAUNCH_SESSION")


def _mesh_as_me(client, mesh: str, session: str = "") -> dict:
    """The mesh document, with ``you`` resolved when a session is named.

    Which member a session is, is the daemon's to answer: the rule is
    ``is_local_member``, and its blank-``machine`` case means opposite things
    on an authority and on a mirror. Matching on blankness here finds nobody
    on a mirror, where our own members are always stamped. That asymmetry is
    why ``send`` has always worked from a bare session — it resolves server
    side — while the commands that identified themselves did not.
    """
    q = f"?session={quote(session)}" if session else ""
    return client.get(f"/api/mesh/{mesh}{q}")


def _cmd_create(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    body = {"name": args.mesh}
    if getattr(args, "project", None):
        body["project"] = args.project
    info = client.post("/api/mesh", body)
    where = f" in project {info['project']!r}" if info.get("project") else ""
    print(f"created mesh {info['name']!r}{where}")
    _print_relay(client.get("/api/daemon").get("relay"))
    return 0


def _cmd_ls(_args: argparse.Namespace) -> int:
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        if not daemon_client.is_absent(why):
            # An unconfirmed look is not an empty roster; see _cmd_sessions.
            print(
                f"{daemon_client.unreachable_reason(why)}; the mesh list was "
                f"not read",
                file=sys.stderr,
            )
            return 1
        print(f"{daemon_client.unreachable_reason(why)}; no meshes")
        return 0
    # Same narrowing as ``claunch sessions``: inside a managed session the
    # list is that session's project unless --project says otherwise.
    scope = projects.listing_scope(
        getattr(_args, "project", None),
        environ=os.environ,
        fetch_own=lambda name: client.get(f"/api/sessions/{name}").get("project"),
    )
    project_filter = scope.project
    query = "" if scope.own or not project_filter else f"?project={quote(project_filter)}"
    payload = client.get(f"/api/mesh{query}")
    everything = payload.get("meshes", [])
    meshes = [m for m in everything if projects.matches(m.get("project"), project_filter)]
    footer = projects.scope_footer(
        scope, hidden=len(everything) - len(meshes),
        noun="mesh", command="claunch mesh ls",
    )
    if not meshes:
        if project_filter:
            print(f"no meshes in project {project_filter!r}")
        else:
            print("no meshes; create one with 'claunch mesh create <name>'")
    for m in meshes:
        tag = f"  (mirror of {m['primary']})" if m.get("primary") else ""
        # The project column is drawn only on the unfiltered list, so a
        # one-project view reads exactly as the list always did.
        proj = "" if project_filter else f"{m.get('project') or 'default':<10} "
        print(
            f"{m['name']:<16} {proj}{len(m['members'])} member(s), "
            f"{m['messages']} message(s)"
            + tag
        )
    if footer:
        print(footer)
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
    if getattr(args, "subroles", None):
        body["subroles"] = list(args.subroles)
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
        f"(role: {_role_label(member)}, session: {member['session']})"
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
        handle = _mesh_as_me(client, args.mesh, session).get("you")
        if not handle:
            print(
                f"error: session {session!r} is not a member of mesh {args.mesh!r}",
                file=sys.stderr,
            )
            return 1
    client.delete(f"/api/mesh/{args.mesh}/members/{handle}")
    print(f"left mesh {args.mesh!r} (handle {handle!r})")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    text = stdio.read_stdin() if args.text == ["-"] else " ".join(args.text)
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
    # Recipients with no terminal left to read this. Named on the RESULT
    # line, not only in the notice: "sent ... to bob" is the sentence a
    # sender acts on, and it is the one that is wrong when bob is dead.
    dead = [str(e.get("handle")) for e in (result.get("undeliverable") or [])]
    # Recipients the mesh REFUSED for: their backlog is at the cap, so this
    # message was not queued for them and never will be. Named on the result
    # line for the same reason `dead` is — "sent to a, b, c" is what the
    # sender acts on, and it is a lie about c. A send refused for EVERY
    # recipient is a 429 and never reaches this line at all.
    busy = [str(e.get("handle")) for e in (result.get("deferred") or [])]
    if result.get("queued"):
        # mirror with its primary unreachable: durably queued, not yet sent
        line = (f"queued {result.get('id')} -- primary daemon unreachable; "
                "will forward on reconnect")
        if dead:
            line += f"; NOT READING: {', '.join(dead)}"
        print(line)
        _print_notice(result)
        _print_relay(result.get("relay"))
        return 0
    recipients = result.get("recipients", [])
    queued = result.get("queued_remote", [])
    line = f"sent {result.get('id')} to {', '.join(recipients) or '(nobody)'}"
    if args.type != "say":
        line += f" [{args.type}]"
    if queued:
        line += f" -- queued for remote: {', '.join(queued)}"
    if dead:
        line += f" -- NOT READING: {', '.join(dead)} (see notice)"
    if busy:
        line += f" -- REFUSED (backlog full): {', '.join(busy)} (see notice)"
    print(line)
    _print_notice(result)
    _print_relay(result.get("relay"))
    return 0


def _print_notice(result: dict) -> None:
    if result.get("notice"):
        print(f"notice: {result['notice']}", file=sys.stderr)


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
    if getattr(args, "subroles", None):
        body["subroles"] = list(args.subroles)
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


def _cmd_connect(args: argparse.Namespace) -> int:
    """Rewire the *member* graph — who may message whom inside the mesh.

    One layer up from cut/uncut, which move daemons' traffic around without
    changing who can reach whom. A disconnected member pair has no fallback:
    members are not routed, so the send is simply refused.
    """
    client = daemon_client.ensure_running()
    enabled = args.func is _cmd_connect
    result = client.patch(
        f"/api/mesh/{args.mesh}/members/{args.a}/links/{args.b}",
        {"enabled": enabled},
    )
    verb = "connected" if result.get("enabled") else "disconnected"
    print(f"{verb} {result['a']} <-> {result['b']}")
    granted = result.get("granted")
    if granted:
        print(
            f"this answers a wire request: {granted['by']} had asked to reach "
            f"{granted['other']} {granted['asks']}x and has been told"
        )
    if not result.get("enabled"):
        print(
            "they can no longer message each other -- sends between them are "
            "refused, and '*' from either one skips the other"
        )
    return 0


def _cmd_disconnect(args: argparse.Namespace) -> int:
    return _cmd_connect(args)


def _cmd_wire_requests(args: argparse.Namespace) -> int:
    """Standing asks for edges the member graph does not have.

    Deliberately not spelled ``requests``: that subcommand is the join queue
    (a daemon asking to enrol a session), and two different approvals under
    one word is how an operator grants the wrong one.
    """
    client = daemon_client.ensure_running()
    if args.decline:
        a, b = args.decline
        row = client.post(
            f"/api/mesh/{args.mesh}/wire-requests/decline",
            {"a": a, "b": b, "reason": args.reason or ""},
        )
        if row.get("already"):
            print(f"already {row['already']}: {row['a']} <-> {row['b']}")
            return 0
        print(f"declined {row['a']} <-> {row['b']}")
        print(f"{row['by']} has been told, and will not be asked again")
        return 0
    rows = client.get(
        f"/api/mesh/{args.mesh}/wire-requests"
        + (f"?state={args.state}" if args.state else "")
    ).get("requests") or []
    if not rows:
        print(f"no wire requests in mesh {args.mesh!r}")
        return 0
    for row in rows:
        other = row["b"] if row["by"] == row["a"] else row["a"]
        detail = f"[{row['state']}] {row['by']} -> {other}  asked {row['count']}x"
        if row["state"] == "open":
            detail += (
                f", with {row['approver']}" if row.get("approver")
                else ", NOBODY ASKED (no session here commands either end)"
            )
        else:
            detail += f" by {row.get('decided_by') or 'an operator'}"
            if row.get("reason"):
                detail += f": {row['reason']}"
        print(detail)
    print()
    print("grant one with: claunch mesh connect MESH A B")
    return 0


def _cmd_rewire(args: argparse.Namespace) -> int:
    """Apply the mesh's auto_link rules to the members already in it.

    A join wires the member that is joining; a rule that arrives afterwards
    has no join left to run in. This is that missing run — for the fleet
    already assembled when the rule (or a new packaged default) showed up.

    It only opens, and it skips every pair somebody already decided, so a
    link cut on purpose stays cut and a second run is a no-op. Those two
    properties are what make it safe to run, not the CLI being a human's
    surface: it sends no ``actor``, so it asks for the whole graph, and
    every edge it can open is one the mesh's own rules already name.
    """
    client = daemon_client.ensure_running()
    opened = (client.post(f"/api/mesh/{args.mesh}/rewire", {}) or {}).get(
        "opened"
    ) or []
    if not opened:
        print(
            "nothing to open -- every pair the rules name is already "
            "connected, or was decided by hand"
        )
        return 0
    print(f"opened {len(opened)} edge(s):")
    for edge in opened:
        print(f"  {edge['a']} <-> {edge['b']}")
    return 0


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
            if r.get("attach"):
                print(
                    f"in   {m['name']:<12} {r['id']:<10} daemon attach "
                    f"from {r['machine']}  {r['requested_at']}"
                )
                continue
            print(
                f"in   {m['name']:<12} {r['id']:<10} {r['handle']!r} "
                f"({r['role']}) from {r['machine']}/{r['session']}  "
                f"{r['requested_at']}"
            )
    for r in payload.get("outgoing", []):
        if args.mesh and r["mesh"] != args.mesh:
            continue
        shown += 1
        who = "daemon attach" if r.get("attach") else f"as {r['handle']!r}"
        print(
            f"out  {r['mesh']:<12} {r['request_id']:<10} {who} "
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


def _cmd_attach(args: argparse.Namespace) -> int:
    """Daemon-level join: this daemon attaches mesh@machine, no member."""
    client = daemon_client.ensure_running()
    body = {"code": args.code} if args.code else {}
    if args.project:
        body["project"] = args.project
    result = client.post(f"/api/mesh/{args.mesh}/attach", body)
    if result.get("pending"):
        print(
            f"requested to attach mesh {result['mesh']!r} on "
            f"{result['primary']!r} -- waiting for approval (request "
            f"{result['request_id']})"
        )
        print(
            "the operator there approves with: claunch mesh approve "
            f"{result['mesh']} <id>   |   track: claunch mesh requests"
        )
        _print_relay(client.get("/api/daemon").get("relay"))
        return 0
    name = result["mesh"]
    print(
        f"mesh {name!r} is {'already ' if result.get('already') else ''}"
        f"attached here (mirror of {result['primary']!r}, "
        f"{result['members']} member(s), project {result.get('project') or 'default'!r})"
    )
    print(f"sessions here join it with: claunch mesh join {name}")
    return 0


def _cmd_detach(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    q = "?force=1" if args.force else ""
    result = client.delete(f"/api/mesh/{args.mesh}/attach{q}")
    print(f"detached mesh {result['mesh']!r} from {result['primary']!r}")
    if not result.get("notified"):
        print(
            f"note: {result['primary']!r} was not told -- its operator can "
            "revoke this daemon there",
            file=sys.stderr,
        )
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    """Meshes this daemon could attach (one relay hop, all relays)."""
    client = daemon_client.ensure_running()
    doc = client.get("/api/relay/meshes")
    rows = doc.get("meshes") or []
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    for r in rows:
        members = r.get("members")
        print(
            f"{r['mesh'] + '@' + r['machine']:<32} {r['state']:<10} "
            f"{r['access']:<8} "
            f"{'-' if members is None else members:>3} member(s)  "
            f"{r.get('project') or ''}"
        )
    if not rows:
        print("no mesh is published to this daemon")
    for where, why in sorted((doc.get("errors") or {}).items()):
        print(f"note: {where}: {why}", file=sys.stderr)
    if rows:
        print(
            "attach one with: claunch mesh attach MESH@MACHINE "
            "(access 'offer' is pre-approved; 'approval' waits for its owner)",
            file=sys.stderr,
        )
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    """Show or change the project a mesh is filed under here."""
    client = daemon_client.ensure_running()
    if args.project is None:
        info = client.get(f"/api/mesh/{args.mesh}?state=running")
        print(info.get("project") or "default")
        return 0
    result = client.put(f"/api/mesh/{args.mesh}/project", {"project": args.project})
    print(f"mesh {result['mesh']!r} is now filed under project {result['project']!r}")
    return 0


def _cmd_visibility(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    if not args.visibility:
        info = client.get(f"/api/mesh/{args.mesh}?state=running")
        if info.get("primary"):
            print(f"mesh {args.mesh!r} is a mirror -- its owner publishes it")
            return 0
        print(info.get("visibility") or "private")
        for machine in info.get("offers") or []:
            print(f"offered to: {machine}")
        return 0
    result = client.put(
        f"/api/mesh/{args.mesh}/visibility", {"visibility": args.visibility}
    )
    print(f"mesh {result['mesh']!r} is now {result['visibility']}")
    for machine in result.get("withdrawn") or []:
        print(f"withdrew the offer to {machine}")
    return 0


def _cmd_offer(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    if args.cancel:
        result = client.delete(f"/api/mesh/{args.mesh}/offers/{args.machine}")
        print(f"withdrew the offer of {result['mesh']!r} to {result['machine']!r}")
        return 0
    result = client.post(
        f"/api/mesh/{args.mesh}/offers", {"machine": args.machine}
    )
    print(
        f"offered mesh {result['mesh']!r} to {result['machine']!r} "
        f"(visibility: {result['visibility']}) -- it attaches with no approval"
    )
    return 0


def _cmd_rename_peer(args: argparse.Namespace) -> int:
    """A relay daemon was renamed: migrate every reference to it here."""
    client = daemon_client.ensure_running()
    result = client.post(
        f"/api/relay/peers/{args.old}/rename", {"new": args.new}
    )
    meshes = result.get("meshes") or []
    print(
        f"renamed daemon {result['old']!r} -> {result['new']!r} in "
        f"{len(meshes)} mesh(es): {', '.join(meshes) or '-'}"
    )
    for r in result.get("rekeyed") or []:
        print(f"mirror {r['from']} is now {r['to']}")
    return 0


def _role_label(member: dict) -> str:
    """``leader+reviewer`` — a member's primary role and its subroles."""
    roles = member.get("roles")
    if isinstance(roles, list) and roles:
        return "+".join(str(r) for r in roles)
    return str(member.get("role") or "")


def _cmd_subroles(args: argparse.Namespace) -> int:
    """Show or change a member's subroles."""
    client = daemon_client.ensure_running()
    handle = args.handle or _own_session(args)
    if not handle:
        print(
            "error: no handle — name one, or run inside a claunch session",
            file=sys.stderr,
        )
        return 1
    if args.set is not None or args.add or args.remove:
        body: dict = {}
        if args.set is not None:
            body["set"] = [s for s in args.set.split(",") if s.strip()]
        if args.add:
            body["add"] = list(args.add)
        if args.remove:
            body["remove"] = list(args.remove)
        member = client.patch(
            f"/api/mesh/{args.mesh}/members/{handle}/subroles", body
        )
    else:
        info = client.get(f"/api/mesh/{args.mesh}")
        member = next(
            (m for m in info.get("members", []) if m.get("handle") == handle),
            None,
        )
        if member is None:
            print(f"error: no member {handle!r} in mesh {args.mesh!r}",
                  file=sys.stderr)
            return 1
    subs = member.get("subroles") or []
    print(
        f"{member.get('handle')} in mesh {args.mesh!r}: role "
        f"{member.get('role')}, subroles: {', '.join(subs) or '-'}"
    )
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
        flags = f" pending:{pending}" if pending else ""
        # 'owed' is the mirror image of 'pending': mail the member HAS seen
        # and has said nothing about ('claunch mesh owed' shows which).
        if m.get("owed"):
            flags += f" owed:{m['owed']}"
        print(
            f"{m['handle']:<16} {_role_label(m):<10} {where + '/' + m['session']:<28} "
            f"[{m['reachability']}]{flags}"
        )
    # The open pairs are printed, not the closed ones: a join wires a member
    # to its parent and to whatever the mesh's rules match and leaves the rest
    # closed, so "can message" is the short list and the one somebody chose.
    # (The dashboard's diagram makes the same call, for the same reason.)
    links = [e for e in info.get("member_links") or [] if e.get("enabled")]
    if links:
        print(f"\nconnected pairs ({len(links)}):")
        for edge in links:
            print(f"  {edge['a']} <-> {edge['b']}")
    for p in info.get("peers", []):
        if p.get("self"):
            continue  # this daemon: its members are the rows above
        if p.get("ok") is False:
            state = f"unreachable ({p.get('error')})"
        elif p.get("ok"):
            state = "ok"
        else:
            state = "linked (no traffic yet)"
        queued = f" -- {p['queued']} message(s) queued" if p.get("queued") else ""
        label = (
            "authority daemon" if p.get("role") in ("authority", "primary")
            else "peer daemon"
        )
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
    legend = False
    for role in payload.get("roles", []):
        aliases = ", ".join(role.get("aliases") or []) or "-"
        held = ", ".join(role.get("members") or []) or "-"
        flag = (" *" if role.get("stall_watch") else "") + (
            "!" if role.get("exclusive") else ""
        )
        legend = legend or bool(role.get("exclusive"))
        print(f"{role['name'] + flag:<12} {aliases[:43]:<44} {held}")
    if legend:
        print("  (! = exclusive: at most one live holder per mesh)")
    if payload.get("orphans"):
        print()
        print("roles held by a member but no longer defined (uploads are not "
              "retroactive):")
        print("  " + ", ".join(payload["orphans"]))
    return 0


def _cmd_stance(args: argparse.Namespace) -> int:
    """Print the stance for a handle's role — the post-compaction recovery."""
    client = daemon_client.ensure_running()
    handle = args.handle
    session = "" if handle else (_own_session(args) or "")
    if not handle and not session:
        print("error: no session — pass --as HANDLE, or run inside a "
              "claunch session (where $CLAUNCH_SESSION is set)",
              file=sys.stderr)
        return 1
    info = _mesh_as_me(client, args.mesh, session)
    if not handle:
        handle = info.get("you")
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
    from .cli import run_install

    print("note: 'mesh install' is now 'claunch install'; installing every "
          "skill and the merged MCP server")
    return run_install(args.profile, args.project, args.global_, args.all_)


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


def _need_session(args: argparse.Namespace) -> Optional[str]:
    session = _own_session(args)
    if not session:
        print("error: no session -- pass --session, or run inside a claunch "
              "session (where $CLAUNCH_SESSION is set)", file=sys.stderr)
    return session


#: How a peer operation may be addressed, said once for both subcommands.
_TARGET_DOC = (
    "a handle, a member's session name, or <machine>/<session> when two daemons run a session of that name"
)

def _cmd_ops_file(args: argparse.Namespace) -> int:
    session = _need_session(args)
    if not session:
        return 1
    client = daemon_client.ensure_running()
    payload = {"actor": session, "member": args.member, "path": args.path}
    if args.max_bytes:
        payload["max_bytes"] = args.max_bytes
    result = client.post(f"/api/mesh/{args.mesh}/ops/file", payload)
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(
        f"# {result.get('member')}@{result.get('machine') or 'local'}:"
        f"{result.get('path')}  {result.get('size')} bytes  "
        f"sha256 {str(result.get('sha256'))[:12]}"
        f"{'  (truncated)' if result.get('truncated') else ''}"
        f"{'  [base64]' if result.get('encoding') == 'base64' else ''}",
        file=sys.stderr,
    )
    sys.stdout.write(str(result.get("content") or ""))
    return 0


def _cmd_ops_git(args: argparse.Namespace) -> int:
    session = _need_session(args)
    if not session:
        return 1
    client = daemon_client.ensure_running()
    gargs = {}
    if args.base:
        gargs["base"] = args.base
    if args.head:
        gargs["head"] = args.head
    if args.ref:
        gargs["ref"] = args.ref
    if args.range:
        gargs["range"] = args.range
    if args.n:
        gargs["n"] = args.n
    if args.stat:
        gargs["stat"] = True
    if args.cached:
        gargs["cached"] = True
    if args.paths:
        gargs["paths"] = list(args.paths)
    result = client.post(
        f"/api/mesh/{args.mesh}/ops/git",
        {"actor": session, "member": args.member, "op": args.op, "args": gargs},
    )
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(
        f"# {result.get('member')}@{result.get('machine') or 'local'}: "
        f"git {' '.join(result.get('argv') or [])}  rc={result.get('rc')}"
        f"{'  (truncated)' if result.get('truncated') else ''}",
        file=sys.stderr,
    )
    sys.stdout.write(str(result.get("output") or ""))
    return 0 if result.get("rc") == 0 else 1


def _cmd_lease(args: argparse.Namespace) -> int:
    session = _need_session(args)
    if not session:
        return 1
    client = daemon_client.ensure_running()
    op = args.op
    if op in ("ls", "list"):
        q = f"?session={quote(session)}"
        if args.key:
            q += f"&holder={quote(args.key)}"
        result = client.get(f"/api/mesh/{args.mesh}/leases{q}")
        if args.json:
            import json

            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        leases = result.get("leases") or []
        if not leases:
            print("no live leases")
            return 0
        import time

        now = time.time()
        for lease in leases:
            left = int(float(lease.get("expires_at") or 0) - now)
            note = f"  {lease['note']}" if lease.get("note") else ""
            print(f"{lease['key']:<40} {lease['holder']:<12} "
                  f"{_fmt_age(max(left, 0))} left{note}")
        return 0
    if not args.key:
        print("error: KEY is required", file=sys.stderr)
        return 1
    payload = {"actor": session, "op": op, "key": args.key}
    if args.ttl:
        payload["ttl"] = args.ttl
    if args.note:
        payload["note"] = args.note
    result = client.post(f"/api/mesh/{args.mesh}/leases", payload)
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if result.get("ok"):
        lease = result.get("lease")
        if lease:
            import time

            left = int(float(lease.get("expires_at") or 0) - time.time())
            print(f"{op}: {lease['key']} held by {lease['holder']} "
                  f"for {_fmt_age(max(left, 0))}")
        else:
            print(f"{op}: {args.key} "
                  f"{'released' if result.get('released') else result.get('reason', 'ok')}")
        return 0
    lease = result.get("lease") or {}
    print(f"{op}: {args.key} is HELD by {result.get('held_by')}"
          f"{' -- ' + lease['note'] if lease.get('note') else ''} "
          f"(until {lease.get('expires_at')})", file=sys.stderr)
    return 2


def _fmt_age(secs) -> str:
    if secs is None:
        return "?"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _cmd_owed(args: argparse.Namespace) -> int:
    """Unanswered mail, per member — the terminal form of the web dashboard.

    Narrowed at the daemon rather than here. Building a local row walks the
    message log twice, so an unnarrowed report is members times messages:
    mesh-0826 answered in 4.2s over 251 members and 27708 messages, and it
    is synchronous, so for those seconds the daemon answers nothing else --
    every terminal it is pumping stops with it. ``--state`` picks the
    partition; the default leaves out the sessions that have ended, which
    are also the ones whose debt nobody can act on.
    """
    client = daemon_client.ensure_running()
    query = f"?state={args.state}"
    if args.handle:
        # By name, so the state filter is not applied at all: a member asked
        # for by name and missing would otherwise read as "no such member".
        query = f"?handle={quote(args.handle)}"
    report = client.get(f"/api/mesh/{args.mesh}/owed{query}")
    rows = report.get("members", [])
    if args.handle and not rows:
        print(f"no member {args.handle!r} in mesh {args.mesh!r}", file=sys.stderr)
        return 1
    if not report.get("owed"):
        print(f"mesh {args.mesh!r}: nobody owes an answer")
    for r in rows:
        owed = r.get("owed")
        if not owed and not args.handle:
            continue  # the point of the command is who is silent
        head = (
            f"{r['handle']:<16} {r['role']:<10} "
            f"owed:{'?' if owed is None else owed}"
        )
        if r.get("pending"):
            head += f" undelivered:{r['pending']}"
        if r.get("oldest_age") is not None:
            head += f" oldest:{_fmt_age(r['oldest_age'])}"
        if not r.get("local"):
            # counted by the member's own daemon; the messages stay there
            head += f" (reported by {r['machine']}"
            head += ", STALE)" if r.get("stale") else ")"
        print(head)
        for m in r.get("messages", []):
            intent = m.get("type") or "say"
            body = " ".join(str(m.get("body") or "").split())
            print(
                f"    {m.get('id')} {_fmt_age(m.get('age')):>6} ago  "
                f"from {m.get('from')} [{intent}]: {body}"
            )
    counts = report.get("member_counts") or {}
    shown = len(rows)
    total = counts.get("all")
    if not args.handle and isinstance(total, int) and total > shown:
        print(
            f"note: {shown} of {total} members, state={args.state} "
            f"('--state all' for every member)"
        )
    hb = report.get("heartbeat") or {}
    if report.get("owed") and not hb.get("enabled"):
        # A debt nobody is chasing: worth saying, because the obvious reading
        # of this list is "the daemon is on it".
        print(
            "note: the heartbeat nudge is OFF for this mesh -- nothing will "
            f"chase these ('claunch mesh policy {args.mesh} heartbeat.enabled=true')"
        )
    return 0


def register(sub) -> None:
    p_mesh = sub.add_parser(
        "mesh", help="group sessions into a mesh and message between them"
    )
    msub = p_mesh.add_subparsers(dest="mesh_command", required=True)

    p = msub.add_parser("create", help="create a mesh")
    p.add_argument("mesh")
    p.add_argument(
        "--project", "-P", metavar="NAME",
        help="file the mesh under this project ('claunch project ls' lists "
        "them; default: the 'default' project)",
    )
    p.set_defaults(func=_cmd_create)

    p = msub.add_parser("ls", aliases=["list"], help="list meshes")
    p.add_argument(
        "--project", "-P", metavar="NAME",
        help="only the meshes filed under this project; inside a managed "
             "session the list is that session's project unless this says "
             "otherwise, and 'all' lists every project",
    )
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
    p.add_argument(
        "--subrole", dest="subroles", action="append", metavar="ROLE",
        help="a further role this member also answers for (repeatable) — "
             "e.g. a leader that is also a reviewer; workflows and the "
             "policy engine find it under either name",
    )
    p.add_argument("--session", help="session to enrol (default: $CLAUNCH_SESSION)")
    p.add_argument("--code", help="invite ticket from 'claunch mesh invite' -- "
                                  "pre-approves the join; without one the "
                                  "request waits for the owner's approval")
    p.set_defaults(func=_cmd_join)

    p = msub.add_parser(
        "owed",
        help="who was asked something and answered nothing (per message)",
    )
    p.add_argument("mesh")
    p.add_argument("--handle", metavar="HANDLE",
                   help="one member only (shown even when it owes nothing, "
                        "and whatever state its session is in)")
    p.add_argument("--state", default="current", choices=MEMBER_STATES,
                   help="which members to read (default: current -- running "
                        "and remote; a report over every member costs a walk "
                        "of the message log per member)")
    p.set_defaults(func=_cmd_owed)

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
    p.add_argument(
        "to",
        help="'*' (every connected member -- warned), '@in_review' (members "
        "whose issue is in_review on your board), or a handle",
    )
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
                        "delivered only its own slice (repeatable). A "
                        "recipient with no --section of its own still "
                        "receives the send -- the preamble alone -- so make "
                        "the preamble a message that stands on its own, or "
                        "leave those handles out of the address")
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
    p.add_argument(
        "--subrole", dest="subroles", action="append", metavar="ROLE",
        help="a further role the member also answers for (repeatable)",
    )
    p.set_defaults(func=_cmd_add)

    p = msub.add_parser(
        "subroles",
        help="show or change a member's subroles — the roles it answers for "
             "besides its primary one (e.g. a leader that is also a reviewer)",
    )
    p.add_argument("mesh")
    p.add_argument("handle", nargs="?",
                   help="member handle (default: this session)")
    p.add_argument("--add", action="append", metavar="ROLE",
                   help="take this role as a subrole (repeatable)")
    p.add_argument("--remove", action="append", metavar="ROLE",
                   help="drop this subrole (repeatable)")
    p.add_argument("--set", metavar="ROLE[,ROLE...]",
                   help="replace the whole subrole list ('' clears it)")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_subroles)

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
        "connect",
        help="let two MEMBERS message each other (the member graph, not the "
             "peer-daemon links cut/uncut edit)",
    )
    p.add_argument("mesh")
    p.add_argument("a", metavar="HANDLE-A")
    p.add_argument("b", metavar="HANDLE-B")
    p.set_defaults(func=_cmd_connect)

    p = msub.add_parser(
        "disconnect",
        help="stop two members messaging each other; sends between them are "
             "refused (members are never routed around a cut)",
    )
    p.add_argument("mesh")
    p.add_argument("a", metavar="HANDLE-A")
    p.add_argument("b", metavar="HANDLE-B")
    p.set_defaults(func=_cmd_disconnect)

    p = msub.add_parser(
        "wire-requests",
        help="members that were refused a peer and are waiting on an edge; "
             "grant one with 'connect', or --decline it",
    )
    p.add_argument("mesh")
    p.add_argument("--state", choices=["open", "granted", "declined"],
                   help="show only requests in this state")
    p.add_argument("--decline", nargs=2, metavar=("HANDLE-A", "HANDLE-B"),
                   help="answer this pair with no, once and for good")
    p.add_argument("--reason", default="",
                   help="why, carried to the requester and into the refusal "
                        "it gets if it asks again")
    p.set_defaults(func=_cmd_wire_requests)

    p = msub.add_parser(
        "rewire",
        help="apply the mesh's auto_link rules to the members already in it "
             "(opens only; a pair decided by hand is left alone)",
    )
    p.add_argument("mesh")
    p.set_defaults(func=_cmd_rewire)

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

    p = msub.add_parser(
        "attach",
        help="attach THIS DAEMON to a remote mesh with no member of its own; "
             "its sessions then join by the bare name without approval",
    )
    p.add_argument("mesh", metavar="MESH@MACHINE")
    p.add_argument("--code", help="invite ticket from 'claunch mesh invite' "
                                  "(an offer pushed to this daemon needs none)")
    p.add_argument("--project", "-P", metavar="NAME",
                   help="file the mirror here under this project (default: "
                        "the 'default' project; 'claunch mesh project' moves it)")
    p.set_defaults(func=_cmd_attach)

    p = msub.add_parser(
        "detach",
        help="undo 'attach': the owner drops this daemon and its members, "
             "and the mirror here is removed",
    )
    p.add_argument("mesh")
    p.add_argument("--force", action="store_true",
                   help="drop the mirror even when the owner cannot be told "
                        "(unreachable, or refusing a link it no longer knows)")
    p.set_defaults(func=_cmd_detach)

    p = msub.add_parser(
        "rename-peer",
        help="a relay daemon was renamed: rewrite every mesh reference to "
             "it here (mirrors dev@OLD become dev@NEW). A renamed daemon "
             "tells its peers itself when it reconnects; this is for when "
             "it could not",
    )
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=_cmd_rename_peer)

    p = msub.add_parser(
        "discover",
        help="meshes this daemon could attach: public ones owned by daemons "
             "on every connected relay, plus offers pushed here (one hop)",
    )
    p.add_argument("--json", action="store_true", help="raw JSON")
    p.set_defaults(func=_cmd_discover)

    p = msub.add_parser(
        "project",
        help="show or change the project a mesh (own or mirrored) is filed "
             "under on this daemon; the peers are not affected",
    )
    p.add_argument("mesh")
    p.add_argument("project", nargs="?")
    p.set_defaults(func=_cmd_project)

    p = msub.add_parser(
        "visibility",
        help="show or set who may discover a mesh you own: private | "
             "public (every relay daemon) | invited (offered daemons only)",
    )
    p.add_argument("mesh")
    p.add_argument("visibility", nargs="?", choices=("private", "public", "invited"))
    p.set_defaults(func=_cmd_visibility)

    p = msub.add_parser(
        "offer",
        help="offer a mesh you own to one relay daemon: it is listed there "
             "and attaches with no approval",
    )
    p.add_argument("mesh")
    p.add_argument("machine")
    p.add_argument("--cancel", action="store_true", help="withdraw the offer")
    p.set_defaults(func=_cmd_offer)

    p = msub.add_parser("members", help="list a mesh's members and reachability")
    p.add_argument("mesh")
    p.set_defaults(func=_cmd_members)

    p = msub.add_parser(
        "policy",
        help="show or edit the mesh's delivery policy (heartbeat/task-poll/stall-warn/backpressure)",
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

    p_ops = msub.add_parser(
        "ops",
        help="read another member's checkout (file / git), here or over the relay",
    )
    osub = p_ops.add_subparsers(dest="ops_cmd", required=True)
    p = osub.add_parser("file", help="print one file from a member's working directory")
    p.add_argument("mesh")
    p.add_argument("member", help="whose checkout to read: " + _TARGET_DOC)
    p.add_argument("path", help="path relative to that session's working directory")
    p.add_argument("--max-bytes", type=int, default=0,
                   help="cut after this many bytes (default 65536)")
    p.add_argument("--json", action="store_true", help="print the raw reply")
    p.add_argument("--session", help="who is asking (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_ops_file)
    p = osub.add_parser(
        "git", help="read-only git query in a member's checkout",
        description="ops: status | diff [--base R [--head R]] [--stat] [--cached] "
                    "| log [-n N] [--range R] | show --ref R [--stat] | branch",
    )
    p.add_argument("mesh")
    p.add_argument("member", help="whose checkout to query: " + _TARGET_DOC)
    p.add_argument("op", choices=["status", "diff", "log", "show", "branch"])
    p.add_argument("paths", nargs="*", help="limit to these paths")
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--ref")
    p.add_argument("--range")
    p.add_argument("-n", type=int, default=0)
    p.add_argument("--stat", action="store_true")
    p.add_argument("--cached", action="store_true")
    p.add_argument("--json", action="store_true", help="print the raw reply")
    p.add_argument("--session", help="who is asking (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_ops_git)

    p = msub.add_parser(
        "lease",
        help="hold a named lease across machines (acquire/renew/release/ls)",
        description="A lease is a coordination key the mesh's authority grants "
                    "to one holder at a time. Exit 2 when the key is held by "
                    "somebody else.",
    )
    p.add_argument("mesh")
    p.add_argument("op", choices=["acquire", "renew", "release", "ls", "list"])
    p.add_argument("key", nargs="?", default="",
                   help="lease key, e.g. path:src/x.py or issue:claunch-abcd "
                        "(for ls: optional holder filter)")
    p.add_argument("--ttl", type=float, default=0,
                   help="seconds until expiry unless renewed (default 900)")
    p.add_argument("--note", default="", help="what you are doing under it")
    p.add_argument("--json", action="store_true", help="print the raw reply")
    p.add_argument("--session", help="who is asking (default: $CLAUNCH_SESSION)")
    p.set_defaults(func=_cmd_lease)

    p = msub.add_parser(
        "mcp", help="run the stdio MCP server (send/members/history tools)"
    )
    p.set_defaults(func=_cmd_mcp)

    p = msub.add_parser(
        "install",
        help="alias for 'claunch install' (one MCP server + every skill)",
    )
    from .cli import add_install_scope_args

    add_install_scope_args(p)
    p.set_defaults(func=_cmd_install)
