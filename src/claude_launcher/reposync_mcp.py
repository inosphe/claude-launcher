"""MCP access to ``claunch reposync`` for agent sessions (claunch-dh73y).

One tool, ``repo_sync``. A push to the shared remote needs a person's
approval unless ``daemon.reposync_push`` is ``auto``; an agent cannot give
it. The first call files the plan and returns ``waiting_approval`` with a
request id, the person presses Approve in the web UI, and a second call with
that ``request`` carries the plan out -- only if the branch and the remote
are still where they were when it was approved.
"""

from __future__ import annotations

from typing import Optional

from . import daemon_client, mcp_rpc, reposync, reposync_flow as flow

TOOLS = [
    {
        "name": "repo_sync",
        "description": (
            "Keep a repository in step with its clones on other machines through "
            "the remote they share. Clones are matched by remote ADDRESS (host/"
            "owner/repo), never by remote or workspace name. Actions: 'status' "
            "(remotes here, peers sharing them, what a sync would do), 'plan' "
            "(fetch and plan master, change nothing), 'sync' (master: fast-forward, "
            "or rebase --rebase-merges onto the remote when both sides moved, then "
            "push), 'branch_push' (push a work branch as <this machine>/<branch>), "
            "'branch_pull' (fetch <machine>/<branch>, fast-forward only). A push "
            "waits for a person's approval in the web UI unless the user set "
            "daemon.reposync_push=auto: the first call answers 'waiting_approval' "
            "with a request id -- tell the user, end your turn, and call again with "
            "'request' once they approved. After a rebase the result lists branches "
            "left on old commits with the 'git rebase --onto' for each; it moves "
            "none of them, and neither should you without their owner."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["status", "plan", "sync", "branch_push", "branch_pull"]},
                "workspace": {"type": "string",
                              "description": "workspace name or path (default: this session's directory)"},
                "remote": {"type": "string", "description": "local remote to sync through"},
                "branch": {"type": "string",
                           "description": "sync/plan: default master then main; branch_*: the work branch"},
                "machine": {"type": "string", "description": "branch_pull: the machine that published it"},
                "peer": {"type": "array", "items": {"type": "string"},
                         "description": "status/plan/sync: consult only these relay peers"},
                "force_with_lease": {"type": "boolean",
                                     "description": "branch_push: allow rewriting this machine's own copy"},
                "request": {"type": "string",
                            "description": "sync/branch_push: the approval request id a previous call returned"},
            },
            "required": ["action"],
        },
    },
]


class ReposyncMcpError(Exception):
    pass


def _remote(repo: str, args: dict, c) -> str:
    if args.get("remote"):
        return reposync.choose_remote(repo, args["remote"])
    rows, _ = flow.discover(repo, c, args.get("peer") or ())
    return reposync.choose_remote(repo, None, [r["key"] for r in rows] if rows else None)


def _status(args: dict) -> dict:
    repo, workspace = flow.resolve_repo(args.get("workspace"))
    c = flow.client()
    me, _ = flow.relay_identity(c)
    rows, problems = flow.discover(repo, c, args.get("peer") or ())
    return {"repo": repo, "workspace": workspace, "machine": me,
            "remotes": reposync.remote_keys(repo), "peers": rows, "problems": problems}


def _plan(args: dict, c) -> tuple:
    repo, workspace = flow.resolve_repo(args.get("workspace"))
    remote = _remote(repo, args, c)
    p = reposync.plan(repo, remote, args.get("branch"))
    key = reposync.url_key(reposync.remotes(repo).get(remote, ""))
    return p, flow.plan_card(p, workspace=workspace, key=key)


def _sync(args: dict) -> dict:
    c = flow.client()
    p, card = _plan(args, c)
    if p.pushes:
        gate = flow.authorize(c, card, args.get("request"))
        if not gate["go"]:
            return {**gate, "plan": card}
    result = reposync.execute(p, scratch=flow.scratch())
    reposync.git(p.repo, "config", reposync.REMOTE_CONFIG, p.remote, check=False)
    board = []
    if result.rewritten:
        board = flow.annotate_board(p.repo, result.rewritten, remote=p.remote, branch=p.branch)
    return {"status": "done", **result.to_dict(), "board": board}


def _branch_push(args: dict) -> dict:
    branch = args.get("branch")
    if not branch:
        raise ReposyncMcpError("branch_push needs 'branch'")
    repo, workspace = flow.resolve_repo(args.get("workspace"))
    c = flow.client()
    me, _ = flow.relay_identity(c)
    remote = reposync.choose_remote(repo, args.get("remote"))
    sha = reposync.git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
    if not sha:
        raise ReposyncMcpError(f"no local branch {branch!r}")
    target = reposync.shared_ref(me, branch)
    force = bool(args.get("force_with_lease"))
    card = {
        "repo": repo, "workspace": workspace, "remote": remote, "branch": target,
        "action": "publish-force" if force else "publish", "local": sha, "upstream": "",
        "summary": f"push {branch} {sha[:8]} to {remote}/{target}"
                   + (" (force-with-lease)" if force else ""),
    }
    gate = flow.authorize(c, card, args.get("request"))
    if not gate["go"]:
        return {**gate, "plan": card}
    reposync.publish_branch(repo, remote, branch, me, force_with_lease=force)
    return {"status": "done", "remote": remote, "ref": target, "sha": sha}


def _branch_pull(args: dict) -> dict:
    if not args.get("machine") or not args.get("branch"):
        raise ReposyncMcpError("branch_pull needs 'machine' and 'branch'")
    repo, _ = flow.resolve_repo(args.get("workspace"))
    remote = reposync.choose_remote(repo, args.get("remote"))
    local, sha = reposync.receive_branch(repo, remote, args["machine"], args["branch"])
    return {"status": "done", "remote": remote, "branch": local, "sha": sha}


def call_tool(name: str, args: dict) -> dict:
    if name != "repo_sync":
        raise ReposyncMcpError(f"unknown tool {name!r}")
    action = args.get("action")
    try:
        if action == "status":
            return _status(args)
        if action == "plan":
            p, card = _plan(args, flow.client())
            return {"plan": p.to_dict(), "card": card}
        if action == "sync":
            return _sync(args)
        if action == "branch_push":
            return _branch_push(args)
        if action == "branch_pull":
            return _branch_pull(args)
    except (reposync.SyncError, flow.FlowError) as exc:
        raise ReposyncMcpError(str(exc)) from None
    raise ReposyncMcpError(f"unknown action {action!r}")


SERVER = mcp_rpc.Server(
    name="claunch-reposync",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(ReposyncMcpError, daemon_client.DaemonClientError),
)
