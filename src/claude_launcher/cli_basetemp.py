"""``claunch basetemp`` -- housekeeping for the gate tools' pytest root.

``prune`` is the on-demand companion of the daemon's boot sweep
(:mod:`claude_launcher.daemon.basetemp_reaper`): it lists -- and with
``--apply`` removes -- the basetemps whose session ended, and the old trees
whose owner is in no registry at all. A dry run is the default. Held
sessions' trees stay, in this instance and in every other one.

The registry is read, never written: the daemon owns it, and this command
works with or without one running.
"""

from __future__ import annotations

import argparse

from .daemon import basetemp_reaper


def _days(text: str) -> float:
    """``--older-than`` value: ``7``, ``7d`` and ``0.5d`` are the same unit."""
    raw = str(text).strip().lower()
    if raw.endswith("d"):
        raw = raw[:-1]
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a day count: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("days cannot be negative")
    return value


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def _cmd_prune(args) -> int:
    sessions = basetemp_reaper.registry_snapshot()
    reaper = basetemp_reaper.BasetempReaper(lambda: list(sessions))
    rows = reaper.prune(older_than=args.older_than * 86400.0, apply=args.apply)
    live = sum(1 for _name, ended in sessions if not ended)
    print(f"root: {reaper.root}")
    print(f"registry: {len(sessions)} session(s), {live} live")
    if not rows:
        print("no removable basetemps")
        return 0
    total = sum(int(row.get("bytes") or 0) for row in rows)
    for row in rows:
        meta = [
            f"{row.get('workers', 0)} worker(s)",
            f"{row.get('files', 0)} file(s)",
            _size(int(row.get("bytes") or 0)),
        ]
        if row.get("started_at") and row.get("ended_at"):
            meta.append(f"{row['started_at']} -> {row['ended_at']}")
        print(
            f"  {row.get('outcome', ''):<9} {row['session']:<16} {row['path']}"
            f"  ({', '.join(meta)})"
        )
    removed = sum(1 for row in rows if row.get("outcome") == "removed")
    locked = sum(1 for row in rows if row.get("outcome") == "locked")
    if not args.apply:
        print(
            f"dry run: {len(rows)} basetemp(s), {_size(total)} "
            "-- pass --apply to remove"
        )
    else:
        line = f"removed {removed}/{len(rows)} basetemp(s), {_size(total)}"
        if locked:
            line += f"; {locked} left locked (see {basetemp_reaper.HISTORY_NAME})"
        print(line)
    return 0


def register(sub) -> None:
    parser = sub.add_parser(
        "basetemp",
        help="pytest basetemp housekeeping for the gate tools' root",
    )
    inner = parser.add_subparsers(dest="basetemp_command", required=True)
    prune = inner.add_parser(
        "prune",
        help="list (or with --apply, remove) basetemps whose session ended "
        "or left the registry; held sessions' trees stay",
    )
    prune.add_argument(
        "--older-than",
        metavar="Nd",
        type=_days,
        default=basetemp_reaper.ORPHAN_AGE_DAYS,
        help="only trees with no write for this many days "
        "(default: %(default)s); 0 removes the age gate",
    )
    prune.add_argument(
        "--apply",
        action="store_true",
        help="remove them (default is a dry run); every removal is recorded "
        f"in {basetemp_reaper.HISTORY_NAME} before the tree is touched",
    )
    prune.set_defaults(func=_cmd_prune)
