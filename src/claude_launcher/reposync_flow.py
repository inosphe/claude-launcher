"""What the reposync CLI and MCP tool share above the git layer.

:mod:`reposync` knows git. This module knows the rest of claunch: which
repository a workspace name means, which peers a relay carries and what their
clones look like, whether a push needs a person's approval and how to ask for
it, and how a rebase is written onto the board.

Approval (decision Q5): ``daemon.reposync_push`` is ``ask`` by default.

* A person at their own terminal (no ``CLAUNCH_SESSION``) answers a y/N
  prompt, or passes ``--yes``.
* A managed session cannot approve its own push: the plan is filed with the
  daemon's gate (:mod:`daemon.reposync_gate`) and a person presses Approve in
  the web UI. The approval is bound to the plan's SHAs -- if the local branch
  or the remote moved since, the approved request no longer matches and a new
  one is needed (:func:`approval_matches`).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from typing import Dict, Iterable, List, Optional, Tuple

from . import daemon_client, reposync, store, workspaces
from .daemon.paths import session_scratch_dir

ASK = "ask"
AUTO = "auto"


class FlowError(Exception):
    pass


# --------------------------------------------------------------------------- #
# where
# --------------------------------------------------------------------------- #
def resolve_repo(target: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """``(repository root, workspace name or None)`` for a workspace name, a
    path, or (no target) the current directory."""
    if target:
        ws = workspaces.find(target)
        path = ws.path if ws else target
    else:
        path = os.getcwd()
    try:
        root = reposync.toplevel(path)
    except reposync.SyncError as exc:
        raise FlowError(f"{target or path}: {exc}") from None
    # a linked worktree syncs the repository it belongs to: the branch refs
    # are shared, and the main checkout is where master is usually out
    common = reposync.git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if common and os.path.basename(common.rstrip("/\\")) == ".git":
        root = os.path.dirname(common.rstrip("/\\"))
    owner = workspaces.owning(root)
    return root, (owner.name if owner else None)


def scratch() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if session:
        return str(session_scratch_dir(session))
    return os.path.join(os.path.expanduser("~"), ".claude-launcher", "reposync-scratch")


# --------------------------------------------------------------------------- #
# peers
# --------------------------------------------------------------------------- #
def client() -> Optional[daemon_client.DaemonClient]:
    c, _why = daemon_client.connect_with_diagnosis()
    return c


def relay_identity(c: Optional[daemon_client.DaemonClient]) -> Tuple[str, List[str]]:
    """``(this machine's relay name, peer names)``. Without a daemon or a relay
    the machine is the host name and there are no peers."""
    if c is None:
        return socket.gethostname(), []
    try:
        doc = c.get("/api/relay/peers")
    except daemon_client.DaemonClientError:
        return socket.gethostname(), []
    relay = doc.get("relay") or {}
    me = str(relay.get("name") or socket.gethostname())
    return me, [p for p in doc.get("peers") or [] if p != me]


def peer_repos(c: daemon_client.DaemonClient, machine: str) -> List[dict]:
    try:
        return list(c.get(f"/api/relay/peers/{machine}/repos").get("repos") or [])
    except daemon_client.DaemonClientError as exc:
        raise FlowError(
            f"{machine}: cannot read its repositories ({exc}) -- its daemon may "
            "predate reposync"
        ) from None


def matches(repo: str, peers: Dict[str, List[dict]]) -> List[dict]:
    """One row per (peer machine, peer workspace) that shares a remote address
    with ``repo``, naming the remote on both sides."""
    local = reposync.remote_keys(repo)
    rows = []
    for machine, repos in peers.items():
        for doc in repos:
            theirs = {r.get("key"): r.get("name") for r in doc.get("remotes") or []}
            for key, name in reposync.common_remotes(local, theirs):
                rows.append({
                    "machine": machine, "workspace": doc.get("workspace"),
                    "key": key, "local_remote": name, "peer_remote": theirs[key],
                    "branches": doc.get("branches") or {},
                })
    return rows


def local_branches(repo: str) -> List[str]:
    out = reposync.git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", check=False)
    return [line for line in out.splitlines() if line]


def workspace_status(path: str, workspace: Optional[str], peers: Dict[str, List[dict]],
                     problems: List[str], *, do_fetch: bool, remote: Optional[str] = None,
                     branch: Optional[str] = None) -> dict:
    """One workspace as the web UI's Sync panel shows it: its remotes, the
    peers that share an address with it, and the plan a sync would carry out.
    ``peers`` is what the daemon could read off the relay; an unreadable peer
    is a line in ``problems``, never a reason not to plan -- the plan rests on
    the shared remote alone."""
    try:
        repo = reposync.toplevel(path)
    except reposync.SyncError as exc:
        return {"workspace": workspace, "error": str(exc)}
    rows = matches(repo, peers)
    out = {
        "workspace": workspace, "repo": repo,
        "remotes": reposync.remote_keys(repo), "peers": rows,
        "problems": list(problems), "branches": local_branches(repo),
        "fetched": bool(do_fetch),
    }
    try:
        name = reposync.choose_remote(repo, remote, [r["key"] for r in rows] if rows else None)
        p = reposync.plan(repo, name, branch, do_fetch=do_fetch)
    except reposync.SyncError as exc:
        out["plan_error"] = str(exc)
        return out
    key = reposync.url_key(reposync.remotes(repo).get(name, ""))
    out["plan"] = plan_card(p, workspace=workspace, key=key)
    return out


def discover(repo: str, c: Optional[daemon_client.DaemonClient],
             only: Iterable[str] = ()) -> Tuple[List[dict], List[str]]:
    """Matching rows across peers, plus one line per peer that could not be
    read (an unreachable peer narrows the answer; it does not stop it)."""
    if c is None:
        return [], ["no daemon -- peers not consulted"]
    _me, names = relay_identity(c)
    wanted = list(only) or names
    found: Dict[str, List[dict]] = {}
    problems = []
    for machine in wanted:
        try:
            found[machine] = peer_repos(c, machine)
        except FlowError as exc:
            problems.append(str(exc))
    return matches(repo, found), problems


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #
def approval_mode() -> str:
    mode = str(store.daemon_config().get("reposync_push") or ASK).lower()
    return AUTO if mode == AUTO else ASK


def plan_card(p: reposync.Plan, *, workspace: Optional[str], key: Optional[str]) -> dict:
    """What the approval card shows and what an approval is bound to."""
    words = {
        reposync.PUSH: f"push {p.ahead} commit(s) to {p.remote}/{p.branch}",
        reposync.REBASE: (
            f"rebase {p.ahead} unpushed commit(s) ({len(p.merges)} merge(s)) onto "
            f"{p.remote}/{p.branch} (+{p.behind}) and push; "
            f"{len(p.affected)} local branch(es) left on the old commits"
        ),
    }
    return {
        "repo": p.repo, "workspace": workspace, "remote": p.remote, "remote_key": key,
        "branch": p.branch, "action": p.action, "ahead": p.ahead, "behind": p.behind,
        "local": p.local, "upstream": p.upstream, "merges": p.merges,
        "affected": p.affected, "summary": words.get(p.action, p.action),
    }


#: The card fields an approval is bound to.
BOUND = ("repo", "remote", "branch", "action", "local", "upstream")


def approval_matches(record: dict, card: dict) -> bool:
    """An approval covers exactly the plan that was shown: same repository,
    remote, branch and action, and the same two tips. Anything that moved
    since is a different plan nobody approved."""
    shown = record.get("plan") or {}
    return record.get("status") == "approved" and all(
        shown.get(k) == card.get(k) for k in BOUND
    )


def authorize(c, card: dict, request: Optional[str]) -> dict:
    """The non-blocking form of the approval, for a caller that cannot wait
    (the MCP tool). ``{"go": True}`` when the push may run now; otherwise
    ``{"go": False, "status": ..., "request": ...}`` -- ``waiting_approval``
    (filed or still pending: call again with the id), or the refusal."""
    if approval_mode() == AUTO:
        return {"go": True, "approval": "auto"}
    if not request:
        record = file_request(c, card)
        return {"go": False, "status": "waiting_approval", "request": record["id"],
                "deadline": record["deadline"]}
    record = read_request(c, request)
    if record.get("status") == "pending":
        return {"go": False, "status": "waiting_approval", "request": request,
                "deadline": record.get("deadline")}
    if record.get("status") != "approved":
        return {"go": False, "status": record.get("status"), "request": request}
    if not approval_matches(record, card):
        return {"go": False, "status": "stale_approval", "request": request,
                "why": "the branch or the remote moved after the approval; "
                       "call without 'request' to ask again"}
    return {"go": True, "approval": request}


def file_request(c: Optional[daemon_client.DaemonClient], card: dict) -> dict:
    if c is None:
        raise FlowError("approval needs the daemon's web UI, and no daemon is running")
    try:
        return c.post("/api/reposync/requests", {
            "session": os.environ.get("CLAUNCH_SESSION", ""), "plan": card,
        })["request"]
    except daemon_client.DaemonClientError as exc:
        raise FlowError(
            f"cannot file the approval request ({exc}) -- the daemon may predate reposync"
        ) from None


def read_request(c: Optional[daemon_client.DaemonClient], rid: str) -> dict:
    if c is None:
        raise FlowError("no daemon to read the approval request from")
    try:
        return c.get(f"/api/reposync/requests/{rid}")["request"]
    except daemon_client.DaemonClientError as exc:
        raise FlowError(f"reposync request {rid}: {exc}") from None


def wait_request(c, rid: str, *, poll: float = 3.0, budget: Optional[float] = None) -> dict:
    """Poll until the request is settled (approved, rejected or expired)."""
    started = time.monotonic()
    while True:
        record = read_request(c, rid)
        if record.get("status") != "pending":
            return record
        if budget is not None and time.monotonic() - started > budget:
            return record
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# board
# --------------------------------------------------------------------------- #
def _beads(repo: str, *args: str) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["claunch", "beads", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout


def board_citations(repo: str, olds: Iterable[str]) -> List[Tuple[str, str]]:
    """``(issue id, old sha)`` for every closed issue whose close reason names
    one of ``olds`` (by any prefix of 7+ characters)."""
    olds = list(olds)
    if not olds or not os.path.isdir(os.path.join(repo, ".beads")):
        return []
    code, out = _beads(repo, "list", "--status", "closed", "--limit", "0", "--json")
    if code != 0:
        return []
    try:
        issues = json.loads(out).get("issues") or []
    except (ValueError, AttributeError):
        return []
    found = []
    for issue in issues:
        reason = str(issue.get("close_reason") or "")
        for token in reason.replace(",", " ").split():
            if len(token) < 7:
                continue
            for old in olds:
                if old.startswith(token.lower()):
                    found.append((str(issue.get("id")), old))
    return found


def annotate_board(repo: str, rewritten: Dict[str, str], *, remote: str, branch: str) -> List[dict]:
    """Leave ``REBASED <old> -> <new>`` on every issue whose ``merged <old>``
    proof the rebase replaced. The close reason itself stays as written: a
    closed issue's record is not rewritten, it is annotated."""
    done = []
    actor = os.environ.get("CLAUNCH_SESSION")
    for issue, old in board_citations(repo, rewritten):
        new = rewritten[old]
        text = f"REBASED {old[:8]} -> {new[:8]} (reposync onto {remote}/{branch})"
        args = ["comments", "add", issue, text]
        if actor:
            args += ["--actor", actor]
        code, _ = _beads(repo, *args)
        done.append({"issue": issue, "old": old, "new": new, "ok": code == 0})
    return done
