"""Command-line interface for claude-launcher.

This module only parses arguments and formats output; all behaviour lives in the
``profile``, ``runner`` and ``usage`` modules.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import List, Optional

from pathlib import Path

from . import (
    __version__,
    credentials,
    lineage,
    migrate as migrate_mod,
    profile,
    runner,
    seed,
    settings,
    sync,
    template,
    usage,
)
from .credentials import CredentialsError
from .lineage import LineageError
from .migrate import MigrateError


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #
def _cmd_create(args: argparse.Namespace) -> int:
    if args.parent:
        profile.require(args.parent)  # fail before creating if parent is missing
    p = profile.create(args.name)
    print(f"created profile {p.name!r} at {p.config_dir}")
    if not args.no_seed:
        source = Path(args.seed_from).expanduser() if args.seed_from else None
        copied = seed.seed_profile(p, source)
        if copied:
            print(f"seeded global config ({', '.join(copied)}); onboarding skipped")
        else:
            print("no global config found to seed; first run will show onboarding")
    if args.parent:
        lineage.set_parent(p, args.parent)
        print(f"inherits from parent {args.parent!r} (env + login)")
        parent = profile.require(args.parent)
        copied = migrate_mod.migrate(p, parent.config_dir, skills=True, mcp=True)
        bits = []
        if copied.skills:
            bits.append(f"skills: {', '.join(copied.skills)}")
        if copied.mcp_servers:
            bits.append(f"mcp: {', '.join(copied.mcp_servers)}")
        if bits:
            print(f"copied from parent ({'; '.join(bits)})")
    else:
        template.ensure_file()
        applied = template.apply_to(p)
        if applied:
            print(f"applied template env: {', '.join(sorted(applied))}")
    print(f"next: claunch login {p.name}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    p = profile.remove(args.name)
    print(f"removed profile {p.name!r} ({p.config_dir})")
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    profiles = profile.list_all()
    if not profiles:
        print("no profiles yet; create one with 'claunch create <name>'")
        return 0
    labels = {
        "ok": "logged in",
        "expired": "token expired",
        "inherited": "inherited",
        "none": "no token",
    }
    for p in profiles:
        flag = labels[lineage.login_state(p)]
        parent = lineage.get_parent(p)
        note = f"  (parent: {parent})" if parent else ""
        print(f"{p.name:<20} [{flag:<13}]  {p.config_dir}{note}")
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    print(p.config_dir)
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    print(f"running 'claude setup-token' for profile {p.name!r}...", file=sys.stderr)
    return runner.login(p)


def _cmd_env(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    if args.apply_template:
        template.apply_to(p)
    if args.unset:
        settings.unset_env(p, args.unset)
    if args.assignments:
        updates = {}
        for item in args.assignments:
            if "=" not in item:
                print(f"error: expected KEY=VALUE, got {item!r}", file=sys.stderr)
                return 1
            key, value = item.split("=", 1)
            if not key:
                print(f"error: empty key in {item!r}", file=sys.stderr)
                return 1
            updates[key] = value
        settings.set_env(p, updates)
    env = lineage.effective_env(p) if args.effective else settings.get_env(p)
    if not env:
        scope = "effective" if args.effective else "own"
        print(f"profile {p.name!r} has no {scope} env vars set")
        return 0
    for key in sorted(env):
        print(f"{key}={env[key]}")
    return 0


def _cmd_parent(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    if args.clear:
        lineage.clear_parent(p)
        print(f"cleared parent of {p.name!r}")
        return 0
    if args.parent:
        lineage.set_parent(p, args.parent)
        print(f"{p.name!r} now inherits from {args.parent!r}")
        return 0
    parent = lineage.get_parent(p)
    if parent:
        names = " -> ".join(a.name for a in lineage.chain(p))
        print(f"parent: {parent}    (chain: {names})")
    else:
        print(f"profile {p.name!r} has no parent")
    return 0


def _cmd_template(args: argparse.Namespace) -> int:
    if args.init:
        template.ensure_file()
    path = template.template_path()
    suffix = "" if path.is_file() else "  (not created; using built-in defaults)"
    print(f"template: {path}{suffix}")
    env = template.env()
    if env:
        print("default env:")
        for key in sorted(env):
            print(f"  {key}={env[key]}")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    source = Path(args.source).expanduser() if args.source else migrate_mod.default_source()
    # Default to skills + mcp when no selector is given; plugins only on request.
    selected = args.skills or args.mcp or args.plugins
    do_skills = args.skills or not selected
    do_mcp = args.mcp or not selected

    targets = [p]
    if args.recursive:
        targets += lineage.descendants(p)

    verb = "would migrate" if args.dry_run else "migrated"
    for target in targets:
        result = migrate_mod.migrate(
            target,
            source,
            skills=do_skills,
            mcp=do_mcp,
            plugins=args.plugins,
            dry_run=args.dry_run,
        )
        print(f"{verb} from {source} into {target.name!r}:")
        if do_skills:
            print(f"  skills: {', '.join(result.skills) if result.skills else '(none)'}")
        if do_mcp:
            servers = ", ".join(result.mcp_servers) if result.mcp_servers else "(none)"
            print(f"  mcp servers: {servers}")
        if args.plugins:
            print(f"  plugins: {'copied' if result.plugins else '(none)'}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    targets = [profile.require(args.name)] if args.name else profile.list_all()
    if not targets:
        print("no profiles to validate")
        return 0
    failed = 0
    for p in targets:
        if lineage.lookup_token(p) is None:
            failed += 1
            print(f"{p.name:<20} FAIL  no token (run 'claunch login {p.name}')")
            continue
        result = runner.heartbeat(p, prompt=args.prompt, timeout=args.timeout)
        if result.ok:
            snippet = " ".join(result.output.split())[:50]
            print(f"{p.name:<20} OK    {snippet}")
        else:
            failed += 1
            reason = " ".join(result.reason.split())[:60]
            print(f"{p.name:<20} FAIL  {reason}")
    return 1 if failed else 0


def _cmd_export(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else None
    written = sync.export_to(path)
    count = len(profile.list_all())
    print(f"exported {count} profile(s) to {written}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else None
    summary = sync.import_from(path, prune=args.prune, do_seed=not args.no_seed)
    if summary.template_applied:
        print("applied template from file")
    if summary.created:
        print(f"created: {', '.join(summary.created)}")
    if summary.updated:
        print(f"updated: {', '.join(summary.updated)}")
    if summary.removed:
        print(f"removed (pruned): {', '.join(summary.removed)}")
    if not (summary.created or summary.updated or summary.removed):
        print("nothing to import")
    return 0


def _cmd_set_token(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    token = args.token
    if not token:
        token = sys.stdin.readline()
    credentials.save_token(p, token)
    print(f"stored token for profile {p.name!r}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    # `args` is argparse.REMAINDER, so it also captures launcher flags like
    # --borrow that appear after the profile name; pull those out here, then
    # drop a leading `--` separator before forwarding the rest to claude.
    borrow_name, rest = _extract_borrow(args.args)
    passthrough = _strip_separator(rest)
    borrow = profile.require(borrow_name) if borrow_name else None
    if borrow is not None:
        print(f"borrowing {borrow.name!r} token for this run", file=sys.stderr)
    return runner.run(p, passthrough, borrow=borrow)


def _cmd_usage(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    report = usage.fetch(p)
    if args.json:
        import json

        print(json.dumps(report.raw, indent=2))
        return 0
    _print_usage(p.name, report)
    return 0


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _strip_separator(args: Optional[List[str]]) -> List[str]:
    """Drop a leading ``--`` that argparse keeps in REMAINDER."""
    args = list(args or [])
    if args and args[0] == "--":
        return args[1:]
    return args


def _extract_borrow(args: List[str]) -> "tuple[Optional[str], List[str]]":
    """Pull a ``--borrow NAME`` / ``--borrow=NAME`` flag out of run passthrough.

    Stops at a ``--`` separator so anything explicitly forwarded to claude after
    it is left untouched.
    """
    borrow: Optional[str] = None
    rest: List[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            rest.extend(args[i:])
            break
        if token == "--borrow":
            if i + 1 >= len(args):
                raise profile.ProfileError("--borrow requires a profile name")
            borrow = args[i + 1]
            i += 2
            continue
        if token.startswith("--borrow="):
            borrow = token.split("=", 1)[1]
            i += 1
            continue
        rest.append(token)
        i += 1
    return borrow, rest


def _fmt_reset(resets_at: Optional[str]) -> str:
    if not resets_at:
        return ""
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return f"(resets {resets_at})"
    delta = dt - datetime.now(timezone.utc)
    mins = int(delta.total_seconds() // 60)
    if mins <= 0:
        return "(resetting)"
    if mins < 60:
        return f"(resets in {mins}m)"
    return f"(resets in {mins // 60}h{mins % 60:02d}m)"


def _print_usage(name: str, report: usage.UsageReport) -> None:
    print(f"usage for profile {name!r}")
    active = [w for w in report.windows if w.utilization > 0 or w.resets_at]
    windows = active or report.windows
    if not windows:
        print("  no usage windows reported")
        return
    for w in windows:
        bar = _bar(w.utilization)
        print(f"  {w.name:<18} {bar} {w.utilization:5.1f}%  {_fmt_reset(w.resets_at)}")


def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claunch",
        description="Run Claude Code under isolated, per-profile login tokens and config.",
    )
    parser.add_argument("--version", action="version", version=f"claude-launcher {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create a new profile (seeds global config)")
    p_create.add_argument("name")
    p_create.add_argument(
        "--no-seed",
        action="store_true",
        help="do not copy global config; start the profile fully fresh",
    )
    p_create.add_argument(
        "--seed-from",
        metavar="DIR",
        help="config dir to seed from (default: CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    p_create.add_argument(
        "--parent",
        metavar="NAME",
        help="inherit env and login from an existing parent profile",
    )
    p_create.set_defaults(func=_cmd_create)

    p_remove = sub.add_parser(
        "remove", aliases=["delete", "rm"], help="delete a profile and its tokens"
    )
    p_remove.add_argument("name")
    p_remove.set_defaults(func=_cmd_remove)

    p_list = sub.add_parser("list", aliases=["ls"], help="list profiles")
    p_list.set_defaults(func=_cmd_list)

    p_path = sub.add_parser("path", help="print a profile's CLAUDE_CONFIG_DIR")
    p_path.add_argument("name")
    p_path.set_defaults(func=_cmd_path)

    p_login = sub.add_parser("login", help="log in via 'claude setup-token'")
    p_login.add_argument("name")
    p_login.set_defaults(func=_cmd_login)

    p_set = sub.add_parser(
        "set-token",
        help="store a setup-token manually (paste it or pipe via stdin)",
    )
    p_set.add_argument("name")
    p_set.add_argument("token", nargs="?", help="token value; read from stdin if omitted")
    p_set.set_defaults(func=_cmd_set_token)

    p_run = sub.add_parser(
        "run",
        help="launch claude with the profile (extra args pass through; "
        "--borrow NAME uses another profile's token for this run only)",
    )
    p_run.add_argument("name")
    p_run.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="--borrow NAME and/or arguments forwarded to claude",
    )
    p_run.set_defaults(func=_cmd_run)

    p_env = sub.add_parser(
        "env", help="view or edit a profile's claude env vars (settings.json)"
    )
    p_env.add_argument("name")
    p_env.add_argument(
        "assignments", nargs="*", metavar="KEY=VALUE", help="env vars to set"
    )
    p_env.add_argument(
        "--unset", nargs="+", metavar="KEY", help="env vars to remove"
    )
    p_env.add_argument(
        "--apply-template",
        action="store_true",
        help="merge the template's default env into the profile",
    )
    p_env.add_argument(
        "--effective",
        action="store_true",
        help="show env merged from parents (what 'run' actually uses)",
    )
    p_env.set_defaults(func=_cmd_env)

    p_parent = sub.add_parser(
        "parent", help="show, set or clear a profile's parent"
    )
    p_parent.add_argument("name")
    p_parent.add_argument("parent", nargs="?", help="parent profile to inherit from")
    p_parent.add_argument(
        "--clear", action="store_true", help="remove the profile's parent"
    )
    p_parent.set_defaults(func=_cmd_parent)

    p_tpl = sub.add_parser(
        "template", help="show or initialize the default profile template"
    )
    p_tpl.add_argument(
        "--init", action="store_true", help="write the default template file"
    )
    p_tpl.set_defaults(func=_cmd_template)

    p_export = sub.add_parser(
        "export", help="write all profile settings to a YAML file (~/.claunch.yaml)"
    )
    p_export.add_argument("path", nargs="?", help="output file (default: ~/.claunch.yaml)")
    p_export.set_defaults(func=_cmd_export)

    p_import = sub.add_parser(
        "import", help="apply profile settings from a YAML file (~/.claunch.yaml)"
    )
    p_import.add_argument("path", nargs="?", help="input file (default: ~/.claunch.yaml)")
    p_import.add_argument(
        "--prune",
        action="store_true",
        help="delete local profiles that are absent from the file",
    )
    p_import.add_argument(
        "--no-seed",
        action="store_true",
        help="do not seed global config into newly created profiles",
    )
    p_import.set_defaults(func=_cmd_import)

    p_migrate = sub.add_parser(
        "migrate",
        help="copy skills/MCP servers from a global or local path into a profile",
    )
    p_migrate.add_argument("name")
    p_migrate.add_argument(
        "source",
        nargs="?",
        help="config dir or project dir to migrate from (default: ~/.claude)",
    )
    p_migrate.add_argument("--skills", action="store_true", help="migrate skills only")
    p_migrate.add_argument("--mcp", action="store_true", help="migrate MCP servers only")
    p_migrate.add_argument(
        "--plugins", action="store_true", help="also copy the plugins directory"
    )
    p_migrate.add_argument(
        "--recursive",
        action="store_true",
        help="also migrate into all child profiles that inherit from this one",
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true", help="show what would be migrated"
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    p_validate = sub.add_parser(
        "validate",
        help="check login health via 'claude -p heartbeat' (all profiles if no name)",
    )
    p_validate.add_argument("name", nargs="?", help="profile to validate (default: all)")
    p_validate.add_argument(
        "--prompt", default="heartbeat", help="prompt to send (default: heartbeat)"
    )
    p_validate.add_argument(
        "--timeout", type=float, default=120.0, help="seconds per profile (default: 120)"
    )
    p_validate.set_defaults(func=_cmd_validate)

    p_usage = sub.add_parser("usage", help="query subscription usage for a profile")
    p_usage.add_argument("name")
    p_usage.add_argument("--json", action="store_true", help="print raw JSON")
    p_usage.set_defaults(func=_cmd_usage)

    return parser


def _harden_console() -> None:
    """Avoid UnicodeEncodeError on non-UTF-8 consoles (e.g. Windows cp949)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: Optional[List[str]] = None) -> int:
    _harden_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        profile.ProfileError,
        runner.RunnerError,
        usage.UsageError,
        CredentialsError,
        sync.SyncError,
        LineageError,
        MigrateError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
