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
again. A spend reading still being caught up (``partial``) is not kept.
The tool-call count is time-dependent: the times are kept and counted
against the clock at every poll, as :func:`ctxsize.tool_calls_for_session`
does. The context reading is kept as read off the transcript, and the windows
the configuration declares (``compact_window``, ``model_context_window``) are
hung on it at every poll (:func:`ctxsize.context_of`), so a profile or
provider change reaches an exited row as fast as a live one. ``tps`` (:mod:`metering`) is not here: the list reads it off one shared
snapshot, with no per-row file read, and its ``idle`` answer moves with the
clock.
"""

from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import ctxsize, session_prs, tokenusage, work_checkout
from .harness import CLAUDE_HARNESS

#: The row keys this module keeps as they were read. ``context`` is kept as
#: its transcript reading and annotated at every poll; ``tool_calls`` is
#: counted at every poll.
FIELDS = (tokenusage.INFO_KEY, work_checkout.FIELD, "prs")

#: More rows than this and the table starts over (records removed from the
#: daemon leave theirs behind).
MAX_ROWS = 4096


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


_rows: dict = {}
_lock = threading.Lock()


def forget() -> None:
    """Drop every kept row. For tests."""
    with _lock:
        _rows.clear()


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


def _answer(session: Any, row: _Row) -> dict:
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
    return info


def attach(session: Any, spend: tokenusage.Budget, cwd: str) -> dict:
    """``session.info()`` with the per-row readings of an exited session.

    The same keys the live path attaches (``context``, ``tool_calls``,
    ``token_usage``, ``work_cwd``, ``prs``), read once and kept while their
    files are unchanged. ``spend`` is the request's read budget, ``cwd`` the
    directory whose cflow2 run holds the session's PRs.
    """
    sdef = getattr(session, "sdef", None)
    name = str(getattr(sdef, "name", "") or "")
    ident = _ident(session)
    with _lock:
        row = _rows.get(name)
    if row is not None and row.ident == ident and _fresh(row, sdef):
        return _answer(session, row)
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
    if (info.get(tokenusage.INFO_KEY) or {}).get("partial"):
        return info
    work_cwd = info.get(work_checkout.FIELD)
    stamps = before[:2] + (os.path.isdir(work_cwd) if work_cwd else None,)
    kept = _Row(
        ident=ident, transcript=transcript, watch=watch, work_cwd=work_cwd,
        stamps=stamps, tool_times=list(tool_times) if tool_times is not None else None,
        reading=copy.deepcopy(reading),
        fields=copy.deepcopy({k: info[k] for k in FIELDS if k in info}),
    )
    with _lock:
        if len(_rows) >= MAX_ROWS:
            _rows.clear()
        _rows[name] = kept
    return info
