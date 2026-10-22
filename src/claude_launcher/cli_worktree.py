"""``claunch worktree ...`` -- the launcher worktrees on this machine, and
cleaning up the ones nothing can come back to.

Every session created with a worktree leaves a checkout under
``<repo>/.claude/worktrees/<name>`` and nothing takes it away. By 2026-10-22
that had come to 308 checkouts under the session scratch directories alone,
about 600k inodes by sample -- more than all the test basetemp debris
together. The web UI's Worktrees page can clear the ones it lists, but it
lists only the repositories the fleet has touched; this command is the same
reading from a shell, over every root, and a selection rule severe enough to
be run unattended.

The reading itself is :mod:`claude_launcher.daemon.worktree_inventory` (the
page's own module) and so is the removal verb: this file adds no second
definition of what a worktree is, and both surfaces remove through
``worktree_inventory.remove`` -- which unlinks links inside the checkout
before handing it to git, keeps the branch, and refuses a checkout that still
holds a session (claunch-4m2s9, claunch-0n31r).

Which checkouts ``prune`` may remove -- all four, or it is kept:

1. no session joined to it is one that can come back: the daemon's seven
   lifecycle partitions, and ``running``/``hold``/``delayed``/``paused`` all
   count as running (the same set the web page files under Running). Only
   ``archived``, ``killed`` and ``failed`` leaves are removable; a checkout
   with no session record at all (``unlinked``) has nobody to come back.
2. ``git status`` is clean. A dirty checkout is reported and kept -- this
   command has no way to discard someone's files, and ``--force`` here means
   "remove for real", not "throw the changes away".
3. the commit it stands on is reachable from a branch or from a
   ``refs/claunch/preview/*`` ref. A detached checkout on a commit no ref
   names would lose that commit with the directory; it is kept.
4. its oldest relevant timestamp is at least ``--older-than`` days old.

**The default is a plan, not a removal.** ``--force`` is the only thing that
deletes, and the word is git's: it says the operator meant it. Without it the
command prints what it would remove, why each other checkout is kept, and
exits 0 -- so it can be run first, read, and then run again with ``--force``.

Exit codes, the same discipline ``claunch window`` follows so a caller can
tell the two failures apart: ``0`` the plan was made (or the removals ran),
``1`` something was refused or failed, ``2`` the session state could not be
read (no daemon, or a daemon that could not answer the session read), so
nothing was even planned.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from . import daemon_client
from . import worktree as worktree_mod
from .daemon import worktree_inventory as inv

#: The partitions that read as Running on both surfaces -- a session in one of
#: these can still come back into its directory, so the checkout is kept. The
#: rest (archived, killed, failed) is a session that has ended for good.
RUNNING_CATEGORIES = frozenset({"running", "hold", "delayed", "paused"})

#: Default for ``--older-than``: a week, the same horizon the basetemp reaper
#: uses, so "old" means the same thing on both cleanup paths.
DEFAULT_OLDER_THAN = 7.0

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NO_DAEMON = 2


def _inventory() -> Dict[str, dict]:
    """The Worktrees page's reading, over a wider set of roots.

    The rows are built by :func:`worktree_inventory.inventory` -- the same
    function the page's ``GET /api/worktrees`` calls -- so there is one answer
    to "what is a launcher worktree". What this command adds is reach: the
    page sees only the repositories the fleet has touched (registered
    workspaces plus session directories), and the largest debris field sits
    outside that -- 308 linked worktrees under the per-session scratch trees,
    of repositories no live session stands in. Their roots are read off the
    ``gitdir:`` line of each scratch checkout, and the session directory is
    passed as an extra base so those checkouts are listed.

    The session state is still the daemon's: categories come from
    ``session.session_category`` over the ``/api/sessions`` rows, never from a
    second reading of the lifecycle fields. Only ``running``/``hold``/
    ``delayed``/``paused`` can come back into a checkout, and that question
    has one answer.

    A client that answered the health probe and then failed on the read is the
    same answer as no client at all -- the session state was not read -- so
    both leave as :class:`_NoDaemon` and both exit 2. Without that, a daemon
    busy enough to time out one request while still answering the probe would
    report exit 1, which the exit codes reserve for "refused or failed": a
    script would read "the cleanup was declined" where in fact nothing was
    even planned (s1448-rev, claunch-rc6z8).
    """
    client, report = daemon_client.connect_with_diagnosis()
    if client is None:
        raise _NoDaemon(daemon_client.unreachable_reason(report))
    try:
        sessions = _sessions(client)
    except daemon_client.DaemonClientError as exc:
        raise _NoDaemon(str(exc)) from exc
    roots = _roots(sessions)
    return inv.inventory(roots, sessions, bases=_bases(roots))


class _NoDaemon(Exception):
    """The session state could not be read, so nothing was planned."""


class _Record:
    """A ``/api/sessions`` row as :func:`session_category` reads a session."""

    def __init__(self, row: dict) -> None:
        self.archived_at = row.get("archived_at")
        self.held_at = row.get("held_at")
        self.delayed_at = row.get("delayed_at")
        self.failed_at = row.get("failed_at")
        self.paused_at = row.get("paused_at")
        self.exit_code = row.get("exit_code")
        self.exited = (row.get("status") or "") == "exited"


def _sessions(client) -> List[dict]:
    from .daemon.session import session_category

    rows = client.get("/api/sessions").get("sessions") or []
    return [
        {
            "name": row.get("name") or "",
            "cwd": row.get("cwd") or "",
            "category": session_category(_Record(row)),
            "status": row.get("status") or "",
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]


def _sessions_base() -> str:
    """The directory every session's scratch tree lives under."""
    from .daemon import paths

    return str(paths.daemon_dir() / "sessions")


def _scratch_roots(base: str) -> List[str]:
    """Repositories whose worktrees were cut into a session's scratch tree.

    Read from the ``gitdir:`` line of each ``.git`` file under
    ``<sessions>/<session>/scratch/`` -- no git process per candidate, which
    matters at 300+ of them.
    """
    if not base or not os.path.isdir(base):
        return []
    found: Dict[str, str] = {}
    patterns = ("*/scratch/*/.git", "*/scratch/*/*/.git", "*/scratch/*/*/*/.git")
    for pattern in patterns:
        for entry in glob.iglob(os.path.join(base, *pattern.split("/"))):
            try:
                with open(entry, encoding="utf-8", errors="replace") as fh:
                    text = fh.read(300)
            except OSError:
                continue
            if not text.startswith("gitdir:"):
                continue
            gitdir = text[len("gitdir:"):].strip().replace("\\", "/")
            # <root>/.git/worktrees/<name> -> <root>
            at = gitdir.find("/.git/worktrees/")
            if at <= 0:
                continue
            for candidate in (gitdir[:at], gitdir[:at].replace("/", os.sep)):
                if os.path.isdir(candidate):
                    found[_key(candidate)] = candidate
                    break
    return list(found.values())


def _roots(sessions: List[dict]) -> List[str]:
    """Every repository a launcher worktree may belong to: the fleet's
    (workspaces and session directories) plus the scratch trees'."""
    from . import workspaces

    base = _sessions_base()
    dirs = [w.path for w in workspaces.list_all()] + [s["cwd"] for s in sessions]
    roots = inv.repo_roots([d for d in dirs if d] + [base])
    seen = {_key(r) for r in roots}
    for root in _scratch_roots(base):
        if _key(root) not in seen:
            seen.add(_key(root))
            roots.append(root)
    return roots


def _bases(roots: List[str]) -> List[str]:
    """Every directory a launcher worktree of one of ``roots`` is cut into:
    each repository's own ``.claude/worktrees`` plus the session scratch
    trees, which is where the checkouts the page cannot see live."""
    out: List[str] = []
    for root in roots:
        out.extend(inv.default_bases(root))
    out.append(_sessions_base())
    return out


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(child: str, parent: str) -> bool:
    c, p = _key(child), _key(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _items(answer: dict, repo: str = "") -> List[dict]:
    """Every worktree row, each carrying its repository root.

    ``repo`` narrows to the worktrees of that repository, matched by path
    containment so ``--repo .`` works from inside a checkout.
    """
    wanted = os.path.abspath(repo) if repo else ""
    out: List[dict] = []
    for entry in answer.get("repos") or []:
        root = str(entry.get("root") or "")
        if wanted and not _inside(wanted, root):
            continue
        for item in entry.get("worktrees") or []:
            out.append({**item, "root": root, "trunk": entry.get("trunk") or ""})
    return out


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{n}B"


def _age_days(item: dict, now: Optional[float] = None) -> Optional[float]:
    """How long the checkout has stood untouched, in days.

    The checkout directory's mtime: it moves when something is written at the
    top of the tree, so a checkout someone has been working in reads as
    recent even when it was created long ago. The creation stamp is the
    fallback when the directory cannot be stat'ed.
    """
    now = time_now() if now is None else now
    path = str(item.get("path") or "")
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        created = item.get("created_at")
        if not created:
            return None
        try:
            stamp = datetime.fromisoformat(str(created)).timestamp()
        except ValueError:
            return None
    return max(0.0, (now - stamp) / 86400.0)


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _sessions_text(item: dict) -> str:
    parts = []
    for session in item.get("sessions") or []:
        name = str(session.get("name") or "")
        category = str(session.get("category") or "")
        parts.append(f"{name}({category})" if category else name)
    return ", ".join(parts) or "-"


def _size(item: dict) -> Optional[dict]:
    """``{"files", "bytes"}`` of the checkout, or ``None`` when it is gone.

    Counted by :func:`workspace_usage.walk_size`, which does not follow a link
    -- a borrowed ``node_modules`` junction is one entry, not the tree behind
    it (claunch-4m2s9's neighbourhood).
    """
    path = str(item.get("path") or "")
    if not os.path.isdir(path):
        return None
    from . import workspace_usage

    files, total = 0, 0
    try:
        total, files = workspace_usage.walk_size(path)
    except OSError:
        return None
    return {"files": files, "bytes": total}


# --------------------------------------------------------------------------- #
# the selection rule
# --------------------------------------------------------------------------- #
def _sizes(items: List[dict]) -> List[Optional[dict]]:
    """The checkouts' sizes, walked in a small pool -- each walk is I/O bound
    and independent, and there are hundreds of them."""
    if not items:
        return []
    if len(items) < 2:
        return [_size(items[0])]
    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as pool:
        return list(pool.map(_size, items))


def select(
    item: dict,
    *,
    older_than_days: float,
    age_days: Optional[float],
    dirty: Optional[bool] = None,
    reachable: Optional[bool] = None,
    cwd: str = "",
) -> List[str]:
    """Why ``item`` is kept, as one line per reason; empty means removable.

    Written as a pure question over a row so the four conditions can be read
    (and tested) without a daemon, a git process or 308 checkouts: ``dirty``
    and ``reachable`` are the two answers that need git, and ``None`` means
    "not measured", which keeps the row.
    """
    keep: List[str] = []

    for session in item.get("sessions") or []:
        category = str(session.get("category") or "")
        if not category or category in RUNNING_CATEGORIES:
            # An unreadable category keeps the checkout: not knowing is not
            # the same as knowing it is gone.
            keep.append(f"session {session.get('name')} is {category or 'unknown'}")

    if item.get("is_trunk"):
        keep.append("stands on the trunk branch")
    if item.get("locked"):
        keep.append("locked by git (git worktree unlock first)")
    if cwd and _inside(cwd, str(item.get("path") or "")):
        keep.append("this command is running from it")

    if age_days is None:
        keep.append("age could not be read")
    elif age_days < older_than_days:
        keep.append(f"{age_days:.1f}d old, under --older-than {older_than_days:g}")

    if dirty is None:
        keep.append("changes could not be read")
    elif dirty:
        keep.append("has uncommitted or untracked files")
    if reachable is not None and not reachable:
        keep.append("stands on a commit no branch or preview ref reaches")

    return keep


def _dirty(path: str) -> Optional[bool]:
    if not os.path.isdir(path):
        return False
    try:
        return bool(inv.changes(path))
    except Exception:  # noqa: BLE001 - an unreadable checkout is kept
        return None


def _reachable(item: dict) -> Optional[bool]:
    """Whether the checkout's commit is reachable from a branch or a preview
    ref. A checkout on a branch is; a detached one is asked of git."""
    if item.get("branch") or not item.get("head"):
        return True
    path = str(item.get("path") or "")
    root = str(item.get("root") or "")
    if not os.path.isdir(path):
        return True  # the directory is gone; only the registration is left
    sha = str(item["head"])
    for ref in ("refs/heads", "refs/claunch/preview"):
        done = worktree_mod._git(["for-each-ref", "--count=1", "--contains", sha, ref],
                                 cwd=root or path)
        if done.returncode == 0 and (done.stdout or "").strip():
            return True
    return False


def _plan(items: List[dict], *, older_than_days: float, now: Optional[float] = None,
          measure_changes: bool = True) -> List[dict]:
    """``[{"item", "keep"}]`` in the order given.

    The two git questions per checkout (``status`` and reachability) are asked
    in a small thread pool: at 400+ checkouts one at a time is minutes of wall
    clock for no reason, and each is an independent read of its own
    repository.
    """
    now = time_now() if now is None else now
    cwd = os.getcwd()

    def one(item: dict) -> dict:
        age = _age_days(item, now)
        dirty = _dirty(str(item.get("path") or "")) if measure_changes else None
        reach = _reachable(item)
        return {
            "item": item,
            "age_days": age,
            "keep": select(item, older_than_days=older_than_days, age_days=age,
                           dirty=dirty, reachable=reach, cwd=cwd),
        }

    if len(items) < 2:
        return [one(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as pool:
        return list(pool.map(one, items))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _cmd_list(args: argparse.Namespace) -> int:
    try:
        answer = _inventory()
    except _NoDaemon as exc:
        print(f"cannot read the worktrees: {exc}", file=sys.stderr)
        return EXIT_NO_DAEMON
    items = _items(answer, args.repo)
    sizes = _sizes(items) if not args.no_size else [None] * len(items)
    if args.json:
        rows = []
        for item, size in zip(items, sizes):
            row = {k: v for k, v in item.items() if k != "sessions"}
            row["sessions"] = [
                {"name": s.get("name"), "category": s.get("category"),
                 "status": s.get("status")}
                for s in item.get("sessions") or []
            ]
            row["size"] = size
            rows.append(row)
        print(json.dumps({"worktrees": rows}, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not items:
        scope = f" under {args.repo}" if args.repo else ""
        print(f"no launcher worktrees{scope}")
        return EXIT_OK

    files_total = 0
    counted = 0
    width = max(len(str(i.get("name") or "")) for i in items)
    header = f"{'STATE':<9} {'NAME':<{width}}  {'BRANCH':<28}  {'SESSIONS':<24}  CREATED"
    if not args.no_size:
        header += "        FILES     SIZE"
    print(header)
    for item, size in zip(items, sizes):
        branch = str(item.get("branch") or ("(detached)" if item.get("detached") else "-"))
        if item.get("merged") is True:
            branch += " *merged*"
        notes = []
        if item.get("missing"):
            notes.append("missing")
        if item.get("locked"):
            notes.append("locked")
        line = (
            f"{str(item.get('state') or ''):<9} "
            f"{str(item.get('name') or ''):<{width}}  "
            f"{branch[:28]:<28}  {_sessions_text(item)[:24]:<24}  "
            f"{str(item.get('created_at') or '-')[:19]}"
        )
        if notes:
            line += "  [" + ",".join(notes) + "]"
        if not args.no_size:
            if size is None:
                line += f"  {'-':>8} {'-':>8}"
            else:
                files_total += size["files"]
                counted += 1
                line += f"  {size['files']:>8,} {_human(size['bytes']):>8}"
        print(line)
    if not args.no_size and counted:
        print(f"\n{counted} checkout(s), {files_total:,} files/inodes counted "
              f"(links are one entry each, never followed)")
    return EXIT_OK


def _cmd_prune(args: argparse.Namespace) -> int:
    try:
        answer = _inventory()
    except _NoDaemon as exc:
        print(f"cannot plan a prune: {exc}", file=sys.stderr)
        print("the session state decides which checkouts are safe to remove, "
              "so nothing was planned", file=sys.stderr)
        return EXIT_NO_DAEMON
    items = _items(answer, args.repo)
    plan = _plan(items, older_than_days=args.older_than)
    removable = [row for row in plan if not row["keep"]]
    kept = [row for row in plan if row["keep"]]

    removed, failed = [], []
    if args.force and removable:
        for row in removable:
            item = row["item"]
            try:
                # The page's own verb: unlink links, keep the branch, refuse a
                # checkout this process is running from.
                removed.append(inv.remove(str(item["root"]), str(item["path"])))
            except Exception as exc:  # noqa: BLE001 - one refusal is not the batch
                failed.append({"path": item.get("path"), "error": str(exc)})

    if args.json:
        print(json.dumps({
            "dry_run": not args.force,
            "older_than_days": args.older_than,
            "remove": [{"name": r["item"].get("name"), "path": r["item"].get("path"),
                        "branch": r["item"].get("branch"),
                        "age_days": round(r["age_days"], 2) if r["age_days"] is not None else None}
                       for r in removable],
            "keep": [{"name": r["item"].get("name"), "path": r["item"].get("path"),
                      "reasons": r["keep"]} for r in kept],
            "removed": removed,
            "failed": failed,
        }, ensure_ascii=False, indent=2))
        return EXIT_REFUSED if failed else EXIT_OK

    verb = "REMOVED" if args.force else "would remove"
    print(f"-- older than {args.older_than:g} days --")
    for row in removable:
        item = row["item"]
        age = row["age_days"]
        print(f"  {verb:<12} {str(item.get('name') or ''):<28} {item.get('path')}"
              f"  ({age:.1f}d, branch {item.get('branch') or '(detached)'},"
              f" sessions {_sessions_text(item)})")
    for row in kept:
        item = row["item"]
        print(f"  {'keep':<12} {str(item.get('name') or ''):<28} {item.get('path')}"
              f"  -- {'; '.join(row['keep'])}")
    for row in failed:
        print(f"  {'FAILED':<12} {row['path']}: {row['error']}", file=sys.stderr)

    total = len(removable)
    if args.force:
        print(f"\nremoved {len(removed)} of {total} candidate(s); kept {len(kept)}."
              f" Branches are kept -- 'git worktree prune' cleared the registrations"
              f" of any whose directory was already gone.")
    elif total:
        print(f"\n{total} candidate(s), {len(kept)} kept. Nothing was removed:"
              f" this is the plan. Run again with --force to remove them.")
    else:
        print(f"\nnothing to remove; {len(kept)} checkout(s) kept.")
    return EXIT_REFUSED if failed else EXIT_OK


def register(sub) -> None:
    p = sub.add_parser(
        "worktree",
        help="list the launcher worktrees, and clean up the ones nothing can "
             "come back to",
        description=(
            "The same reading the web UI's Worktrees page draws, over every "
            "repository the fleet has touched. 'prune' only removes a checkout "
            "when no live session is joined to it, it is clean, its commit is "
            "reachable from a branch or preview ref, and it is older than "
            "--older-than days; without --force it only prints the plan."
        ),
    )
    wsub = p.add_subparsers(dest="worktree_command", required=True)

    p_list = wsub.add_parser(
        "list", aliases=["ls"],
        help="every launcher worktree with its sessions, state and size",
    )
    p_list.add_argument("--repo", help="only the worktrees of this repository")
    p_list.add_argument("--no-size", action="store_true",
                        help="skip the file/inode count (it walks every checkout)")
    p_list.add_argument("--json", action="store_true", help="dump rows as JSON")
    p_list.set_defaults(func=_cmd_list)

    p_prune = wsub.add_parser(
        "prune",
        help="plan (or, with --force, run) removal of finished worktrees",
        description=(
            "Removes a launcher worktree only when (1) no session joined to it "
            "is running, held, delayed or paused, (2) git status is clean, "
            "(3) its commit is reachable from a branch or refs/claunch/preview/*, "
            "and (4) it is at least --older-than days old. Prints the plan "
            "unless --force is given."
        ),
    )
    p_prune.add_argument("--repo", help="only this repository's worktrees")
    p_prune.add_argument("--older-than", type=float, default=DEFAULT_OLDER_THAN,
                         metavar="DAYS",
                         help=f"minimum age in days (default: {DEFAULT_OLDER_THAN:g})")
    p_prune.add_argument("--dry-run", action="store_true",
                         help="print the plan; this is already the default")
    p_prune.add_argument("--force", action="store_true",
                         help="actually remove the planned checkouts")
    p_prune.add_argument("--json", action="store_true", help="dump the plan as JSON")
    p_prune.set_defaults(func=_cmd_prune)
