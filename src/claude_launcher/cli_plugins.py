"""``claunch plugin ...``, ``claunch shared ...`` and ``claunch apply`` — the shared layer's CLI.

Three verbs over one declaration (``shared`` in the config file, see
:mod:`store`): ``plugin`` edits the plugins and marketplaces in it, ``shared``
edits the ``settings.json`` keys in it, and ``apply`` converges profiles onto
it. Editing applies immediately by default, because "declare it and then
remember to run apply" is a two-step the user would have to repeat for every
profile added later; ``--no-apply`` keeps the declaration-only door open.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from . import harnesses, lineage, plugins, profile, store
from .profile import Profile


def _claude_profiles(name: Optional[str] = None) -> List[Profile]:
    """The profiles this layer applies to: everything running Claude Code.

    A profile on another harness has a config dir that ``claude`` never reads,
    so installing a Claude Code plugin into it would write files nothing loads.
    """
    if name:
        targets = [profile.require(name)]
    else:
        targets = profile.list_all()
    return [
        p for p in targets
        if lineage.effective_harness(p) == harnesses.CLAUDE_HARNESS
    ]


def _report(results: Sequence[plugins.Result], *, dry_run: bool) -> int:
    """Print one line per profile; return the exit code for the run."""
    verb = "would apply" if dry_run else "applied"
    changed = failed = 0
    for result in results:
        if result.done:
            changed += 1
            actions = ", ".join(a.describe() for a in result.done)
            print(f"  {result.profile}: {verb} {actions}")
        for action, error in result.failed:
            failed += 1
            print(
                f"  {result.profile}: FAILED {action.describe()}: "
                f"{plugins.error_line(error)}"
            )
    if not changed and not failed:
        print(f"  all {len(results)} profiles already match the declaration")
    return 1 if failed else 0


def _apply(name: Optional[str], *, dry_run: bool = False) -> int:
    targets = _claude_profiles(name)
    if not targets:
        print("no Claude Code profiles to apply to")
        return 0
    return _report(plugins.apply_all(targets, dry_run=dry_run), dry_run=dry_run)


# --------------------------------------------------------------------------- #
# claunch apply
# --------------------------------------------------------------------------- #
def _cmd_apply(args: argparse.Namespace) -> int:
    targets = _claude_profiles(args.name)
    if not targets:
        print("no Claude Code profiles to apply to")
        return 0
    if args.check:
        pending = [(p, plugins.plan(p)) for p in targets]
        drifted = [(p, actions) for p, actions in pending if actions]
        for p, actions in drifted:
            print(f"  {p.name}: missing {', '.join(a.describe() for a in actions)}")
        if not drifted:
            print(f"  all {len(targets)} profiles match the declaration")
            return 0
        print(f"{len(drifted)} of {len(targets)} profiles have drifted; run 'claunch apply'")
        return 1
    return _report(
        plugins.apply_all(targets, dry_run=args.dry_run), dry_run=args.dry_run
    )


# --------------------------------------------------------------------------- #
# claunch plugin
# --------------------------------------------------------------------------- #
def _cmd_plugin_list(args: argparse.Namespace) -> int:
    doc = store.load()
    marketplaces = store.shared_marketplaces(doc)
    declared = store.shared_plugins(doc)
    keys = store.shared_settings(doc)
    if args.json:
        print(json.dumps(
            {"marketplaces": marketplaces, "plugins": declared, "settings": keys},
            indent=2, ensure_ascii=False,
        ))
        return 0
    if not (marketplaces or declared or keys):
        print("nothing declared shared yet (claunch plugin install <plugin@marketplace>)")
        return 0
    if marketplaces:
        print("marketplaces:")
        for source in marketplaces:
            print(f"  {source}")
    if declared:
        print("plugins:")
        for plugin_id in declared:
            print(f"  {plugin_id}")
    if keys:
        print("settings:")
        for key in sorted(keys):
            print(f"  {key}={json.dumps(keys[key], ensure_ascii=False)}")
    targets = _claude_profiles()
    drifted = [p.name for p in targets if plugins.plan(p, doc)]
    if drifted:
        print(f"pending on {len(drifted)} of {len(targets)} profiles: {', '.join(drifted)}")
        print("run 'claunch apply' to converge them")
    else:
        print(f"all {len(targets)} profiles match the declaration")
    return 0


def _ensure_marketplace_for(plugin_id: str) -> None:
    """Declare the plugin's marketplace, reading its source off a profile that knows it.

    ``plugin@marketplace`` cannot install where the marketplace is unknown, and
    the usual order of events is that the user added it in one profile before
    deciding every profile should have it. Read the source back rather than
    making them type it twice. When no profile knows it either, say nothing --
    the install itself reports it, and guessing a source would be worse.
    """
    name = plugins.marketplace_of(plugin_id)
    if not name:
        return
    source = plugins.discover_marketplace_source(name, _claude_profiles())
    if source and plugins.declare_marketplace(source):
        print(f"declared marketplace {source!r} (found in an existing profile)")


def _cmd_plugin_install(args: argparse.Namespace) -> int:
    _ensure_marketplace_for(args.plugin)
    if plugins.declare_plugin(args.plugin):
        print(f"declared plugin {args.plugin!r}")
    else:
        print(f"plugin {args.plugin!r} was already declared")
    if args.no_apply:
        print("not applied (--no-apply); run 'claunch apply' when ready")
        return 0
    return _apply(args.profile)


def _cmd_plugin_uninstall(args: argparse.Namespace) -> int:
    if plugins.undeclare_plugin(args.plugin):
        print(f"undeclared plugin {args.plugin!r}")
    else:
        print(f"plugin {args.plugin!r} was not declared")
    if args.no_apply:
        print("left installed in the profiles (--no-apply)")
        return 0
    failed = 0
    for p in _claude_profiles(args.profile):
        ok, output = plugins.uninstall_from(p, args.plugin)
        if ok:
            print(f"  {p.name}: {output or 'uninstalled'}")
        else:
            failed += 1
            print(f"  {p.name}: FAILED: {plugins.error_line(output)}")
    return 1 if failed else 0


def _cmd_marketplace_add(args: argparse.Namespace) -> int:
    if plugins.declare_marketplace(args.source):
        print(f"declared marketplace {args.source!r}")
    else:
        print(f"marketplace {args.source!r} was already declared")
    if args.no_apply:
        print("not applied (--no-apply); run 'claunch apply' when ready")
        return 0
    return _apply(args.profile)


def _cmd_marketplace_remove(args: argparse.Namespace) -> int:
    if plugins.undeclare_marketplace(args.source):
        print(f"undeclared marketplace {args.source!r}")
        print("profiles keep the marketplace they already have")
        return 0
    print(f"marketplace {args.source!r} was not declared")
    return 0


# --------------------------------------------------------------------------- #
# claunch shared (settings.json keys)
# --------------------------------------------------------------------------- #
def _parse_value(raw: str):
    """A shared settings value: JSON when it parses, otherwise the literal string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _cmd_shared_set(args: argparse.Namespace) -> int:
    if args.unset:
        for key in args.unset:
            if plugins.unset_shared_setting(key):
                print(f"no longer managing settings key {key!r}")
            else:
                print(f"settings key {key!r} was not managed")
    for item in args.assignments or []:
        if "=" not in item:
            print(f"error: expected KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        key, value = item.split("=", 1)
        if not key:
            print(f"error: empty key in {item!r}", file=sys.stderr)
            return 1
        plugins.set_shared_setting(key, _parse_value(value))
        print(f"declared settings key {key}={value}")
    keys = store.shared_settings()
    if not (args.assignments or args.unset):
        if not keys:
            print("no shared settings keys declared")
            return 0
        for key in sorted(keys):
            print(f"{key}={json.dumps(keys[key], ensure_ascii=False)}")
        return 0
    if args.no_apply:
        print("not applied (--no-apply); run 'claunch apply' when ready")
        return 0
    return _apply(args.profile)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def register(sub) -> None:
    p_apply = sub.add_parser(
        "apply",
        help="converge profiles onto the shared declaration (all profiles if "
        "no name is given)",
        description=(
            "Install the declared marketplaces and plugins and write the "
            "declared settings.json keys into each Claude Code profile that is "
            "missing them. Additive: nothing a profile has on its own is removed."
        ),
    )
    p_apply.add_argument("name", nargs="?", help="one profile (default: all)")
    p_apply.add_argument(
        "--dry-run", action="store_true", help="show what would be applied"
    )
    p_apply.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 if any profile is missing something",
    )
    p_apply.set_defaults(func=_cmd_apply)

    p_plugin = sub.add_parser(
        "plugin",
        help="declare plugins/marketplaces for every profile, and install them",
        description=(
            "The declaration lives in ~/.claunch.yaml (the 'shared' block) and "
            "is applied to every Claude Code profile, including profiles created "
            "later. Installs run Claude Code's own 'claude plugin' CLI once per "
            "profile with CLAUDE_CONFIG_DIR pointed at it."
        ),
    )
    plugin_sub = p_plugin.add_subparsers(dest="plugin_command", required=True)

    p_list = plugin_sub.add_parser("list", help="show the declaration and any drift")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(func=_cmd_plugin_list)

    p_install = plugin_sub.add_parser(
        "install", aliases=["add"], help="declare a plugin and install it everywhere"
    )
    p_install.add_argument("plugin", help="plugin id, e.g. name@marketplace")
    p_install.add_argument(
        "--profile", metavar="NAME", help="apply to this profile only, not all"
    )
    p_install.add_argument(
        "--no-apply", action="store_true", help="declare only; install later via apply"
    )
    p_install.set_defaults(func=_cmd_plugin_install)

    p_uninstall = plugin_sub.add_parser(
        "uninstall",
        aliases=["remove"],
        help="undeclare a plugin and uninstall it from the profiles",
    )
    p_uninstall.add_argument("plugin", help="plugin id, e.g. name@marketplace")
    p_uninstall.add_argument(
        "--profile", metavar="NAME", help="uninstall from this profile only"
    )
    p_uninstall.add_argument(
        "--no-apply",
        action="store_true",
        help="undeclare only; leave it installed in the profiles",
    )
    p_uninstall.set_defaults(func=_cmd_plugin_uninstall)

    p_market = plugin_sub.add_parser("marketplace", help="declare marketplace sources")
    market_sub = p_market.add_subparsers(dest="marketplace_command", required=True)

    p_madd = market_sub.add_parser("add", help="declare a marketplace and register it")
    p_madd.add_argument("source", help="URL, directory path or owner/repo")
    p_madd.add_argument(
        "--profile", metavar="NAME", help="apply to this profile only, not all"
    )
    p_madd.add_argument(
        "--no-apply", action="store_true", help="declare only; register later via apply"
    )
    p_madd.set_defaults(func=_cmd_marketplace_add)

    p_mrm = market_sub.add_parser(
        "remove", help="stop declaring a marketplace (profiles keep theirs)"
    )
    p_mrm.add_argument("source", help="URL, directory path or owner/repo")
    p_mrm.set_defaults(func=_cmd_marketplace_remove)

    p_shared = sub.add_parser(
        "shared",
        help="settings.json keys every profile should carry (e.g. outputStyle)",
        description=(
            "Show, declare or stop managing the settings.json keys applied to "
            "every Claude Code profile. Values parse as JSON when they can, so "
            "true/2/[\"a\"] keep their types and anything else is a string."
        ),
    )
    p_shared.add_argument(
        "assignments", nargs="*", metavar="KEY=VALUE", help="keys to declare"
    )
    p_shared.add_argument(
        "--unset", action="append", metavar="KEY", help="stop managing this key"
    )
    p_shared.add_argument(
        "--profile", metavar="NAME", help="apply to this profile only, not all"
    )
    p_shared.add_argument(
        "--no-apply", action="store_true", help="declare only; write later via apply"
    )
    p_shared.set_defaults(func=_cmd_shared_set)
