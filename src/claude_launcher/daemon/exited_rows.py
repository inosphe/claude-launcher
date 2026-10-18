"""The per-row reads of an exited session's list row, done once (claunch-0gysk).

The session list (``h_sessions_list``) hangs five readings on every row it
does not skip: the context reading and recent tool calls (:mod:`ctxsize`),
the token spend (:mod:`tokenusage`), the checkout the session moved into
(:mod:`work_checkout`) and its recorded PRs (:mod:`session_prs`). Each module
caches its parse against its file's identity, but answering "is the cache
still good" is itself a path lookup, several stats, a glob of the subagent
folder and a run-store construction per row -- on a list where 524 of 574
rows were exited sessions whose transcript no longer grows, that was the
largest share of the list's worker time (py-spy, s430, 2026-10-14).

So an exited row is computed once after the exit, kept here, and handed back
on later polls for as long as the files it was read from are unchanged:

* the transcript (mtime, size) -- context, tool calls, spend, work checkout;
* its ``subagents`` folder (mtime) -- a new subagent file is spend;
* ``prs.json`` and the session's cflow2 state and checkpoint -- the PRs;
* whether the recorded ``work_cwd`` directory still exists.

A change to any of them, a new exit (``exited_at``) or a change to what the
record points at (harness, profile, conversation, directory) reads the row
again. Looking at the files is itself a handful of stats per row, which on
590 exited rows was 3,000 stats a poll (claunch-lcwv3); so a kept row is
handed back without looking for :data:`CHECK_TTL` seconds (spread by
:data:`CHECK_SPREAD`, so rows filled by one poll do not all fall due on the
same later one). A change to what the record points at is seen at once, as
it takes no stat; a PR the wizard records here calls :func:`expire`; the
run state and the transcript are seen within the TTL. A spend reading still
being caught up (``partial``) is not kept.
The tool-call count is time-dependent: the times are kept and counted
against the clock at every poll, as :func:`ctxsize.tool_calls_for_session`
does. The context reading is kept as read off the transcript, and the windows
the configuration declares (``compact_window``, ``model_context_window``) are
hung on it at every poll (:func:`ctxsize.context_of`), so a profile or
provider change reaches an exited row as fast as a live one. ``tps`` (:mod:`metering`) is not here: the list reads it off one shared
snapshot, with no per-row file read, and its ``idle`` answer moves with the
clock.

The card's commit chips (``rail_commits``) are kept apart from the row, in
the session's own directory (``rail-commits.json`` beside ``meta.json``), so
they outlive a daemon restart and the session's worktree (claunch-ork3b).
They are read once per exit and then only brought up to date
(:func:`session_commits.kept_rail`): a record whose commits are all merged
costs no git. A first reading walks the repository, so a poll makes at most
:data:`FILL_LIMIT` of them per :data:`FILL_WINDOW` seconds -- the hundreds of
exited sessions a daemon has before its first poll fill in over a few polls.
"""

from __future__ import annotations

import copy
import json
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .. import atomic, session_commits
from . import ctxsize, paths, session_prs, tokenusage, work_checkout
from .harness import CLAUDE_HARNESS

#: The row keys this module keeps as they were read. ``context`` is kept as
#: its transcript reading and annotated at every poll; ``tool_calls`` is
#: counted at every poll.
FIELDS = (tokenusage.INFO_KEY, work_checkout.FIELD, "prs")

#: More rows than this and the table starts over (records removed from the
#: daemon leave theirs behind).
MAX_ROWS = 4096

#: Seconds a kept row is handed back before its files are looked at again,
#: as the list's branch lookup (``api._BRANCH_TTL``) and the other miss TTLs.
CHECK_TTL = 30.0

#: Each row's wait is ``CHECK_TTL`` stretched by up to this fraction, drawn
#: per check.
CHECK_SPREAD = 0.5

#: First readings of an exited session's commits allowed per
#: :data:`FILL_WINDOW` seconds; past it a row shows no chips until a later
#: poll.
FILL_LIMIT = 16
FILL_WINDOW = 5.0

#: The name of the file a session's kept chips live in, in its directory.
RAIL_FILE = "rail-commits.json"

_now = time.monotonic


@dataclass
class _Row:
    ident: Tuple
    transcript: Optional[Path]
    watch: List[Path]
    work_cwd: Optional[str]
    stamps: Tuple
    tool_times: Optional[List[float]]
    reading: Optional[dict]
    fields: dict
    due: float = 0.0


_rows: dict = {}
_lock = threading.Lock()
#: name -> kept commit record (or None), read off disk once.
_rails: dict = {}
_fills: List[float] = []


def forget() -> None:
    """Drop every kept row. For tests."""
    with _lock:
        _rows.clear()
        _rails.clear()
        _fills.clear()


def _due() -> float:
    return _now() + CHECK_TTL * (1.0 + CHECK_SPREAD * random.random())


def expire(name: str) -> None:
    """Look at ``name``'s files on its next poll, whatever its wait.

    For a writer in this process that changed one of them (the PR wizard's
    ``prs.json``) and should not wait :data:`CHECK_TTL` to be seen.
    """
    with _lock:
        row = _rows.get(name)
        if row is not None:
            row.due = 0.0


def _ident(session: Any) -> Tuple:
    sdef = getattr(session, "sdef", None)
    return (
        getattr(session, "exited_at", None),
        str(getattr(sdef, "harness", None) or CLAUDE_HARNESS),
        str(getattr(sdef, "profile", "") or ""),
        str(getattr(sdef, "conversation_id", "") or ""),
        str(getattr(sdef, "cwd", "") or ""),
    )


def _stat(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _stamps(transcript: Optional[Path], watch: List[Path],
            work_cwd: Optional[str]) -> Tuple:
    return (
        _stat(transcript) if transcript is not None else None,
        tuple(_stat(p) for p in watch),
        os.path.isdir(work_cwd) if work_cwd else None,
    )


def _fresh(row: _Row, sdef: Any) -> bool:
    if row.transcript is None:
        # No transcript when the row was read: one appearing since is a
        # change. The lookup's miss is cached (``ctxsize.MISS_TTL``), so
        # asking again costs what the uncached row asked.
        if ctxsize.transcript_of(sdef) is not None:
            return False
    return _stamps(row.transcript, row.watch, row.work_cwd) == row.stamps


def _take_fill() -> bool:
    now = _now()
    with _lock:
        _fills[:] = [t for t in _fills if now - t < FILL_WINDOW]
        if len(_fills) >= FILL_LIMIT:
            return False
        _fills.append(now)
        return True


def _rail_path(name: str) -> Path:
    return paths.session_dir(name) / RAIL_FILE


def _load_rail(name: str) -> Optional[dict]:
    try:
        record = json.loads(_rail_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _save_rail(name: str, record: dict) -> None:
    path = _rail_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(path) as tmp:
            tmp.write_text(json.dumps(record), encoding="utf-8")
            atomic.replace(tmp, path)
    except OSError:
        pass


def _attach_rail(session: Any, info: dict, cwd: str) -> None:
    """Hang the kept commit chips on ``info``: the commits come off the
    worktree the session moved into, else ``cwd``, as the live path reads
    them; the PR frames off the row's ``prs``."""
    name = str(info.get("name") or "")
    if not name:
        return
    with _lock:
        known = name in _rails
        record = _rails.get(name)
    if not known:
        record = _load_rail(name)
    created_at = getattr(session, "created_at", None)
    exited_at = getattr(session, "exited_at", None)
    current = session_commits.kept_current(record, name, created_at, exited_at)
    record, changed = session_commits.kept_rail(
        record, name, created_at=created_at, exited_at=exited_at,
        cwds=[info.get(work_checkout.FIELD), cwd], prs=info.get("prs"),
        read=current or _take_fill(),
    )
    if changed:
        _save_rail(name, record)
    with _lock:
        if len(_rails) >= MAX_ROWS:
            _rails.clear()
        _rails[name] = record
    chips = session_commits.kept_view(record)
    if chips:
        info["rail_commits"] = chips


def _answer(session: Any, row: _Row, cwd: str) -> dict:
    info = session.info()
    calls = ctxsize.count_tool_calls(row.tool_times)
    if calls is not None:
        info["tool_calls"] = calls
    # The windows on ``context`` come from the configuration, not the
    # transcript: hung on again here, as the live path does on every poll.
    context = ctxsize.context_of(
        getattr(session, "sdef", None), copy.deepcopy(row.reading))
    if context:
        info["context"] = context
    # Copied: the kept row outlives this response and its readers.
    info.update(copy.deepcopy(row.fields))
    _attach_rail(session, info, cwd)
    return info


def attach(session: Any, spend: tokenusage.Budget, cwd: str) -> dict:
    """``session.info()`` with the per-row readings of an exited session.

    The same keys the live path attaches (``context``, ``tool_calls``,
    ``token_usage``, ``work_cwd``, ``prs``), read once and kept while their
    files are unchanged, and the card's ``rail_commits`` kept on disk. ``spend`` is the request's read budget, ``cwd`` the
    directory whose cflow2 run holds the session's PRs.
    """
    sdef = getattr(session, "sdef", None)
    name = str(getattr(sdef, "name", "") or "")
    ident = _ident(session)
    with _lock:
        row = _rows.get(name)
    if row is not None and row.ident == ident:
        due = row.due
        if _now() < due:
            return _answer(session, row, cwd)
        if _fresh(row, sdef):
            with _lock:
                # Unless :func:`expire` ran while the files were looked at.
                if row.due == due:
                    row.due = _due()
            return _answer(session, row, cwd)
    # Stamped before reading: a write that lands during the read is a change
    # the next poll sees.
    transcript = ctxsize.transcript_of(sdef)
    watch: List[Path] = []
    if transcript is not None and ident[1] == CLAUDE_HARNESS:
        watch.append(transcript.parent / transcript.stem / "subagents")
    watch.extend(session_prs.sources(cwd, name))
    before = _stamps(transcript, watch, None)
    info = ctxsize.attach(session)
    tool_times = ctxsize.tool_times_for_session(sdef)
    reading = ctxsize.for_session(sdef)
    tokenusage.attach(info, sdef, spend)
    work_checkout.attach(info, sdef)
    info["prs"] = session_prs.for_session(cwd, name)
    _attach_rail(session, info, cwd)
    if (info.get(tokenusage.INFO_KEY) or {}).get("partial"):
        return info
    work_cwd = info.get(work_checkout.FIELD)
    stamps = before[:2] + (os.path.isdir(work_cwd) if work_cwd else None,)
    kept = _Row(
        ident=ident, transcript=transcript, watch=watch, work_cwd=work_cwd,
        stamps=stamps, tool_times=list(tool_times) if tool_times is not None else None,
        reading=copy.deepcopy(reading),
        fields=copy.deepcopy({k: info[k] for k in FIELDS if k in info}),
        due=_due(),
    )
    with _lock:
        if len(_rows) >= MAX_ROWS:
            _rows.clear()
        _rows[name] = kept
    return info
