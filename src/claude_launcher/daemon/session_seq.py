"""The session-name counter: the next number the auto-assignment hands out.

Session names used to be chosen by scanning for the lowest free ``sN``, and the
two costs of that scan are one cost. A session directory left behind by a
creation attempt that failed held its number forever, so the numbers a fleet
had spent ran far ahead of the sessions it had (2026-10-27: 5,807 directories,
2,388 daemon records, 3,824 of the directories empty). Deleting such a
directory then made its number available again, so the next session could be
named *lower* than a session created before it — and a recycled name makes two
histories share one output log, one report directory and one worktree name.

This module keeps the number in one small file instead, ``{"next": N}``,
beside the state it names. The counter only moves forward: assigning gives out
``N`` and stores ``N + 1``, and nothing here ever walks it back. Deleting a
directory therefore frees no number.

Two rules the callers keep, because each is about ordering or intent this
module cannot see (:meth:`SessionManager.settle_name`,
:meth:`SessionManager.discard`):

* **The directory is created before the counter moves.** The directory is the
  reservation: ``mkdir`` refusing a name that is already there is what stops
  two creators that read the same counter from taking one number. Writing the
  counter first would instead spend a number on every attempt that then failed,
  which is the behaviour being replaced.
* **A name a person chose does not move the counter**, whether or not it looks
  like a number — ``s1305-rev``, ``job-7`` and a hand-typed ``s9000`` alike. The
  scan steps over a name that is taken, so a person's ``s9000`` costs the
  sequence nothing.

A missing or unreadable file is not an error: :func:`read` rebuilds the next
number from what is in use — both the session directories and the daemon's
records, since each survives a different kind of clear (see :func:`recover`) —
and :func:`log_source` says it did.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import threading
from pathlib import Path
from typing import Callable, Iterable, Iterator, Tuple

from .. import atomic
from . import paths

log = logging.getLogger(__name__)

#: Serialises read-decide-write inside this process. The file is the record
#: across processes; this only keeps two threads of one daemon from both
#: reading ``5`` and both writing ``6``.
_lock = threading.Lock()

#: The document's only key, kept to one number so the file stays something a
#: person can read, correct by hand and diff.
NEXT = "next"

#: What a name in the auto sequence looks like.
NUMBER_RE = re.compile(r"^s(\d+)$")


def file() -> Path:
    """The counter's file: one per launcher home, shared by its instances.

    Anchored on :func:`paths.base_home` rather than :func:`paths.daemon_dir`,
    as the issue asked. Two instances counting from zero on one machine would
    otherwise hand out two different ``s12``s, and the number is what a person
    reads in a listing and what a report directory and a worktree are keyed by.
    """
    return paths.base_home() / "daemon" / "session-seq.json"


def numbers(names: Iterable[str]) -> Iterator[int]:
    """The sequence numbers among ``names``, in order, skipping the rest."""
    for name in names:
        match = NUMBER_RE.match(name or "")
        if match:
            yield int(match.group(1))


def recover(records: Iterable[str] = ()) -> int:
    """The next number the disk says is free: the highest in use, + 1.

    ``records`` is the daemon's own registry, when the caller has one. Both
    sources are needed and neither covers the other: a clear without ``--logs``
    drops the record and keeps the directory, while ``--logs`` takes the
    directory and leaves the record's name in the store until the next prune.
    Either one alone would let a recovery hand out a name the other still
    holds.

    A home with no sessions is ``0``, which is where a fresh one starts.
    """
    return max(numbers(itertools.chain(dirs(), records)), default=-1) + 1


def dirs() -> Iterator[str]:
    """Every session directory name under this launcher home, across instances.

    The default instance's root, the named instances' roots, and whatever the
    active instance resolves to — which is a third place only when an instance
    manifest gave this process a home of its own.
    """
    root = paths.base_home()
    found = [root / "daemon" / "sessions", paths.sessions_root()]
    instances = root / "daemons"
    if instances.is_dir():
        found.extend(sorted(instances.glob("*/sessions")))
    seen = set()
    for sessions in found:
        key = str(sessions)
        if key in seen:
            continue
        seen.add(key)
        try:
            children = list(sessions.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                yield child.name


def read(
    fallback: Callable[[], int] = recover,
) -> Tuple[int, str]:
    """``(next, source)``: the number to hand out, and where it came from.

    ``source`` is ``"file"`` when the counter was read, else a
    ``"recovered: ..."`` string naming what was wrong with it. ``fallback``
    answers the recovered number; a caller that has the daemon's registry in
    hand passes :func:`recover` with its names, so a record whose directory is
    gone is still counted.
    """
    path = file()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return max(0, fallback()), "recovered: no counter file"
    except (OSError, UnicodeError) as exc:
        return max(0, fallback()), f"recovered: unreadable ({exc})"
    try:
        value = json.loads(raw)[NEXT]
    except (ValueError, TypeError, KeyError) as exc:
        return max(0, fallback()), f"recovered: unusable document ({exc})"
    # ``bool`` is an ``int`` here and a negative count is not a count: both are
    # a document this code did not write, so they read as damage rather than
    # as a number to hand out.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return max(0, fallback()), f"recovered: unusable {NEXT} ({value!r})"
    return value, "file"


def commit(number: int) -> int:
    """Record that ``number`` has been handed out; answer the new ``next``.

    Stores ``max(current, number + 1)``, so the counter never moves backwards
    even when something else advanced it first. Callers reach this *after* the
    session directory exists (see the module docstring): a failure to write
    spends nothing, because the next recovery reads the directory.
    """
    current, _ = read(lambda: 0)
    nxt = max(current, number + 1)
    path = file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic.scratch(path) as tmp:
        tmp.write_bytes(
            (json.dumps({NEXT: nxt}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        atomic.replace(tmp, path)
    return nxt


def allocate(taken: Callable[[str], bool] = lambda name: False,
             records: Iterable[str] = ()) -> str:
    """Hand out the next ``sN`` and reserve it, in that order; answer its name.

    The whole read-decide-write is one critical section, because two creators
    reading the counter together and both writing ``N + 1`` would hand out the
    same number twice. Two things hold that:

    * :data:`_lock`, for the threads of this process — the manager runs
      creation on whatever thread called it.
    * ``mkdir(exist_ok=False)``, for other processes. The directory is the
      reservation, and the kernel refuses it to the loser. A process that finds
      the name already there steps to the next number and tries again.

    The directory is made **before** the counter moves, which is the order the
    issue asked for and the one that spends least on a failure: a crash between
    the two leaves a directory holding the name — which the next allocation
    steps over — rather than a counter that skipped a number for a session that
    never existed.

    ``taken`` covers what the filesystem cannot: this process's records, whose
    directory may be absent (a record restored from the durable store is a
    name in use even if nobody has made its directory yet). ``records`` is the
    same registry as names, for the recovery in :func:`read`.
    """
    with _lock:
        number, source = read(lambda: recover(records))
        log_source(source, number)
        while True:
            name = f"s{number}"
            if taken(name):
                number += 1
                continue
            try:
                paths.session_dir(name).mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                number += 1
                continue
            break
        commit(number)
        return name


def log_source(source: str, number: int) -> None:
    """Say where an allocation's number came from, when it did not come from
    the file. A warning, because a counter that keeps being rebuilt is one
    somebody should look at and a silent repair hides that."""
    if source != "file":
        log.warning(
            "session counter %s %s; continuing from s%d",
            file(), source, number,
        )
