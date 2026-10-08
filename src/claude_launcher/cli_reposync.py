"""``claunch reposync`` -- keep a repository in step across machines.

    claunch reposync status [WORKSPACE] [--peer M]...
    claunch reposync master [WORKSPACE] [--remote R] [--branch B] [--dry-run] [--yes]
    claunch reposync branch push BRANCH [WORKSPACE] [--remote R] [--force-with-lease] [--yes]
    claunch reposync branch pull MACHINE BRANCH [WORKSPACE] [--remote R]

See :mod:`reposync` (git) and :mod:`reposync_flow` (peers, approval, board).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Tuple

from . import reposync, reposync_flow as flow


def _out(args, doc: dict, lines) -> int:
    if getattr(args, "json", False):
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)
    return 0


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #
def _approve(card: dict, *, yes: bool, c) -> Tuple[Optional[dict], dict]:
    """``(None, approval)`` when the push may go ahead -- ``approval`` says
    how, for the history; otherwise ``(refusal, {})``.

    ``auto`` needs nobody. Under ``ask``, a person at their own terminal says
    y (or passed --yes); a managed session waits on the web UI's card, and
    its --yes is not an answer -- the asker cannot be the approver.
    """
    if flow.approval_mode() == flow.AUTO:
        return None, {"mode": flow.AUTO}
    if not os.environ.get("CLAUNCH_SESSION"):
        if yes:
            return None, {"mode": "terminal", "yes": True}
        if not sys.stdin.isatty():
            return {"status": "needs_approval", "why": "not a terminal -- pass --yes"}, {}
        answer = input(f"{card['summary']}\nproceed? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return None, {"mode": "terminal"}
        return {"status": "declined"}, {}
    record = flow.file_request(c, card)
    print(
        f"waiting for approval in the web UI (request {record['id']}, "
        f"expires {record['deadline']}): {card['summary']}",
        file=sys.stderr,
    )
    record = flow.wait_request(c, record["id"])
    if record.get("status") == "approved":
        return None, flow.card_approval(record)
    return {"status": record.get("status"), "request": record["id"]}, {}


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _cmd_status(args: argparse.Namespace) -> int:
    repo, workspace = flow.resolve_repo(args.workspace)
    c = flow.client()
    me, _ = flow.relay_identity(c)
    rows, problems = flow.discover(repo, c, args.peer or ())
    local = reposync.remote_keys(repo)
    lines = [f"{workspace or '-'}  {repo}  (this machine: {me})"]
    for key, name in local.items():
        lines.append(f"  remote {name:<20} {key}")
    for row in rows:
        lines.append(
            f"  peer {row['machine']}:{row['workspace']} shares {row['key']} "
            f"(here {row['local_remote']}, there {row['peer_remote']})"
        )
        for branch, info in row["branches"].items():
            for t in info.get("tracking") or []:
                if t.get("key") == row["key"]:
                    lines.append(
                        f"    there {branch} {info['sha'][:8]}: +{t.get('ahead')} -{t.get('behind')} "
                        f"vs {t['remote']}/{branch} as last fetched there"
                    )
    for problem in problems:
        lines.append(f"  ({problem})")
    plan_doc = None
    if not args.no_fetch:
        try:
            remote = reposync.choose_remote(
                repo, args.remote, [r["key"] for r in rows] if rows else None
            )
            p = reposync.plan(repo, remote, args.branch)
            plan_doc = p.to_dict()
            lines.append(
                f"  here {p.branch} {p.local[:8]}: +{p.ahead} -{p.behind} vs "
                f"{p.remote}/{p.branch} -> {p.action}"
            )
        except reposync.SyncError as exc:
            lines.append(f"  (no plan: {exc})")
    return _out(args, {"repo": repo, "workspace": workspace, "machine": me,
                       "remotes": local, "peers": rows, "problems": problems,
                       "plan": plan_doc}, lines)


# --------------------------------------------------------------------------- #
# master
# --------------------------------------------------------------------------- #
def _cmd_master(args: argparse.Namespace) -> int:
    repo, workspace = flow.resolve_repo(args.workspace)
    c = flow.client()
    peer_keys = None
    if not args.remote:
        rows, _ = flow.discover(repo, c, args.peer or ())
        peer_keys = [r["key"] for r in rows] if rows else None
    remote = reposync.choose_remote(repo, args.remote, peer_keys)
    p = reposync.plan(repo, remote, args.branch)
    key = reposync.url_key(reposync.remotes(repo).get(remote, ""))
    card = flow.plan_card(p, workspace=workspace, key=key)
    lines = [f"{p.branch} {p.local[:8]} vs {remote}/{p.branch} {p.upstream[:8]}: "
             f"+{p.ahead} -{p.behind} -> {p.action}"]
    if p.action == reposync.REBASE:
        lines.append(f"  {len(p.merges)} landing merge(s) will be recreated; "
                     f"{len(p.affected)} branch(es) stay on the old commits")
        lines += [f"    {name}" for name in p.affected]
    if args.dry_run or p.action == reposync.UP_TO_DATE:
        return _out(args, {"plan": p.to_dict(), "dry_run": args.dry_run}, lines)
    approval = {"mode": "none"}
    if p.pushes:
        refusal, approval = _approve(card, yes=args.yes, c=c)
        if refusal:
            print("\n".join(lines))
            return _err(f"not synced: {refusal}")
    entry = flow.history_entry(repo, kind="master", workspace=workspace, remote=remote,
                               branch=p.branch, by=flow.actor(), approval=approval)
    result = flow.recorded(entry, lambda: reposync.execute(p, scratch=flow.scratch()),
                           flow.master_done(p))
    reposync.git(repo, "config", reposync.REMOTE_CONFIG, remote, check=False)
    board = []
    if result.rewritten:
        board = flow.annotate_board(repo, result.rewritten, remote=remote, branch=p.branch)
    lines.append(f"done: {p.branch} = {result.new[:8]}" + (" (pushed)" if result.pushed else ""))
    for row in result.restack:
        lines.append(f"  restack {row['branch']}"
                     + (f" in {row['worktree']}" if row["worktree"] else "")
                     + f": {row['command']}")
    for row in board:
        lines.append(f"  board {row['issue']}: REBASED {row['old'][:8]} -> {row['new'][:8]}"
                     + ("" if row["ok"] else " (comment failed)"))
    return _out(args, {**result.to_dict(), "board": board}, lines)


# --------------------------------------------------------------------------- #
# branch
# --------------------------------------------------------------------------- #
def _cmd_branch_push(args: argparse.Namespace) -> int:
    repo, workspace = flow.resolve_repo(args.workspace)
    c = flow.client()
    me, _ = flow.relay_identity(c)
    remote = reposync.choose_remote(repo, args.remote)
    target = reposync.shared_ref(me, args.branch)
    sha = reposync.git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{args.branch}", check=False)
    if not sha:
        return _err(f"no local branch {args.branch!r}")
    card = {
        "repo": repo, "workspace": workspace, "remote": remote, "branch": target,
        "action": "publish", "local": sha, "upstream": "",
        "summary": f"push {args.branch} {sha[:8]} to {remote}/{target}"
                   + (" (force-with-lease)" if args.force_with_lease else ""),
    }
    refusal, approval = _approve(card, yes=args.yes, c=c)
    if refusal:
        return _err(f"not pushed: {refusal}")
    entry = flow.history_entry(repo, kind="publish", workspace=workspace, remote=remote,
                               branch=args.branch, by=flow.actor(), approval=approval,
                               ref=target, force=bool(args.force_with_lease))
    flow.recorded(entry, lambda: reposync.publish_branch(
        repo, remote, args.branch, me, force_with_lease=args.force_with_lease),
        lambda _ref: {"after": sha, "pushed": True})
    return _out(args, {"remote": remote, "ref": target, "sha": sha},
                [f"pushed {args.branch} {sha[:8]} -> {remote}/{target}"])


def _cmd_branch_pull(args: argparse.Namespace) -> int:
    repo, workspace = flow.resolve_repo(args.workspace)
    remote = reposync.choose_remote(repo, args.remote)
    entry = flow.history_entry(repo, kind="pull", workspace=workspace, remote=remote,
                               branch=args.branch, by=flow.actor(), approval={"mode": "none"},
                               ref=reposync.shared_ref(args.machine, args.branch))
    local, sha = flow.recorded(
        entry, lambda: reposync.receive_branch(repo, remote, args.machine, args.branch),
        lambda got: {"after": got[1], "pushed": False})
    return _out(args, {"remote": remote, "branch": local, "sha": sha},
                [f"{local} = {sha[:8]} (from {remote})"])


def _guard(handler):
    def run(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except (reposync.SyncError, flow.FlowError) as exc:
            return _err(str(exc))
    return run


def register(sub) -> None:
    p = sub.add_parser(
        "reposync",
        help="keep a repository in step with its clones on other machines, "
             "through the remote they share (matched by address, not name)",
    )
    rsub = p.add_subparsers(dest="reposync_cmd", required=True)

    def common(q, *, peers: bool = False):
        q.add_argument("workspace", nargs="?", default=None,
                       help="workspace name or path (default: the current directory)")
        q.add_argument("--remote", default=None,
                       help="local remote to sync through (default: the one a peer shares, "
                            "then the remembered one, then the only one)")
        q.add_argument("--json", action="store_true", help="machine-readable output")
        if peers:
            q.add_argument("--peer", action="append", default=None, metavar="MACHINE",
                           help="consult only this relay peer (repeatable; default: all)")

    st = rsub.add_parser("status", help="remotes here, peers sharing them, and what a sync would do")
    common(st, peers=True)
    st.add_argument("--branch", default=None, help="branch to plan (default master, then main)")
    st.add_argument("--no-fetch", action="store_true", help="do not fetch or plan")
    st.set_defaults(func=_guard(_cmd_status))

    ms = rsub.add_parser(
        "master",
        help="bring master onto the shared remote: fast-forward, or rebase "
             "--rebase-merges when both sides moved, then push",
    )
    common(ms, peers=True)
    ms.add_argument("--branch", default=None, help="default master, then main")
    ms.add_argument("--dry-run", action="store_true", help="fetch and plan only")
    ms.add_argument("--yes", action="store_true",
                    help="approve the push here (a person's terminal only; an agent "
                         "session waits for the web UI's approval card)")
    ms.set_defaults(func=_guard(_cmd_master))

    br = rsub.add_parser("branch", help="share a work branch as <machine>/<branch> on the remote")
    bsub = br.add_subparsers(dest="reposync_branch_cmd", required=True)
    bp = bsub.add_parser("push", help="push BRANCH to <this machine>/BRANCH")
    bp.add_argument("branch")
    common(bp)
    bp.add_argument("--force-with-lease", action="store_true",
                    help="allow rewriting this machine's own published copy")
    bp.add_argument("--yes", action="store_true", help="as for 'master'")
    bp.set_defaults(func=_guard(_cmd_branch_push))
    bl = bsub.add_parser("pull", help="fetch MACHINE/BRANCH into a local branch of that name (ff only)")
    bl.add_argument("machine")
    bl.add_argument("branch")
    common(bl)
    bl.set_defaults(func=_guard(_cmd_branch_pull))
