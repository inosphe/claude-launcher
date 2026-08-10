"""CLI for profile sync: the ``sync`` client command and the ``sync-server`` side.

Argument parsing and output only; the behaviour lives in :mod:`sync` (client)
and :mod:`syncserver` (server).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import config, store, sync as sync_mod
from .sync import SyncError, SyncResult


# --------------------------------------------------------------------------- #
# client: claunch sync
# --------------------------------------------------------------------------- #
def _print_result(result: SyncResult) -> None:
    verb = "would sync" if result.dry_run else "synced"
    print(f"{verb} '{result.namespace}' with {result.url}  (mode: {result.mode})")
    if result.retried:
        print("  note: another machine pushed first; merged again on top of it")

    if result.conflicts:
        print(f"  conflicts ({len(result.conflicts)}, kept {result.conflicts[0].winner}):")
        for c in result.conflicts:
            print(f"    ! {c.path}   local={c.local!r}  remote={c.remote!r}")

    if result.local_changes:
        header = "  local changes" if not result.dry_run else "  local would change"
        print(f"{header} ({config.sync_file()}):")
        for line in result.local_changes:
            print(f"    {line}")
    if result.remote_changes:
        header = "  pushed to server" if result.pushed else "  server would change"
        print(f"{header}:")
        for line in result.remote_changes:
            print(f"    {line}")
    if not result.changed:
        print("  already in sync; nothing to do")
    print(f"  revision: {result.revision}")

    # Sync follows the launcher's standing rule: reconciliation only ever
    # *creates* profile directories. A profile deleted elsewhere therefore
    # loses its declaration here but keeps its directory (and its token) until
    # the user says so — say that out loud, or the profile looks undeleted.
    dropped = result.dropped_profiles
    if dropped and not result.dry_run:
        sys.stdout.flush()  # keep the note below the report it refers to
        if len(dropped) == 1:
            what = f"{dropped[0]!r} is no longer declared; its directory still exists"
            how = "delete it"
        else:
            names = ", ".join(repr(n) for n in dropped)
            what = f"{names} are no longer declared; their directories still exist"
            how = "delete them"
        print(f"note: {what} (run 'claunch prune' to {how})", file=sys.stderr)


def _cmd_sync(args: argparse.Namespace) -> int:
    if args.status:
        return _print_status()
    result = sync_mod.run(args.mode, prefer=args.prefer, dry_run=args.dry_run)
    _print_result(result)
    if result.conflicts and args.prefer == "local":
        # Not a failure — the merge did resolve them — but the losing side is
        # only recoverable from the server, so point at the other resolution.
        sys.stdout.flush()  # keep the note below the report it refers to
        print(
            "note: re-run with '--prefer remote' to resolve conflicts the other way",
            file=sys.stderr,
        )
    return 0


def _print_status() -> int:
    """Everything about this machine's sync setup that needs no network."""
    cfg = sync_mod.load_config()
    print(f"config file:  {store.path()}")
    print(f"server:       {cfg.url}")
    print(f"namespace:    {cfg.namespace}")
    print(f"sections:     {', '.join(cfg.sections)}")
    print(f"tls verify:   {'on' if cfg.verify_tls else 'OFF'}")
    base = sync_mod.load_base(cfg)
    print(f"merge base:   {sync_mod.base_path()}")
    if not base.matches(cfg):
        print("  (no base for this server yet; the next sync unions both sides)")
        return 0
    print(f"  last synced revision: {base.revision}")
    pending = sync_mod.diff_lines(base.doc, sync_mod.local_subset(cfg.sections))
    if pending:
        print("  local changes since then:")
        for line in pending:
            print(f"    {line}")
    else:
        print("  no local changes since then")
    print("run 'claunch sync --dry-run' to see the server side too")
    return 0


# --------------------------------------------------------------------------- #
# server: claunch sync-server
# --------------------------------------------------------------------------- #
def _root(args: argparse.Namespace) -> Path:
    return Path(args.data_dir).expanduser() if args.data_dir else config.sync_server_dir()


def _stores(args: argparse.Namespace):
    from .syncserver.docs import DocStore
    from .syncserver.users import UserStore

    root = _root(args)
    return DocStore(root), UserStore(root)


def _cmd_serve(args: argparse.Namespace) -> int:
    from .syncserver.api import serve

    docs, users = _stores(args)
    if not users.list():
        print(
            "warning: no accounts yet — nobody can sync. Create one with:\n"
            "  claunch sync-server user add <name>",
            file=sys.stderr,
        )
    try:
        return asyncio.run(serve(docs, users, args.host, args.port))
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


def _cmd_user_add(args: argparse.Namespace) -> int:
    _, users = _stores(args)
    token = users.add(args.name, args.namespace)
    user = users.get(args.name)
    print(f"created user {args.name!r} (namespaces: {', '.join(user.namespaces)})")
    print("token (shown once — the server stores only its hash):")
    print(f"  {token}")
    print("on the client machine:")
    print(f"  export CLAUNCH_SYNC_TOKEN={token}")
    return 0


def _cmd_user_ls(args: argparse.Namespace) -> int:
    docs, users = _stores(args)
    rows = users.list()
    if not rows:
        print("no users yet; add one with 'claunch sync-server user add <name>'")
        return 0
    width = max(len(u.name) for u in rows)
    for user in rows:
        print(f"{user.name:<{width}}  namespaces: {', '.join(user.namespaces)}")
    stored = docs.namespaces()
    print(f"documents: {', '.join(stored) if stored else '(none yet)'}")
    return 0


def _cmd_user_token(args: argparse.Namespace) -> int:
    _, users = _stores(args)
    token = users.rotate(args.name)
    print(f"new token for {args.name!r} (the previous one no longer works):")
    print(f"  {token}")
    return 0


def _cmd_user_rm(args: argparse.Namespace) -> int:
    _, users = _stores(args)
    users.remove(args.name)
    print(f"removed user {args.name!r} (their documents are untouched)")
    return 0


def _cmd_user_namespaces(args: argparse.Namespace) -> int:
    _, users = _stores(args)
    user = users.set_namespaces(args.name, args.namespace)
    print(f"{user.name!r} may now access: {', '.join(user.namespaces)}")
    return 0


def _cmd_docs(args: argparse.Namespace) -> int:
    docs, _ = _stores(args)
    names = docs.namespaces()
    if not names:
        print(f"no documents in {docs.docs_dir}")
        return 0
    for name in names:
        stored = docs.read(name)
        profiles = stored.doc.get("profiles")
        count = len(profiles) if isinstance(profiles, dict) else 0
        when = stored.updated_at or "?"
        by = stored.updated_by or "?"
        print(f"{name:<20} rev {stored.revision:<5} {count} profile(s)   {when} by {by}")
    return 0


# --------------------------------------------------------------------------- #
# parser wiring
# --------------------------------------------------------------------------- #
def register(sub) -> None:
    p = sub.add_parser(
        "sync",
        help="synchronize ~/.claunch.yaml with the configured sync server "
        "(--mode merge|up|down)",
        description="Reconcile this machine's profile config with a sync server. "
        "The server is described by the 'sync:' block in ~/.claunch.yaml.",
    )
    p.add_argument(
        "--mode",
        choices=list(sync_mod.MODES),
        default="merge",
        help="merge: three-way merge both ways (default); up: local wins, push; "
        "down: server wins, pull",
    )
    p.add_argument(
        "--prefer",
        choices=["local", "remote"],
        default="local",
        help="which side wins a merge conflict (default: local)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change on both sides, write nothing",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="show the local sync config and pending changes without contacting "
        "the server",
    )
    p.set_defaults(func=_cmd_sync)

    s = sub.add_parser(
        "sync-server",
        help="run the profile sync server and manage its accounts",
        description="The server half of 'claunch sync': stores one shared config "
        "document per namespace.",
    )
    s.add_argument(
        "--data-dir",
        metavar="DIR",
        help="documents + user list (default: <launcher home>/sync-server, "
        "or CLAUNCH_SYNC_SERVER_DIR)",
    )
    ssub = s.add_subparsers(dest="sync_server_command", required=True)

    q = ssub.add_parser("serve", help="run the server in the foreground")
    q.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    q.add_argument("--port", type=int, default=8378, help="bind port (default: 8378)")
    q.set_defaults(func=_cmd_serve)

    q = ssub.add_parser("docs", help="list stored documents and their revisions")
    q.set_defaults(func=_cmd_docs)

    u = ssub.add_parser("user", help="manage accounts and their tokens")
    usub = u.add_subparsers(dest="sync_user_command", required=True)

    q = usub.add_parser("add", help="create an account and print its token (once)")
    q.add_argument("name")
    q.add_argument(
        "--namespace",
        action="append",
        metavar="NS",
        help="namespace this account may sync (repeatable; default: its own name; "
        "'*' for all)",
    )
    q.set_defaults(func=_cmd_user_add)

    q = usub.add_parser("ls", help="list accounts")
    q.set_defaults(func=_cmd_user_ls)

    q = usub.add_parser("token", help="issue a new token, invalidating the old one")
    q.add_argument("name")
    q.set_defaults(func=_cmd_user_token)

    q = usub.add_parser("namespaces", help="replace the namespaces an account may sync")
    q.add_argument("name")
    q.add_argument("namespace", nargs="+", metavar="NS")
    q.set_defaults(func=_cmd_user_namespaces)

    q = usub.add_parser("rm", help="remove an account")
    q.add_argument("name")
    q.set_defaults(func=_cmd_user_rm)


__all__ = ["register", "SyncError"]
