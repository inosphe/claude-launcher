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
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

from . import daemon_client, reposync, store, workspaces
from .daemon.paths import daemon_dir, session_scratch_dir

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


def linked_worktrees(path: str) -> List[dict]:
    """The linked worktrees of the repository at ``path`` (claunch-kjjta),
    from one ``git worktree list --porcelain``: each one's path, branch (or
    ``detached``) and HEAD. The main checkout -- the workspace itself -- is
    left out, and so is a bare entry; a worktree whose directory is gone is
    kept with ``prunable`` so the row can say so. A workspace registered at
    a linked worktree lists the others, not itself."""
    out = reposync.git(path, "worktree", "list", "--porcelain", check=False)
    rows: List[dict] = []
    cur: Optional[dict] = None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": os.path.normpath(line[len("worktree "):]), "branch": None,
                   "detached": False, "head": None, "prunable": False, "bare": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            cur["detached"] = True
        elif line == "bare":
            cur["bare"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
        elif not line:
            rows.append(cur)
            cur = None
    # the first entry is always the main working tree; a workspace that is
    # itself a linked worktree does not list itself either
    try:
        here = os.path.normcase(os.path.normpath(reposync.toplevel(path)))
    except reposync.SyncError:
        here = ""
    return [{k: v for k, v in r.items() if k != "bare"} for r in rows[1:]
            if not r["bare"] and os.path.normcase(r["path"]) != here]


def git_summary(path: str) -> dict:
    """What a workspace row shows without opening its Sync panel: the
    checked-out branch, how far it is ahead of / behind its upstream, and how
    many tracked files carry uncommitted changes (claunch-gax44).

    One ``git status`` and nothing else -- no fetch, so ahead/behind is
    against the remote-tracking ref as the last fetch left it, and untracked
    files are not scanned (on a large checkout that scan is what takes the
    seconds). A directory git cannot read answers ``{"error": ...}``.
    ``worktrees`` lists its linked worktrees (:func:`linked_worktrees`), a
    second git process."""
    try:
        out = reposync.git(path, "status", "--porcelain=v2", "--branch",
                           "--untracked-files=no", check=True)
    except reposync.SyncError as exc:
        return {"error": str(exc)}
    doc: dict = {"branch": None, "detached": False, "upstream": None,
                 "ahead": None, "behind": None, "changed": 0}
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            if head == "(detached)":
                doc["detached"] = True
            else:
                doc["branch"] = head
        elif line.startswith("# branch.upstream "):
            doc["upstream"] = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            ahead, behind = line[len("# branch.ab "):].split()
            doc["ahead"], doc["behind"] = int(ahead.lstrip("+")), int(behind.lstrip("-"))
        elif line and not line.startswith("#"):
            doc["changed"] += 1
    doc["worktrees"] = linked_worktrees(path)
    return doc


#: How many shared branches the Sync panel lists, newest commit first.
SHARED_LIMIT = 50


def shared_branches(repo: str, remote: str, machine: Optional[str],
                    peers: Iterable[str] = ()) -> List[dict]:
    """The work branches other machines published on ``remote`` as
    ``<machine>/<branch>`` (:func:`reposync.publish_branch`), read from the
    remote-tracking refs as the last fetch left them (claunch-tamel).

    This machine's own prefix is left out. Any other ``<prefix>/<rest>`` is
    listed, and ``peer`` says whether the prefix is a relay peer that shares
    this address -- a remote may carry ``feature/x`` branches nobody
    published, and the panel keeps those apart. ``local`` is the local branch
    a pull lands on (:func:`reposync.receive_branch` names it the same) and
    ``state`` how it stands: ``new`` (none yet), ``same``, ``behind`` (a pull
    fast-forwards it) or ``diverged`` (a pull refuses)."""
    mine = reposync.machine_prefix(machine) if machine else None
    known = set()
    for m in peers:
        try:
            known.add(reposync.machine_prefix(m))
        except reposync.SyncError:
            continue
    base = f"refs/remotes/{remote}/"
    out = reposync.git(
        repo, "for-each-ref", "--sort=-committerdate",
        "--format=%(refname)%00%(objectname)%00%(committerdate:iso-strict)%00%(subject)",
        base, check=False,
    )
    heads = {}
    for line in reposync.git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)",
                             "refs/heads", check=False).splitlines():
        ref, _, sha = line.partition("\0")
        heads[ref[len("refs/heads/"):]] = sha
    rows = []
    for line in out.splitlines():
        ref, sha, date, subject = (line.split("\0") + ["", "", ""])[:4]
        name = ref[len(base):]
        prefix, slash, branch = name.partition("/")
        if not slash or not branch or name == "HEAD" or prefix == mine:
            continue
        local = heads.get(name)
        if local is None:
            state = "new"
        elif local == sha:
            state = "same"
        elif reposync._ok(repo, "merge-base", "--is-ancestor", local, sha):
            state = "behind"
        else:
            state = "diverged"
        rows.append({
            "machine": prefix, "branch": branch, "ref": name, "sha": sha,
            "date": date, "subject": subject, "peer": prefix in known,
            "local": local, "state": state,
        })
        if len(rows) >= SHARED_LIMIT:
            break
    return rows


#: How many unlanded branches the Sync panel lists, newest commit first.
UNMERGED_LIMIT = 50


def unmerged_branches(repo: str, base: str, *, skip: Iterable[str] = ()) -> List[dict]:
    """Local branches with commits ``base`` does not have yet -- the work
    waiting to land (claunch-6p7ri). ``base`` is a commit, normally the
    remote's master as the plan read it, so a branch counts as landed once
    the shared master holds it, whatever the local master says.

    Each row carries its tip's commit time and subject, how many commits it
    has beyond ``base``, and ``session``: the ``Claunch-Session`` trailer of
    its tip commit (the commit-stamp convention), ``None`` when unstamped.
    ``skip`` names branches left out -- the planned branch itself."""
    out = reposync.git(
        repo, "for-each-ref", "--sort=-committerdate", f"--no-merged={base}",
        "--format=%(refname:short)%00%(objectname)%00%(committerdate:iso-strict)%00"
        "%(subject)%00%(trailers:key=Claunch-Session,valueonly,separator=%x2C)",
        "refs/heads", check=False,
    )
    left = set(skip)
    rows = []
    for line in out.splitlines():
        name, sha, date, subject, stamp = (line.split("\0") + [""] * 4)[:5]
        if not name or name in left:
            continue
        ahead = reposync.git(repo, "rev-list", "--count", f"{base}..{sha}", check=False)
        rows.append({
            "branch": name, "sha": sha, "date": date, "subject": subject,
            "ahead": int(ahead) if ahead.isdigit() else None,
            "session": stamp.split(",")[0].strip() or None,
        })
        if len(rows) >= UNMERGED_LIMIT:
            break
    return rows


def peer_distance(repo: str, ours: str, theirs: Optional[str]) -> dict:
    """How a peer's master tip stands against ours (claunch-gjjdp), from
    local git only: ``same``; ``ahead`` / ``behind`` (the peer has commits we
    lack / we have commits the peer lacks); ``diverged``; ``unknown`` when
    the peer's tip is not in this clone yet (it never reached the shared
    remote, or we have not fetched since) or the peer reported none.
    ``theirs_only`` / ``ours_only`` count the commits on each side."""
    doc: dict = {"state": "unknown", "theirs_only": None, "ours_only": None}
    if not theirs or not ours:
        return doc
    if theirs == ours:
        return {"state": "same", "theirs_only": 0, "ours_only": 0}
    if not reposync._ok(repo, "cat-file", "-e", f"{theirs}^{{commit}}"):
        return doc
    counts = reposync.git(repo, "rev-list", "--left-right", "--count",
                          f"{theirs}...{ours}", check=False).split()
    if len(counts) != 2 or not all(c.isdigit() for c in counts):
        return doc
    t, o = int(counts[0]), int(counts[1])
    state = "diverged" if t and o else "ahead" if t else "behind"
    return {"state": state, "theirs_only": t, "ours_only": o}


def _peer_tip(row: dict, branch: str) -> Optional[str]:
    got = (row.get("branches") or {}).get(branch)
    if isinstance(got, dict):
        return got.get("sha") or None
    return got or None     # an older peer reported the bare sha


def plan_facts(repo: str, p: reposync.Plan, rows: List[dict],
               machine: Optional[str]) -> dict:
    """What a status shows beside the plan ``p`` (claunch-1cvq4): each peer
    row gains ``distance`` (its master tip against ours, :func:`peer_distance`),
    and the answer carries ``shared`` (:func:`shared_branches` on the planned
    remote, leaving out ``machine``'s own prefix) and ``unmerged``
    (:func:`unmerged_branches` against the remote's master). The Sync panel,
    ``claunch reposync status`` and the MCP tool's status all read it here.
    Call it after the plan: when the plan fetched, that fetch is what brought
    the refs these read."""
    for row in rows:
        row["distance"] = peer_distance(repo, p.local, _peer_tip(row, p.branch))
    return {
        "shared": shared_branches(repo, p.remote, machine, {r["machine"] for r in rows}),
        "unmerged": unmerged_branches(repo, p.upstream or p.local, skip=[p.branch]),
    }


def workspace_status(path: str, workspace: Optional[str], peers: Dict[str, List[dict]],
                     problems: List[str], *, do_fetch: bool, remote: Optional[str] = None,
                     branch: Optional[str] = None, machine: Optional[str] = None) -> dict:
    """One workspace as the web UI's Sync panel shows it: its remotes, the
    peers that share an address with it, and the plan a sync would carry out.
    ``peers`` is what the daemon could read off the relay; an unreadable peer
    is a line in ``problems``, never a reason not to plan -- the plan rests on
    the shared remote alone. ``shared`` lists the branches other machines
    published on the planned remote (:func:`shared_branches`); ``machine`` is
    this one, whose own prefix it leaves out. ``unmerged`` lists the local
    branches the remote's master does not hold yet (:func:`unmerged_branches`).
    ``history`` is what this machine synced, pushed and pulled in the
    repository, newest first (:func:`read_history`)."""
    try:
        repo = reposync.toplevel(path)
    except reposync.SyncError as exc:
        return {"workspace": workspace, "error": str(exc)}
    rows = matches(repo, peers)
    out = {
        "workspace": workspace, "repo": repo,
        "remotes": reposync.remote_keys(repo), "peers": rows,
        "problems": list(problems), "branches": local_branches(repo),
        "fetched": bool(do_fetch), "history": read_history(repo),
    }
    try:
        name = reposync.choose_remote(repo, remote, [r["key"] for r in rows] if rows else None)
        p = reposync.plan(repo, name, branch, do_fetch=do_fetch)
    except reposync.SyncError as exc:
        out["plan_error"] = str(exc)
        return out
    key = reposync.url_key(reposync.remotes(repo).get(name, ""))
    out["plan"] = plan_card(p, workspace=workspace, key=key)
    out.update(plan_facts(repo, p, rows, machine))
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
    return {"go": True, "approval": request, "decided_by": record.get("decided_by"),
            "decided_at": record.get("decided_at")}


def card_approval(record: dict) -> dict:
    """How a push was let through, for the history: the approval card a
    person answered (:mod:`daemon.reposync_gate`)."""
    return {"mode": "card", "request": record.get("id"),
            "decided_by": record.get("decided_by"), "decided_at": record.get("decided_at")}


def gate_approval(gate: dict) -> dict:
    """:func:`authorize`'s go answer, as a history approval."""
    if gate.get("approval") == "auto":
        return {"mode": AUTO}
    return {"mode": "card", "request": gate.get("approval"),
            "decided_by": gate.get("decided_by"), "decided_at": gate.get("decided_at")}


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


# --------------------------------------------------------------------------- #
# history (claunch-iaihd)
# --------------------------------------------------------------------------- #
# What was synced, pushed or pulled, when, by whom and under whose approval.
# The approval gate cannot answer that: it lives in memory, keeps the last
# twenty settled cards, records the decision but not whether the push ran,
# and never sees a push under ``auto`` or one a person ran at their own
# terminal. So every operation that ran appends one line here, from the
# daemon (the Sync panel) and from the CLI and the MCP tool alike -- they
# all run on this machine and share this daemon directory.

HISTORY_FILE = "reposync-history.jsonl"
#: lines the file keeps once it grows past :data:`HISTORY_TRIM_BYTES`
HISTORY_KEEP = 1000
HISTORY_TRIM_BYTES = 512 * 1024
#: entries a workspace's panel shows, newest first
HISTORY_SHOWN = 30

T = TypeVar("T")


def history_path() -> str:
    return str(daemon_dir() / HISTORY_FILE)


def repo_identity(repo: str) -> str:
    """The repository a history line belongs to: its shared git directory,
    so a sync run from any worktree of a repository shows under every
    workspace registered at one of them."""
    common = reposync.git(repo, "rev-parse", "--path-format=absolute",
                          "--git-common-dir", check=False)
    return os.path.normcase(os.path.normpath(common or repo))


def actor() -> str:
    """Who ran it: the managed session, or a person at their own shell."""
    return os.environ.get("CLAUNCH_SESSION") or "terminal"


def history_entry(repo: str, *, kind: str, workspace: Optional[str], remote: str,
                  branch: str, by: str, approval: dict, **extra) -> dict:
    return {"kind": kind, "repo": repo, "repo_id": repo_identity(repo),
            "workspace": workspace, "remote": remote, "branch": branch,
            "by": by, "approval": approval, **extra}


def record_history(entry: dict) -> None:
    """Append one line. A history that cannot be written is a lost record,
    never a reason the operation it describes fails."""
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    path = history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if os.path.getsize(path) > HISTORY_TRIM_BYTES:
            with open(path, encoding="utf-8") as fh:
                keep = fh.read().splitlines()[-HISTORY_KEEP:]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(keep) + "\n")
    except OSError:
        pass


def read_history(repo: str, limit: int = HISTORY_SHOWN) -> List[dict]:
    """The lines for ``repo``'s repository, newest first."""
    ident = repo_identity(repo)
    try:
        with open(history_path(), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    rows: List[dict] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("repo_id") == ident:
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def recorded(entry: dict, run: Callable[[], T], done: Callable[[T], dict]) -> T:
    """Run one operation and append what it did -- ``done(result)`` on
    success, the error when git refused. The error is raised on."""
    try:
        out = run()
    except reposync.SyncError as exc:
        record_history({**entry, "ok": False, "error": str(exc)})
        raise
    record_history({**entry, "ok": True, **done(out)})
    return out


def master_done(p: reposync.Plan) -> Callable[[object], dict]:
    """The history fields of a master sync, from its :class:`reposync.Result`
    or that result's ``to_dict()`` form (what the daemon's handler holds)."""
    def done(result) -> dict:
        doc = result if isinstance(result, dict) else result.to_dict()
        return {"action": p.action, "before": p.local, "after": doc.get("new"),
                "pushed": bool(doc.get("pushed")), "rewritten": len(doc.get("rewritten") or {})}
    return done
