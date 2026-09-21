"""``claunch project ...`` — the tier meshes and sessions are filed under.

See :mod:`claude_launcher.projects` for what a project is, what its default
workspace means, and why the ``default`` project always exists.
"""

from __future__ import annotations

import argparse

from . import projects


def _cmd_add(args: argparse.Namespace) -> int:
    before = {p.name for p in projects.list_all()}
    project = projects.add(args.name, default_workspace=args.default_workspace)
    verb = "added" if project.name not in before else "project"
    ws = (
        f" (default workspace: {project.default_workspace})"
        if project.default_workspace else ""
    )
    print(f"{verb} {project.name!r}{ws}")
    print(
        "file sessions under it with 'claunch new-session --project "
        f"{project.name} ...' and meshes with 'claunch mesh create --project "
        f"{project.name} <mesh>'"
    )
    return 0


def _cmd_ls(_args: argparse.Namespace) -> int:
    entries = projects.list_all()
    width = max(len(p.name) for p in entries)
    for p in entries:
        if p.default_workspace:
            cwd = p.default_cwd()
            where = (
                f"{p.default_workspace}  ({cwd})" if cwd
                else f"{p.default_workspace}  (workspace not registered)"
            )
        else:
            where = "-"
        tag = "   (unfiled records live here)" if p.is_default else ""
        print(f"{p.name:<{width}}  default workspace: {where}{tag}")
    return 0


def _cmd_set_workspace(args: argparse.Namespace) -> int:
    project = projects.set_default_workspace(args.name, args.workspace)
    if project.default_workspace:
        print(
            f"project {project.name!r}: sessions now start in workspace "
            f"{project.default_workspace!r} ({project.default_cwd()}) unless "
            "told otherwise"
        )
    else:
        print(f"project {project.name!r}: default workspace cleared")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    project = projects.remove(args.name)
    print(
        f"removed project {project.name!r} (its sessions and meshes keep "
        "the name on their records; nothing was killed)"
    )
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "project",
        aliases=["proj"],
        help="manage projects: the tier meshes and sessions are filed under",
    )
    psub = p.add_subparsers(dest="project_command", required=True)

    p_add = psub.add_parser(
        "add", help="create a project (or set the default workspace of one)"
    )
    p_add.add_argument("name", help="project name (letters, digits, '.', '_', '-')")
    p_add.add_argument(
        "--default-workspace", "-w", metavar="WORKSPACE", dest="default_workspace",
        help="the registered workspace new sessions of this project start in "
        "when no directory is given ('claunch workspace ls' lists them)",
    )
    p_add.set_defaults(func=_cmd_add)

    p_ls = psub.add_parser("ls", aliases=["list"], help="list projects")
    p_ls.set_defaults(func=_cmd_ls)

    p_ws = psub.add_parser(
        "set-workspace",
        help="set (or clear, with '') a project's default workspace",
    )
    p_ws.add_argument("name", help="project name")
    p_ws.add_argument(
        "workspace", help="a registered workspace name, or '' to clear the default"
    )
    p_ws.set_defaults(func=_cmd_set_workspace)

    p_rm = psub.add_parser(
        "rm", aliases=["remove"], help="drop a project from the registry"
    )
    p_rm.add_argument("name", help="project name (the default project cannot be removed)")
    p_rm.set_defaults(func=_cmd_rm)
