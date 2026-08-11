"""``claunch workspace ...`` — the directories a session may be spawned in.

See :mod:`claude_launcher.workspaces` for what a workspace is and why the web
UI's directory field is a picker over these rather than a text box.
"""

from __future__ import annotations

import argparse
import os

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


def register(sub) -> None:
    p = sub.add_parser(
        "workspace",
        aliases=["ws"],
        help="manage the directories sessions can be spawned in",
    )
    wsub = p.add_subparsers(dest="workspace_command", required=True)

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
