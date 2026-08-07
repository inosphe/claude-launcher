"""tmux-flavored session subcommands (``new-session``, ``send-keys``, ...).

Kept out of ``cli.py`` for size; :func:`register` wires the subparsers in.
Every handler is a thin client of the daemon's HTTP API via
:mod:`daemon_client` — session commands auto-start the daemon like tmux, while
``claunch daemon ...`` manages it explicitly.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from typing import List

from . import cli_mesh, daemon_client, store
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
    body = {
        "name": args.name or "",
        "harness": args.harness,
        "profile": args.profile,
        "cwd": os.path.abspath(args.cwd) if args.cwd else os.getcwd(),
        "args": extra,
        "env": env,
        "cols": args.cols,
        "rows": args.rows,
    }
    if args.restore is not None:
        body["restore"] = args.restore
    client = daemon_client.ensure_running()
    info = client.post("/api/sessions", body)
    print(
        f"created session {info['name']!r} "
        f"(harness: {info['harness']}"
        + (f", profile: {info['profile']}" if info.get("profile") else "")
        + f", pid: {info.get('pid')})"
    )
    if args.attach:
        from . import attach as attach_mod

        return attach_mod.attach(client, info["name"])
    print(
        f"attach: claunch attach {info['name']}  |  browser: {client.base_url}/  "
        f"|  capture: claunch capture-pane {info['name']}"
    )
    _print_relay_status(client)
    return 0


def _cmd_sessions(_args: argparse.Namespace) -> int:
    client = daemon_client.connect()
    if client is None:
        print("daemon is not running; no sessions")
        return 0
    sessions = client.get("/api/sessions").get("sessions", [])
    if not sessions:
        print("no sessions; create one with 'claunch new-session --profile <name>'")
        return 0
    for s in sessions:
        state = s["status"]
        if state == "exited":
            code = s.get("exit_code")
            state = f"exited({code})" if code is not None else "exited"
        prof = s.get("profile") or "-"
        print(
            f"{s['name']:<16} [{state:<10}] {s['harness']:<8} {prof:<12} "
            f"{s['cols']}x{s['rows']}  {s.get('cwd', '')}"
        )
<<<<<<< HEAD
    _print_relay_status(client)
=======
    dead = [s["name"] for s in sessions if s["status"] == "exited"]
    if dead:
        print(
            f"\n{len(dead)} exited session(s) kept for respawn: "
            f"'claunch respawn {dead[0]}' revives one, "
            f"'claunch clear-sessions' drops them all"
        )
>>>>>>> master
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
    if not removed:
        print("no exited sessions to clear")
        return 0
    print(
        f"cleared {len(removed)} exited session record(s): {', '.join(removed)}"
        + (" (output logs deleted too)" if args.logs else "")
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
        help="spawn a harness (claude, ...) in a daemon-managed PTY session",
    )
    p_new.add_argument("-s", "--name", help="session name (auto-generated if omitted)")
    p_new.add_argument("--profile", help="claunch profile (required for the claude harness)")
    p_new.add_argument("--harness", default="claude", help="harness to run (default: claude)")
    p_new.add_argument("-c", "--cwd", help="working directory (default: current dir)")
    p_new.add_argument("--cols", type=int, default=120)
    p_new.add_argument("--rows", type=int, default=30)
    p_new.add_argument("--env", action="append", metavar="KEY=VALUE", help="extra env override")
    restore = p_new.add_mutually_exclusive_group()
    restore.add_argument(
        "--restore", dest="restore", action="store_true", default=None,
        help="relaunch this session when the daemon restarts",
    )
    restore.add_argument(
        "--no-restore", dest="restore", action="store_false",
        help="do not relaunch on daemon restart",
    )
    p_new.add_argument(
        "-a", "--attach", action="store_true",
        help="attach this terminal to the new session right away (detach: Ctrl+])",
    )
    p_new.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="extra arguments passed to the harness (prefix with -- if they start with -)",
    )
    p_new.set_defaults(func=_cmd_new_session)

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
