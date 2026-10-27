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
:meth:`SessionManager._reserve`):

* **A name is only proposed until the request cannot be refused.** Computing
  the next name touches nothing (:func:`next_name`); reserving it
  (:func:`claim`) makes the directory and moves the counter. The two are split
  because a caller must name a *worktree* before the rest of its request is
  validated, and a name spent on a request that validation then refuses leaves
  an empty directory — the exact litter this module exists to stop. The price
  of the split is that a candidate can lose a race and the worktree ends up
  named for a number the session did not get; that is a misnamed directory, not
  a broken naming invariant.
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
    current, source = read(lambda: 0)
    log_source(source, number)
    nxt = max(current, number + 1)
    path = file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic.scratch(path) as tmp:
        tmp.write_bytes(
            (json.dumps({NEXT: nxt}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        atomic.replace(tmp, path)
    return nxt


def next_name(
    taken: Callable[[str], bool] = lambda name: False,
    records: Iterable[str] = (),
) -> str:
    """The name the next allocation would take, **without taking it**.

    No directory is made and the counter does not move. Callers use this where
    they must name something after the session before knowing the request will
    be accepted — the worktree cut for it is the reason this exists — so that a
    request that is then refused costs nothing.

    What it returns is a *candidate*: another creator can take it before the
    caller reserves it. :func:`claim` is what refuses such a name, so a caller
    must treat this as a proposal and not as a reservation.
    """
    number, _ = read(lambda: recover(records))
    # No warning here: this is a peek, and a caller that is refused spends
    # nothing. :func:`commit` reports recovery, because that is where a number
    # is actually handed out of a rebuilt value.
    while taken(f"s{number}") or paths.session_dir(f"s{number}").is_dir():
        number += 1
    return f"s{number}"


def claim(
    name: str,
    taken: Callable[[str], bool] = lambda name: False,
) -> bool:
    """Reserve ``name``: make its directory and move the counter past it.

    ``False`` means the name is not available — either this process's registry
    holds it (``taken``) or its directory already exists, which is how a name
    taken a moment ago by a request in another process shows up. A caller then
    asks :func:`next_name` again.

    The directory is created **before** the counter moves: the directory is the
    reservation (``mkdir`` refusing is what refuses the loser of a race), and a
    crash between the two leaves a directory holding the name — which the next
    scan steps over — rather than a counter that skipped a number for a session
    that never existed.
    """
    match = NUMBER_RE.match(name or "")
    if not match or taken(name):
        return False
    try:
        with _lock:
            paths.session_dir(name).mkdir(parents=True, exist_ok=False)
            commit(int(match.group(1)))
    except FileExistsError:
        return False
    return True


def allocate(
    taken: Callable[[str], bool] = lambda name: False,
    records: Iterable[str] = (),
) -> str:
    """Hand out the next ``sN`` and reserve it; answer its name.

    :func:`next_name` plus :func:`claim`, retried when the candidate turns out
    to be taken. Callers that can still refuse the request afterwards should
    not use this — see :func:`next_name` for why.
    """
    while True:
        name = next_name(taken, records)
        if claim(name, taken):
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
