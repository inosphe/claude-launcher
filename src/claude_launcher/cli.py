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

from . import __version__, credentials, profile, runner, seed, settings, template, usage
from .credentials import CredentialsError


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #
def _cmd_create(args: argparse.Namespace) -> int:
    p = profile.create(args.name)
    print(f"created profile {p.name!r} at {p.config_dir}")
    if not args.no_seed:
        source = Path(args.seed_from).expanduser() if args.seed_from else None
        copied = seed.seed_profile(p, source)
        if copied:
            print(f"seeded global config ({', '.join(copied)}); onboarding skipped")
        else:
            print("no global config found to seed; first run will show onboarding")
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
    for p in profiles:
        flag = "logged in" if credentials.has_token(p) else "no token"
        print(f"{p.name:<20} [{flag}]  {p.config_dir}")
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
    env = settings.get_env(p)
    if not env:
        print(f"profile {p.name!r} has no env vars set")
        return 0
    for key in sorted(env):
        print(f"{key}={env[key]}")
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
    passthrough = _strip_separator(args.args)
    return runner.run(p, passthrough)


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

    p_run = sub.add_parser("run", help="launch claude with the profile (args after -- are passed through)")
    p_run.add_argument("name")
    p_run.add_argument("args", nargs=argparse.REMAINDER, help="arguments forwarded to claude")
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
    p_env.set_defaults(func=_cmd_env)

    p_tpl = sub.add_parser(
        "template", help="show or initialize the default profile template"
    )
    p_tpl.add_argument(
        "--init", action="store_true", help="write the default template file"
    )
    p_tpl.set_defaults(func=_cmd_template)

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
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
