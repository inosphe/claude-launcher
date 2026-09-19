"""Discover the session file pi creates when a managed session runs ``/new``.

A pi session is pinned at launch: claunch names the file itself
(``--session <home>/sessions/<encoded cwd>/<id>.jsonl``, see
:func:`harness.pi_session_file`), so the first conversation needs no
discovery. ``/new`` breaks that pin. pi's ``SessionManager.newSession`` mints
its own id and its own file name -- ``<ISO timestamp with :. as ->_<id>.jsonl``
in the same directory -- and the definition keeps pointing at the file the
session left behind. A restore then reopens the old conversation and the
transcript page reads it (pi-coding-agent 0.73.1, ``core/session-manager.js``).

Two facts about that file shape what this module does:

* It is written late. ``_persist`` writes nothing until the first assistant
  message arrives, so the file appears when the session first answers after
  ``/new`` -- seconds later, or an hour later. A short wait cannot find it; the
  caller keeps the claim pending and retries (see
  :meth:`SessionManager._recover_claims`).
* Its header timestamp is the moment of ``/new``. ``newSession`` builds the
  ``session`` header in memory at that instant and writes it verbatim later,
  so the header dates the command, not the first answer. That is what tells
  two pi sessions in one directory apart when both run ``/new``: each claims
  the file whose header falls within seconds of *its* command.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Set

#: How far a candidate's header timestamp may sit from the ``/new`` that
#: is being claimed for. The header is stamped in-process at the command, so
#: the real gap is milliseconds; the slack covers clock granularity and the
#: keystroke reaching pi after the daemon saw it.
HEADER_WINDOW = 15.0


def _header(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            record = json.loads(stream.readline())
    except (OSError, ValueError):
        # pi may still be writing the first line; the caller retries.
        return None
    if not isinstance(record, dict) or record.get("type") != "session":
        return None
    return record


def _header_epoch(record: dict) -> Optional[float]:
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def snapshot(session_dir: Path) -> Set[str]:
    """Every session file stem already in ``session_dir``.

    Taken before ``/new`` reaches pi, so the file it later writes is the one
    not in this set. Stems, not paths: the stem is what the definition pins
    (:func:`harness.pi_session_file` puts ``.jsonl`` back on).
    """
    try:
        return {p.stem for p in session_dir.iterdir() if p.suffix == ".jsonl"}
    except OSError:
        return set()


def claim_new(
    session_dir: Path,
    cwd: str,
    known: Iterable[str],
    *,
    since: float,
    timeout: float = 0.0,
    poll: float = 0.05,
    window: float = HEADER_WINDOW,
) -> Optional[str]:
    """The stem of the session file ``/new`` created, or ``None`` (not yet).

    A candidate is a ``.jsonl`` not in ``known`` whose header is a pi
    ``session`` record for ``cwd`` stamped within ``window`` seconds of
    ``since`` (epoch seconds, the moment the daemon saw the command). When
    exactly one candidate qualifies it is the answer; more than one is a tie
    the daemon does not break, and none means pi has not written it yet.
    """
    target = os.path.normcase(os.path.abspath(cwd))
    claimed = {str(k) for k in known}
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        matches = []
        try:
            entries = list(session_dir.iterdir())
        except OSError:
            entries = []
        for path in entries:
            if path.suffix != ".jsonl" or path.stem in claimed:
                continue
            record = _header(path)
            if record is None:
                continue
            record_cwd = os.path.normcase(os.path.abspath(str(record.get("cwd") or "")))
            if record_cwd != target:
                continue
            stamped = _header_epoch(record)
            if stamped is None or abs(stamped - since) > window:
                continue
            matches.append(path.stem)
        if len(matches) == 1:
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
