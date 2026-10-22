"""Index the test debris under the test-output root and plan its removal.

Every pytest basetemp and every ``$CLAUNCH_TEST_DIR`` this machine ever made
sits under one root (``pytest_basetemp.root()``: ``<launcher home>/basetemp``
on POSIX, ``C:/t`` on Windows). The exit reaper deletes a session's entries
when the session ends, but three shapes survive it and accumulate:

* an entry whose session has left the registry -- nothing owns the name any
  more, so no session's exit ever matches it (an *orphan*);
* a tree the reaper could not open, recorded ``locked`` in
  ``basetemp-history.jsonl`` -- 49 directories at mode ``0200`` on
  2026-10-22, 9,135 recorded attempts (claunch-zbu54);
* a held session's trees, which are correct to keep but count against the
  inode budget for as long as the session is held.

On 2026-10-22 that accumulation reached 100% inode use on ``/`` and the
daemon began failing writes (``OSError: [Errno 28]``) with 500 GB of bytes
still free (claunch-zbu54, claunch-slia4.1). This tool is the measurement
behind the cleanup: it reads the root, attributes every entry with the
reaper's own name rule, and prints removal batches. It never deletes and
never changes a permission bit -- the plan is for a person to approve.

    uv run --no-sync python tools/test_debris_index.py
    uv run --no-sync python tools/test_debris_index.py --measure
    uv run --no-sync python tools/test_debris_index.py --json
    uv run --no-sync python tools/test_debris_index.py --root D:/tmp/t --stale-days 3

Exit codes, in the shape the other tools use:

    0  an index was produced (an empty root is an empty index, not a failure)
    2  the root could not be listed

``--measure`` walks each tree to count files and bytes. That is the expensive
part (a full pass over every inode under the root) and it is off by default:
the plan's batches are ordered by name and age, which do not need a walk.

Everything that decides ownership comes from ``daemon.basetemp_reaper``, and
that is the guarantee this tool makes: the name rule (``owner``), the set of
registry directories (``registry_dirs``, anchored at ``paths.base_home`` so a
named instance's registry is found), the records themselves (``registry``,
which reads ``sessions.db`` and falls back to the legacy ``sessions.json``),
and the live rule (``live_name``: running *or held*). It does not re-derive
any of them, so this index and the reaper go wrong together or not at all.
That matters in one direction only — an entry read as *ended* when a session
is live lands in a removal batch nobody will review again.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher import pytest_basetemp  # noqa: E402
from claude_launcher.daemon import basetemp_reaper as reaper  # noqa: E402

LIVE, ENDED, ORPHAN, AMBIGUOUS = "live", "ended", "orphan", "ambiguous"


def session_states(dirs: Optional[Iterable[Path]] = None) -> Dict[str, str]:
    """``name -> 'live' | 'ended'`` for every session any registry knows.

    Both the directory set (:func:`basetemp_reaper.registry_dirs`) and the
    records (:func:`basetemp_reaper.registry`) come from the reaper, so this
    index cannot disagree with the exit reaper about which sessions exist --
    including an instance that still keeps a ``sessions.json`` and one whose
    home sits under ``CLAUNCH_INSTANCE_BASE``. ``live_name`` is the reaper's
    own live rule: a held session's program has ended but the session has
    not, and resuming it must find its basetemps where it left them.
    """
    states: Dict[str, str] = {}
    for directory in dirs if dirs is not None else reaper.registry_dirs():
        for entry in reaper.registry(directory):
            sdef = entry.get("def") if isinstance(entry.get("def"), dict) else {}
            name = sdef.get("name") or entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            state = LIVE if reaper.live_name(entry) else ENDED
            # One registry saying live wins: a name is recorded in more than
            # one instance when it was adopted, and only the live one matters.
            if states.get(name) == LIVE:
                continue
            states[name] = state
    return states


def classify(name: str, states: Dict[str, str]) -> str:
    """Whose entry ``name`` is: a live session, an ended one, or nobody's.

    ``ambiguous`` is the fourth answer, and it exists because the reaper's
    shape list is deliberately narrow: ``s29d``, ``s29om`` and ``s29w2`` are
    none of `<session>`, `<session>w`, `<session>c<stamp>` or
    `<session>-<anything>`, so nothing owns them -- while they are obviously
    named after a session that is still alive. Deleting them on the strength
    of "no owner" is the mistake this guards (measured 2026-10-22: five such
    trees under basetemp, 510 inodes, all held by hand).
    """
    owner = reaper.owner(name, states)
    if owner is not None:
        return states[owner]
    for live in (n for n, s in states.items() if s == LIVE):
        if name.startswith(live):
            return AMBIGUOUS
    return ORPHAN


def readable(path: Path) -> bool:
    """Whether the entry can be listed at all (mode ``0200`` cannot).

    Only the entry itself is tried, not the whole tree: listing every level is
    the ``--measure`` walk again, and a ``0200`` directory deeper inside shows
    up as a failed batch rather than as a wrong plan. The 49 directories
    claunch-zbu54 recorded were all at the root, and re-running this after the
    repair step finds any that were not.
    """
    try:
        with os.scandir(path):
            return True
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return False


def measure(path: Path) -> dict:
    """File count and bytes for one tree, with the reaper's own walk."""
    try:
        return reaper.describe(path)
    except OSError:
        return {"files": 0, "bytes": 0}


def build(root: Path, states: Dict[str, str], do_measure: bool) -> List[dict]:
    """One row per entry under ``root``, newest metadata first."""
    try:
        paths = sorted(p for p in root.iterdir())
    except OSError:
        return None
    rows = []
    now = time.time()
    for path in paths:
        try:
            st = path.lstat()
        except OSError:
            continue
        row = {
            "path": path.as_posix(),
            "name": path.name,
            "kind": "dir" if path.is_dir() else "file",
            "owner": reaper.owner(path.name, states) or None,
            "state": classify(path.name, states),
            "mode": oct(st.st_mode & 0o777),
            "age_hours": round((now - st.st_mtime) / 3600.0, 1),
            "readable": readable(path) if path.is_dir() else True,
        }
        if do_measure:
            row.update(measure(path))
        rows.append(row)
    return rows


def missing_root(root: Path) -> int:
    print(f"root not listable: {root}", file=sys.stderr)
    return 2


def plan(rows: List[dict], stale_days: float) -> List[dict]:
    """Removal batches, in the order they have to run.

    1. ``repair`` -- ``chmod -R u+rwX`` each unreadable tree. Nothing else can
       touch a mode-``0200`` tree, including ``rmdir`` (claunch-zbu54).
    2. ``orphaned`` -- no registered session owns the name; no exit will ever
       claim it.
    3. ``stale`` -- owned by an ended session and older than ``stale_days``.
       The exit reaper should already have taken these; when it has not, the
       history says why (``locked``).
    4. ``held`` -- not in the plan. A held session may still be resumed.
    """
    stale_hours = stale_days * 24.0
    batches = {"repair": [], "orphaned": [], "stale": []}
    for row in rows:
        if row["kind"] != "dir":
            continue
        if not row["readable"]:
            batches["repair"].append(row)
        elif row["state"] == ORPHAN:
            batches["orphaned"].append(row)
        elif row["state"] == ENDED and row["age_hours"] >= stale_hours:
            batches["stale"].append(row)
    return [
        {
            "step": step,
            "count": len(items),
            "files": sum(item.get("files", 0) for item in items),
            "bytes": sum(item.get("bytes", 0) for item in items),
            "paths": [item["path"] for item in items],
        }
        for step, items in batches.items()
    ]


# ---- worktrees ----------------------------------------------------------- #
#
# `$CLAUNCH_SCRATCH` sits inside the launcher home, so a linked worktree made
# there (a review checkout, a test tree) is registered with the session's own
# repository and outlives the session unless someone removes it. 443 were
# registered on 2026-10-22, 308 of them under a session's scratch.
#
# Removal is `git worktree remove` (which unregisters) -- not `rm -rf`, which
# leaves the registration behind and makes `git worktree prune` the only way
# to find it again. Nothing here removes anything.

#: A path under this segment belongs to the session named right after it:
#: ``<launcher home>/daemon/sessions/<session>/scratch/<tree>``.
_SESSIONS_SEGMENT = "sessions"
_SCRATCH_SEGMENT = "scratch"


def parse_worktrees(porcelain: str) -> List[dict]:
    """``git worktree list --porcelain`` -> one dict per worktree.

    The format is blank-line separated stanzas of ``key value`` lines; only
    the three keys this needs are read, and an unparsable stanza is skipped
    rather than guessed at.
    """
    out: List[dict] = []
    current: Dict[str, str] = {}
    for line in porcelain.splitlines() + [""]:
        if not line.strip():
            if current.get("worktree"):
                branch = current.get("branch") or ""
                out.append({
                    "path": current["worktree"],
                    "head": current.get("HEAD", ""),
                    "branch": branch[len("refs/heads/"):] if branch else "",
                    "detached": not branch,
                })
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return out


def session_of(path: str) -> Optional[str]:
    """The session a scratch worktree belongs to, or ``None``.

    Only the daemon's own layout is read: a path that does not sit under
    ``daemon/sessions/<name>/scratch/`` names no session here, and guessing
    from an arbitrary directory name would be worse than saying nothing.
    """
    parts = Path(path).parts
    for i in range(len(parts) - 2):
        if parts[i] == "sessions" and parts[i + 2] == _SCRATCH_SEGMENT:
            return parts[i + 1]
    return None


def worktree_rows(repo: Path, states: Dict[str, str], root: Path,
                  reachable: Optional[set] = None) -> List[dict]:
    """Every registered worktree whose path sits under ``root``, judged.

    ``reachable`` is the set of commits some ref still points at
    (:func:`reachable_commits`). A commit nothing reaches is the one case a
    worktree cannot be recovered from after removal, so it decides the
    verdict together with whether the owning session is alive.
    """
    rows = []
    for item in parse_worktrees(_git(repo, "worktree", "list", "--porcelain")):
        path = Path(item["path"])
        try:
            inside = path.resolve().is_relative_to(root.resolve())
        except OSError:
            inside = False
        if not inside:
            continue
        session = session_of(item["path"])
        state = states.get(session) if session else None
        row = {
            "path": item["path"],
            "session": session,
            "state": state or "unknown",
            "branch": item["branch"] or "(detached)",
            "head": item["head"],
            "removable": False,
            "why": "",
        }
        if state == LIVE:
            row["why"] = "live session"
        elif reachable is not None and item["head"] and item["head"] not in reachable:
            row["why"] = "commit reachable from no ref"
        else:
            dirty = _git(path, "status", "--porcelain").strip()
            if dirty:
                row["why"] = "uncommitted changes"
            else:
                row["removable"] = True
                row["why"] = "ended session, clean, commit still reachable"
        rows.append(row)
    return rows


def reachable_commits(repo: Path) -> Optional[set]:
    """Every commit some ref points at, or ``None`` when git cannot say."""
    try:
        out = _git(repo, "rev-list", "--all")
    except OSError:
        return None
    if out is None:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stdout; ``""`` when it fails."""
    import subprocess

    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def render(rows: List[dict], batches: List[dict], root: Path,
           worktrees: Optional[List[dict]] = None) -> str:
    lines = [f"root: {root}", f"entries: {len(rows)}", ""]
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    unreadable = sum(1 for row in rows if not row["readable"])
    lines.append(
        "states: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + f", unreadable={unreadable}"
    )
    lines.append("")
    for batch in batches:
        if not batch["count"]:
            continue
        size = ""
        if batch["files"] or batch["bytes"]:
            size = f" ({batch['files']} files, {batch['bytes'] / 1e6:.1f} MB)"
        lines.append(f"[{batch['step']}] {batch['count']} dirs{size}")
        for path in batch["paths"][:20]:
            lines.append(f"  {path}")
        if batch["count"] > 20:
            lines.append(f"  ... and {batch['count'] - 20} more")
        lines.append("")
    if worktrees is not None:
        removable = [w for w in worktrees if w["removable"]]
        lines.append(
            f"[worktrees] {len(worktrees)} under the launcher home, "
            f"{len(removable)} removable "
            f"(git worktree remove <path> && git worktree prune)"
        )
        for item in worktrees:
            mark = "REMOVE" if item["removable"] else "keep  "
            lines.append(f"  {mark} {item['path']}  [{item['state']}] {item['why']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=None,
                    help="test-output root (default: pytest_basetemp.root())")
    ap.add_argument("--stale-days", type=float, default=1.0,
                    help="an ended session's tree older than this is stale (default 1)")
    ap.add_argument("--measure", action="store_true",
                    help="walk each tree for file and byte counts (slow)")
    ap.add_argument("--json", action="store_true", help="emit the index as JSON")
    ap.add_argument("--worktrees", action="store_true",
                    help="also index the linked worktrees under the launcher home "
                         "(one `git status` per candidate; slower)")
    ap.add_argument("--repo", type=Path, default=Path.cwd(),
                    help="repository whose worktrees to index (default: cwd)")
    args = ap.parse_args(argv)

    root = args.root or pytest_basetemp.root()
    states = session_states()
    rows = build(root, states, args.measure)
    if rows is None:
        return missing_root(root)
    batches = plan(rows, args.stale_days)
    trees = None
    if args.worktrees:
        trees = worktree_rows(args.repo, states, reaper.paths.base_home(),
                              reachable_commits(args.repo))
    if args.json:
        payload = {"root": root.as_posix(), "entries": rows, "batches": batches}
        if trees is not None:
            payload["worktrees"] = trees
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(render(rows, batches, root, trees))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
