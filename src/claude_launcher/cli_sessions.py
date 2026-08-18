"""tmux-flavored session subcommands (``new-session``, ``send-keys``, ...).

Kept out of ``cli.py`` for size; :func:`register` wires the subparsers in.
Every handler is a thin client of the daemon's HTTP API via
:mod:`daemon_client` — session commands auto-start the daemon like tmux, while
``claunch daemon ...`` manages it explicitly.

**Two doors make a session, and they are not interchangeable.**
``new-session`` is the human's: every field is spelled out, nothing is
inherited, no lineage is recorded and no policy applies — the caller is the
person who owns the machine. ``spawn`` is a session's: the child inherits what
its parent runs, is recorded as its parent's, joins its mesh, and counts
against the ``spawn`` policy.

``$CLAUNCH_SESSION`` is what tells them apart, and the daemon cannot see it —
an HTTP request carries no caller environment, and the same endpoint serves
the web UI. So the split is enforced here, in the CLI, which is the only place
that knows whose shell it is running in (see :func:`_use_spawn_instead`).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from typing import List

from . import cli_mesh, daemon_client, herdr, store, worktree
from .daemon import paths as daemon_paths
from .daemon import runtime_state
from .daemon_client import DaemonClientError


def _print_relay_status(client) -> None:
    """Session commands surface relay connectivity constantly (to stderr, so
    scripted stdout parsing stays safe)."""
    try:
        relay = client.get("/api/daemon").get("relay")
    except DaemonClientError:
        return
    print(cli_mesh.relay_line(relay), file=sys.stderr)


# --------------------------------------------------------------------------- #
# session commands
# --------------------------------------------------------------------------- #
def _cmd_new_session(args: argparse.Namespace) -> int:
    inside = os.environ.get("CLAUNCH_SESSION") or ""
    if inside and not args.detached:
        print(_use_spawn_instead(args, inside), file=sys.stderr)
        return 2
    if getattr(args, "wizard", False) and not _run_wizard(args):
        return 1
    env = {}
    for item in args.env or []:
        if "=" not in item:
            print(f"error: --env expects KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        key, _, value = item.partition("=")
        env[key] = value
    extra = list(args.args or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    # Resolved here and not in the daemon, on purpose. The daemon builds
    # sessions for three other callers too -- the web UI, a restore after a
    # restart, an agent's spawn -- and none of them has a terminal to ask in
    # or a repository in front of them; a restore in particular must reopen
    # the recorded directory, not invent a second one every boot. The CLI is
    # the only door with a human behind it, so the question is asked here and
    # the daemon is handed a plain, already-decided cwd.
    tree = worktree.resolve(cwd, args.worktree)
    worktree.announce(tree)
    if tree is not None:
        cwd = str(tree.path)
        herdr.rename_pane(tree.label)
    body = {
        "name": args.name or "",
        "harness": args.harness,
        "profile": args.profile,
        "cwd": cwd,
        "args": extra,
        "env": env,
        "cols": args.cols,
        "rows": args.rows,
    }
    if args.restore is not None:
        body["restore"] = args.restore
    if args.role:
        body["role"] = args.role
    # Decided at creation because they are what the session is FOR: a mesh it
    # is not in and a run it does not drive have to be arranged afterwards,
    # with the agent already sitting at a prompt not knowing either.
    for key, value in (
        ("mesh", args.mesh), ("handle", args.handle),
        ("workflow", args.workflow), ("context", args.context),
        ("task", args.task),
    ):
        if value:
            body[key] = value
    if args.connect:
        body["connect"] = args.connect
    # `--resume` with no value is the picker, which argparse hands back as the
    # empty string — the same spelling the API uses, so it passes through.
    if args.resume is not None:
        body["resume"] = args.resume
        body["fork_session"] = args.fork_session
    elif args.fork_session:
        print(
            "error: --fork-session needs --resume [SESSION|UUID]", file=sys.stderr
        )
        return 1
    client = daemon_client.ensure_running()
    info = client.post("/api/sessions", body)
    print(
        f"created session {info['name']!r} "
        f"(harness: {info['harness']}"
        + (f", profile: {info['profile']}" if info.get("profile") else "")
        + (f", role: {info['role']}" if info.get("role") else "")
        + f", pid: {info.get('pid')})"
    )
    _print_onboarding(info)
    if args.attach:
        from . import attach as attach_mod

        return attach_mod.attach(client, info["name"])
    print(
        f"attach: claunch attach {info['name']}  |  browser: {client.base_url}/  "
        f"|  capture: claunch capture-pane {info['name']}"
    )
    _print_relay_status(client)
    return 0


def _run_wizard(args: argparse.Namespace) -> bool:
    """Fill ``args`` in from the terminal form. False when the user backed out.

    Deliberately *before* everything else in :func:`_cmd_new_session` and
    deliberately writing back onto the same namespace: the wizard is a second
    way to answer ``new-session``, not a second way to create a session. The
    refusal from inside a managed session is checked first (an agent must not
    get a form either), and the worktree, the onboarding payload and the
    attach that follow are the code that has always run.

    The daemon is started here rather than at create time because the form's
    lists -- harnesses, profiles, workspaces, roles, meshes, workflows,
    resumable conversations -- are all things only it knows. Whether there is
    anyone to show a form to is settled first, so a scripted ``--wizard``
    fails having started nothing.
    """
    from . import wizard as wizard_mod

    wizard_mod.require_terminal()
    client = daemon_client.ensure_running()
    return wizard_mod.run(
        args,
        sources=wizard_mod.DaemonSources(client),
        cwd=os.path.abspath(args.cwd) if args.cwd else os.getcwd(),
    )


def _use_spawn_instead(args: argparse.Namespace, parent: str) -> str:
    """The refusal ``new-session`` gives when an agent runs it.

    ``new-session`` and ``spawn`` build the same thing by different rights.
    ``new-session`` is the human's: every field spelled out, no lineage, no
    limits — the caller is the person who owns the machine. ``spawn`` is a
    session's: the child inherits what its parent runs, is recorded as its
    parent's, joins its mesh, and counts against the spawn policy.

    An agent that reaches for the human door gets a session that answers to
    nobody: absent from its parent's subtree, in no mesh, with no way to
    report what it was made to do. That has happened, and it fails silently —
    the session comes up, works, and has nowhere to put the result. So the
    door is closed from inside a session, and closed with the other command
    already written out: an agent that is told only "use spawn" still has to
    translate its own flags, and translating ``-c DIR`` is exactly the step
    that sent it here.
    """
    from . import workspaces

    out = ["claunch spawn"]
    if args.name:
        out.append(f"-s {args.name}")
    notes = []
    if args.cwd:
        found = workspaces.find(args.cwd)
        if found is not None:
            out.append(f"--workspace {found.name}")
        else:
            notes.append(
                f"{args.cwd!r} is not a registered workspace, and a child "
                "cannot be sent to a bare path (spawn.allow_cwd) — the user "
                f"registers one with 'claunch workspace add {args.cwd}'"
            )
    for flag, value in (
        ("--mesh", args.mesh), ("--as", args.handle), ("--role", args.role),
        ("--workflow", args.workflow), ("--context", args.context),
        ("--task", args.task),
        # only when it is a real choice: 'claude' is this parser's default,
        # and a child inherits its parent's harness anyway
        ("--harness", args.harness if args.harness != "claude" else ""),
    ):
        if value:
            out.append(f"{flag} {value!r}" if " " in str(value) else f"{flag} {value}")
    for handle in args.connect or []:
        out.append(f"--connect {handle}")
    if args.profile:
        notes.append(
            f"--profile {args.profile} is dropped: a child runs under its "
            "parent's profile"
        )
    if args.worktree is not worktree.ASK and args.worktree is not worktree.NEVER:
        notes.append(
            "--worktree is dropped: a child inherits its parent's working "
            "directory, so it is already in whatever worktree you are in. To "
            "give one its own checkout, make the worktree and register it "
            "('claunch workspace add <path>'), then pass --workspace"
        )
    if not args.mesh:
        notes.append(
            "no --mesh needed: the child joins yours, and starts connected "
            "to you"
        )
    # ASCII only, like the session listing: this prints to a Windows console
    # as often as not, where an em dash arrives as a question mark.
    lines = [
        f"refused: you are inside the managed session {parent!r}, and "
        "'new-session' is the human's command -- it records no parent, joins "
        "no mesh, and would leave a session that cannot report back to you.",
        "",
        "spawn a child instead:",
        f"  {' '.join(out)}",
    ]
    if notes:
        lines.append("")
        lines.extend(f"  note: {n}" for n in notes)
    lines.extend([
        "",
        "'claunch spawn --help' lists the rest; the 'children' MCP tool says "
        "how many you may still spawn and which workspaces you may send one "
        "to. If you genuinely want a session that is not yours -- unrelated "
        "work, nothing to report -- pass --detached.",
    ])
    return "\n".join(lines)


def _cmd_spawn(args: argparse.Namespace) -> int:
    """Spawn a child of a session, exactly as that session's agent would.

    The same endpoint and the same policy — this is here so the arrangement
    can be built and inspected by hand, and so a refusal can be reproduced
    without an agent in the loop.

    **No worktree question is asked here, and none can be.** ``spawn`` is the
    agent's door: the caller is a session, not a person, so there is nobody to
    answer and a prompt would hang the child that was being created. A child
    inherits its parent's working directory, which means it is already in
    whatever worktree the parent was launched into — the isolation the
    question buys was bought once, upstream. A child that needs a checkout of
    its *own* gets it the way every other spawn directory is chosen: the user
    registers one with ``claunch workspace add`` and it is picked by name.
    """
    client = daemon_client.ensure_running()
    parent = args.parent or os.environ.get("CLAUNCH_SESSION")
    if not parent:
        print(
            "no parent session: pass one, or run this inside a managed "
            "session (which sets $CLAUNCH_SESSION)"
        )
        return 2
    payload = {
        k: v
        for k, v in (
            ("name", args.name),
            ("mesh", args.mesh),
            ("handle", args.handle),
            ("role", args.role),
            ("connect", args.connect),
            ("workflow", args.workflow),
            ("context", args.context),
            ("task", args.task),
            ("harness", args.harness),
            ("workspace", args.workspace),
        )
        if v
    }
    try:
        result = client.post(f"/api/sessions/{parent}/children", payload)
    except daemon_client.DaemonClientError as exc:
        print(exc)
        return 1
    child = result.get("session") or {}
    print(f"spawned {child.get('name')} (child of {parent})")
    if args.workspace and child.get("cwd"):
        # The one field that was asked for by name and answered by path:
        # printing it is how the caller sees the registry resolved.
        print(f"  in {child['cwd']}")
    _print_onboarding(result)
    return 0


def _print_onboarding(result: dict) -> None:
    """Report the legs a create/spawn asked for, one line each.

    Each is reported separately because each can fail on its own: the session
    exists even when the mesh join is what went wrong, and a caller told only
    "created" would go looking for a member that is not there.
    """
    mesh = result.get("mesh") or {}
    if mesh:
        if mesh.get("ok"):
            # `connected_to` is now the whole answer — the join wires the
            # member and everything it did not wire is closed, so there is no
            # second "and cut off from" list to print. "nobody" is a real
            # outcome (a mesh whose rules connect nothing but the parent, and
            # a member with no parent in it) and reads better than an empty
            # list, which looks like the field failed to arrive.
            reach = ", ".join(mesh.get("connected_to") or []) or "nobody yet"
            print(f"  mesh {mesh.get('mesh')}: joined as {mesh.get('handle')}, "
                  f"can reach {reach}")
            for peer, err in sorted((mesh.get("connect_errors") or {}).items()):
                print(f"  could not connect to {peer}: {err}")
        else:
            print(f"  mesh join failed: {mesh.get('error') or mesh.get('pending')}")
    flow = result.get("workflow") or {}
    if flow:
        print(
            f"  workflow {flow.get('workflow')}: "
            + ("started" if flow.get("ok") else f"failed -- {flow.get('error')}")
        )
    if result.get("task"):
        print("  opening task will be typed in once it settles")


def _by_lineage(sessions):
    """Order sessions parent-before-child, yielding ``(session, depth)``.

    Spawned sessions are indented under the one that created them, so a fleet
    reads as the tree it is. A session whose parent is not in the list (an
    exited record cleared away, a hand-edited definition) is shown as a root
    rather than dropped — the listing's job is to account for every session,
    and a cycle or a dangling name must not make one invisible.
    """
    by_name = {s["name"]: s for s in sessions}
    children = {}
    roots = []
    for s in sessions:
        parent = s.get("parent")
        if parent and parent in by_name and parent != s["name"]:
            children.setdefault(parent, []).append(s)
        else:
            roots.append(s)
    out = []
    seen = set()

    def walk(node, depth):
        if node["name"] in seen:
            return
        seen.add(node["name"])
        out.append((node, depth))
        for child in children.get(node["name"], []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    for s in sessions:  # anything a cycle kept out of the walk
        if s["name"] not in seen:
            out.append((s, 0))
    return out


def _cmd_sessions(_args: argparse.Namespace) -> int:
    client = daemon_client.connect()
    if client is None:
        print("daemon is not running; no sessions")
        return 0
    sessions = client.get("/api/sessions").get("sessions", [])
    if not sessions:
        print("no sessions; create one with 'claunch new-session --profile <name>'")
        return 0
    for s, depth in _by_lineage(sessions):
        state = s["status"]
        if state == "exited":
            code = s.get("exit_code")
            state = f"exited({code})" if code is not None else "exited"
        prof = s.get("profile") or "-"
        # ASCII only: this prints to a Windows console as often as not, and
        # the box-drawing characters arrive there as mojibake.
        label = ("  " * (depth - 1) + "`- " + s["name"] if depth else s["name"])[:16]
        print(
            f"{label:<16} [{state:<10}] {s['harness']:<8} {prof:<12} "
            f"{s['cols']}x{s['rows']}  {s.get('cwd', '')}"
        )
    dead = [s["name"] for s in sessions if s["status"] == "exited"]
    if dead:
        print(
            f"\n{len(dead)} exited session(s) kept for respawn: "
            f"'claunch respawn {dead[0]}' revives one, "
            f"'claunch clear-sessions' drops them all"
        )
    _print_relay_status(client)
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    name = args.session
    if not name:
        sessions = client.get("/api/sessions").get("sessions", [])
        live = [s["name"] for s in sessions if s["status"] != "exited"]
        if not live:
            print("error: no running sessions to attach to", file=sys.stderr)
            return 1
        if len(live) > 1:
            print(
                "error: several sessions are running — pick one: " + ", ".join(live),
                file=sys.stderr,
            )
            return 1
        name = live[0]
    from . import attach as attach_mod

    return attach_mod.attach(client, name)


def _cmd_respawn(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    info = client.post(f"/api/sessions/{args.session}/respawn")
    print(
        f"session {info['name']!r} respawned (pid {info.get('pid')})"
        + (" — resuming its conversation" if info.get("harness") == "claude" else "")
    )
    if args.attach:
        from . import attach as attach_mod

        return attach_mod.attach(client, info["name"])
    return 0


def _cmd_clear_sessions(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    doc = client.delete("/api/sessions" + ("?logs=1" if args.logs else ""))
    removed = doc.get("removed") or []
    # Kept, not failed: a record a mesh row still names stays, because the row
    # and the record are one fact and half of it left behind is a member
    # nobody can respawn or reach. Printed either way — an omission this
    # command does not mention reads as the clear not having taken.
    kept = doc.get("kept") or []
    if not removed:
        print("no exited sessions to clear" if not kept else "nothing cleared")
    else:
        print(
            f"cleared {len(removed)} exited session record(s): {', '.join(removed)}"
            + (" (output logs deleted too)" if args.logs else "")
        )
    for k in kept:
        meshes = ", ".join(m["mesh"] for m in k.get("meshes") or [])
        print(
            f"kept {k['name']!r} — still a member of {meshes}. "
            f"'claunch mesh leave' it first, then clear again."
        )
    return 0


def _cmd_send_keys(args: argparse.Namespace) -> int:
    keys: List[str] = list(args.keys)
    if keys and keys[0] == "--":
        keys = keys[1:]
    if args.paste:
        # One paste, not per-argument keys: '-' reads stdin (the natural way
        # to hand over genuinely multiline text), else args joined by spaces.
        text = sys.stdin.read() if keys == ["-"] else " ".join(keys)
        if not text:
            print("error: no text to paste", file=sys.stderr)
            return 1
        client = daemon_client.ensure_running()
        client.post(
            f"/api/sessions/{args.session}/keys",
            {"paste": text, "enter": bool(args.enter)},
        )
        return 0
    if not keys:
        print("error: no keys given", file=sys.stderr)
        return 1
    client = daemon_client.ensure_running()
    client.post(
        f"/api/sessions/{args.session}/keys",
        {"keys": keys, "literal": bool(args.literal)},
    )
    return 0


def _cmd_capture_pane(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    query = []
    if args.history:
        query.append("history=1")
    if args.json:
        query.append("format=json")
    if args.no_trim:
        query.append("trim=0")
    suffix = ("?" + "&".join(query)) if query else ""
    payload = client.get(f"/api/sessions/{args.session}/capture{suffix}", raw=True)
    out = payload.decode("utf-8", errors="replace")
    sys.stdout.write(out)
    if out and not out.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_wait_for(args: argparse.Namespace) -> int:
    state = "exited" if args.exited else "idle"
    client = daemon_client.ensure_running()
    query = f"?state={state}&timeout={args.timeout}"
    if args.idle_threshold is not None:
        query += f"&threshold={args.idle_threshold}"
    try:
        info = client.get(
            f"/api/sessions/{args.session}/wait{query}",
            timeout=float(args.timeout) + 15.0,
        )
    except DaemonClientError as exc:
        if "timed out" in str(exc):
            print(f"timeout: session {args.session!r} did not become {state}", file=sys.stderr)
            return 1
        raise
    print(f"session {args.session!r} is {info.get('status')}")
    return 0


def _cmd_kill_session(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    suffix = "?force=1" if args.force else ""
    info = client.delete(f"/api/sessions/{args.session}{suffix}")
    if info.get("status") == "exited" and info.get("exit_code") is not None:
        print(f"session {args.session!r} removed (exit code {info['exit_code']})")
    else:
        print(f"session {args.session!r} killed")
    return 0


def _cmd_resize(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    client.post(
        f"/api/sessions/{args.session}/resize", {"cols": args.cols, "rows": args.rows}
    )
    return 0


# --------------------------------------------------------------------------- #
# daemon commands
# --------------------------------------------------------------------------- #
def _cmd_daemon(args: argparse.Namespace) -> int:
    action = args.action
    if action == "start":
        if daemon_client.connect() is not None:
            print("daemon is already running")
            return 0
        client = daemon_client.ensure_running()
        print(f"daemon started at {client.base_url}")
        return 0
    if action == "stop":
        if daemon_client.stop():
            print("daemon stopped")
        else:
            print("daemon is not running")
        return 0
    if action == "restart":
        if getattr(args, "all", False):
            return _restart_all_instances()
        daemon_client.stop()
        time.sleep(0.3)
        client = daemon_client.ensure_running()
        print(f"daemon restarted at {client.base_url}")
        return 0
    if action == "status":
        info = daemon_client.status()
        if info is None:
            print("daemon is not running")
            return 1
        print(f"pid:      {info.get('pid')}")
        if info.get("instance"):
            print(f"instance: {info.get('instance')}")
        print(f"address:  http://{info.get('host')}:{info.get('port')}")
        print(f"version:  {info.get('version')}")
        print(f"started:  {info.get('started_at')}")
        print(f"uptime:   {info.get('uptime')}s")
        running = info.get("running")
        print(
            f"sessions: {info.get('sessions')}"
            + (f" ({running} running, rest exited)" if running is not None else "")
        )
        print(f"log:      {daemon_paths.log_file()}")
        print(cli_mesh.relay_line(info.get("relay")))
        return 0
    raise AssertionError(f"unknown daemon action {action!r}")


def _restart_all_instances() -> int:
    """Restart every daemon instance that is currently serving.

    The client stack resolves all its paths through ``CLAUNCH_DAEMON``, and a
    spawned daemon inherits the environment — so iterating means swapping the
    variable per instance. Instances that merely have state on disk but no
    live server are left alone (restart should not *start* servers you shut
    down on purpose).
    """
    instances = daemon_paths.known_instances()
    if not instances:
        print("no daemon instances found")
        return 0
    saved = os.environ.get(daemon_paths.INSTANCE_ENV)
    restarted = 0
    try:
        for name in instances:
            if name:
                os.environ[daemon_paths.INSTANCE_ENV] = name
            else:
                os.environ.pop(daemon_paths.INSTANCE_ENV, None)
            label = name or "default"
            if daemon_client.connect() is None:
                print(f"{label}: not running -- skipped")
                continue
            daemon_client.stop()
            time.sleep(0.3)
            client = daemon_client.ensure_running()
            restarted += 1
            print(f"{label}: restarted at {client.base_url}")
    finally:
        if saved is None:
            os.environ.pop(daemon_paths.INSTANCE_ENV, None)
        else:
            os.environ[daemon_paths.INSTANCE_ENV] = saved
    print(f"{restarted} daemon(s) restarted")
    return 0


def _cmd_daemon_token(args: argparse.Namespace) -> int:
    if args.rotate:
        token = runtime_state.rotate_token()
        print(token)
        print(
            "token rotated; restart the daemon ('claunch daemon restart') so it "
            "picks up the new value",
            file=sys.stderr,
        )
        return 0
    print(runtime_state.load_or_create_token())
    return 0


_CONFIG_KEYS = tuple(store.DAEMON_DEFAULTS)


def _cmd_daemon_config(args: argparse.Namespace) -> int:
    cfg = store.daemon_config()
    if not args.key:
        for key in _CONFIG_KEYS:
            print(f"{key}: {cfg[key]}")
        return 0
    if args.key not in _CONFIG_KEYS:
        print(
            f"error: unknown daemon setting {args.key!r} "
            f"(known: {', '.join(_CONFIG_KEYS)})",
            file=sys.stderr,
        )
        return 1
    if args.value is None:
        print(cfg[args.key])
        return 0
    store.set_daemon_field(args.key, _parse_value(args.value))
    print(f"{args.key} = {args.value}")
    if daemon_client.connect() is not None:
        print("(restart the daemon to apply: claunch daemon restart)", file=sys.stderr)
    return 0


def _parse_value(raw: str):
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


#: Settable ``daemon.relay`` uplink keys (token is write-only via config file /
#: CLAUNCH_RELAY_TOKEN, never printed back).
_RELAY_KEYS = ("url", "name", "token", "verify_tls")


def _cmd_daemon_relay(args: argparse.Namespace) -> int:
    cfg = store.relay_config()
    if not args.key:
        url = cfg.get("url") or "(unset)"
        name = cfg.get("name") or "(hostname)"
        has_token = bool(os.environ.get("CLAUNCH_RELAY_TOKEN") or cfg.get("token"))
        verify = cfg.get("verify_tls", True)
        print(f"url:        {url}")
        print(f"name:       {name}")
        print(f"token:      {'set' if has_token else '(unset)'}")
        print(f"verify_tls: {verify}")
        if not has_token:
            print(
                "\nset a token with 'claunch daemon relay token <TOKEN>' or the "
                "CLAUNCH_RELAY_TOKEN env var (matches relay.toml backend_token)",
                file=sys.stderr,
            )
        return 0
    if args.key not in _RELAY_KEYS:
        print(
            f"error: unknown relay setting {args.key!r} (known: {', '.join(_RELAY_KEYS)})",
            file=sys.stderr,
        )
        return 1
    if args.value is None:
        if args.key == "token":
            print("set" if (os.environ.get("CLAUNCH_RELAY_TOKEN") or cfg.get("token")) else "(unset)")
        else:
            print(cfg.get(args.key, ""))
        return 0
    clear = args.value == "" or args.value.lower() == "none"
    store.set_relay_field(args.key, None if clear else _parse_value(args.value))
    if args.key == "token" and not clear:
        print("token = set")
    else:
        print(f"{args.key} = {'(cleared)' if clear else args.value}")
    if daemon_client.connect() is not None:
        print("(restart the daemon to apply: claunch daemon restart)", file=sys.stderr)
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    client = daemon_client.ensure_running()
    url = client.base_url + "/"
    print(url)
    print("login token: claunch daemon token", file=sys.stderr)
    if args.open:
        webbrowser.open(url)
    return 0


# --------------------------------------------------------------------------- #
# parser wiring
# --------------------------------------------------------------------------- #
def register(sub) -> None:
    """Attach all session/daemon subparsers to ``claunch``'s subparsers."""
    p_new = sub.add_parser(
        "new-session",
        aliases=["new"],
        help="spawn a harness (claude, ...) in a daemon-managed PTY session "
             "-- the human's command; from inside a session use 'claunch spawn'",
    )
    p_new.add_argument(
        "--wizard", action="store_true",
        help="pick every field from a form in this terminal instead of "
        "spelling them out as flags -- harness, profile, directory, worktree, "
        "role, resume, mesh, workflow and whether to attach, each from the "
        "list the daemon publishes. Any flag given alongside it pre-fills its "
        "field",
    )
    p_new.add_argument("-s", "--name", help="session name (auto-generated if omitted)")
    p_new.add_argument("--profile", help="claunch profile (required for the claude harness)")
    p_new.add_argument(
        "--harness", default="claude",
        help="harness to run: claude (default), codex, pi, or one you declared "
        "under 'harnesses:' — see 'claunch harnesses'",
    )
    p_new.add_argument("-c", "--cwd", help="working directory (default: current dir)")
    wt = p_new.add_mutually_exclusive_group()
    wt.add_argument(
        "--worktree", nargs="?", const="", default=worktree.ASK, metavar="NAME",
        help="run the session in a git worktree of that directory instead of "
        "the directory itself, so it cannot collide with agents working in "
        "the same checkout; bare, the worktree is named after this Herdr pane "
        "and the current time. Asked interactively when neither this nor "
        "--no-worktree is given",
    )
    wt.add_argument(
        "--no-worktree", dest="worktree", action="store_const",
        const=worktree.NEVER,
        help="use the directory as it stands, and do not ask",
    )
    p_new.add_argument("--cols", type=int, default=120)
    p_new.add_argument("--rows", type=int, default=30)
    p_new.add_argument("--env", action="append", metavar="KEY=VALUE", help="extra env override")
    p_new.add_argument(
        "--role",
        help="run as this role — leader, operator, worker, reviewer or "
        "specialist (aliases accepted): its stance is injected into the "
        "session's system prompt at every spawn",
    )
    p_new.add_argument(
        "--resume", nargs="?", const="", metavar="SESSION|UUID",
        help="open an existing conversation instead of a new one: another "
        "session's name, a conversation uuid, or bare for claude's picker",
    )
    p_new.add_argument(
        "--fork-session", dest="fork_session", action="store_true",
        help="with --resume, work on a COPY of that conversation and leave "
        "the original untouched",
    )
    restore = p_new.add_mutually_exclusive_group()
    restore.add_argument(
        "--restore", dest="restore", action="store_true", default=None,
        help="relaunch this session when the daemon restarts",
    )
    restore.add_argument(
        "--no-restore", dest="restore", action="store_false",
        help="do not relaunch on daemon restart",
    )
    p_new.add_argument("--mesh", help="mesh to join at creation")
    p_new.add_argument(
        "--as", dest="handle", help="its handle in that mesh (default: session name)"
    )
    p_new.add_argument(
        "--connect", action="append", metavar="HANDLE",
        help="a member it may message (repeatable); without any it can reach "
             "the whole mesh",
    )
    p_new.add_argument("--workflow", help="cflow workflow to start for it")
    p_new.add_argument("--context", help="context string for that workflow run")
    p_new.add_argument(
        "--task", help="opening instruction typed in once it has booted"
    )
    p_new.add_argument(
        "-a", "--attach", action="store_true",
        help="attach this terminal to the new session right away (detach: Ctrl+])",
    )
    p_new.add_argument(
        "--detached", action="store_true",
        help="create it even from inside a managed session, as nobody's child "
             "-- no parent recorded, no mesh inherited. Refused without this, "
             "because a session created by a session is a child and 'claunch "
             "spawn' is what makes one",
    )
    p_new.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="extra arguments passed to the harness (prefix with -- if they start with -)",
    )
    p_new.set_defaults(func=_cmd_new_session)

    p_spawn = sub.add_parser(
        "spawn",
        help="spawn a CHILD of a session (inherits its harness/profile/cwd), "
             "optionally enrolling it in a mesh -- what an agent's 'spawn' "
             "tool does",
    )
    p_spawn.add_argument(
        "--parent", help="parent session (default: $CLAUNCH_SESSION)"
    )
    p_spawn.add_argument("-s", "--name", help="child session name")
    p_spawn.add_argument(
        "--mesh",
        help="mesh to enrol the child in (default: the parent's own, opening "
             "one for the pair if it is in none; '-' for no mesh at all)",
    )
    p_spawn.add_argument("--as", dest="handle", help="the child's mesh handle")
    p_spawn.add_argument("--role", help="the child's mesh role")
    p_spawn.add_argument(
        "--connect", action="append", metavar="HANDLE",
        help="another member the child may message (repeatable); it can "
             "always reach its parent",
    )
    p_spawn.add_argument("--workflow", help="cflow workflow to start for the child")
    p_spawn.add_argument("--context", help="context string for that workflow run")
    p_spawn.add_argument("--task", help="opening instruction typed into the child")
    p_spawn.add_argument(
        "--harness", help="a different harness (needs spawn.allow_harness)"
    )
    p_spawn.add_argument(
        "--workspace", "-w", metavar="NAME",
        help="run the child in a registered workspace instead of the "
             "parent's directory ('claunch workspace ls' lists them; "
             "spawn.allow_workspace turns this off)",
    )
    p_spawn.set_defaults(func=_cmd_spawn)

    p_ls = sub.add_parser("sessions", aliases=["lss"], help="list daemon-managed sessions")
    p_ls.set_defaults(func=_cmd_sessions)

    p_attach = sub.add_parser(
        "attach",
        aliases=["attach-session", "a"],
        help="attach this terminal to a session, tmux-style (detach: Ctrl+])",
    )
    p_attach.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_attach.add_argument(
        "session", nargs="?",
        help="session name (may be omitted when exactly one session is running)",
    )
    p_attach.set_defaults(func=_cmd_attach_dispatch)

    p_respawn = sub.add_parser(
        "respawn",
        help="relaunch an exited session (claude resumes its own conversation)",
    )
    p_respawn.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_respawn.add_argument("session", nargs="?")
    p_respawn.add_argument(
        "-a", "--attach", action="store_true", help="attach once respawned"
    )
    p_respawn.set_defaults(func=_cmd_respawn_dispatch)

    p_send = sub.add_parser(
        "send-keys",
        help="send keys to a session (tmux semantics: Enter, Escape, C-c, ... or literal text)",
    )
    p_send.add_argument("-l", "--literal", action="store_true", help="send arguments as literal text")
    p_send.add_argument(
        "-p", "--paste", action="store_true",
        help="inject as one (bracketed) paste — newlines don't submit; '-' reads stdin",
    )
    p_send.add_argument(
        "--enter", action="store_true", help="with --paste: press Enter after the paste"
    )
    p_send.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)  # tmux muscle memory
    p_send.add_argument("session", nargs="?")
    p_send.add_argument("keys", nargs=argparse.REMAINDER)
    p_send.set_defaults(func=_cmd_send_keys_dispatch)

    p_cap = sub.add_parser(
        "capture-pane", help="print a session's current screen (or scrollback)"
    )
    p_cap.add_argument("-p", action="store_true", help=argparse.SUPPRESS)  # tmux compat no-op
    p_cap.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_cap.add_argument("session", nargs="?")
    p_cap.add_argument("--history", action="store_true", help="dump scrolled-off lines instead")
    p_cap.add_argument("--json", action="store_true", help="JSON output (lines + cursor + status)")
    p_cap.add_argument("--no-trim", action="store_true", help="keep trailing blank lines")
    p_cap.set_defaults(func=_cmd_capture_pane_dispatch)

    p_wait = sub.add_parser(
        "wait-for", help="block until a session becomes idle (or exits)"
    )
    p_wait.add_argument("session")
    which = p_wait.add_mutually_exclusive_group()
    which.add_argument("--idle", action="store_true", help="wait for idle (default)")
    which.add_argument("--exited", action="store_true", help="wait for process exit")
    p_wait.add_argument("--timeout", type=float, default=300.0, metavar="SECS")
    p_wait.add_argument(
        "--idle-threshold", type=float, default=None, metavar="SECS",
        help="seconds of screen quiet that count as idle (default: daemon setting)",
    )
    p_wait.set_defaults(func=_cmd_wait_for)

    p_kill = sub.add_parser(
        "kill-session", help="kill a running session (or remove an exited one)"
    )
    p_kill.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_kill.add_argument("session", nargs="?")
    p_kill.add_argument("--force", action="store_true", help="skip graceful terminate")
    p_kill.set_defaults(func=_cmd_kill_session_dispatch)

    p_clear = sub.add_parser(
        "clear-sessions",
        aliases=["clear"],
        help="drop the records of all exited sessions (running ones are kept)",
    )
    p_clear.add_argument(
        "--logs",
        action="store_true",
        help="also delete their captured output logs, freeing their names for reuse",
    )
    p_clear.set_defaults(func=_cmd_clear_sessions)

    p_resize = sub.add_parser("resize", help="resize a session's terminal")
    p_resize.add_argument("session")
    p_resize.add_argument("cols", type=int)
    p_resize.add_argument("rows", type=int)
    p_resize.set_defaults(func=_cmd_resize)

    p_daemon = sub.add_parser("daemon", help="manage the session daemon")
    dsub = p_daemon.add_subparsers(dest="daemon_command", required=True)
    for action in ("start", "stop", "status", "restart"):
        p = dsub.add_parser(action, help=f"{action} the daemon")
        if action == "restart":
            p.add_argument(
                "--all", action="store_true",
                help="restart every running daemon instance (the default one "
                     "and all named -L instances)",
            )
        p.set_defaults(func=_cmd_daemon, action=action)
    p_token = dsub.add_parser("token", help="print the API/web login token")
    p_token.add_argument("--rotate", action="store_true", help="generate a new token")
    p_token.set_defaults(func=_cmd_daemon_token)
    p_cfg = dsub.add_parser("config", help="show or set daemon settings (in ~/.claunch.yaml)")
    p_cfg.add_argument("key", nargs="?")
    p_cfg.add_argument("value", nargs="?")
    p_cfg.set_defaults(func=_cmd_daemon_config)

    p_relay = dsub.add_parser(
        "relay",
        help="show or set the relay uplink (reach this daemon from outside the LAN)",
    )
    p_relay.add_argument("key", nargs="?", help="url | name | token | verify_tls")
    p_relay.add_argument("value", nargs="?", help="new value ('' or none to clear)")
    p_relay.set_defaults(func=_cmd_daemon_relay)

    p_web = sub.add_parser("web", help="print the web UI URL")
    p_web.add_argument("--open", action="store_true", help="also open it in the browser")
    p_web.set_defaults(func=_cmd_web)


def _resolve_target(args: argparse.Namespace) -> bool:
    """Support tmux-style ``-t SESSION`` by shifting args when it was used."""
    if getattr(args, "session_t", None):
        if args.session is not None:
            # both -t and a positional: positional is actually part of keys
            rest = getattr(args, "keys", None)
            if rest is not None:
                rest.insert(0, args.session)
        args.session = args.session_t
    if not args.session:
        print("error: no session given", file=sys.stderr)
        return False
    return True


def _cmd_attach_dispatch(args: argparse.Namespace) -> int:
    # ``-t`` tmux muscle memory; unlike the others, no session at all is fine
    # (attach auto-picks when exactly one session is running).
    if getattr(args, "session_t", None):
        args.session = args.session_t
    return _cmd_attach(args)


def _cmd_respawn_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    return _cmd_respawn(args)


def _cmd_send_keys_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    return _cmd_send_keys(args)


def _cmd_capture_pane_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    return _cmd_capture_pane(args)


def _cmd_kill_session_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    return _cmd_kill_session(args)
