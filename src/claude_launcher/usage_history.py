"""Subscription usage samples, one append-only JSONL file per usage key
(claunch-f5w4x.3).

:mod:`profile_usage` calls :func:`append` each time a provider read succeeds,
never for an answer served from its cache, so a line is one real reading of
the account. The files live under ``<daemon dir>/usage-history`` and are kept
forever; :func:`stats` reports what they weigh so the page can show it.

A line is the reading's payload (the same dict ``/api/usage`` answers) plus
``at``, the epoch seconds of the read. The last line of a key is also what a
restarted daemon serves until that key's next read (:func:`last`), so the
refresh clock does not ask every account again the moment the daemon is back.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterator, List, Optional
from urllib.parse import quote

SUFFIX = ".jsonl"
#: Bytes read from the end of a file to find its last line.
_TAIL = 64 * 1024

_guard = threading.Lock()


def history_dir() -> Path:
    from .daemon import paths

    return paths.daemon_dir() / "usage-history"


def path_for(key: str) -> Path:
    """The file of ``key`` (``profile/provider/reader``): the key quoted whole,
    so a ``/`` in it names no directory."""
    return history_dir() / (quote(key, safe="") + SUFFIX)


def append(key: str, payload: dict, at: float) -> None:
    """Write one sample of ``key``. Raises :class:`OSError` when the disk does."""
    line = json.dumps({**payload, "at": at}, ensure_ascii=False, separators=(",", ":"))
    target = path_for(key)
    with _guard:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _lines(path: Path) -> Iterator[dict]:
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        return
    with fh:
        for raw in fh:
            try:
                row = json.loads(raw)
            except ValueError:
                continue  # a line cut short by a crash mid-write
            if isinstance(row, dict) and isinstance(row.get("at"), (int, float)):
                yield row


def read(key: str, since: Optional[float] = None) -> List[dict]:
    """Samples of ``key`` in file order, those at or after ``since`` when given."""
    return [row for row in _lines(path_for(key)) if since is None or row["at"] >= since]


def last(key: str) -> Optional[dict]:
    """The newest sample of ``key``, read from the end of its file."""
    path = path_for(key)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(size - _TAIL, 0))
            tail = fh.read()
    except FileNotFoundError:
        return None
    for raw in reversed(tail.splitlines()):
        try:
            row = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("at"), (int, float)):
            return row
    return None


def stats(key: str, *, period: float) -> dict:
    """What the file of ``key`` weighs: bytes, samples, first and last sample
    time, and the average growth per day.

    The growth is observed (bytes over the days between the first and the
    last sample) once the file spans a day; before that it is projected from
    the mean line size at one sample per ``period`` seconds, and ``basis``
    says which.
    """
    path = path_for(key)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    first_at = last_at = None
    count = 0
    for row in _lines(path):
        count += 1
        first_at = row["at"] if first_at is None else first_at
        last_at = row["at"]
    per_day, basis = None, None
    if count:
        span = (last_at - first_at) / 86400
        if span >= 1:
            per_day, basis = size / span, "observed"
        elif period > 0:
            per_day, basis = size / count * 86400 / period, "projected"
    return {
        "bytes": size, "samples": count, "first_at": first_at, "last_at": last_at,
        "per_day_bytes": per_day, "basis": basis,
    }


def total() -> dict:
    """Bytes and files of the whole history directory."""
    size = files = 0
    try:
        entries = list(history_dir().iterdir())
    except FileNotFoundError:
        entries = []
    for entry in entries:
        if entry.suffix != SUFFIX:
            continue
        try:
            size += entry.stat().st_size
        except FileNotFoundError:
            continue
        files += 1
    return {"bytes": size, "files": files, "dir": str(history_dir())}

