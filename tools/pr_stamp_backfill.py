"""Recover the landing date of recorded PR rows from the run journals on disk.

The PR page (``#/pr``) groups rows by their ``at``. A v2 landing record did
not carry one until claunch-ijkge.4.1.3, so every row written before that
reads "날짜 미상" — and a run that settled emptied its ``prs`` collection at
the same time, so the checkpoint no longer holds the value either. The
journals still do: a ``collection_pushed`` event for ``prs`` carries the
item with the epoch time the push stamped.

This tool reads those journals and writes the recovered dates into the
daemon's sidecar cache (``daemon/pr_stamps.py``), which
``session_prs.for_session`` merges into rows that have no date of their own.
It never writes a run's own state: ``.cflow2/runs/…`` belongs to the session
in that worktree, and a scan here is read-only.

Report first, write second — the default is a dry run:

    uv run --no-sync python tools/pr_stamp_backfill.py --cwd <worktree> …
    uv run --no-sync python tools/pr_stamp_backfill.py --root ~/works --apply

Where it looks, per directory:

    <dir>/.cflow2/runs/*/journal.jsonl          a live run
    <dir>/.cflow2/archive/*/*/journal.jsonl     a run that was archived

``--root`` walks a tree for those two shapes (``--root ~/works`` covers every
checkout below it); ``--cwd`` names one working directory; ``--journal`` names
one journal file. At least one of the three is required — there is no guess
at where the caller's worktrees are.

Exit codes: 0 the scan ran (whether or not anything was recovered), 2 no
source was given or a journal could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Same as the other tools: run from a checkout, against that checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher.daemon import pr_stamps  # noqa: E402

RUNS = ".cflow2/runs/*/journal.jsonl"
ARCHIVE = ".cflow2/archive/*/*/journal.jsonl"


def _journals(args) -> list[Path]:
    found: list[Path] = []
    for name in args.journal:
        found.append(Path(name).expanduser())
    for cwd in args.cwd:
        base = Path(cwd).expanduser()
        found.extend(sorted(base.glob(RUNS)))
        found.extend(sorted(base.glob(ARCHIVE)))
    for root in args.root:
        base = Path(root).expanduser()
        for pattern in (RUNS, ARCHIVE):
            found.extend(sorted(base.glob(f"**/{pattern}")))
    seen, unique = set(), []
    for path in found:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _iso(at) -> str | None:
    """The journal's epoch stamp as the ISO-8601 string the page reads."""
    if isinstance(at, str) and at:
        return at
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        try:
            return datetime.fromtimestamp(at, timezone.utc).isoformat(
                timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def scan(paths: list[Path]) -> dict:
    """``{url: {"at", "number", "repo"}}`` from every journal, newest wins."""
    rows: dict[str, dict] = {}
    read = failed = 0
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or "collection_pushed" not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (not isinstance(event, dict)
                            or event.get("event") != "collection_pushed"
                            or event.get("collection") != "prs"):
                        continue
                    item = event.get("item")
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url")
                    name = pr_stamps.key(url)
                    at = _iso(item.get("at"))
                    if not name or not at:
                        continue
                    old = rows.get(url)
                    if old and old["at"] >= at:
                        continue
                    rows[url] = {"at": at, "number": item.get("number")}
            read += 1
        except OSError as exc:
            failed += 1
            print(f"skip (unreadable): {path}: {exc}", file=sys.stderr)
    return {"rows": rows, "journals": read, "failed": failed}


def apply(rows: dict, *, write: bool) -> dict:
    """Write each recovered date into the sidecar unless one is already kept."""
    entries = pr_stamps.entries()
    updates, already = {}, 0
    for url, row in rows.items():
        name = pr_stamps.key(url)
        if not name:
            continue
        if isinstance((entries.get(name) or {}).get("at"), str):
            already += 1
            continue
        updates[name] = {"at": row["at"], "at_source": "journal"}
    if write and updates:
        pr_stamps.write(updates)
    return {"writable": len(updates), "already": already,
            "written": len(updates) if write else 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover PR landing dates from run journals into the "
                    "daemon's PR stamp cache.")
    parser.add_argument("--cwd", action="append", default=[],
                        metavar="DIR",
                        help="a working directory whose runs are scanned")
    parser.add_argument("--root", action="append", default=[],
                        metavar="DIR",
                        help="walk this tree for every run it contains")
    parser.add_argument("--journal", action="append", default=[],
                        metavar="FILE", help="one journal file")
    parser.add_argument("--apply", action="store_true",
                        help="write the recovered dates (default: report only)")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON")
    parser.add_argument("--show", type=int, default=0, metavar="N",
                        help="print the first N recovered rows")
    args = parser.parse_args(argv)
    if not (args.cwd or args.root or args.journal):
        parser.error("give at least one of --cwd, --root or --journal")
    paths = _journals(args)
    if not paths:
        parser.error("no journal found under the given paths")
    found = scan(paths)
    rows = found["rows"]
    result = apply(rows, write=args.apply)
    cache = pr_stamps.path()
    if args.json:
        print(json.dumps({
            "journals": found["journals"], "unreadable": found["failed"],
            "pr_rows": len(rows), "cache": str(cache), "applied": args.apply,
            **result,
            "sample": [{"url": u, "at": r["at"]}
                       for u, r in list(rows.items())[:max(args.show, 0)]],
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"journals : {found['journals']} read"
          + (f", {found['failed']} unreadable" if found["failed"] else ""))
    print(f"pr rows  : {len(rows)} distinct (with a journal date)")
    print(f"cache    : {cache}")
    print(f"fillable : {result['writable']}"
          f" (already dated in the cache: {result['already']})")
    if args.apply:
        print(f"written  : {result['written']}")
    else:
        print("dry run  : pass --apply to write them")
    for url, row in list(rows.items())[:max(args.show, 0)]:
        print(f"  {row['at']}  {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
