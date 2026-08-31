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
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote

from . import (
    cli_mesh,
    daemon_client,
    harnesses,
    lineage,
    profile as profile_mod,
    store,
    worktree,
)
from .daemon import harness as harness_def
from .daemon import paths as daemon_paths
from .daemon import restart_notice
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
    if getattr(args, "harness", None):
        print(
            "error: --harness is read-only; select a profile whose harness is "
            "configured with 'claunch set-harness PROFILE HARNESS'",
            file=sys.stderr,
        )
        return 1
    if not args.profile:
        print("error: --profile is required; the profile selects the harness", file=sys.stderr)
        return 1
    if args.role and not getattr(args, "mesh", None):
        print("error: --role requires --mesh", file=sys.stderr)
        return 1
    # Resolve from the shared config without requiring a local directory: a
    # CLI may be pointed at a named daemon instance whose reconciled storage
    # is authoritative. The daemon still performs the existence check.
    selected = lineage.effective_harness(
        profile_mod.resolve_selector(args.profile)
    )
    selected_entry = harnesses.get(selected)
    if selected != harnesses.CLAUDE_HARNESS:
        claude_only = []
        for flag, given in (
            ("--resume", args.resume is not None),
            ("--fork-session", args.fork_session),
            ("--null", args.null_token),
        ):
            if given:
                claude_only.append(flag)
        if claude_only:
            print(
                f"error: {', '.join(claude_only)} only applies to the claude "
                f"harness; profile {args.profile!r} selects {selected!r}",
                file=sys.stderr,
            )
            return 1
    if args.borrow and not (selected_entry and selected_entry.borrowable):
        print(
            f"error: --borrow is not supported by harness {selected!r}; "
            "OAuth harnesses use the selected profile's own namespaced login",
            file=sys.stderr,
        )
        return 1
    chosen_model = getattr(args, "model", None)
    if chosen_model and selected_entry and chosen_model not in selected_entry.models:
        known = ", ".join(selected_entry.models) or "(none)"
        print(
            f"error: unknown model {chosen_model!r} for harness {selected!r} "
            f"(known: {known})",
            file=sys.stderr,
        )
        return 1
    if args.borrow:
        try:
            lender_name, lender_harness = profile_mod.split_selector(args.borrow)
        except profile_mod.ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if lender_harness:
            print(
                f"error: borrow targets a base profile; use {lender_name!r}, "
                f"not {args.borrow!r}",
                file=sys.stderr,
            )
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
    # A resumed conversation belongs to the directory it was held in (claude
    # stores transcripts per cwd), so it pins the launch there -- from the
    # flag, or from a conversation flag handed straight to the harness.
    tree = worktree.resolve(
        cwd,
        args.worktree,
        resuming=(
            selected == harnesses.CLAUDE_HARNESS
            and (args.resume is not None or harness_def.steers_conversation(extra))
        ),
        rebase_onto=getattr(args, "rebase_onto", "") or "",
    )
    worktree.announce(tree)
    if tree is not None:
        cwd = str(tree.path)
    # No pane label here, deliberately. A created session runs in the daemon's
    # PTY, not in this pane; labelling the shell that launched it would claim
    # the pane is showing an agent that is somewhere else, and nothing would
    # ever take the label off again. `attach` is where a pane really does
    # become a session's terminal, so that is where the label is set -- which
    # covers `-a` below and a later `claunch attach` with the same code.
    body = {
        "name": args.name or "",
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
    if args.borrow:
        body["borrow"] = args.borrow
    if args.null_token:
        body["null_token"] = True
    if chosen_model:
        body["model"] = chosen_model
    if getattr(args, "effort", None):
        body["effort"] = args.effort
    # (the daemon echoes both back; _warn_dropped_auth reads that echo)
    # Decided at creation because they are what the session is FOR: a mesh it
    # is not in and a run it does not drive have to be arranged afterwards,
    # with the agent already sitting at a prompt not knowing either.
    for key, value in (
        ("mesh", args.mesh), ("handle", args.handle),
        ("workflow", args.workflow), ("context", args.context),
        ("task", args.task), ("issue", getattr(args, "issue", None)),
        ("issue_text", getattr(args, "issue_text", None)),
    ):
        if value:
            body[key] = value
    # Only sent when it is the answer: a missing key means "mint one", which
    # is what every caller that has never heard of this flag wants.
    if getattr(args, "no_issue", False):
        body["beads"] = False
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
    joined_role = str((info.get("mesh") or {}).get("role") or "")
    print(
        f"created session {info['name']!r} "
        f"(harness: {info['harness']}"
        + (f", profile: {info['profile']}" if info.get("profile") else "")
        + (f", role: {joined_role}" if joined_role else "")
        + f", pid: {info.get('pid')})"
    )
    _warn_dropped_auth(args, info)
    _warn_dropped_model(args, info)
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


def _run_wizard(args: argparse.Namespace, *, spawn: bool = False) -> bool:
    """Fill ``args`` in from the terminal form. False when the user backed out.

    Deliberately *before* everything else in :func:`_cmd_new_session` and
    :func:`_cmd_spawn`, and deliberately writing back onto the same namespace:
    the wizard is a second way to answer these commands, not a second way to
    create a session. ``new-session``'s refusal from inside a managed session
    is checked first (an agent must not get a form either), and the worktree,
    the spawn policy, the onboarding payload and the attach that follow are
    the code that has always run.

    The daemon is started here rather than at create time because the form's
    lists -- harnesses, profiles, workspaces, roles, meshes, workflows,
    resumable conversations, and for ``spawn`` the parent's own budget -- are
    all things only it knows. Whether there is anyone to show a form to is
    settled first, so a scripted ``--wizard`` fails having started nothing.
    """
    from . import wizard as wizard_mod

    wizard_mod.require_terminal()
    client = daemon_client.ensure_running()
    return wizard_mod.run(
        args,
        sources=wizard_mod.DaemonSources(client),
        cwd=(
            os.path.abspath(args.cwd) if getattr(args, "cwd", None) else os.getcwd()
        ),
        form=wizard_mod.SpawnWizard if spawn else wizard_mod.Wizard,
    )


def _warn_dropped_auth(args: argparse.Namespace, info: dict) -> None:
    """Say so when the daemon ignored --borrow/--null instead of honouring it.

    A daemon older than these flags reads no such key and builds the session
    anyway — which for auth is the worst kind of silence: the session comes
    up working, on the wrong login. The echo in the create/spawn response is
    how the caller can tell.
    """
    if (getattr(args, "borrow", None) and not info.get("borrow")) or (
        getattr(args, "null_token", False) and not info.get("null_token")
    ):
        print(
            "  warning: this daemon ignored the auth choice (--borrow/--null) "
            "and the session runs on the profile's own login -- the daemon "
            "may predate these flags ('claunch daemon restart')",
            file=sys.stderr,
        )


def _warn_dropped_model(args: argparse.Namespace, info: dict) -> None:
    """Report an old daemon that created the session without its model."""
    selected = getattr(args, "model", None)
    if selected is None:
        return
    wanted = str(selected).strip() or None
    if (info.get("model") or None) != wanted:
        print(
            "  warning: this daemon ignored the model choice and the session "
            "uses its harness default -- restart the daemon before relying on "
            "--model",
            file=sys.stderr,
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
        ("--task", args.task), ("--issue", getattr(args, "issue", None)),
        ("--issue-text", getattr(args, "issue_text", None)),
    ):
        if value:
            out.append(f"{flag} {value!r}" if " " in str(value) else f"{flag} {value}")
    for handle in args.connect or []:
        out.append(f"--connect {handle}")
    if args.profile:
        out.append(f"--profile {args.profile}")
        notes.append(
            "--profile needs spawn.allow_profile — without it the child "
            "runs under its parent's profile"
        )
    if args.borrow:
        out.append(f"--borrow {args.borrow}")
        notes.append(
            "--borrow needs spawn.allow_profile — without it the child "
            "authenticates the way its parent does"
        )
    if args.null_token:
        out.append("--null")
    if getattr(args, "model", None):
        out.append(f"--model {args.model}")
        notes.append("--model needs spawn.allow_args")
    for item in args.env or []:
        out.append(f"--env {item!r}" if " " in item else f"--env {item}")
    if args.env:
        notes.append("--env needs spawn.allow_env")
    if args.worktree is not worktree.ASK and args.worktree is not worktree.NEVER:
        # `spawn` grew a worktree of its own, so this is a translation now
        # rather than a refusal -- but only a named one travels: the child is
        # cut by the daemon, which has no pane to name a checkout after.
        out.append(
            f"--worktree {args.worktree}" if args.worktree
            else "--worktree <name>"
        )
        if not args.worktree:
            notes.append(
                "name the worktree: a child's is cut by the daemon, which has "
                "no Herdr pane to name one after"
            )
    extra = [a for a in (args.args or []) if a != "--"]
    if extra:
        # Last, always: everything after `--` reaches the harness verbatim,
        # so a flag appended behind it would be swallowed too.
        out.append("-- " + " ".join(extra))
        notes.append("extra harness args need spawn.allow_args")
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

    ``--wizard`` does not change who this command is for. It is refused from
    inside a session like every other form, so the only caller who ever sees
    it is the person the parent picker exists for -- an agent has its parent
    in ``$CLAUNCH_SESSION`` and needs no list to pick from.
    """
    if getattr(args, "wizard", False) and not _run_wizard(args, spawn=True):
        return 1
    if getattr(args, "harness", None):
        print(
            "error: --harness is read-only; a child gets the harness of its "
            "profile (change --profile instead)",
            file=sys.stderr,
        )
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
            # True only. The cap crosses by default, so "did not say" has to
            # reach the daemon as an ABSENT key -- and the False that
            # --within-limit sets is put back below, past the truthy filter
            # that would otherwise swallow it.
            ("over_limit", getattr(args, "over_limit", None)),
            ("fork", getattr(args, "fork", False)),
            ("mesh", args.mesh),
            ("handle", args.handle),
            ("role", args.role),
            ("connect", args.connect),
            ("workflow", args.workflow),
            ("context", args.context),
            ("task", args.task),
            ("issue", getattr(args, "issue", None)),
            ("issue_text", getattr(args, "issue_text", None)),
            ("profile", args.profile),
            ("borrow", args.borrow),
            ("null_token", args.null_token),
            ("model", getattr(args, "model", None)),
            ("effort", getattr(args, "effort", None)),
            ("args", extra),
            ("env", env),
            ("workspace", args.workspace),
            # Cut by the daemon, from the parent's own repository: the child
            # is on the daemon's filesystem and this CLI may not be, and a
            # path travelling in `cwd` is the door `spawn.allow_cwd` keeps
            # shut. See spawn.make_worktree.
            ("worktree", args.worktree),
            ("rebase_onto", args.rebase_onto),
        )
        if v
    }
    # `beads: False` is a falsey answer, and the comprehension above keeps
    # only truthy values — it has to be set after, or "no issue" would read
    # as "you did not say".
    if getattr(args, "no_issue", False):
        payload["beads"] = False
    # Same shape, same reason as `beads` above: `over_limit: False` is the
    # answer --within-limit gives, and the comprehension keeps only truthy
    # values, so it has to be set after or "hold me to the cap" would read as
    # "you did not say" and cross it.
    if getattr(args, "over_limit", None) is False:
        payload["over_limit"] = False
    # The wizard uses an explicit empty model to remove the parent's pin and
    # return to the selected harness default.  Keep that distinct from an
    # omitted value, which inherits the parent.
    if getattr(args, "model", None) == "":
        payload["model"] = ""
    if getattr(args, "effort", None) == "":
        payload["effort"] = ""
    try:
        result = client.post(f"/api/sessions/{parent}/children", payload)
    except daemon_client.DaemonClientError as exc:
        print(exc)
        return 1
    child = result.get("session") or {}
    # Before the success line, not after: a warning that scrolls past the
    # thing it is about reads as being about whatever came next.
    for warning in result.get("warnings") or ():
        print(f"warning: {warning}")
    print(f"spawned {child.get('name')} (child of {parent})")
    if args.worktree:
        landed = os.path.basename(str(child.get("cwd") or "").rstrip("/\\"))
        if landed != os.path.basename(str(args.worktree).rstrip("/")):
            # A daemon older than this reads no 'worktree' key and ignores it
            # silently, which would leave the child in the shared checkout the
            # flag was used to escape -- the one failure worth being loud about.
            print(
                f"  warning: asked for worktree {args.worktree!r} but the child "
                f"is in {child.get('cwd')} -- this daemon may predate worktree "
                "spawning ('claunch daemon restart')",
                file=sys.stderr,
            )
        else:
            print(f"  in {child['cwd']}")
    if args.workspace and child.get("cwd"):
        # The one field that was asked for by name and answered by path:
        # printing it is how the caller sees the registry resolved.
        print(f"  in {child['cwd']}")
    _warn_dropped_auth(args, child)
    _warn_dropped_model(args, child)
    _print_onboarding(result)
    if args.attach and child.get("name"):
        from . import attach as attach_mod

        return attach_mod.attach(client, child["name"])
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
    board = result.get("beads") or {}
    if board.get("issue"):
        # Compared as literals rather than against daemon.beads' constants:
        # this is the daemon's JSON answer, so the strings ARE the contract,
        # and the CLI does not import the daemon package to read one.
        mode = board.get("mode")
        if mode == "joined":
            # The one outcome a human must not have to go looking for: the
            # session is NOT the assignee, and somebody else still is.
            held = board.get("held_by") or "another session"
            went = board.get("notified")
            print(f"  issue {board['issue']}: JOINED -- {held} holds it and "
                  "keeps the assignment; the two settle ownership"
                  + (f" (told {held} on mesh {went})" if went else ""))
        elif mode == "assigned":
            print(f"  issue {board['issue']}: assigned to it")
        elif board.get("from_issue_text"):
            print(f"  issue {board['issue']}: created from the issue text")
        else:
            print(f"  issue {board['issue']}: created from the task")
    if result.get("task"):
        print("  opening task will be typed in once it settles")


def _rebrief_unavailable(name: str, exc: Exception, ident: str = "") -> str:
    """What a session hears when its re-briefing could not be fetched.

    Same shape as the block it replaces, and deliberately so: the agent has
    just lost the conversation-carried half of what it knew, and a bare error
    line does not tell it that. This says which half is missing, that the
    daemon -- not the session -- is what failed, and the one command that
    fixes it once the daemon answers again.

    It does not restate parent/mesh/run/task: every one of those is read from
    the daemon, and the daemon is what could not be reached. Guessing them
    from the environment would put stale answers in front of an agent that
    cannot tell them from fresh ones.

    ``ident`` is the id the caller asked for, when it asked for one. It is
    not decoration: the two callers are missing different things and recover
    by different commands. Naming the whole briefing at a caller that wanted
    one block is wrong on both counts, and the protocol line is the half that
    costs something -- "run the same command again" is only true if the
    command it prints is the one that was run. An agent that follows it
    literally after ``--id`` would fetch the whole briefing and still not
    hold the text it came for.
    """
    if ident:
        missing = (
            f"what is missing: the one block you asked for by id ({ident}) "
            "-- not the whole re-briefing, which is a separate call. Whether "
            "that id is still held is unknown from here: the daemon is what "
            "stores the text and it is what did not answer."
        )
        protocol = (
            "protocol: do not read this as 'no such id' -- that is a "
            "different answer and it arrives on stderr with exit 1. Run "
            f"`claunch rebrief --id {ident}` again; it will answer once the "
            "daemon is back. If it keeps failing, say that the text is "
            "unrecoverable for now rather than reconstructing it from memory "
            "-- reconstructing it is the thing the id exists to avoid."
        )
    else:
        missing = (
            "what is missing: who is reachable on your mesh, which replies "
            "you owe, where your cflow run stands, your parent and children, "
            "and your opening task. None of it is in this conversation any "
            "more."
        )
        protocol = (
            "protocol: do not carry on as if the summary above were "
            "complete. Run `claunch rebrief` again -- it is the same command "
            "and it will answer once the daemon is back. If it keeps "
            "failing, say so rather than guessing at the missing state."
        )
    head = "# claunch rebrief: unavailable"
    if ident:
        head += f" [text id: {ident}]"
    return "\n".join(
        [
            "---",
            head + " -- machine-generated",
            f"session: {name}",
            "what happened: your context was compacted or cleared, and the "
            "re-briefing that restores the derived half of it could not be "
            f"fetched -- the daemon did not answer ({exc}).",
            missing,
            protocol,
            "---",
        ]
    )


def _cmd_rebrief(args: argparse.Namespace) -> int:
    """Print a session's re-briefing — the SessionStart hook's whole job.

    Every claude session's hook runs this bare on ``compact``/``clear`` (see
    :data:`harness.REBRIEF_HOOK_SETTINGS`), and claude reads the stdout back
    into context — so stdout carries the block and nothing else, and a session
    with nothing to be told prints nothing there. The aside goes to stderr,
    for the human running it by hand: silence would read as the command
    failing, when it is the answer.

    An unreachable daemon is the one failure answered on stdout rather than
    raised (:func:`_rebrief_unavailable`): a hook that fires once, at the
    moment the context was lost, has no second chance, and an error the agent
    never sees leaves it working from a summary it believes is complete.
    """
    name = args.session or os.environ.get("CLAUNCH_SESSION")
    if not name:
        print(
            "error: no session: pass --session NAME, or run this inside a "
            "managed session (which sets $CLAUNCH_SESSION)",
            file=sys.stderr,
        )
        return 2
    # Read before the try, not inside it. This comes from the argv the caller
    # typed and needs no daemon, and the except branch below has to know which
    # of the two calls it is answering -- computed inside, it would be unbound
    # exactly when ensure_running() is what raised.
    ident = (getattr(args, "id", "") or "").strip()
    try:
        client = daemon_client.ensure_running()
        if ident:
            # One addressed block instead of the whole briefing. Printed as
            # plain text on stdout like the block is, because the caller is an
            # agent reading it back into context, not a program parsing it.
            #
            # Inside the same try as the whole-briefing fetch, and for a
            # sharper version of the same reason: a session calling with an id
            # has already worked out that the text is gone from its context.
            # An error it cannot see would leave it believing the id it holds
            # is unrecoverable rather than momentarily unreachable.
            found = client.get(
                f"/api/sessions/{name}/rebrief?id={quote(ident, safe='')}"
            )
            if found.get("status") == "recalled":
                print(f"# claunch rebrief: {found.get('kind')} [text id: {ident}]")
                print(found.get("text") or "")
                return 0
            # The daemon answered and has no such id -- a different fact from
            # not reaching it, and the caller must be able to tell them apart.
            print(found.get("note") or f"no block with id {ident!r}", file=sys.stderr)
            return 1
        block = client.get(f"/api/sessions/{name}/rebrief").get("block") or ""
    except DaemonClientError as exc:
        # The one failure that costs something. This command is a hook, it
        # fires exactly once, and the moment it fires is the moment the
        # session's derived context has just been squeezed or thrown away --
        # so a daemon that cannot be reached here is not a retry away, it is
        # a re-briefing the session never gets. Observed: s167, 2026-08-26
        # 13:19:27Z, 'daemon did not come up within 15s', 21 seconds after
        # its compaction, and nothing told it.
        #
        # The answer goes to STDOUT, because stdout is the half claude reads
        # back into context and stderr is not: an error the agent cannot see
        # is the same as no error at all. Exit 0 for the same reason a hook
        # is declared non-blocking -- failing the hook does not un-compact
        # anything, and a session that at least knows what it is missing can
        # ask for it again.
        print(_rebrief_unavailable(name, exc, ident))
        return 0
    if not block:
        print(
            f"(nothing to re-brief for {name!r}: no mesh membership, no cflow "
            "run, no parent or children, no recorded task)",
            file=sys.stderr,
        )
        return 0
    print(block)
    return 0


#: One line for why a backlog is still a backlog, keyed by the daemon's own
#: state word (``/queued``'s ``state``, walked in the delivery gate's order —
#: see :func:`daemon.api._session_queued`). The web banner says the same thing
#: in :func:`queuedReason`; this is that sentence for a terminal.
_QUEUE_HOLD_REASON = {
    "exited": "the session has exited — its backlog is delivered if it is respawned",
    "hold": "delivery is pinned shut here — release it with 'claunch delivery-hold --off'",
    "busy": "the agent is mid-turn",
    "keyboard": "a keyboard is active on this session",
    "paced": "the mesh just typed a block in here — it is spacing the next one",
    "settling": "nothing is holding it — the next delivery tick types it in",
}


def _resolve_session(args: argparse.Namespace) -> Optional[str]:
    """The session a delivery command is about: the argument, or the one we
    are running inside. Prints its own error, so callers return 2 on None."""
    name = getattr(args, "session", None) or os.environ.get("CLAUNCH_SESSION")
    if not name:
        print(
            "error: no session: pass NAME, or run this inside a managed "
            "session (which sets $CLAUNCH_SESSION)",
            file=sys.stderr,
        )
    return name


def _cmd_deliver_now(args: argparse.Namespace) -> int:
    """Type a session's held backlog into it now, because a person said so.

    The terminal's half of the dashboard's "deliver now" button — the same
    endpoint, so the same thing happens. It exists because the button did
    not: :meth:`MeshManager.flush_session` had exactly one caller, the HTTP
    route the web UI posts to, which left every operator who works from a
    shell with no way to overrule a hold at all.

    Every hold a person is in a position to overrule is dropped: the pinned
    hold, the mid-turn idle-gate, and the keyboard holds inside
    :meth:`Session.deliver` (an unsent line in the composer is submitted
    ahead of the delivery rather than refusing it). A session that has exited
    keeps its backlog, and that is reported rather than dressed up — exit 1,
    so a script can tell "I delivered it" from "I asked".
    """
    name = _resolve_session(args)
    if not name:
        return 2
    client = daemon_client.ensure_running()
    info = client.post(f"/api/sessions/{name}/queued/flush")
    flushed = int(info.get("flushed") or 0)
    handles = info.get("handles") or []
    queued = info.get("queued") or {}
    left = len(queued.get("messages") or [])
    if flushed:
        where = f" ({', '.join(handles)})" if handles else ""
        print(f"delivered {flushed} message(s) into {name!r}{where}")
    elif not left:
        print(f"nothing was queued for {name!r} — nothing to deliver")
        return 0
    else:
        print(f"nothing was delivered into {name!r}")
    if left:
        reason = _QUEUE_HOLD_REASON.get(
            str(queued.get("state") or ""), "still held"
        )
        print(f"  {left} message(s) still queued — {reason}")
    return 0 if flushed else 1


def _cmd_delivery_hold(args: argparse.Namespace) -> int:
    """Pin a session shut, or let it go again — the opposite of
    :func:`_cmd_deliver_now` and the other half of the same pair.

    With neither flag it toggles, which is what the header chip's click does;
    the flags are for a script that must not depend on what the state was.
    Nothing is dropped either way: messages accepted while held stay in their
    mesh log and go in when it is released, or when somebody says 'deliver
    now' — a hold with no way past it would be a trap rather than a setting.
    """
    name = _resolve_session(args)
    if not name:
        return 2
    want = True if args.on else (False if args.off else None)
    client = daemon_client.ensure_running()
    info = client.post(
        f"/api/sessions/{name}/queued/hold",
        {} if want is None else {"hold": want},
    )
    held = bool(info.get("hold"))
    waiting = len((info.get("queued") or {}).get("messages") or [])
    if held:
        print(
            f"session {name!r} delivery held — nothing is typed in here "
            f"until it is released ('claunch delivery-hold {name} --off') "
            f"or somebody says 'claunch deliver-now {name}'"
        )
    else:
        print(
            f"session {name!r} delivery released — the ordinary gate is "
            "back, so a message still waits out a running turn"
        )
    if waiting:
        print(f"  {waiting} message(s) waiting")
    return 0


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
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        if not daemon_client.is_absent(why):
            # Not "no sessions": we did not get to look. Said on stderr and
            # with a non-zero status because the two failures have to be
            # distinguishable to something that only counts lines -- a caller
            # piping this into `grep -c` reads an unconfirmed look as an empty
            # roster, which is how a live session once got reported as retired.
            print(
                f"{daemon_client.unreachable_reason(why)}; the session list "
                f"was not read",
                file=sys.stderr,
            )
            return 1
        print(f"{daemon_client.unreachable_reason(why)}; no sessions")
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


def _cmd_migrate_session(args: argparse.Namespace) -> int:
    """Move a session (kill, carry its conversation, relaunch) elsewhere."""
    body: dict = {"children": bool(args.children)}
    if args.to:
        # Resolved against *this* shell before it travels: the daemon would
        # resolve a relative path against its own cwd, which is nowhere the
        # person typing stands.
        body["cwd"] = os.path.abspath(args.to)
    else:
        body["worktree"] = args.worktree_name or ""
    client = daemon_client.ensure_running()
    # Roomy on purpose: a migrate is a graceful shutdown (up to ~5s), a git
    # worktree add, and a relaunch — per session, children included.
    info = client.post(
        f"/api/sessions/{args.session}/migrate", body, timeout=120.0
    )
    wt = info.get("worktree")
    where = (
        f"worktree {wt['name']!r} (branch {wt['branch']!r}, "
        f"{'created' if wt['created'] else 'reused'}): {wt['path']}"
        if wt
        else info.get("cwd", "")
    )
    carried = (
        " — conversation carried" if info.get("transcript_moved")
        else ""
    )
    print(f"session {info['name']!r} migrated to {where}{carried}")
    failed = False
    for child in info.get("children") or []:
        if child.get("ok"):
            print(f"  child {child['name']!r} migrated too")
        else:
            failed = True
            print(
                f"  child {child['name']!r} NOT migrated: {child.get('error')}",
                file=sys.stderr,
            )
    if args.attach:
        from . import attach as attach_mod

        return attach_mod.attach(client, info["name"])
    return 1 if failed else 0


def _cmd_reborrow(args: argparse.Namespace) -> int:
    """Restart a session on another answer to "whose token"."""
    client = daemon_client.ensure_running()
    body = {"borrow": args.borrow}  # None for --none and --null alike
    if args.null_token:
        body["null_token"] = True
    # Roomy like a migrate: a graceful shutdown plus a relaunch.
    info = client.post(
        f"/api/sessions/{args.session}/reborrow", body, timeout=120.0
    )
    if info.get("borrow"):
        what = f"now borrowing {info['borrow']!r}'s token"
    elif info.get("null_token"):
        what = "now running with no token (--null)"
    else:
        what = f"back on profile {info.get('profile')!r}'s own token"
    print(f"session {info['name']!r} restarted, {what} (pid {info.get('pid')})")
    if args.attach:
        from . import attach as attach_mod

        return attach_mod.attach(client, info["name"])
    return 0


def _cmd_skip_permissions(args: argparse.Namespace) -> int:
    """Restart a session with permission prompts off (or back on)."""
    client = daemon_client.ensure_running()
    # Roomy like a migrate: a graceful shutdown plus a relaunch.
    info = client.post(
        f"/api/sessions/{args.session}/skip-permissions",
        {"skip": args.mode == "on"},
        timeout=120.0,
    )
    skipping = "--dangerously-skip-permissions" in (info.get("args") or [])
    print(
        f"session {info['name']!r} restarted, "
        + (
            "now skipping permission prompts — claude acts without asking "
            "(pid {})".format(info.get("pid"))
            if skipping
            else "asking before it acts again (pid {})".format(info.get("pid"))
        )
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


def _keys_timeout() -> float:
    """How long ``send-keys`` waits on the daemon. Text (and a paste) may be
    held while a human types at that terminal — up to the daemon's
    ``CLAUNCH_TYPING_HOLD_TIMEOUT`` (see ``Session.send_keys``) — and the
    request must outlive that hold, or the CLI reports a failure for keys
    the daemon then goes on to type."""
    hold = float(os.environ.get("CLAUNCH_TYPING_HOLD_TIMEOUT") or 30.0)
    return hold + 15.0


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
            timeout=_keys_timeout(),
        )
        return 0
    if not keys:
        print("error: no keys given", file=sys.stderr)
        return 1
    client = daemon_client.ensure_running()
    client.post(
        f"/api/sessions/{args.session}/keys",
        {"keys": keys, "literal": bool(args.literal)},
        timeout=_keys_timeout(),
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
    """End a running session. On one that has already exited this does
    nothing and says so — the record stays, and stays respawnable, because
    dropping it is a different verb ('clear-sessions', or the web UI's
    remove button), never this one."""
    client = daemon_client.ensure_running()
    suffix = "?force=1" if args.force else ""
    info = client.post(f"/api/sessions/{args.session}/kill{suffix}")
    if info.get("already_exited"):
        code = info.get("exit_code")
        print(
            f"session {args.session!r} had already exited"
            + (f" (exit code {code})" if code is not None else "")
            + " — nothing to kill. It is still respawnable "
            f"('claunch respawn {args.session}'); "
            "'claunch clear-sessions' drops the record."
        )
    elif info.get("winding_down"):
        print(
            f"session {args.session!r} is winding down — it was asked to "
            "settle its board issues first, then it will be terminated. "
            "Run this again to stop it now."
        )
    else:
        print(f"session {args.session!r} killed")
    return 0


def _cmd_keep_alive(args: argparse.Namespace) -> int:
    """Set (or clear) a session's keep-alive flag.

    The flag is the user-side half of the daemon's kill-on-end: a finished
    one-shot run's driving session is recorded and ended automatically, and
    this is how a session a user said "don't close me" to survives that —
    the ending record is still written, the termination is skipped. Clear it
    (``off``) when the context is no longer wanted: after that the session
    may be ended like any other."""
    client = daemon_client.ensure_running()
    suffix = "?off=1" if getattr(args, "off", False) else ""
    info = client.post(f"/api/sessions/{args.session}/keep-alive{suffix}")
    flag = bool(info.get("keep_alive"))
    print(
        f"session {args.session!r} keep-alive "
        + ("set" if flag else "cleared")
        + " — a finished run records its ending but "
        + ("leaves this session running" if flag else "terminates this session")
    )
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
        # A stop is recorded for the same reason a restart is, and for only
        # one of the two reasons: nobody is owed a notice (nothing is coming
        # back to send one), but the successor needs the alibi. Without this
        # line an operator's 'daemon stop' followed later by 'daemon start'
        # looks, from inside the new daemon, exactly like a restart nobody
        # asked for -- and it would say so, wrongly, to every session it
        # brought back.
        restart_notice.record_request_from_env(kind=restart_notice.KIND_STOP)
        if daemon_client.stop():
            print("daemon stopped")
        else:
            print("daemon is not running")
        return 0
    if action == "restart":
        if getattr(args, "all", False):
            return _restart_all_instances()
        if getattr(args, "force", False):
            return _force_replace()
        # The ordinary path talks to the daemon, so it only works while the
        # daemon is listening. A wedged one cannot be asked anything -- say so
        # here rather than leaving the operator with "did not come up in 15s",
        # which reads as a broken install instead of a daemon to replace.
        # One full-budget round of silence buys a second look, not a verdict:
        # the brief -- the only place --force is ever recommended -- prints
        # after two consecutive rounds agree.
        report = daemon_client.diagnose()
        if report["state"] == daemon_client.WEDGED:
            print(
                f"no answer in {report['budget']:.0f}s (pid {report['pid']} "
                f"alive) — looking once more before judging",
                file=sys.stderr,
            )
            confirm = daemon_client.diagnose()
            if confirm["state"] == daemon_client.WEDGED:
                _print_wedged(report, confirm)
                return 1
            report = confirm  # it moved between looks — busy, not wedged
        # An agent session's restart goes through the web UI's approval gate
        # instead of this immediate path: the person every restart cuts off
        # gets to approve or reject first (five minutes unanswered counts as
        # approved). The distinction is the environment this CLI runs in —
        # the same one record_request uses to decide who a restart is owed
        # to below — and the daemon cannot see it, so only the CLI can draw
        # it (see restart_gate's module docstring).
        if os.environ.get("CLAUNCH_SESSION"):
            return _gated_restart()
        # Written first, and this order is the whole fix. Everything below
        # this line runs in a turn that is about to die: the daemon takes
        # every terminal attached to it down, so the print at the end reaches
        # a stdout nobody will read. Only what is on disk before the stop can
        # be handed back to the asker once a daemon exists again.
        restart_notice.record_request_from_env()
        daemon_client.stop()
        time.sleep(0.3)
        client = daemon_client.ensure_running()
        print(f"daemon restarted at {client.base_url}")
        return 0
    if action == "status":
        info = daemon_client.status()
        if info is None:
            # "cannot connect" is not the same fact as "not running", and
            # reporting them as one is what makes a wedged daemon look like an
            # absent one -- the operator then starts a replacement that stands
            # down against a lock the absent daemon is still holding.
            # status is a quick command, so it gets a quick budget -- and a
            # quick budget only earns an observation. The WEDGED verdict (and
            # any mention of --force) belongs to the full-budget path in
            # restart, which can afford to out-wait a busy daemon.
            report = daemon_client.diagnose(
                budget=daemon_client.OBSERVATION_BUDGET
            )
            if report["state"] == daemon_client.UNRESPONSIVE:
                _print_unanswered(report)
                return 1
            if report["state"] == daemon_client.STALE_RECORD:
                print(f"daemon is not running ({report['why']})")
                return 1
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


def _gated_restart() -> int:
    """A managed session's restart request, waited out to its outcome.

    The ordinary path above stops the daemon on the spot — and with it the
    turn that asked, which is the whole thing the approval gate exists to
    stop. This path asks the daemon to open the gate instead, then waits:

    - the **user** (not the asker) approves or rejects in the web UI;
    - an unanswered gate counts as approved after its deadline and the
      daemon restarts itself;
    - either way the request is attributed to this session, so when the
      restart does go out the successor daemon hands the outcome back as a
      restart notice — this turn dies with the daemon and the notice is what
      it reads afterwards.

    When nothing dies (a rejection, a daemon that went away, an absent
    daemon at request time) this command reports it and the caller's turn
    carries on.

    ``--all`` does not pass through here — its per-instance iteration wants
    the immediate path — and neither does ``--force`` when the daemon cannot
    be asked at all: a wedged daemon has no gate to wait behind, so the
    wedge branch stays immediate. ``--force`` against a daemon that IS
    answering falls through to this same gate, because then there is
    nothing to force: a session's restart waits here whatever spelling
    asked for it.
    """
    report = daemon_client.diagnose()
    state = report["state"]
    if state in (daemon_client.NOT_RUNNING, daemon_client.STALE_RECORD):
        # A restart with nothing running is a start, exactly as on the
        # immediate path — there is no daemon to host a gate.
        client = daemon_client.ensure_running()
        print(f"daemon started at {client.base_url}")
        return 0
    if state != daemon_client.SERVING:
        # Announced but not answering: the gate lives in the daemon, so the
        # ordinary choices are this or --force, like the immediate path.
        print(daemon_client.unreachable_reason(report), file=sys.stderr)
        return 1
    session = os.environ.get("CLAUNCH_SESSION") or "?"
    client = daemon_client.connect()
    if client is None:
        # Gone between the diagnosis and the ask — start nothing behind its
        # back; the caller re-runs the command.
        print("daemon went away before the request could be filed", file=sys.stderr)
        return 1
    try:
        resp = client.post("/api/daemon/restart-request", {"session": session})
    except DaemonClientError as exc:
        print(f"could not request the restart: {exc}", file=sys.stderr)
        return 1
    record = (resp or {}).get("request") or {}
    deadline_at = None
    try:
        deadline_at = datetime.fromisoformat(record.get("deadline") or "")
    except (KeyError, TypeError, ValueError):
        deadline_at = None
    # The waiting announcement goes to stderr, the scripted-console channel
    # (the file's own convention: stdout stays parseable).
    print(
        f"restart requested by session {session} — the web UI decides: "
        "approve or reject it there, and an unanswered request counts as "
        "approved after its timeout and restarts on its own",
        file=sys.stderr,
    )
    if deadline_at is not None:
        print(
            f"  counts as approved at {deadline_at.isoformat(timespec='minutes')} "
            "(this command waits for the outcome)",
            file=sys.stderr,
        )
    poll_secs = 2.0
    try:
        while True:
            if deadline_at is not None and datetime.now(timezone.utc) >= deadline_at:
                print("no answer before the deadline — counting as approved", file=sys.stderr)
                return 0
            time.sleep(poll_secs)
            try:
                resp = client.get("/api/daemon/restart-request")
            except DaemonClientError as exc:
                # The daemon is gone or stopped answering while the request
                # was pending. If a restart is what took it down, this turn
                # is already dead and never reaches this line; reaching it
                # means the request died unanswered.
                print(
                    f"cannot reach the daemon while the request was pending: "
                    f"{exc} — nothing will restart unless it comes back",
                    file=sys.stderr,
                )
                return 1
            record = (resp or {}).get("request")
            if not record:
                print(
                    "the restart request is gone without a decision — the "
                    "daemon may have restarted through another door",
                    file=sys.stderr,
                )
                return 1
            status = record.get("status")
            if status == "rejected":
                print(
                    "the restart request was rejected — nothing was restarted",
                    file=sys.stderr,
                )
                return 1
            if status == "approved":
                # The daemon is going down; the outcome travels as a restart
                # notice if this turn survives the trip at all.
                return 0
    except KeyboardInterrupt:
        print(
            "interrupted — the request stays open in the web UI until its "
            "deadline",
            file=sys.stderr,
        )
        return 1


def _print_unanswered(report: dict) -> None:
    """An unanswered short look, said as an observation and nothing more.

    Seconds cannot tell a busy daemon from a wedged one -- ensure_running
    itself sits out START_TIMEOUT of the same silence for a starting daemon.
    So no verdict and no --force here: just what was seen, and where the
    verdict-grade look lives.
    """
    print(
        f"daemon is announced but did not answer: {report['why']}",
        file=sys.stderr,
    )
    print(
        f"  a look this short cannot tell busy from stuck; "
        f"'claunch daemon restart' watches for "
        f"{daemon_client.VERDICT_BUDGET:.0f}s before judging",
        file=sys.stderr,
    )


def _log_progress_line() -> str:
    """Whether daemon.log has grown lately -- reported, never judged.

    A wedged event loop takes the daemon's logging down with it, so recent
    growth argues "alive but starving". The converse is weak and the line
    says so: a relay that is reconnecting logs a retry at least every
    BACKOFF_MAX seconds, but a connected, idle daemon may legitimately write
    nothing at all -- silence in the log must not be read as death.
    """
    log = daemon_paths.log_file()
    try:
        age = max(0.0, time.time() - log.stat().st_mtime)
    except OSError:
        return "daemon.log: missing or unreadable — no progress signal to read"
    line = f"daemon.log last grew {age:.0f}s ago"
    try:  # the bound lives with the relay; its aiohttp import stays lazy here
        from .daemon.relay_uplink import BACKOFF_MAX
    except ImportError:
        return line
    return line + (
        f" (a reconnecting relay logs at least every {BACKOFF_MAX:.0f}s; an "
        f"idle connected daemon may write nothing — weak evidence either way)"
    )


def _print_wedged(report: dict, confirm: dict) -> None:
    """The WEDGED verdict, and the decision brief that has to go with it.

    Printed only after two consecutive full-budget rounds answered nothing --
    and even then --force stays a recommendation carrying its evidence, its
    cost and its alternative, because from out here the tool cannot prove
    the daemon will never answer again. The tool judges; the person decides.
    """
    print(f"daemon is WEDGED: {report['why']}", file=sys.stderr)
    print(
        f"  confirmed twice: {report['probes'] + confirm['probes']} probes "
        f"across {report['budget'] + confirm['budget']:.0f}s in two "
        f"consecutive rounds, 0 answered",
        file=sys.stderr,
    )
    print(
        "  it still holds the singleton lock, so a replacement cannot start "
        "while it lives",
        file=sys.stderr,
    )
    print(f"  {_log_progress_line()}", file=sys.stderr)
    print(
        "  caveat: from the outside this tool cannot tell a permanently "
        "stalled daemon from one starving under load — daemons have been "
        "observed to answer nothing for minutes at a stretch and then "
        "recover on their own",
        file=sys.stderr,
    )
    print(
        "  recover with: claunch daemon restart --force  (ends that process "
        "tree and every live session in it; the new daemon restores only "
        "the sessions marked for restore)",
        file=sys.stderr,
    )
    print(
        "  or wait: a starving daemon may come back by itself — re-run "
        "'claunch daemon status' in a few minutes and force only if it "
        "stays silent",
        file=sys.stderr,
    )


def _force_replace() -> int:
    """Replace a daemon that cannot be asked to leave.

    Reserved for the wedged case and never automatic: ending the process
    skips the drain a graceful shutdown does, and a single missed health
    check is a normal thing on a loaded machine. So the state is re-checked
    here -- two consecutive rounds of the full verdict budget -- and anything
    that answers even once is sent down the ordinary path instead.
    """
    report = daemon_client.diagnose()
    if report["state"] == daemon_client.WEDGED:
        print(
            f"no answer in {report['budget']:.0f}s (pid {report['pid']} "
            f"alive) — confirming once more before ending anything",
            file=sys.stderr,
        )
        # Acting on the fresher look makes two consecutive full-budget
        # rounds of silence the price of ending anything; a daemon that
        # answers (or dies) between the looks takes its own branch below.
        report = daemon_client.diagnose()
    state = report["state"]
    if state == daemon_client.SERVING:
        print(
            "daemon is answering — no force needed; "
            "restarting it the ordinary way",
            file=sys.stderr,
        )
        # "The ordinary way" includes the gate for a session's shell: --force
        # only exists to reach a daemon that cannot be asked, and this one
        # can. The wedged branch below has no daemon to host a gate and stays
        # immediate.
        if os.environ.get("CLAUNCH_SESSION"):
            return _gated_restart()
        restart_notice.record_request_from_env(via="cli-force")
        daemon_client.stop()
        time.sleep(0.3)
        client = daemon_client.ensure_running()
        print(f"daemon restarted at {client.base_url}")
        return 0
    if state == daemon_client.NOT_RUNNING:
        client = daemon_client.ensure_running()
        print(f"daemon started at {client.base_url}")
        return 0
    pid = report.get("pid") or 0
    if state == daemon_client.WEDGED:
        print(f"daemon pid {pid} is wedged ({report['why']}); ending it")
        # A killed daemon is still a restart somebody asked for, so it leaves
        # the same record. The stale_record branch below deliberately does
        # not: there the daemon was already dead when this command arrived,
        # and claiming that death was asked for would excuse exactly the boot
        # the notice exists to flag.
        restart_notice.record_request_from_env(via="cli-force")
        if not daemon_client.terminate_process(int(pid)):
            print(
                f"error: could not end pid {pid} — end it by hand, "
                "then run 'claunch daemon start'",
                file=sys.stderr,
            )
            return 1
        print(f"pid {pid} ended (its sessions went with it; the new daemon "
              "restores the ones marked for it)")
    else:  # stale_record: the process is already gone, only the file remains
        print(f"clearing a stale record for pid {pid}")
    client = daemon_client.ensure_running()
    print(f"daemon restarted at {client.base_url}")
    return 0


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
            # Per instance, because every path this module reads resolves
            # through CLAUNCH_DAEMON -- the record lands in the instance
            # directory whose daemon is about to be stopped.
            restart_notice.record_request_from_env()
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

#: Keys the daemon re-reads from the config file at runtime (the cflow
#: reminder, stall-ping and event clocks read them every tick), so an edit
#: needs no restart.
_LIVE_KEYS = (
    "cflow_reminder", "cflow_reminder_interval",
    "cflow_ping", "cflow_ping_interval", "cflow_ping_message",
    "cflow_events",
)


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
        if args.key in _LIVE_KEYS:
            print("(applies within one clock tick; no restart needed)",
                  file=sys.stderr)
        else:
            print("(restart the daemon to apply: claunch daemon restart)",
                  file=sys.stderr)
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


def _cmd_reparent(args: argparse.Namespace) -> int:
    """Move a session under another parent, as an operator.

    The agents' ``reparent`` tool sends the same request with an ``actor``,
    which scopes it to the caller's own subtree; this command sends none, so
    it can move anything — the same split as ``kill-session`` against the
    tool's ``kill``. The daemon's own refusals (a cycle, an exited parent, the
    depth limit) apply to both.
    """
    client = daemon_client.ensure_running()
    try:
        result = client.post(
            f"/api/sessions/{args.session}/parent", {"parent": args.parent}
        )
    except daemon_client.DaemonClientError as exc:
        print(exc)
        return 1
    was = result.get("previous") or "a root"
    print(
        f"{result.get('session')}: now a child of {result.get('parent')} "
        f"(was {was}), depth {result.get('depth')}"
    )
    for edge in result.get("connected") or []:
        print(f"  mesh {edge['mesh']}: connected {edge['a']} <-> {edge['b']}")
    return 0


# --------------------------------------------------------------------------- #
# parser wiring
# --------------------------------------------------------------------------- #
#: The board answer, worded once because ``new-session`` and ``spawn`` ask it
#: the same way. Three shapes: say nothing and the daemon mints an issue from
#: the task, name one and it is adopted, ``--no-issue`` and there is none.
ISSUE_HELP = (
    "put this session on an EXISTING board issue instead of minting one from "
    "the task. What that means is the daemon's call, not the flag's: an issue "
    "nobody holds is assigned to the session, one a running session holds is "
    "joined without moving the assignment, and both sessions are told so "
    "they can settle it"
)
NOISSUE_HELP = (
    "no board issue at all -- neither minted nor adopted (without this, a "
    "session created with --task gets one minted for it)"
)
ISSUETEXT_HELP = (
    "what the minted issue SAYS, written here instead of being read off "
    "--task. Its first line becomes the title and the whole of it the goal, "
    "so the board can hold the specification while --task holds only the "
    "first instruction; the session is told to go read it. Without this the "
    "issue is minted from --task, exactly as before"
)


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
        "spelling them out as flags -- profile (with its read-only harness), borrow/null, "
        "directory, worktree, role, resume, mesh, workflow and whether to "
        "attach, each from the list the daemon publishes. Any flag given "
        "alongside it pre-fills its field",
    )
    p_new.add_argument("-s", "--name", help="session name (auto-generated if omitted)")
    p_new.add_argument("--profile", help="claunch profile (required; selects the harness)")
    p_new.add_argument(
        "--model",
        help="model alias for the selected profile harness (Claude: "
        "haiku/sonnet/opus/fable; Codex: luna/terra/sol)",
    )
    p_new.add_argument("--effort", help="reasoning effort for the selected harness")
    auth = p_new.add_mutually_exclusive_group()
    auth.add_argument(
        "--borrow", metavar="NAME",
        help="use base profile NAME's shared token for this session, keeping "
        "--profile's harness/config/env/skills; Claude also borrows NAME's "
        "provider/backend, API-key harnesses only project the token; reapplied "
        "on every restore",
    )
    auth.add_argument(
        "--null", dest="null_token", action="store_true",
        help="launch with no OAuth token at all: nothing is injected and any "
        "inherited CLAUDE_CODE_OAUTH_TOKEN is cleared, so claude starts "
        "unauthenticated (log in with /login inside)",
    )
    p_new.add_argument(
        "--harness", default=None,
        help="deprecated/read-only: configure it on the profile with set-harness",
    )
    p_new.add_argument("-c", "--cwd", help="working directory (default: current dir)")
    wt = p_new.add_mutually_exclusive_group()
    wt.add_argument(
        "--worktree", nargs="?", const="", default=worktree.ASK, metavar="NAME",
        help="run the session in a git worktree of that directory instead of "
        "the directory itself, so it cannot collide with agents working in "
        "the same checkout; bare, the worktree is named after this Herdr pane "
        "and the current time. Asked interactively when neither this nor "
        "--no-worktree is given -- except with --resume, which pins the "
        "launch to the directory holding that conversation",
    )
    wt.add_argument(
        "--no-worktree", dest="worktree", action="store_const",
        const=worktree.NEVER,
        help="use the directory as it stands, and do not ask",
    )
    p_new.add_argument(
        "--rebase-onto", dest="rebase_onto", metavar="BRANCH",
        help="with a REUSED --worktree, rebase it onto BRANCH before the "
        "agent is let in -- a checkout you come back to is as far behind as "
        "the day you left it. Local only (no fetch); a rebase that cannot be "
        "done cleanly refuses the launch rather than half-doing it",
    )
    p_new.add_argument("--cols", type=int, default=120)
    p_new.add_argument("--rows", type=int, default=30)
    p_new.add_argument("--env", action="append", metavar="KEY=VALUE", help="extra env override")
    p_new.add_argument(
        "--role",
        help="run as this role — leader, operator, worker, reviewer or "
        "specialist (aliases accepted): requires --mesh; its stance is "
        "delivered in the session opening and recovered by reminders",
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
    # The board answer, in the three shapes it has: say nothing and the daemon
    # mints an issue from --task, name one and it is adopted, --no-issue and
    # there is none. Mutually exclusive because "this issue, and also none" is
    # not a question the daemon could answer.
    n_issue = p_new.add_mutually_exclusive_group()
    n_issue.add_argument("--issue", metavar="ID", help=ISSUE_HELP)
    n_issue.add_argument(
        "--no-issue", action="store_true", dest="no_issue", help=NOISSUE_HELP,
    )
    # In the same group as the other two: the text only has meaning under the
    # answer that mints, so "this text, and also that existing issue" and
    # "this text, and also no issue" are both contradictions and are refused
    # here rather than silently resolved by the daemon.
    n_issue.add_argument(
        "--issue-text", metavar="TEXT", dest="issue_text", help=ISSUETEXT_HELP,
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
        "--wizard", action="store_true",
        help="pick the child from a form in this terminal: which session it "
        "is a child of (with what that parent may still spawn), and its "
        "profile (with its read-only harness), borrow/null, workspace, mesh, role, workflow, "
        "opening task, extra args and whether to attach -- each "
        "from the list the daemon publishes. Any flag given alongside it "
        "pre-fills its field",
    )
    p_spawn.add_argument(
        "--parent", help="parent session (default: $CLAUNCH_SESSION)"
    )
    p_spawn.add_argument(
        "--fork", action="store_true",
        help="give the child a COPY of the PARENT's conversation "
        "(--resume <the parent's> --fork-session), so it starts with "
        "everything the parent knows instead of the opening task alone -- "
        "the parent's own conversation is left untouched. Needs the claude "
        "harness and a parent that has one, and cannot be combined with "
        "--workspace or --worktree: claude keeps transcripts per directory, "
        "so a child started elsewhere would find nothing to open",
    )
    p_spawn.add_argument(
        "--over-limit", dest="over_limit", action="store_true", default=None,
        help="spawn past the parent's child cap (spawn.max_children). This "
        "is what happens anyway -- the cap is soft and warns rather than "
        "refusing -- so the flag only says it out loud",
    )
    p_spawn.add_argument(
        "--within-limit", dest="over_limit", action="store_false", default=None,
        help="be REFUSED at the parent's child cap instead of warned past it "
        "-- the strict reading of spawn.max_children, which a fan-out loop "
        "should be stopped dead by; the wizard asks the same question on its "
        "Over limit row",
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
    p_spawn.add_argument(
        "--workflow",
        help="cflow workflow to start for the child; omitted, it takes the "
             "one the parent's own run pairs children with "
             "(default_child_cflow), and none when there is no pair. "
             "'-' declines that pair",
    )
    p_spawn.add_argument("--context", help="context string for that workflow run")
    p_spawn.add_argument("--task", help="opening instruction typed into the child")
    s_issue = p_spawn.add_mutually_exclusive_group()
    s_issue.add_argument("--issue", metavar="ID", help=ISSUE_HELP)
    s_issue.add_argument(
        "--no-issue", action="store_true", dest="no_issue", help=NOISSUE_HELP,
    )
    s_issue.add_argument(
        "--issue-text", metavar="TEXT", dest="issue_text", help=ISSUETEXT_HELP,
    )
    p_spawn.add_argument(
        "--harness", help="deprecated/read-only: the selected profile owns it"
    )
    p_spawn.add_argument(
        "--profile",
        help="a different profile for the child (needs spawn.allow_profile; "
             "inherited from the parent otherwise)",
    )
    p_spawn.add_argument(
        "--model",
        help="model alias for the child; inherited when omitted and governed "
        "by spawn.allow_args when changed",
    )
    p_spawn.add_argument("--effort", help="reasoning effort for the child")
    s_auth = p_spawn.add_mutually_exclusive_group()
    s_auth.add_argument(
        "--borrow", metavar="NAME",
        help="authenticate the child with profile NAME's token (and backend) "
        "while it keeps its own config -- needs spawn.allow_profile, the "
        "same gate as --profile: both decide whose login the child holds",
    )
    s_auth.add_argument(
        "--null", dest="null_token", action="store_true",
        help="launch the child with no OAuth token at all -- it starts "
        "logged out (/login inside). Never gated: this takes a credential "
        "away rather than granting one",
    )
    p_spawn.add_argument(
        "--env", action="append", metavar="KEY=VALUE",
        help="extra env override for the child (needs spawn.allow_env)",
    )
    p_spawn.add_argument(
        "-a", "--attach", action="store_true",
        help="attach this terminal to the child right away (detach: Ctrl+])",
    )
    p_spawn.add_argument(
        "--worktree", metavar="NAME",
        help="run the child in a git worktree of its parent's repository "
        "(named, always -- the daemon cuts it and has no pane to name one "
        "after). Needs spawn.allow_worktree, which is on by default",
    )
    p_spawn.add_argument(
        "--rebase-onto", dest="rebase_onto", metavar="BRANCH",
        help="with a REUSED --worktree, rebase it onto BRANCH first (usually "
        "the parent's own branch); a rebase that cannot be done cleanly "
        "refuses the spawn",
    )
    p_spawn.add_argument(
        "--workspace", "-w", metavar="NAME",
        help="run the child in a registered workspace instead of the "
             "parent's directory ('claunch workspace ls' lists them; "
             "spawn.allow_workspace turns this off)",
    )
    p_spawn.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="extra arguments passed to the harness, REPLACING the inherited "
             "ones (prefix with -- if they start with -; needs "
             "spawn.allow_args)",
    )
    p_spawn.set_defaults(func=_cmd_spawn)

    p_reparent = sub.add_parser(
        "reparent",
        help="move a session (and its subtree) under another parent -- the "
             "operator's form of the agents' 'reparent' tool, which is scoped "
             "to their own subtree; this one is not",
    )
    p_reparent.add_argument("session", help="the session to move")
    p_reparent.add_argument("parent", help="its new parent")
    p_reparent.set_defaults(func=_cmd_reparent)

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

    p_rebrief = sub.add_parser(
        "rebrief",
        help="print a session's current briefing (mesh, run, parent, task), "
             "re-derived from the daemon -- run automatically by the claude "
             "SessionStart hook after /compact or /clear, and by hand after "
             "any context loss",
    )
    p_rebrief.add_argument(
        "--session", help="session to brief (default: $CLAUNCH_SESSION)"
    )
    p_rebrief.add_argument(
        "--id",
        help="print only the block with this content id (as printed next to "
             "the text when it was given), instead of the whole briefing",
    )
    p_rebrief.set_defaults(func=_cmd_rebrief)

    p_flush = sub.add_parser(
        "deliver-now",
        help="type a session's held mesh backlog into it now -- the terminal's "
             "half of the dashboard's 'deliver now' button; overrules a pinned "
             "hold, a running turn and a live keyboard (an unsent line in the "
             "composer is submitted first, never typed over)",
    )
    p_flush.add_argument(
        "session", nargs="?",
        help="session to deliver into (default: $CLAUNCH_SESSION)",
    )
    p_flush.set_defaults(func=_cmd_deliver_now)

    p_hold = sub.add_parser(
        "delivery-hold",
        help="pin a session shut so nothing is typed into it, or release it "
             "(no flag toggles) -- the opposite of deliver-now; nothing is "
             "dropped, a held backlog goes in when it is released",
    )
    p_hold.add_argument(
        "session", nargs="?",
        help="session to hold (default: $CLAUNCH_SESSION)",
    )
    hold_which = p_hold.add_mutually_exclusive_group()
    hold_which.add_argument("--on", action="store_true", help="hold it shut")
    hold_which.add_argument("--off", action="store_true", help="release it")
    p_hold.set_defaults(func=_cmd_delivery_hold)

    p_migrate = sub.add_parser(
        "migrate-session",
        help="move a session to a git worktree (or another directory): stop "
             "it, carry its claude conversation, relaunch it there",
    )
    p_migrate.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_migrate.add_argument("session", nargs="?")
    where = p_migrate.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--worktree", dest="worktree_name", nargs="?", const="", metavar="NAME",
        help="move into this worktree of the session's own repository "
             "(created under .claude/worktrees/, or reused; omit NAME for a "
             "generated one)",
    )
    where.add_argument(
        "--to", metavar="DIR",
        help="move into an existing directory instead of a worktree",
    )
    p_migrate.add_argument(
        "--children", action="store_true",
        help="also migrate the session's descendants that stand in the same "
             "directory (those already elsewhere stay put)",
    )
    p_migrate.add_argument(
        "-a", "--attach", action="store_true", help="attach once migrated"
    )
    p_migrate.set_defaults(func=_cmd_migrate_session_dispatch)

    p_reborrow = sub.add_parser(
        "reborrow",
        help="restart a session on another answer to 'whose token' "
             "(--borrow NAME, --none for its own, --null for none): stop "
             "it, relaunch it with the auth swapped — same name, same "
             "conversation, same directory",
    )
    p_reborrow.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_reborrow.add_argument("session", nargs="?")
    p_reborrow.add_argument(
        "borrow", nargs="?", metavar="NAME",
        help="profile whose token (and provider) the relaunch borrows",
    )
    p_reborrow.add_argument(
        "--none", action="store_true",
        help="run on the session's own profile token (clears --null too)",
    )
    p_reborrow.add_argument(
        "--null", dest="null_token", action="store_true",
        help="run with no token at all (the create form's --null)",
    )
    p_reborrow.add_argument(
        "-a", "--attach", action="store_true", help="attach once restarted"
    )
    p_reborrow.set_defaults(func=_cmd_reborrow_dispatch)

    p_skip = sub.add_parser(
        "skip-permissions",
        help="restart a session with claude's "
             "--dangerously-skip-permissions added (on) or removed (off) — "
             "stop it, relaunch it with the flag toggled: same name, same "
             "conversation, same directory",
    )
    p_skip.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_skip.add_argument("session", nargs="?")
    p_skip.add_argument("mode", nargs="?", choices=("on", "off"))
    p_skip.add_argument(
        "-a", "--attach", action="store_true", help="attach once restarted"
    )
    p_skip.set_defaults(func=_cmd_skip_permissions_dispatch)

    p_kill = sub.add_parser(
        "kill-session",
        help="kill a running session (an exited one is left alone)",
    )
    p_kill.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_kill.add_argument("session", nargs="?")
    p_kill.add_argument("--force", action="store_true", help="skip graceful terminate")
    p_kill.set_defaults(func=_cmd_kill_session_dispatch)

    p_keep = sub.add_parser(
        "keep-alive",
        help="protect a session from the automatic end-of-run kill "
             "(record is still written); 'off' lifts the protection",
    )
    p_keep.add_argument("-t", dest="session_t", help=argparse.SUPPRESS)
    p_keep.add_argument("session", nargs="?")
    p_keep.add_argument(
        "off", nargs="?", choices=("off",),
        help="clear the flag — the session may be ended at its run's close",
    )
    p_keep.set_defaults(func=_cmd_keep_alive_dispatch)

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
            p.add_argument(
                "--force", action="store_true",
                help="for a daemon that has stopped answering: end its "
                     "process (and its sessions) and start a fresh one -- "
                     "the only way past a lock a wedged daemon still holds",
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


def _cmd_migrate_session_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    return _cmd_migrate_session(args)


def _cmd_reborrow_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    # Exactly one answer to "whose token": a lender's name, --none for the
    # session's own, --null for none at all. Both and neither are the same
    # refusal the daemon would give, said before the round-trip.
    picks = sum((args.borrow is not None, args.none, args.null_token))
    if picks != 1:
        print(
            "error: pass exactly one — a profile NAME to borrow, --none for "
            "its own token, or --null for no token",
            file=sys.stderr,
        )
        return 1
    if args.none:
        args.borrow = None
    return _cmd_reborrow(args)


def _cmd_skip_permissions_dispatch(args: argparse.Namespace) -> int:
    # ``-t S on``: with -t given, the first positional is the mode, not the
    # session — argparse cannot tell them apart, so shift by hand.
    if getattr(args, "session_t", None):
        if args.session is not None and args.mode is None:
            args.mode = args.session
        args.session = args.session_t
    if not args.session:
        print("error: no session given", file=sys.stderr)
        return 1
    if not args.mode:
        print("error: pass 'on' or 'off'", file=sys.stderr)
        return 1
    return _cmd_skip_permissions(args)


def _cmd_kill_session_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    return _cmd_kill_session(args)


def _cmd_keep_alive_dispatch(args: argparse.Namespace) -> int:
    if not _resolve_target(args):
        return 1
    return _cmd_keep_alive(args)
