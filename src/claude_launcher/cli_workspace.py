"""``claunch workspace ...`` — the directories a session may be spawned in.

See :mod:`claude_launcher.workspaces` for what a workspace is and why the web
UI's directory field is a picker over these rather than a text box.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import workspaces


def _cmd_add(args: argparse.Namespace) -> int:
    before = {w.name for w in workspaces.list_all()}
    ws = workspaces.add(args.path or os.getcwd(), name=args.name)
    verb = "added" if ws.name not in before else "workspace"
    print(f"{verb} {ws.name!r} -> {ws.path}")
    print(
        "pick it as the directory when creating a session "
        "(web UI, or 'claunch new-session -c <path>')"
    )
    return 0


def _cmd_ls(_args: argparse.Namespace) -> int:
    entries = workspaces.list_all()
    if not entries:
        print(
            "no workspaces yet; register one with "
            "'claunch workspace add <dir>' (or '.' for the current directory)"
        )
        return 0
    width = max(len(w.name) for w in entries)
    for w in entries:
        # A missing directory is reported, not hidden: the entry is still the
        # user's, and an unplugged drive should read as "not here right now"
        # rather than as the workspace having vanished.
        note = "" if w.exists() else "   (missing)"
        print(f"{w.name:<{width}}  {w.path}{note}")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    ws = workspaces.remove(args.workspace)
    print(f"removed workspace {ws.name!r} (the directory itself is untouched)")
    return 0


def _cmd_code(args: argparse.Namespace) -> int:
    """Open (or stop, or list) a workspace's VS Code server via the daemon.

    The server lives in the daemon (daemon/vscode.py); this prints the URL to
    open, with the daemon's own address in front. Reaching it from a browser
    needs the dashboard's login, the same as every other page of it.
    """
    from . import daemon_client

    client = daemon_client.ensure_running()
    status = client.get("/api/vscode")
    if not status.get("supported"):
        print(f"error: {status.get('reason') or 'not supported here'}", file=sys.stderr)
        return 1
    if args.stop:
        if not args.workspace:
            print("error: --stop needs the workspace (and --worktree) to stop", file=sys.stderr)
            return 1
        key = args.workspace + (f"~{args.worktree}" if args.worktree else "")
        client.post("/api/vscode/stop", {"key": key})
        print(f"stopped {key}")
        return 0
    if not args.workspace and not args.session:
        servers = status.get("servers") or []
        if not servers:
            print("no VS Code servers running")
            return 0
        for s in servers:
            print(f"{s['key']:<40} {s['state']:<9} pid {s.get('pid') or '-':<8} "
                  f"conns {s['connections']}  {s['folder']}")
        return 0
    install = status.get("install") or {}
    if not install.get("installed"):
        if not args.install:
            print(
                f"code-server {install.get('version', '')} is not installed; rerun with "
                "--install to download it (about 220 MB), or open it from the dashboard",
                file=sys.stderr,
            )
            return 1
        client.post("/api/vscode/install")
        print(f"installing code-server {install.get('version', '')} ...", flush=True)
        while True:
            time.sleep(1.0)
            install = client.get("/api/vscode").get("install") or {}
            if install.get("installed"):
                break
            if install.get("state") == "failed":
                print(f"error: install failed: {install.get('error')}", file=sys.stderr)
                return 1
    body = {"session": args.session} if args.session else {"workspace": args.workspace}
    if args.worktree and not args.session:
        body["worktree"] = args.worktree
    opened = client.post("/api/vscode/open", body, timeout=90.0)
    print(f"{client.base_url}/{opened['path']}")
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "workspace",
        aliases=["ws"],
        help="manage the directories sessions can be spawned in",
    )
    wsub = p.add_subparsers(dest="workspace_command", required=True)

    from . import workspace_check

    p_check = wsub.add_parser(
        "check", help="check the session's repository and linked worktree occupancy"
    )
    workspace_check.add_arguments(p_check)
    p_check.set_defaults(func=workspace_check.check)

    p_add = wsub.add_parser(
        "add", help="register a directory as a workspace (it must exist)"
    )
    p_add.add_argument(
        "path", nargs="?", help="directory to register (default: current directory)"
    )
    p_add.add_argument(
        "--name", help="name to show in pickers (default: the directory's own name)"
    )
    p_add.set_defaults(func=_cmd_add)

    p_ls = wsub.add_parser("ls", aliases=["list"], help="list registered workspaces")
    p_ls.set_defaults(func=_cmd_ls)

    p_rm = wsub.add_parser(
        "rm", aliases=["remove"], help="unregister a workspace (by name or path)"
    )
    p_rm.add_argument("workspace", help="workspace name, or the directory it points at")
    p_rm.set_defaults(func=_cmd_rm)

    p_code = wsub.add_parser(
        "code",
        help="open a workspace (or one of its worktrees) in VS Code in the browser",
        description=(
            "Start the workspace's code-server in the daemon and print the URL. "
            "With no workspace, list the running servers."
        ),
    )
    p_code.add_argument("workspace", nargs="?", help="registered workspace name")
    p_code.add_argument("--worktree", help="a worktree of that workspace (.claude/worktrees/<name>)")
    p_code.add_argument("--session", help="open the server for this session's directory instead")
    p_code.add_argument("--install", action="store_true",
                        help="download code-server first if it is missing")
    p_code.add_argument("--stop", action="store_true", help="stop that server")
    p_code.set_defaults(func=_cmd_code)
