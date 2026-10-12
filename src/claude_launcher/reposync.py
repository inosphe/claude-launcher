"""Keep one repository's master in step across machines through a shared remote.

Two daemons on two machines often hold clones of the same repository under
different workspace names and different remote names: this machine's
``github.ncsoft.com`` remote and another machine's ``origin`` can be the same
URL. Neither name means anything across machines, so the shared ground is the
remote's *address*. :func:`url_key` reduces a remote URL to ``host/owner/repo``
(lower-cased, no ``.git``, no credentials), and two clones are the same
repository on that key.

Each machine syncs its own half (claunch-dh73y, decision Q2): fetch the shared
remote, bring the local branch onto it, push. Nothing here writes to another
machine; the peer's state is only ever read (``/peer/repos``). Running the sync
on machine A, then B, then A again leaves all three refs equal.

How the local branch is brought onto the remote (decision Q3):

* work-unit branches land on master with ``--no-ff`` -- untouched here;
* master itself is kept on one line: fast-forward when only one side moved,
  ``git rebase --rebase-merges`` onto the remote when both did. The rebase
  keeps every ``--no-ff`` landing merge (a plain rebase would flatten them)
  and never creates a sync merge commit.

A rebase rewrites the SHAs of the unpushed commits, so :func:`execute` returns
what that costs: the old->new map (the board's ``merged <hash>`` proofs point at
old ones), and the local branches still built on the old commits, each with the
``git rebase --onto`` that would move it. Those branches belong to other
sessions; this module lists them and moves none.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .ghcli import parse_remote_url

#: Branch names tried, in order, when the caller does not name one.
DEFAULT_BRANCHES = ("master", "main")

#: The board export. Decision Q4: the repository should not track it (policy
#: b); when it is tracked anyway and is the only thing a rebase trips on, the
#: way out is the board's own three-way merge (policy a), never a hand edit.
BOARD_EXPORT = ".beads/issues.jsonl"

#: Seconds one git call may take. Fetch and push talk to a network remote.
GIT_TIMEOUT = 300.0


class SyncError(Exception):
    """A sync that cannot go on. The message says why, in one line."""


class RebaseConflict(SyncError):
    """The rebase stopped on conflicts and was aborted; nothing moved."""

    def __init__(self, files: Sequence[str]):
        self.files = list(files)
        only_board = self.files == [BOARD_EXPORT]
        hint = (
            f" -- only the board export conflicts: untrack {BOARD_EXPORT} "
            "(policy b) or merge the boards with 'claunch beads sync --merge' "
            "(policy a), then sync again"
            if only_board
            else ""
        )
        super().__init__(
            "rebase stopped on conflicts and was aborted, nothing moved: "
            + ", ".join(self.files)
            + hint
        )
        self.only_board = only_board


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def git(cwd: str, *args: str, check: bool = True, timeout: float = GIT_TIMEOUT) -> str:
    """Run ``git -C cwd args`` and return stdout stripped.

    ``check`` raises :class:`SyncError` carrying git's stderr on a non-zero
    exit; without it the caller reads the empty string.
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        raise SyncError(f"git {' '.join(args)} timed out after {timeout:.0f}s") from None
    except OSError as exc:
        raise SyncError(f"git could not run: {exc}") from None
    if proc.returncode != 0 and check:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise SyncError(f"git {' '.join(args)}: " + (detail[-1] if detail else f"exit {proc.returncode}"))
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _ok(cwd: str, *args: str) -> bool:
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def toplevel(path: str) -> str:
    """The working tree root that holds ``path``."""
    out = git(path, "rev-parse", "--show-toplevel")
    if not out:
        raise SyncError(f"not a git repository: {path}")
    return out


# --------------------------------------------------------------------------- #
# remotes, by address
# --------------------------------------------------------------------------- #
def url_key(url: str) -> Optional[str]:
    """``host/owner/repo`` for a network remote URL; None for a local path.

    ``https://user:tok@GitHub.com/Org/Repo.git``, ``git@github.com:org/repo``
    and ``ssh://git@github.com/org/repo/`` all give ``github.com/org/repo``.
    The key never carries the userinfo, so it is safe to send to a peer.
    """
    host, path = parse_remote_url(url)
    if not host or not path:
        return None
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path.lower()}"


def remotes(repo: str) -> Dict[str, str]:
    """Remote name -> fetch URL."""
    out = git(repo, "remote", "-v", check=False)
    found: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            found.setdefault(parts[0], parts[1])
    return found


def remote_keys(repo: str) -> Dict[str, str]:
    """URL key -> local remote name. Two names for one address keep the first
    in ``git remote`` order."""
    keyed: Dict[str, str] = {}
    for name, url in remotes(repo).items():
        key = url_key(url)
        if key and key not in keyed:
            keyed[key] = name
    return keyed


def common_remotes(local: Dict[str, str], peer_keys: Iterable[str]) -> List[Tuple[str, str]]:
    """``(key, local remote name)`` for every address the peer also has."""
    wanted = set(peer_keys)
    return [(key, name) for key, name in local.items() if key in wanted]


#: git config key that remembers which remote this repository syncs through.
REMOTE_CONFIG = "claunch.reposync.remote"


def choose_remote(repo: str, explicit: Optional[str] = None,
                  peer_keys: Optional[Iterable[str]] = None) -> str:
    """The local remote name to sync through.

    In order: the name given; the one remote whose address a peer shares;
    the remembered choice (:data:`REMOTE_CONFIG`); the only remote there is.
    Anything else is ambiguous and raises with the candidates listed.
    """
    names = remotes(repo)
    if explicit:
        if explicit not in names:
            raise SyncError(f"no remote named {explicit!r} (have: {', '.join(names) or 'none'})")
        return explicit
    if peer_keys is not None:
        shared = common_remotes(remote_keys(repo), peer_keys)
        if len(shared) == 1:
            return shared[0][1]
        if len(shared) > 1:
            raise SyncError(
                "the peer shares more than one remote address with this repository: "
                + ", ".join(f"{name} ({key})" for key, name in shared)
                + " -- name one with --remote"
            )
    remembered = git(repo, "config", "--get", REMOTE_CONFIG, check=False)
    if remembered and remembered in names:
        return remembered
    if len(names) == 1:
        return next(iter(names))
    raise SyncError(
        "cannot tell which remote to sync through "
        f"(have: {', '.join(names) or 'none'}) -- name one with --remote"
    )


def default_branch(repo: str, remote: str) -> str:
    """``master`` or ``main``: the first of :data:`DEFAULT_BRANCHES` that both
    the local repository and the remote-tracking refs have."""
    for name in DEFAULT_BRANCHES:
        if _ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}") and _ok(
            repo, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{name}"
        ):
            return name
    for name in DEFAULT_BRANCHES:
        if _ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}"):
            return name
    raise SyncError("no master or main branch -- name one with --branch")


# --------------------------------------------------------------------------- #
# describe (what a peer is told)
# --------------------------------------------------------------------------- #
def _counts(repo: str, left: str, right: str) -> Optional[Tuple[int, int]]:
    out = git(repo, "rev-list", "--left-right", "--count", f"{left}...{right}", check=False)
    parts = out.split()
    if len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def describe(repo: str) -> dict:
    """What this clone looks like, without touching the network.

    Remote addresses travel as keys only. Per default branch, the local tip
    and, per remote that has a tracking ref for it, how far the local branch
    is ahead/behind that ref *as last fetched* -- ``fetched`` says the ref can
    be stale, because describing must not fetch.
    """
    keyed = remote_keys(repo)
    by_name = {name: key for key, name in keyed.items()}
    branches: Dict[str, dict] = {}
    for name in DEFAULT_BRANCHES:
        sha = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}", check=False)
        if not sha:
            continue
        tracking = []
        for remote, key in by_name.items():
            ref = f"refs/remotes/{remote}/{name}"
            rsha = git(repo, "rev-parse", "--verify", "-q", ref, check=False)
            if not rsha:
                continue
            counts = _counts(repo, f"refs/heads/{name}", ref)
            tracking.append({
                "remote": remote, "key": key, "sha": rsha,
                "ahead": counts[0] if counts else None,
                "behind": counts[1] if counts else None,
                "fetched": True,
            })
        branches[name] = {"sha": sha, "tracking": tracking}
    return {
        "remotes": [{"name": name, "key": key} for key, name in keyed.items()],
        "branches": branches,
    }


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
UP_TO_DATE = "up-to-date"
FAST_FORWARD = "ff"
PUSH = "push"
REBASE = "rebase"


@dataclass
class Plan:
    repo: str
    remote: str
    branch: str
    local: str
    upstream: str
    action: str
    ahead: int
    behind: int
    #: unpushed commits (``upstream..local``), oldest first
    unpushed: List[str] = field(default_factory=list)
    #: which of them are merge commits
    merges: List[str] = field(default_factory=list)
    #: the worktree that has ``branch`` checked out, if any
    checkout: Optional[str] = None
    #: local branches that contain an unpushed commit (a rebase strands them)
    affected: List[str] = field(default_factory=list)

    @property
    def pushes(self) -> bool:
        return self.action in (PUSH, REBASE)

    @property
    def moves_local(self) -> bool:
        return self.action in (FAST_FORWARD, REBASE)

    def to_dict(self) -> dict:
        return {
            "repo": self.repo, "remote": self.remote, "branch": self.branch,
            "local": self.local, "upstream": self.upstream, "action": self.action,
            "ahead": self.ahead, "behind": self.behind,
            "unpushed": self.unpushed, "merges": self.merges,
            "checkout": self.checkout, "affected": self.affected,
        }


def checkout_of(repo: str, branch: str) -> Optional[str]:
    """The worktree path that has ``refs/heads/<branch>`` checked out."""
    out = git(repo, "worktree", "list", "--porcelain", check=False)
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path:
            return path
    return None


def worktrees_by_branch(repo: str) -> Dict[str, str]:
    """Branch name -> worktree path, for every checked-out branch."""
    out = git(repo, "worktree", "list", "--porcelain", check=False)
    found: Dict[str, str] = {}
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path:
            found[line[len("branch refs/heads/"):]] = path
    return found


def _refs(repo: str, *filters: str) -> List[str]:
    out = git(repo, "for-each-ref", *filters, "--format=%(refname:short)", "refs/heads/",
              check=False)
    return [name for name in out.splitlines() if name]


def stranded_branches(repo: str, upstream: str, local: str, *,
                      exclude: Iterable[str] = ()) -> List[str]:
    """Local branches a rebase of ``upstream..local`` leaves on old commits.

    A rebase rewrites every unpushed commit -- the ``--no-ff`` landings and,
    measured, the side commits under them too. Two kinds of branch hold them:

    * built on the master line: they contain its oldest unpushed first-parent
      commit (every later one descends from it), with or without commits of
      their own -- a branch a session cut a minute ago counts;
    * built on an unpushed side commit and not landed yet (``--no-merged``).
      A work branch whose tip is already in ``local`` has nothing to carry
      over and is left out.
    """
    skip = set(exclude)
    found = set()
    line = git(repo, "rev-list", "--first-parent", "--reverse", f"{upstream}..{local}",
               check=False).splitlines()[:1]
    for sha in line:
        found.update(_refs(repo, "--contains", sha))
    for sha in unpushed_roots(repo, upstream, local):
        found.update(_refs(repo, "--contains", sha, "--no-merged", local))
    return sorted(found - skip)


def unpushed_roots(repo: str, upstream: str, local: str) -> List[str]:
    """The unpushed commits none of whose parents are unpushed. Every unpushed
    commit descends from one of them, so ``--contains`` on these alone finds
    every branch that holds any unpushed commit."""
    out = git(repo, "rev-list", "--parents", f"{upstream}..{local}", check=False)
    rows = [line.split() for line in out.splitlines() if line]
    inside = {row[0] for row in rows}
    return [row[0] for row in rows if not any(parent in inside for parent in row[1:])]


def fetch(repo: str, remote: str) -> None:
    git(repo, "fetch", "--quiet", remote)


def plan(repo: str, remote: str, branch: Optional[str] = None, *, do_fetch: bool = True) -> Plan:
    """Fetch ``remote`` and decide what bringing ``branch`` onto it takes."""
    repo = toplevel(repo)
    if do_fetch:
        fetch(repo, remote)
    branch = branch or default_branch(repo, remote)
    local = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
    if not local:
        raise SyncError(f"no local branch {branch!r}")
    upstream = git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{branch}", check=False)
    if not upstream:
        raise SyncError(f"{remote} has no branch {branch!r} (fetched)")
    counts = _counts(repo, local, upstream)
    if counts is None:
        raise SyncError(f"cannot compare {branch} with {remote}/{branch}")
    ahead, behind = counts
    if ahead == 0 and behind == 0:
        action = UP_TO_DATE
    elif ahead == 0:
        action = FAST_FORWARD
    elif behind == 0:
        action = PUSH
    else:
        action = REBASE
    unpushed = git(repo, "rev-list", "--reverse", f"{upstream}..{local}", check=False).splitlines()
    merges = git(repo, "rev-list", "--merges", "--reverse", f"{upstream}..{local}", check=False).splitlines()
    result = Plan(
        repo=repo, remote=remote, branch=branch, local=local, upstream=upstream,
        action=action, ahead=ahead, behind=behind, unpushed=unpushed, merges=merges,
        checkout=checkout_of(repo, branch),
    )
    if action == REBASE:
        result.affected = stranded_branches(repo, upstream, local, exclude=[branch])
    return result


# --------------------------------------------------------------------------- #
# execute
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    plan: Plan
    new: str
    pushed: bool
    #: old sha -> new sha for every unpushed commit the rebase rewrote
    rewritten: Dict[str, str] = field(default_factory=dict)
    #: one ``git rebase --onto`` per affected branch (not run)
    restack: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan": self.plan.to_dict(), "new": self.new, "pushed": self.pushed,
            "rewritten": self.rewritten, "restack": self.restack,
        }


def _identity(repo: str, rev_range: str) -> Dict[tuple, str]:
    """(author, author date, subject, is-merge) -> sha over a range. A rebase
    keeps the authorship of every commit it replays, merges included
    (``--rebase-merges`` recreates them with ``merge -C``)."""
    fmt = "%H%x1f%an%x1f%ae%x1f%ad%x1f%s%x1f%P"
    out = git(repo, "log", "--date=raw", f"--format={fmt}", rev_range, check=False)
    found: Dict[tuple, str] = {}
    for line in out.splitlines():
        sha, an, ae, ad, subject, parents = (line.split("\x1f") + [""] * 6)[:6]
        found.setdefault((an, ae, ad, subject, len(parents.split()) > 1), sha)
    return found


def rewritten_map(repo: str, upstream: str, old_tip: str, new_tip: str) -> Dict[str, str]:
    """old -> new for the unpushed commits a rebase replaced (unchanged ones
    are left out)."""
    old = _identity(repo, f"{upstream}..{old_tip}")
    new = _identity(repo, f"{upstream}..{new_tip}")
    return {sha: new[key] for key, sha in old.items() if key in new and new[key] != sha}


def _conflicts(worktree: str) -> List[str]:
    return git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()


def _rebased_tip(p: Plan, scratch: str) -> str:
    """Rebase the local tip onto the upstream in a throwaway detached worktree,
    so neither the checked-out tree nor any other session's tree is touched
    until the result is known to be whole."""
    os.makedirs(scratch, exist_ok=True)
    where = tempfile.mkdtemp(prefix="reposync-", dir=scratch)
    os.rmdir(where)  # worktree add wants to create it
    git(p.repo, "worktree", "add", "--detach", "--quiet", where, p.local)
    try:
        if not _ok(where, "rebase", "--rebase-merges", "--quiet", p.upstream):
            files = _conflicts(where) or ["(unknown -- see git status)"]
            git(where, "rebase", "--abort", check=False)
            raise RebaseConflict(files)
        return git(where, "rev-parse", "HEAD")
    finally:
        git(p.repo, "worktree", "remove", "--force", where, check=False)
        shutil.rmtree(where, ignore_errors=True)
        git(p.repo, "worktree", "prune", check=False)


def _move_branch(p: Plan, new: str, *, old: Optional[str] = None) -> None:
    """Move ``refs/heads/<branch>`` from ``old`` (default ``p.local``) to ``new``.

    A checked-out branch moves with ``reset --keep``: the tree follows, local
    edits survive, and git refuses (moving nothing) when an edited file is one
    the move would change. An idle branch moves with a compare-and-swap
    ``update-ref`` so a concurrent writer is detected, not overwritten.
    """
    old = old or p.local
    if p.checkout:
        head = git(p.checkout, "rev-parse", "HEAD")
        if head != old:
            raise SyncError(f"{p.branch} moved under the sync ({head[:8]} != {old[:8]}) -- run it again")
        git(p.checkout, "reset", "--keep", new)
    else:
        git(p.repo, "update-ref", f"refs/heads/{p.branch}", new, old)


def _push(p: Plan, sha: str) -> None:
    from .local_sync import require_pr
    require_pr(p.repo, p.remote, p.branch, "push")
    # never forced: a rejected push means the remote moved after the fetch
    git(p.repo, "push", "--quiet", p.remote, f"{sha}:refs/heads/{p.branch}")


def execute(p: Plan, *, scratch: str) -> Result:
    """Carry out a plan. Raises :class:`SyncError` (or :class:`RebaseConflict`)
    with nothing moved when the work cannot be finished whole."""
    if p.action == UP_TO_DATE:
        return Result(plan=p, new=p.local, pushed=False)
    from .local_sync import require_pr
    require_pr(p.repo, p.remote, p.branch, "generic sync")
    if p.action == FAST_FORWARD:
        _move_branch(p, p.upstream)
        return Result(plan=p, new=p.upstream, pushed=False)
    if p.action == PUSH:
        _push(p, p.local)
        return Result(plan=p, new=p.local, pushed=True)
    new = _rebased_tip(p, scratch)
    rewritten = rewritten_map(p.repo, p.upstream, p.local, new)
    # the local move goes first because it is the step that can refuse on
    # its own (reset --keep over an edited file); a refused push then puts
    # the branch back, so a failure leaves the local branch where the plan
    # found it and a rerun plans from the same old commits
    _move_branch(p, new)
    try:
        _push(p, new)
    except SyncError as exc:
        try:
            _move_branch(p, p.local, old=new)
        except SyncError as back:
            raise SyncError(
                f"{exc}; and {p.branch} could not be put back to {p.local[:8]} "
                f"({back}) -- it is at the unpushed rebase {new[:8]}"
            ) from None
        raise
    trees = worktrees_by_branch(p.repo)
    restack = [
        {
            "branch": name,
            "worktree": trees.get(name),
            "command": f"git rebase --onto {new} {p.local} {name}",
        }
        for name in p.affected
    ]
    return Result(plan=p, new=new, pushed=True, rewritten=rewritten, restack=restack)


# --------------------------------------------------------------------------- #
# work branches, namespaced by machine
# --------------------------------------------------------------------------- #
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def machine_prefix(machine: str) -> str:
    """A ref-safe, lower-cased form of a machine (relay) name."""
    cleaned = _UNSAFE.sub("-", machine.strip()).strip("-.").lower()
    if not cleaned:
        raise SyncError("no machine name to namespace the branch with")
    return cleaned


def shared_ref(machine: str, branch: str) -> str:
    """Where ``branch`` of ``machine`` lives on the shared remote. Session
    names are minted per daemon, so two machines can both have ``s118-x``."""
    return f"{machine_prefix(machine)}/{branch}"


def publish_branch(repo: str, remote: str, branch: str, machine: str, *,
                   force_with_lease: bool = False) -> str:
    """Push local ``branch`` to ``<machine>/<branch>`` on the remote. A
    rewrite of a branch already published needs ``force_with_lease``; the
    namespace is this machine's own, so nobody else's work is at stake."""
    sha = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
    if not sha:
        raise SyncError(f"no local branch {branch!r}")
    target = shared_ref(machine, branch)
    from .local_sync import require_pr
    require_pr(repo, remote, target, "publish")
    args = ["push", "--quiet"]
    if force_with_lease:
        args.append(f"--force-with-lease=refs/heads/{target}")
    git(repo, *args, remote, f"{sha}:refs/heads/{target}")
    return target


def receive_branch(repo: str, remote: str, machine: str, branch: str) -> Tuple[str, str]:
    """Fetch ``<machine>/<branch>`` from the remote into the local branch of
    the same name, fast-forward only. Returns ``(local branch, sha)``."""
    target = shared_ref(machine, branch)
    from .local_sync import require_pr
    require_pr(repo, remote, target, "receive")
    # a tracking ref follows the remote even across its rewrites ("+"); the
    # fast-forward check below guards the local branch, which is the one
    # that can hold work
    git(repo, "fetch", "--quiet", remote, f"+refs/heads/{target}:refs/remotes/{remote}/{target}")
    sha = git(repo, "rev-parse", f"refs/remotes/{remote}/{target}")
    current = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{target}", check=False)
    if current and not _ok(repo, "merge-base", "--is-ancestor", current, sha):
        raise SyncError(
            f"local {target} has commits the remote one does not -- not a fast-forward"
        )
    holder = checkout_of(repo, target)
    if holder:
        git(holder, "merge", "--ff-only", "--quiet", sha)
    elif current:
        git(repo, "update-ref", f"refs/heads/{target}", sha, current)
    else:
        git(repo, "branch", "--quiet", target, sha)
    return target, sha
