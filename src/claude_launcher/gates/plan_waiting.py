"""Is this session's plan still waiting on the user?

``improv-worker-v2`` decides when to stop for the user in two places that
used to be one: the coding-approval gate (does the run ask at all) and the
plan panel (does the user still owe the session a decision). The workflow
stopped reading the second one, so a session whose plan still held an
unchosen decision went straight into the coding-approval gate and then into
``work`` -- the person read the same plan twice, in two surfaces, and the
gate asked about a plan the panel was still waiting on (claunch-rzepk).

This is the answer that puts the two in order: it reads the plan summary the
store already computes (``plan.summary.waiting``, "what the user can act on
now": an unchosen decision, an unchecked verify, an undisposed followup) and
turns it into the three-valued verdict a ``wait`` probe consumes:

    0  nothing is waiting -- go on to the gate
    1  the user still owes this plan a decision -- keep waiting
    2  the question could not be answered (no daemon state, no plan for the
       session, an unreadable row)

The middle value is the point of the gate. ``wait`` polls it every ``poll``
seconds and only 0 lets the run through, so 1 is "not yet" and nothing else.
That is also why an unreadable plan is 2 and not 0: a session that cannot
show its plan has not shown that the user owes it nothing, and the workflow's
confirmed rule reads an unknown value as "stop" (docs/plan-coding-approval.md
규칙 1). Three undecidable polls end the wait as ``undecidable`` and send the
round to a person instead of guessing.

It reads the daemon's plan database directly (``daemon_dir()/plan.sqlite3``)
in read-only mode. The plan HTTP API is a surface the session writes through;
a probe that asked it would need the daemon to be up and would sit on a
route that exists only in a build new enough to serve it. The file is the
same one the API writes, and opening it read-only means a probe can never
change what the user is looking at.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

#: Nothing waits on the user -- the plan is clear.
CLEAR = 0
#: The user still owes this plan something (an unchosen decision, an
#: unchecked verify, an undisposed followup). The wait keeps polling.
WAITING = 1
#: The question could not be answered. Never folded into CLEAR.
CANNOT_TELL = 2

#: Where the daemon keeps the plan document and its computed summary.
DB_NAME = "plan.sqlite3"


def _session_from_env() -> str:
    """The session this probe acts as. A v2 probe is given the run's driver
    (``cflow2.runtime._probe_session``), never the daemon's own ambient name,
    so reading the environment is reading the run."""
    return (os.environ.get("CLAUNCH_SESSION") or "").strip()


def db_path() -> Optional[str]:
    """The plan database, or None when this launcher home has none yet."""
    from ..daemon import paths

    path = paths.daemon_dir() / DB_NAME
    return str(path) if path.exists() else None


def read_summary(path: str, session: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """(the stored summary, why there is none). Read-only: a probe may run
    every ``poll`` seconds next to the daemon's own writers, and it must
    never be the one that changes the row."""
    try:
        uri = "file:{}?mode=ro".format(path.replace("?", "%3f").replace("#", "%23"))
        db = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return None, f"cannot open {path} ({exc})"
    try:
        row = db.execute(
            "SELECT summary FROM plans WHERE session = ?", (session,)).fetchone()
    except sqlite3.Error as exc:
        return None, f"cannot read the plans table ({exc})"
    finally:
        db.close()
    if row is None:
        return None, f"session {session!r} has no plan"
    try:
        summary = json.loads(row[0] or "{}")
    except (TypeError, ValueError) as exc:
        return None, f"the stored summary is not JSON ({exc})"
    if not isinstance(summary, dict) or "waiting" not in summary:
        return None, "the stored summary has no waiting count"
    return summary, ""


def verdict(summary: Optional[Dict[str, Any]]) -> int:
    """The exit code for one summary. An unreadable one is ``CANNOT_TELL``:
    it has not shown that the plan is clear."""
    if summary is None:
        return CANNOT_TELL
    if not isinstance(summary, dict) or "waiting" not in summary:
        return CANNOT_TELL
    try:
        waiting = int(summary["waiting"])
    except (TypeError, ValueError):
        return CANNOT_TELL
    return WAITING if waiting > 0 else CLEAR


def _what_waits(summary: Dict[str, Any]) -> List[str]:
    """The titles the summary says are on the user. The store keeps them in
    ``blockers`` with ``who: user``; a summary without them still counts."""
    out = []
    for row in summary.get("blockers") or []:
        if isinstance(row, dict) and row.get("who") == "user":
            title = row.get("title") or row.get("id") or ""
            if title:
                out.append(str(title))
    return out


def _render(summary: Optional[Dict[str, Any]], code: int, why: str = "") -> str:
    """One line a reader (and the journal) can act on."""
    if summary is None:
        return f"cannot tell: {why or 'the plan could not be read'}"
    waiting = summary.get("waiting")
    if code == CLEAR:
        return "plan clear: nothing waits on the user"
    titles = _what_waits(summary)
    tail = f": {'; '.join(titles)}" if titles else ""
    return f"plan waiting: {waiting}{tail}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--session",
        default=None,
        help="the session whose plan is read (default: this probe's session "
             "-- a v2 probe is given the run's driver)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print the stored summary instead of the one-line verdict",
    )
    args = parser.parse_args(argv)

    session = (args.session or _session_from_env()).strip()
    if not session:
        print("cannot tell: no session -- CLAUNCH_SESSION is unset and "
              "--session was not given", file=sys.stderr)
        return CANNOT_TELL

    path = db_path()
    if path is None:
        print(f"cannot tell: no plan database under this launcher home "
              f"({DB_NAME})", file=sys.stderr)
        return CANNOT_TELL

    summary, why = read_summary(path, session)
    code = verdict(summary)
    if args.json:
        print(json.dumps(summary if summary is not None else {"error": why},
                         ensure_ascii=False))
    else:
        print(_render(summary, code, why),
              file=sys.stdout if code != CANNOT_TELL else sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised through cli_gate
    raise SystemExit(main())
