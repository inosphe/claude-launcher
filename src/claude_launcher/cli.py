"""Command-line interface for claude-launcher.

This module only parses arguments and formats output; all behaviour lives in the
``profile``, ``runner`` and ``usage`` modules.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

from pathlib import Path

from . import (
    __version__,
    bootstrap,
    borrowing,
    cli_beads,
    cli_commits,
    cli_cflow,
    cli_connections,
    cli_loops,
    cli_mesh,
    cli_plugins,
    cli_report,
    cli_search,
    cli_sessions,
    cli_sync,
    cli_transcript,
    cli_window,
    cli_project,
    cli_workspace,
    config,
    credentials,
    harness_policy,
    harnesses,
    herdr,
    lineage,
    metering,
    migrate as migrate_mod,
    migrate_config,
    pi_provider,
    provider_spec,
    plugins as plugins_mod,
    profile,
    prompt_input,
    providers,
    routing,
    prune as prune_mod,
    runner,
    seed,
    settings,
    stdio,
    store,
    template,
    usage,
    projects,
    workspaces,
    worktree,
)
from .cflow.engine import CflowError
from .cflow.model import WorkflowError
from .cflow.state import StateError as CflowStateError
from .daemon import harness as harness_def
from .daemon import paths as daemon_paths
from .daemon import instance_manifest
from .daemon_client import DaemonClientError
from .credentials import CredentialsError
from .lineage import LineageError
from .migrate import MigrateError
from .prompt_input import PromptInputError
from .providers import ProviderError
from .routing import RoutingError
from .sync import SyncError
from .syncserver.docs import SyncServerError
from .wizard import WizardUnavailable


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #
def _carries_seeded_files(p: profile.Profile) -> bool:
    """Whether the profile already holds a file ``seed`` would copy.

    ``--reinit`` asks seed for missing files only, so an empty result means
    either that the profile already had them or that the seed source had none.
    The summary line says which.
    """
    return any(
        (p.config_dir / name).is_file()
        for name in (seed.CONFIG_FILENAME, seed.SETTINGS_FILENAME)
    )


def _cmd_create(args: argparse.Namespace) -> int:
    if args.parent:
        profile.require(args.parent)  # fail before creating if parent is missing
    if args.harness and harnesses.get(args.harness) is None:
        raise LineageError(
            f"unknown harness {args.harness!r} (known: {', '.join(harnesses.names())})"
        )
    # Validate the would-be config before creating a directory. A denied
    # --harness must not leave a half-created profile behind.
    prospective = profile.resolve(args.name)
    prospective_doc = store.load()
    section = prospective_doc.get("profiles")
    if not isinstance(section, dict):
        section = {}
        prospective_doc["profiles"] = section
    entry = section.setdefault(prospective.name, {})
    if not isinstance(entry, dict):
        entry = {}
        section[prospective.name] = entry
    if args.parent:
        entry["parent"] = args.parent
    if args.harness:
        harness_policy.require(
            prospective, args.harness, doc=prospective_doc
        )
    else:
        lineage.effective_harness(prospective, prospective_doc)
    if args.reinit:
        # A create that stopped on a store write leaves its directory behind
        # with no entry to match it, and running create again then refuses --
        # the directory "already exists". So the initialization is finished in
        # place instead: register the directory (retrying the write that
        # failed), then run the same tail below. Skipping seeding is not an
        # option here: the failure may have landed before it.
        p = profile.require(args.name)
        store.ensure_profile(p.name)
        verb = "reinitializing"
    else:
        p = profile.create(args.name)
        verb = "created"
    if args.parent:
        lineage.set_parent(p, args.parent)
    if args.harness:
        # Parent first: inherited allowed_harnesses is part of deciding whether
        # this explicit pin is legal.
        lineage.set_harness(p, args.harness)
    print(f"{verb} profile {p.name!r} at {p.config_dir}")
    selected = lineage.effective_harness(p)
    is_claude = selected == harnesses.CLAUDE_HARNESS
    if not args.no_seed and is_claude:
        source = Path(args.seed_from).expanduser() if args.seed_from else None
        # --reinit fills only what is missing: this directory may already have
        # been used, and seed writes the destination unconditionally.
        copied = seed.seed_profile(p, source, missing_only=args.reinit)
        if copied:
            print(f"seeded global config ({', '.join(copied)}); onboarding skipped")
        elif args.reinit and _carries_seeded_files(p):
            print("seed: profile already carries its global config; left as it is")
        else:
            print("no global config found to seed; first run will show onboarding")
    elif not args.no_seed:
        print(f"skipped Claude Code config seed for harness {selected!r}")
    if args.parent:
        print(f"inherits from parent {args.parent!r} (env + harness/auth)")
        parent = profile.require(args.parent)
        copied = (
            migrate_mod.migrate(p, parent.config_dir, skills=True, mcp=True)
            if is_claude
            else None
        )
        bits = []
        if copied and copied.skills:
            bits.append(f"skills: {', '.join(copied.skills)}")
        if copied and copied.mcp_servers:
            bits.append(f"mcp: {', '.join(copied.mcp_servers)}")
        if bits:
            print(f"copied from parent ({'; '.join(bits)})")
    if is_claude:
        # A new profile is one more copy of the harness-global state, so it
        # starts at whatever the shared declaration says -- otherwise it is
        # born drifted and someone has to remember to converge it later.
        shared = plugins_mod.apply_to(p)
        for action in shared.done:
            print(f"applied shared {action.describe()}")
        for action, error in shared.failed:
            line = plugins_mod.error_line(error)
            print(f"could not apply shared {action.describe()}: {line}")
    entry = harnesses.get(selected)
    if entry and entry.auth == "api-key":
        print(f"next: claunch set-token {p.name}")
    else:
        print(f"next: claunch login {p.name}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    p = profile.remove(args.name)
    print(f"removed profile {p.name!r} ({p.config_dir})")
    return 0


def _provider_cell(p: profile.Profile, doc: dict) -> str:
    """Provider column for ``list``: ``name`` if pinned here, ``(name)`` if inherited.

    Plain Anthropic that nobody pinned shows as ``-``; a broken parent chain shows
    as ``?`` rather than aborting the listing.
    """
    own = store.profile_entry(p.name, doc).get("provider")
    if own:
        return str(own)
    try:
        effective = providers.resolve_name(p, doc)
    except LineageError:
        return "?"
    if effective == providers.DEFAULT_PROVIDER:
        return "-"
    return f"({effective})"


def _cmd_list(_args: argparse.Namespace) -> int:
    rows = lineage.tree()
    if not rows:
        print("no profiles yet; create one with 'claunch create <name>'")
        return 0
    labels = {
        "ok": "logged in",
        "expired": "token expired",
        "inherited": "inherited",
        "none": "no token",
        "managed": "harness auth",
    }
    names = {p.name: "  " * depth + p.name for p, depth in rows}
    width = max(20, max(len(n) for n in names.values()))
    known = {p.name for p, _ in rows}
    doc = store.load()
    provs = {p.name: _provider_cell(p, doc) for p, _ in rows}
    pwidth = max(len(v) for v in provs.values())
    selected = {}
    for p, _ in rows:
        try:
            selected[p.name] = lineage.effective_harness(p)
        except LineageError:
            selected[p.name] = "?"
    hwidth = max(len(v) for v in selected.values())
    for p, depth in rows:
        try:
            flag = labels[lineage.login_state(p)]
        except LineageError:  # broken lineage must not abort the listing
            flag = "parent cycle"
        parent = lineage.get_parent(p)
        # the indent already shows a resolved parent; only call out broken links
        note = ""
        if parent and depth == 0:
            why = "missing" if parent not in known else "cycle"
            note = f"  (parent: {parent}, {why})"
        print(
            f"{names[p.name]:<{width}} [{flag:<13}]  "
            f"{selected[p.name]:<{hwidth}}  {provs[p.name]:<{pwidth}}  "
            f"{p.config_dir}{note}"
        )
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    p = profile.require_selector(args.name)
    if not p.harness_override:
        print(p.config_dir)
        return 0
    entry = runner.profile_harness(p)
    print(entry.profile_home(p.config_dir))
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    p = profile.require_selector(args.name)
    entry = runner.profile_harness(p)
    if entry.auth == "api-key":
        raise runner.RunnerError(
            f"harness {entry.name!r} uses the profile token as an API key; run "
            f"'claunch set-token {p.name}'"
        )
    command = [entry.program(), *(entry.login_args or ["setup-token"])]
    print(
        f"running {' '.join(command)!r} for profile {p.selector!r} "
        f"(harness: {entry.name})...",
        file=sys.stderr,
    )
    return runner.login(p)


def _cmd_env(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    if args.unset:
        settings.unset_env(p, args.unset)
    if args.clear:
        # A key with no value takes it away from what the template and the
        # parent chain would give this profile.
        settings.set_env(p, {key: None for key in args.clear})
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
        if env[key] is None:
            print(f"{key}  (cleared: the template's or a parent's value is not used)")
        else:
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
    # The live default env lives in ~/.claunch.yaml; template.yaml only seeds it.
    print(f"source of truth: {store.path()}")
    path = template.template_path()
    suffix = "" if path.is_file() else "  (not created; built-in defaults used to bootstrap)"
    print(f"bootstrap template: {path}{suffix}")
    fields = template.layer()
    env = template.env()
    if fields:
        print("defaults under every profile (a profile or parent that sets the field wins):")
        for key in sorted(fields):
            print(f"  {key}: {fields[key]}")
    if env:
        print("common env under every profile:")
        for key in sorted(env):
            print(f"  {key}={env[key]}")
    if not fields and not env:
        print("defaults: (none)")
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
    targets = (
        [profile.require_selector(args.name)] if args.name else profile.list_all()
    )
    if not targets:
        print("no profiles to validate")
        return 0
    failed = 0
    for p in targets:
        entry = runner.profile_harness(p)
        if entry.builtin:
            claude_credential = (
                lineage.lookup_token(p)
                if providers.uses_anthropic_oauth(providers.resolve_name(p))
                else lineage.stored_auth_token(p)
            )
            if claude_credential is None:
                failed += 1
                print(
                    f"{p.selector:<20} FAIL  no auth "
                    f"(run 'claunch login {p.name}' or "
                    f"'claunch set-token {p.name}')"
                )
                continue
        if entry.auth == "api-key" and lineage.login_state(p) == "none":
            failed += 1
            print(
                f"{p.selector:<20} FAIL  no API key "
                f"(run 'claunch set-token {p.name}')"
            )
            continue
        result = runner.heartbeat(p, prompt=args.prompt, timeout=args.timeout)
        if result.ok:
            snippet = " ".join(result.output.split())[:50]
            print(f"{p.selector:<20} OK    {snippet}")
        else:
            failed += 1
            reason = " ".join(result.reason.split())[:60]
            print(f"{p.selector:<20} FAIL  {reason}")
    return 1 if failed else 0


def _cmd_prune(args: argparse.Namespace) -> int:
    targets = prune_mod.orphans()
    if not targets:
        print("nothing to prune; every profile dir is declared in the store")
        return 0
    for p in targets:
        if args.dry_run:
            print(f"would remove {p.name!r} ({p.config_dir})")
        else:
            profile.remove(p.name)
            print(f"removed {p.name!r} ({p.config_dir})")
    return 0


def _credential_profile(value: str, command: str) -> profile.Profile:
    """Resolve the base profile used by the one shared credential file."""
    name, harness_override = profile.split_selector(value)
    if harness_override:
        raise CredentialsError(
            f"{command} targets the base profile because all harness variants "
            f"share one token; use 'claunch {command} {name}'"
        )
    return profile.require(name)


def _cmd_set_token(args: argparse.Namespace) -> int:
    p = _credential_profile(args.name, "set-token")
    token = args.token
    if not token:
        token = stdio.read_stdin_line()
    credentials.save_token(p, token)
    print(f"stored token for profile {p.name!r}")
    return 0


def _cmd_set_harness(args: argparse.Namespace) -> int:
    p = profile.require(args.name)
    if args.clear and args.harness:
        raise LineageError("give a harness name or --clear, not both")
    if args.clear:
        lineage.clear_harness(p)
        print(
            f"cleared harness override on {p.name!r} "
            f"(effective: {lineage.effective_harness(p)})"
        )
        return 0
    if not args.harness:
        own = store.profile_entry(p.name).get("harness")
        effective = lineage.effective_harness(p)
        suffix = "" if own else " (inherited/default)"
        print(f"harness: {effective}{suffix}")
        return 0
    lineage.set_harness(p, args.harness)
    print(f"profile {p.name!r} now runs harness {args.harness!r}")
    return 0


def _cmd_get_token(args: argparse.Namespace) -> int:
    p = _credential_profile(args.name, "get-token")
    if args.own:
        token = credentials.own_token(p)
        if not token:
            raise CredentialsError(
                f"profile {p.name!r} has no token of its own "
                f"(run 'claunch login {p.name}' first)"
            )
    else:
        token = lineage.lookup_token(p)
        if not token:
            raise CredentialsError(
                f"no token for profile {p.name!r}; "
                f"run 'claunch login {p.name}' first"
            )
    # Bare value on stdout so it pipes cleanly (e.g. into env or a clipboard);
    # the token is a secret, so nothing else is printed on this stream.
    print(token)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    p = profile.require_selector(args.name)
    selected = lineage.effective_harness(p)
    _announce_translation(p, selected)
    # `args` is argparse.REMAINDER, so it also captures launcher flags like
    # --borrow / --add-prompt that appear after the profile name; pull those out
    # here, then drop a leading `--` separator before forwarding the rest.
    wt_choice, rest = _extract_worktree(list(args.args or []))
    borrow_name, rest = _extract_borrow(rest)
    provider_name = None
    null_token = add_prompt = False
    if selected == harnesses.CLAUDE_HARNESS:
        null_token, rest = _extract_null(rest)
        if null_token and borrow_name is not None:
            raise profile.ProfileError(
                "--null launches without any OAuth token; "
                f"it cannot be combined with --borrow {borrow_name}"
            )
        provider_name, rest = _extract_value_flag(rest, "--provider")
        add_prompt, rest = _extract_add_prompt(rest)
    tools_choice = None
    entry_for_tools = harnesses.get(selected)
    if entry_for_tools is not None and entry_for_tools.tools:
        tools_value, rest = _extract_value_flag(rest, "--tools")
        if tools_value is not None:
            tools_choice = parse_tools_flag(tools_value, entry_for_tools)
    passthrough = _strip_separator(rest)
    # Before --add-prompt: that one opens an editor, and answering "which
    # worktree?" after writing a system prompt is the wrong order to be asked
    # in. A failure here is fatal on purpose -- a worktree that was asked for
    # and could not be made must not silently launch in the shared checkout.
    #
    # `--resume` (and its siblings) settles the question on its own: claude's
    # transcripts are per-directory, so the conversation being resumed lives
    # in *this* one. Read from the whole forwarded list, since everything in
    # it reaches claude.
    tree = worktree.resolve(
        os.getcwd(),
        wt_choice,
        resuming=(
            selected == harnesses.CLAUDE_HARNESS
            and harness_def.steers_conversation(passthrough)
        ),
    )
    worktree.announce(tree)
    if add_prompt:
        text = prompt_input.collect()
        if text:
            # Prepend so it reaches claude even alongside REMAINDER passthrough;
            # --append-system-prompt *adds* to the built-in prompt (never replaces).
            passthrough = ["--append-system-prompt", text, *passthrough]
            print("added context to the system prompt for this run", file=sys.stderr)
        else:
            print(
                "no prompt entered; launching without --append-system-prompt",
                file=sys.stderr,
            )
    if provider_name:
        providers.provider_env(provider_name)  # fail fast on an unknown name
    borrow = None
    borrow_report = None
    if borrow_name:
        borrow, borrow_report = borrowing.require_allowed(
            p,
            borrow_name,
            entry=harnesses.get(selected),
            provider_override=provider_name,
        )
    if borrow is not None:
        print(
            f"borrowing {borrow.name!r} for this run: {borrow_report.message}",
            file=sys.stderr,
        )
    # `run` occupies the pane for as long as claude lives, so the pane says so
    # for exactly that long: the profile (run's nearest thing to a session
    # name), the branch, and the directory it is all happening in. Cleared
    # afterwards, because a label for an agent that has exited still reads as
    # true.
    where = str(tree.path) if tree is not None else os.getcwd()
    labelled = herdr.rename_pane(worktree.pane_label(p.name, where))
    try:
        return runner.run(
            p,
            passthrough,
            borrow=borrow,
            provider=provider_name,
            null_token=null_token,
            cwd=str(tree.path) if tree is not None else None,
            tools=tools_choice,
        )
    finally:
        if labelled:
            herdr.clear_pane_label()


def parse_tools_flag(value: str, entry) -> "list[str]":
    """``--tools a,b`` -> the names; ``--tools none`` -> ``[]``.

    Validated against the harness declaration so a typo fails here rather
    than as a tool the model never sees.
    """
    text = str(value or "").strip()
    if text.lower() in ("none", "off", ""):
        return []
    names = []
    for part in text.split(","):
        name = part.strip()
        if not name:
            continue
        if name not in entry.tools:
            raise profile.ProfileError(
                f"unknown tool {name!r} for harness {entry.name!r} "
                f"(known: {', '.join(entry.tools)})"
            )
        if name not in names:
            names.append(name)
    return names


def _cmd_tools(args: argparse.Namespace) -> int:
    """Show or set a profile's default builtin tools (``harness_options``)."""
    p = profile.require(args.name)
    harness_name = args.harness or "pi"
    entry = harnesses.get(harness_name)
    if entry is None:
        raise LineageError(f"unknown harness {harness_name!r}")
    if not entry.tools:
        print(f"harness {harness_name!r} declares no builtin tools")
        return 0
    changes = {}
    for name in args.on or []:
        if name not in entry.tools:
            raise LineageError(
                f"unknown tool {name!r} for harness {harness_name!r} "
                f"(known: {', '.join(entry.tools)})"
            )
        changes[name] = True
    for name in args.off or []:
        if name not in entry.tools:
            raise LineageError(
                f"unknown tool {name!r} for harness {harness_name!r} "
                f"(known: {', '.join(entry.tools)})"
            )
        changes[name] = False
    if changes:
        def _mutate(doc: dict) -> None:
            entry_doc = store._writable_entry(doc, p.name)
            options = entry_doc.get(provider_spec.HARNESS_OPTIONS_FIELD)
            if not isinstance(options, dict):
                options = {}
                entry_doc[provider_spec.HARNESS_OPTIONS_FIELD] = options
            block = options.get(harness_name)
            if not isinstance(block, dict):
                block = {}
                options[harness_name] = block
            switches = block.get("tools")
            if not isinstance(switches, dict):
                switches = {}
                block["tools"] = switches
            for name, on in changes.items():
                if on:
                    switches.pop(name, None)  # "on" is the default: no entry
                else:
                    switches[name] = False
            if not switches:
                block.pop("tools", None)
            if not block:
                options.pop(harness_name, None)
            if not options:
                entry_doc.pop(provider_spec.HARNESS_OPTIONS_FIELD, None)

        store.update(_mutate)
    enabled = set(pi_provider.enabled_tools(p, entry)) if harness_name == "pi" else set()
    if harness_name != "pi":
        spec = providers.spec_for(p)
        switches = spec.options(harness_name).get("tools") or {}
        enabled = {t for t in entry.tools if switches.get(t, True) is not False}
    print(f"profile {p.name!r}, harness {harness_name!r}: default builtin tools")
    for name in entry.tools:
        print(f"  {name:<12} {'on' if name in enabled else 'off'}")
    print(
        "(a session may still choose otherwise: --tools on run/new-session/"
        "spawn, the wizard's Pi tools row, or the web form)"
    )
    return 0


def _announce_translation(p, harness_name: str) -> None:
    """Say before launch what the harness could not carry from the spec."""
    entry = harnesses.get(harness_name)
    if entry is None or entry.builtin:
        return
    try:
        translation = runner.harness_translation(p, entry)
    except runner.RunnerError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return
    for note in translation.notes:
        print(f"note: {note}", file=sys.stderr)


def _cmd_set_provider(args: argparse.Namespace) -> int:
    # Records the choice in the config file (~/.claunch.yaml). Forms:
    #   set-provider NAME            -> global provider
    #   set-provider PROFILE NAME    -> pin a profile (NAME may be 'default')
    #   set-provider PROFILE --clear -> drop a profile's override (inherit)
    #   set-provider --clear         -> drop the global provider
    if args.clear:
        if args.provider is not None:
            print("error: --clear takes no PROVIDER", file=sys.stderr)
            return 1
        if args.name_or_provider is None:
            providers.clear_active()
            print("cleared global provider (back to 'default')")
            return 0
        p = profile.require(args.name_or_provider)
        providers.clear_profile_selection(p)
        print(f"cleared provider override on {p.name!r} (inherits global/default)")
        return 0
    if args.name_or_provider is None:
        print("error: provider name required", file=sys.stderr)
        return 1
    if args.provider is None:
        name = args.name_or_provider
        providers.set_active(name)
        if name == providers.DEFAULT_PROVIDER:
            print("global provider reset to 'default' (anthropic)")
        else:
            print(f"global provider set to {name!r}")
        return 0
    p = profile.require(args.name_or_provider)
    name = args.provider
    providers.set_profile_selection(p, name)
    if name == providers.DEFAULT_PROVIDER:
        print(f"profile {p.name!r} pinned to 'default' (anthropic)")
    else:
        print(f"profile {p.name!r} now uses provider {name!r}")
    return 0


def _cmd_providers(_args: argparse.Namespace) -> int:
    doc = store.load()
    registry = providers.registry(doc)
    global_choice = providers.active(doc) or providers.DEFAULT_PROVIDER
    print(f"config file: {config.sync_file()}")
    print(f"global provider: {global_choice}")
    print("available providers:")
    pinned = routing.configured(doc)
    for name in sorted(registry):
        url = registry[name].get("ANTHROPIC_BASE_URL", "")
        suffix = f"  -> {url}" if url else ""
        # A routing pin changes which upstream actually serves the request, so
        # it belongs next to the backend URL rather than one command away.
        if name in pinned:
            suffix += f"  [routing: {_routing_line(pinned[name])}]"
        allowed = harness_policy.provider_constraint(name, doc)
        if allowed is not None:
            suffix += f"  [harnesses: {', '.join(allowed) or '(none)'}]"
        print(f"  {name}{suffix}")
        for line in _spec_lines(name, doc):
            print(f"      {line}")
    rows = []
    for p in profile.list_all():
        eff = providers.resolve_name(p, doc)
        if eff != providers.DEFAULT_PROVIDER:
            rows.append((p.name, eff))
    if rows:
        print("profiles using a provider:")
        for n, v in rows:
            print(f"  {n:<20} {v}")
    return 0


def _spec_lines(name: str, doc: dict) -> list:
    """The harness-neutral description of a provider, one fact per line."""
    try:
        spec = providers.spec(name, doc)
    except providers.ProviderError:
        return []
    lines = []
    if spec.legacy_env is not None:
        lines.append("legacy env (run `claunch migrate-config`)")
    for protocol in provider_spec.PROTOCOLS:
        url = spec.endpoint(protocol)
        if url:
            lines.append(f"{protocol}: {url}")
    if spec.models:
        lines.append(
            "models: "
            + ", ".join(
                f"{role}={spec.models[role]}"
                for role in provider_spec.MODEL_ROLES
                if role in spec.models
            )
        )
    window = []
    if spec.context_window:
        window.append(f"context_window={spec.context_window}")
    if spec.auto_compact_at:
        window.append(f"auto_compact_at={spec.auto_compact_at}")
    if window:
        lines.append("  ".join(window))
    reasoning = []
    if spec.reasoning_effort:
        reasoning.append(f"reasoning_effort={spec.reasoning_effort}")
    if spec.openai_reasoning_format:
        reasoning.append(
            f"openai_reasoning_format={spec.openai_reasoning_format}"
        )
    if reasoning:
        lines.append("  ".join(reasoning))
    for harness_name, block in sorted(spec.harness_options.items()):
        shown = ", ".join(
            f"{channel}={value}" if not isinstance(value, dict)
            else f"{channel}: {', '.join(map(str, value))}"
            for channel, value in block.items()
        )
        lines.append(f"{harness_name} options: {shown}")
    return lines


def _cmd_migrate_config(args: argparse.Namespace) -> int:
    try:
        report = migrate_config.run(dry_run=args.dry_run)
    except migrate_config.MigrateConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    verb = "would change" if args.dry_run else "changed"
    if report.changed:
        print(f"{verb}:")
    for line in report.lines:
        print(line)
    for line in report.warnings:
        print(f"warning: {line}", file=sys.stderr)
    return 0


def _routing_line(block: dict) -> str:
    """One-line rendering of a routing spec, in the field order people read."""
    order = ["order", "only", "ignore", "sort", "allow_fallbacks"]
    keys = [k for k in order if k in block] + [k for k in block if k not in order]
    parts = []
    for key in keys:
        value = block[key]
        if isinstance(value, list):
            rendered = ",".join(map(str, value))
        else:
            # JSON spelling, because that is what goes over the wire — a
            # Python-shaped `False` here would read as a different setting.
            rendered = json.dumps(value)
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


def _cmd_routing(_args: argparse.Namespace) -> int:
    """Show which providers pin their routing, and which shims are serving it."""
    doc = store.load()
    declared = routing.configured(doc)
    print(f"config file: {config.sync_file()}")
    if not declared:
        print("no provider declares a routing spec")
        print(
            "  set one with: claunch routing set <provider> --order coreweave "
            "--no-fallbacks"
        )
    else:
        print("providers with body routing:")
        for name in sorted(declared):
            url = providers.registry(doc).get(name, {}).get("ANTHROPIC_BASE_URL", "")
            print(f"  {name}{f'  -> {url}' if url else ''}")
            print(f"    {_routing_line(declared[name])}")
    live = routing.instances()
    if live:
        print("running shims:")
        for info in live:
            print(
                f"  {info['fingerprint']}  {info['url']}  -> {info['upstream']}"
                f"  (pid {info.get('pid', '?')})"
            )
            print(f"    {_routing_line(info.get('spec') or {})}")
    else:
        print("running shims: none (one starts with the next launch)")
    return 0


def _cmd_routing_set(args: argparse.Namespace) -> int:
    block: dict = {}
    for key, raw in (("order", args.order), ("only", args.only), ("ignore", args.ignore)):
        if raw:
            slugs = [s.strip() for s in raw.split(",") if s.strip()]
            if not slugs:
                print(f"error: --{key} needs at least one provider slug", file=sys.stderr)
                return 2
            block[key] = slugs
    if args.sort:
        block["sort"] = args.sort
    if args.allow_fallbacks is not None:
        block["allow_fallbacks"] = args.allow_fallbacks
    if not block:
        print(
            "error: nothing to set (try --order coreweave --no-fallbacks)",
            file=sys.stderr,
        )
        return 2
    routing.set_spec(args.provider, block)
    print(f"provider {args.provider!r} routing: {_routing_line(block)}")
    print("takes effect on the next launch (running sessions keep their shim)")
    return 0


def _cmd_routing_clear(args: argparse.Namespace) -> int:
    routing.set_spec(args.provider, None)
    print(f"cleared routing on provider {args.provider!r}")
    return 0


def _cmd_routing_stop(args: argparse.Namespace) -> int:
    if not args.all and not args.fingerprint:
        print("error: name a shim fingerprint, or pass --all", file=sys.stderr)
        return 2
    stopped = routing.stop(None if args.all else [args.fingerprint])
    if not stopped:
        print("no matching shim was running")
        return 0
    for fp in stopped:
        print(f"stopped shim {fp}")
    return 0


def _fmt(value, unit: str = "") -> str:
    return "-" if value is None else f"{value}{unit}"


def _tps_summary_lines(label: str, s: dict) -> List[str]:
    return [
        f"  {label}",
        f"    requests {s['requests']} (counted {s['counted']})  "
        f"out {s['output_tokens']} tok  in {s['input_tokens']} tok  "
        f"cache-read {s['cache_read']} tok",
        f"    tps median {_fmt(s['tps_median'])}  mean {_fmt(s['tps_mean'])}  "
        f"min {_fmt(s['tps_min'])}  max {_fmt(s['tps_max'])}  "
        f"ttft median {_fmt(s['ttft_ms_median'], 'ms')}",
        # The line above is tokens over the whole call, which every counted
        # call has. The backend's own speed is a different measurement, and
        # only the calls that were watched arriving a piece at a time have it.
        f"    generation median {_fmt(s['generation_median'])}  "
        f"(over the {s['generation_n']} calls that streamed and were timed)",
    ]


def _cmd_tps(args: argparse.Namespace) -> int:
    """Throughput of API-key provider calls, from the shim's records."""
    if args.clear:
        n = metering.clear()
        print(f"removed {n} record file(s) under {metering.records_dir()}")
        return 0
    records = metering.load(session=args.session, upstream=args.upstream)
    if args.json:
        print(json.dumps(records[-args.last:] if args.last else records, indent=2))
        return 0
    doc = store.load()
    print(f"records: {metering.records_dir()}  (metering {'on' if metering.enabled(doc) else 'OFF'})")
    if not records:
        print("no records yet — one is written per /v1/messages call that goes "
              "through a provider shim (API-key providers on the claude harness)")
        return 0
    print("total:")
    for line in _tps_summary_lines("all", metering.summarize(records)):
        print(line)
    for key in ("model", "session"):
        groups = metering.by_key(records, key)
        if len(groups) > 1 or key == "model":
            print(f"by {key}:")
            for name, s in groups.items():
                for line in _tps_summary_lines(name, s):
                    print(line)
    if args.last:
        print(f"last {args.last}:")
        print(f"  {'ts':<20} {'session':<12} {'model':<28} {'out':>6} {'ttft':>7} {'tps':>7} status")
        for rec in records[-args.last:]:
            print(
                f"  {str(rec.get('ts') or '')[:19]:<20} "
                f"{str(rec.get('session') or '-')[:12]:<12} "
                f"{str(rec.get('model') or '-')[:28]:<28} "
                f"{_fmt(rec.get('output_tokens')):>6} "
                f"{_fmt(rec.get('ttft_ms'), 'ms'):>7} "
                f"{_fmt(metering.reported_tps(rec)):>7} "
                f"{_fmt(rec.get('status'))}"
            )
    return 0


def _cmd_harnesses(_args: argparse.Namespace) -> int:
    """List the declared harnesses and whether this machine can run them."""
    reg = harnesses.registry()
    print(f"config file: {config.sync_file()}")
    print("declared harnesses:")
    for name in harnesses.names():
        h = reg[name]
        state = "ready" if h.available() else "not installed"
        run = "profile-managed" if h.builtin else " ".join(h.command + h.args)
        print(f"  {name:<10} [{state:<13}] {run}")
        if h.description:
            print(f"             {h.description}")
    print(
        "\ndeclare or override one under 'harnesses:' in the config file "
        "(name: null drops a packaged one)"
    )
    return 0


def add_install_scope_args(parser: argparse.ArgumentParser) -> None:
    """The one scope vocabulary every install-ish command shares.

    Used by ``claunch install`` and its ``cflow install`` / ``mesh install``
    aliases, so the three cannot drift apart. The scopes are mutually
    exclusive by construction; project is the default.
    """
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--project",
        nargs="?",
        const=".",
        default=None,
        metavar="DIR",
        help="install into a project (.mcp.json + .claude/skills; "
        "DIR defaults to the current directory) — the default scope",
    )
    scope.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="install for the user globally (~/.claude/skills + user-scope "
        "MCP config) and seed the global workflow layer",
    )
    scope.add_argument("--profile", help="install into this profile's config dir")
    scope.add_argument(
        "--all-profile",
        "--all",
        dest="all_",
        action="store_true",
        help="install into every existing profile (not the user's global "
        "setup; that stays --global)",
    )


def run_install(
    profile_name: Optional[str],
    project: Optional[str],
    global_: bool = False,
    all_: bool = False,
) -> int:
    """Register the MCP server and every skill — the body of ``claunch install``.

    Shared with the ``cflow install`` / ``mesh install`` aliases, which now
    install the same lot: the tools ship in one server, so there is no half of
    it to register on its own.
    """
    from . import install as install_mod
    from .cflow import state as cflow_state

    if all_:
        done = install_mod.install_into_all_profiles()
        if not done:
            print("note: no profiles exist; nothing to install into")
            return 0
    elif global_:
        done = install_mod.install_into_user()
    elif profile_name:
        # Installation follows the same PROFILE[:HARNESS] selector contract
        # as run, validate, usage, and the daemon APIs.
        done = install_mod.install_into_profile(profile.require_selector(profile_name))
    else:
        done = install_mod.install_into_project(Path(project or ".").resolve())
    for line in done:
        print(f"installed: {line}")
    if not global_ and not profile_name and not all_:
        # A project install stays inside its project; if nothing has seeded
        # the machine's workflow layer yet, say where that happens.
        if not any(cflow_state.global_workflows_dir().glob("*.y*ml")):
            print(
                "note: the global workflow layer is empty; "
                "'claunch install --global' seeds the bundled workflows"
            )
    print("note: restart active agent sessions for the MCP server and skills to be picked up")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    return run_install(args.profile, args.project, args.global_, args.all_)


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from . import mcp_server

    return mcp_server.serve()


def _cmd_usage(args: argparse.Namespace) -> int:
    p = usage.resolve_target(profile.require_selector(args.name))
    report = usage.fetch(p)
    if args.json:
        import json

        print(json.dumps(report.raw, indent=2))
        return 0
    _print_usage(p.selector, report)
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


def _extract_value_flag(
    args: List[str], flag: str
) -> "tuple[Optional[str], List[str]]":
    """Pull a ``<flag> VALUE`` / ``<flag>=VALUE`` option out of run passthrough.

    Stops at a ``--`` separator so anything explicitly forwarded to claude after
    it is left untouched.
    """
    value: Optional[str] = None
    rest: List[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            rest.extend(args[i:])
            break
        if token == flag:
            if i + 1 >= len(args):
                raise profile.ProfileError(f"{flag} requires a value")
            value = args[i + 1]
            i += 2
            continue
        if token.startswith(flag + "="):
            value = token.split("=", 1)[1]
            i += 1
            continue
        rest.append(token)
        i += 1
    return value, rest


def _extract_borrow(args: List[str]) -> "tuple[Optional[str], List[str]]":
    """Pull a ``--borrow NAME`` / ``--borrow=NAME`` flag out of run passthrough."""
    return _extract_value_flag(args, "--borrow")


def _extract_worktree(args: List[str]) -> "tuple[worktree.Choice, List[str]]":
    """Pull the ``--worktree`` / ``--no-worktree`` answer out of run passthrough.

    Three outcomes, matching :data:`claude_launcher.worktree.Choice`: a name
    from ``--worktree=NAME``, ``""`` from a bare ``--worktree`` (name it for
    me), :data:`~claude_launcher.worktree.NEVER` from ``--no-worktree``, and
    :data:`~claude_launcher.worktree.ASK` when neither appears.

    Unlike ``--borrow``, a bare ``--worktree`` does **not** consume the next
    token: ``run`` forwards everything it does not recognise to claude, and
    ``claunch run nc --worktree "fix the parser"`` must send that prompt to
    claude rather than try to name a branch after it. The value form needs the
    ``=``. Stops at ``--``, like its siblings.
    """
    choice: worktree.Choice = worktree.ASK
    rest: List[str] = []
    for i, token in enumerate(args):
        if token == "--":
            rest.extend(args[i:])
            break
        if token == "--worktree":
            choice = ""
            continue
        if token.startswith("--worktree="):
            choice = token.split("=", 1)[1]
            continue
        if token == "--no-worktree":
            choice = worktree.NEVER
            continue
        rest.append(token)
    return choice, rest


def _extract_bool_flag(args: List[str], flag: str) -> "tuple[bool, List[str]]":
    """Pull a boolean ``<flag>`` out of run passthrough.

    Like :func:`_extract_borrow`, it stops at a ``--`` separator so a literal
    flag explicitly forwarded to claude is left untouched.
    """
    found = False
    rest: List[str] = []
    for i, token in enumerate(args):
        if token == "--":
            rest.extend(args[i:])
            break
        if token == flag:
            found = True
            continue
        rest.append(token)
    return found, rest


def _extract_add_prompt(args: List[str]) -> "tuple[bool, List[str]]":
    """Pull a boolean ``--add-prompt`` flag out of run passthrough."""
    return _extract_bool_flag(args, "--add-prompt")


def _extract_null(args: List[str]) -> "tuple[bool, List[str]]":
    """Pull a boolean ``--null`` flag out of run passthrough.

    ``--null`` launches with no OAuth token at all: nothing is injected and
    any inherited ``CLAUDE_CODE_OAUTH_TOKEN`` is cleared, so claude starts
    unauthenticated (e.g. to /login fresh inside claude).
    """
    return _extract_bool_flag(args, "--null")


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
    suffix = {
        "ratelimit-headers": "  (via rate-limit headers)",
        "codex-app-server": "  (via Codex app-server)",
        "kimi-managed-usage": "  (via Kimi Code usage API)",
        "kimi-web-server": "  (via Kimi Code local server)",
    }.get(report.source, "")
    print(f"usage for profile {name!r}{suffix}")
    active = [w for w in report.windows if w.utilization > 0 or w.resets_at]
    windows = active or report.windows
    if not windows:
        print("  no usage windows reported")
        return
    for w in windows:
        bar = _bar(w.utilization)
        note = _fmt_reset(w.resets_at)
        if w.status and w.status not in ("allowed", "ok"):
            note = f"{note}  [{w.status}]".strip()
        print(f"  {w.name:<18} {bar} {w.utilization:5.1f}%  {note}")


def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claunch",
        description="Run CLI agent harnesses under isolated profiles and authentication.",
    )
    parser.add_argument("--version", action="version", version=f"claude-launcher {__version__}")
    parser.add_argument(
        "-L",
        "--instance",
        metavar="NAME",
        help="target the named daemon instance (tmux -L style; every daemon/"
        "session/mesh command then talks to that instance's server — equivalent "
        "to setting CLAUNCH_DAEMON=NAME)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create a new profile (seeds global config)")
    p_create.add_argument("name")
    p_create.add_argument(
        "--reinit",
        action="store_true",
        help="finish the setup of a profile whose directory already exists "
        "(a create that stopped on a config-file write leaves one behind)",
    )
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
    p_create.add_argument(
        "--harness",
        help="harness this profile runs (default: inherit, then claude)",
    )
    p_create.set_defaults(func=_cmd_create)

    p_remove = sub.add_parser(
        "remove", aliases=["delete", "rm"], help="delete a profile and its tokens"
    )
    p_remove.add_argument("name")
    p_remove.set_defaults(func=_cmd_remove)

    p_list = sub.add_parser("list", aliases=["ls"], help="list profiles")
    p_list.set_defaults(func=_cmd_list)

    p_path = sub.add_parser(
        "path", help="print a profile root, or a PROFILE:HARNESS namespaced home"
    )
    p_path.add_argument("name")
    p_path.set_defaults(func=_cmd_path)

    p_login = sub.add_parser("login", help="run the profile harness's login flow")
    p_login.add_argument("name")
    p_login.set_defaults(func=_cmd_login)

    p_set = sub.add_parser(
        "set-token",
        help="store the profile's one shared token (paste it or pipe via stdin)",
    )
    p_set.add_argument("name")
    p_set.add_argument("token", nargs="?", help="token value; read from stdin if omitted")
    p_set.set_defaults(func=_cmd_set_token)

    p_tools = sub.add_parser(
        "tools",
        help="show or set a profile's default builtin tools for a harness "
        "(pi: full_read); written to harness_options.<harness>.tools",
    )
    p_tools.add_argument("name", help="profile name")
    p_tools.add_argument(
        "--harness", default=None, help="harness whose tools to set (default: pi)"
    )
    p_tools.add_argument(
        "--on", action="append", metavar="TOOL", help="enable a tool by default"
    )
    p_tools.add_argument(
        "--off", action="append", metavar="TOOL", help="disable a tool by default"
    )
    p_tools.set_defaults(func=_cmd_tools)

    p_hselect = sub.add_parser(
        "set-harness", help="show, pin or clear a profile's harness"
    )
    p_hselect.add_argument("name")
    p_hselect.add_argument("harness", nargs="?")
    p_hselect.add_argument(
        "--clear", action="store_true", help="inherit from the parent/default"
    )
    p_hselect.set_defaults(func=_cmd_set_harness)

    p_get = sub.add_parser(
        "get-token",
        help="print a profile's OAuth token to stdout (resolves inheritance; "
        "use --own to require the profile's own token)",
    )
    p_get.add_argument("name")
    p_get.add_argument(
        "--own",
        action="store_true",
        help="print only the profile's own token, without inheriting a parent's",
    )
    p_get.set_defaults(func=_cmd_get_token)

    p_run = sub.add_parser(
        "run",
        help="launch PROFILE or PROFILE:HARNESS (extra args pass through; "
        "--borrow NAME uses another profile's token for this run only; "
        "--null clears CLAUDE_CODE_OAUTH_TOKEN and injects nothing; "
        "--provider NAME overrides the API provider for this run only; "
        "--add-prompt opens an editor to append text to the system prompt; "
        "--worktree[=NAME] / --no-worktree answer the git-worktree question "
        "this run would otherwise ask; --tools a,b|none picks the builtin "
        "tools for a harness that declares them, e.g. PROFILE:pi)",
    )
    p_run.add_argument("name")
    p_run.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="--borrow NAME, --null, --provider NAME, --add-prompt, "
        "--worktree[=NAME] (bare = name it after this pane and the time; the "
        "value form needs the '='), --no-worktree, and/or arguments forwarded "
        "to claude",
    )
    p_run.set_defaults(func=_cmd_run)

    p_env = sub.add_parser(
        "env",
        help="view or edit a profile's common env vars (every harness, "
        "inherited along the parent chain, over the template's env)",
    )
    p_env.add_argument("name")
    p_env.add_argument(
        "assignments", nargs="*", metavar="KEY=VALUE", help="env vars to set"
    )
    p_env.add_argument(
        "--unset", nargs="+", metavar="KEY", help="env vars to remove"
    )
    p_env.add_argument(
        "--clear",
        nargs="+",
        metavar="KEY",
        help="write KEY with no value, so the template's or a parent's value "
        "is not used",
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
        "template",
        help="show the profile template (the layer under every profile) or "
        "initialize its bootstrap file",
    )
    p_tpl.add_argument(
        "--init", action="store_true", help="write the default template file"
    )
    p_tpl.set_defaults(func=_cmd_template)

    p_prune = sub.add_parser(
        "prune",
        help="delete local profile dirs not declared in the store (~/.claunch.yaml)",
    )
    p_prune.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be removed without deleting anything",
    )
    p_prune.set_defaults(func=_cmd_prune)

    p_mig_cfg = sub.add_parser(
        "migrate-config",
        help="rewrite ~/.claunch.yaml from provider/profile `env` to the "
        "harness-neutral schema (api_key, endpoints, models, context_window, "
        "auto_compact_at, reasoning_effort, openai_reasoning_format, "
        "harness_options); a .v1.bak copy is kept",
    )
    p_mig_cfg.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    p_mig_cfg.set_defaults(func=_cmd_migrate_config)

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

    p_setprov = sub.add_parser(
        "set-provider",
        help="select a provider globally ('set-provider NAME') or for one profile "
        "('set-provider PROFILE NAME'); records it in the config file; "
        "use 'default' for plain Anthropic",
    )
    p_setprov.add_argument(
        "name_or_provider",
        nargs="?",
        metavar="PROFILE|PROVIDER",
        help="provider name (global), or a profile name when PROVIDER follows",
    )
    p_setprov.add_argument(
        "provider",
        nargs="?",
        metavar="PROVIDER",
        help="provider to pin on the named profile ('default' = plain Anthropic)",
    )
    p_setprov.add_argument(
        "--clear",
        action="store_true",
        help="drop the override (profile inherits, or global resets to default)",
    )
    p_setprov.set_defaults(func=_cmd_set_provider)

    p_provs = sub.add_parser(
        "providers",
        help="list API providers from the config file and which one is active",
    )
    p_provs.set_defaults(func=_cmd_providers)

    p_routing = sub.add_parser(
        "routing",
        help="show (and manage) providers that pin routing in the request body, "
        "plus the local shims serving them",
    )
    p_routing.set_defaults(func=_cmd_routing)
    routing_sub = p_routing.add_subparsers(dest="routing_cmd")

    p_rset = routing_sub.add_parser(
        "set", help="declare a routing spec on a provider (e.g. pin CoreWeave)"
    )
    p_rset.add_argument("provider")
    p_rset.add_argument(
        "--order", help="comma-separated provider slugs to try, in order"
    )
    p_rset.add_argument("--only", help="comma-separated slugs to allow, nothing else")
    p_rset.add_argument("--ignore", help="comma-separated slugs to skip")
    p_rset.add_argument(
        "--sort", help="upstream sort key (backend's vocabulary, e.g. price)"
    )
    fallbacks = p_rset.add_mutually_exclusive_group()
    fallbacks.add_argument(
        "--no-fallbacks",
        dest="allow_fallbacks",
        action="store_false",
        default=None,
        help="fail rather than serve the request from an unlisted provider",
    )
    fallbacks.add_argument(
        "--allow-fallbacks",
        dest="allow_fallbacks",
        action="store_true",
        default=None,
        help="let the backend fall back to other providers (its default)",
    )
    p_rset.set_defaults(func=_cmd_routing_set)

    p_rclear = routing_sub.add_parser(
        "clear", help="drop a provider's routing spec (back to the backend's default)"
    )
    p_rclear.add_argument("provider")
    p_rclear.set_defaults(func=_cmd_routing_clear)

    p_rstop = routing_sub.add_parser("stop", help="shut a running routing shim down")
    p_rstop.add_argument("fingerprint", nargs="?")
    p_rstop.add_argument("--all", action="store_true", help="stop every shim")
    p_rstop.set_defaults(func=_cmd_routing_stop)

    p_tps = sub.add_parser(
        "tps",
        help="throughput (tokens/s, time to first token) of API-key provider "
        "calls, from the records the provider shims write",
    )
    p_tps.add_argument("--session", help="only requests sent by this session")
    p_tps.add_argument("--upstream", help="only upstreams containing this text")
    p_tps.add_argument(
        "-n", "--last", type=int, default=10, help="list the last N requests (0: none)"
    )
    p_tps.add_argument("--json", action="store_true", help="dump records as JSON")
    p_tps.add_argument("--clear", action="store_true", help="delete every record file")
    p_tps.set_defaults(func=_cmd_tps)

    p_harn = sub.add_parser(
        "harnesses",
        help="list the declared harnesses (claude, codex, pi, ...) and "
        "whether this machine can run them",
    )
    p_harn.set_defaults(func=_cmd_harnesses)

    p_install = sub.add_parser(
        "install",
        help="give an agent the claunch toolkit: register the MCP server "
        "(workflow + mesh + team-building tools) and write the /cflow, "
        "/mesh and commit-stamp skills (--project, --global, --profile, "
        "or --all-profile)",
    )
    add_install_scope_args(p_install)
    p_install.set_defaults(func=_cmd_install)

    p_mcp = sub.add_parser(
        "mcp",
        help="the stdio MCP server itself (spawned by claude, not by hand)",
    )
    p_mcp.set_defaults(func=_cmd_mcp)

    cli_sessions.register(sub)
    cli_mesh.register(sub)
    cli_cflow.register(sub)
    cli_sync.register(sub)
    cli_workspace.register(sub)
    cli_project.register(sub)
    cli_beads.register(sub)
    cli_search.register(sub)
    cli_transcript.register(sub)
    cli_plugins.register(sub)
    cli_report.register(sub)
    cli_commits.register(sub)
    cli_window.register(sub)
    cli_connections.register(sub)
    cli_loops.register(sub)

    return parser


def _harden_console() -> None:
    """Avoid UnicodeEncodeError on non-UTF-8 consoles (e.g. Windows cp949)."""
    stdio.harden_console()


def main(argv: Optional[List[str]] = None) -> int:
    _harden_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "instance", None):
        try:
            # Into the environment so daemon paths, auto-started daemons and
            # any spawned children all target the same instance.
            os.environ[daemon_paths.INSTANCE_ENV] = daemon_paths.validate_instance(
                args.instance
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        # Before anything resolves a path: an instance manifest may move the
        # launcher home, the config file and the port for this process tree.
        instance_manifest.apply()
    except (ValueError, instance_manifest.InstanceManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        # Read the source of truth and reconcile local state (migrate any legacy
        # config, materialize declared-but-missing profile dirs) before running.
        bootstrap.run()
        return args.func(args)
    except (
        profile.ProfileError,
        harness_policy.HarnessPolicyError,
        borrowing.BorrowError,
        runner.RunnerError,
        usage.UsageError,
        CredentialsError,
        LineageError,
        MigrateError,
        PromptInputError,
        ProviderError,
        RoutingError,
        store.StoreError,
        SyncError,
        SyncServerError,
        DaemonClientError,
        CflowError,
        WorkflowError,
        CflowStateError,
        workspaces.WorkspaceError,
        projects.ProjectError,
        worktree.WorktreeError,
        WizardUnavailable,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
