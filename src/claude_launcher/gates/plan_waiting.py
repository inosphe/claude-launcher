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

Two gates read it, and they wait on different things (claunch-jw1v2).
``--scope settle`` (the default, and what the probe always answered) counts
everything the user owes the plan -- the settle closure gate waits on that.
``--scope start`` counts only the unchosen decisions
(``summary.waiting_decisions``): the start gates in front of ``work`` wait
on the branch the user still has to pick, and a verify item left from the
round before -- a check of work that already landed -- is not a reason to
hold the next round's start. A row the store wrote before that count
existed is answered from its ``blockers`` (the decision rows on the user).

A stop is told to the person once (``--notify``, claunch-jw1v2 C). A gate
that waits on the user and says so only in the run journal is a gate nobody
reads: the person who owes the plan a choice is looking at tens of sessions.
On the first WAITING poll for a given set of items the probe posts one
message, as the session, into a thread of its own ("plan 게이트 대기") -- a
thread message is a dashboard notice. The same set waiting on the next poll
posts nothing; a CLEAR forgets it, so the next stop is told again. What was
told is kept in the session's scratch directory, never in the plan database,
and the notice is best effort: a daemon that cannot take it changes nothing
about the verdict.

It reads the daemon's plan database directly (``daemon_dir()/plan.sqlite3``)
in read-only mode. The plan HTTP API is a surface the session writes through;
a probe that asked it would need the daemon to be up and would sit on a
route that exists only in a build new enough to serve it. The file is the
same one the API writes, and opening it read-only means a probe can never
change what the user is looking at.

Rounds (claunch-r3j1o.1). A plan opened by ``new_round`` carries the run and
round it belongs to. When the probe is given the run's own (``CFLOW2_RUN``,
``CFLOW2_ROUND``; ``--run``/``--round``) and the plan names another, the plan
is the previous round's -- the round trigger has not opened this one yet --
and its emptiness says nothing about this round: the verdict is 2, not 0. A
plan without a run (written before rounds, or outside a run) and a probe
without one are read as before.
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

#: What a gate waits on: ``start`` the unchosen decisions only, ``settle``
#: everything on the user (decisions, unchecked verify items, undisposed
#: followups).
SCOPES = ("start", "settle")


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


def read_summary(path: str, session: str, *, run: str = "",
                 round: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """(the stored summary, why there is none). Read-only: a probe may run
    every ``poll`` seconds next to the daemon's own writers, and it must
    never be the one that changes the row. With ``run`` (and ``round``), a
    plan that names another run or round has no summary for this one."""
    try:
        uri = "file:{}?mode=ro".format(path.replace("?", "%3f").replace("#", "%23"))
        db = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return None, f"cannot open {path} ({exc})"
    try:
        row = db.execute(
            "SELECT summary, json_extract(doc, '$.run'), json_extract(doc, '$.round') "
            "FROM plans WHERE session = ?", (session,)).fetchone()
    except sqlite3.Error as exc:
        return None, f"cannot read the plans table ({exc})"
    finally:
        db.close()
    if row is None:
        return None, f"session {session!r} has no plan"
    plan_run, plan_round = row[1], row[2]
    if run and plan_run:
        if plan_run != run or (round is not None and plan_round != round):
            return None, (f"the plan of {session!r} is round {plan_run}/{plan_round}, not "
                          f"{run}/{round if round is not None else '?'}: this round's plan is not open yet")
    try:
        summary = json.loads(row[0] or "{}")
    except (TypeError, ValueError) as exc:
        return None, f"the stored summary is not JSON ({exc})"
    if not isinstance(summary, dict) or "waiting" not in summary:
        return None, "the stored summary has no waiting count"
    return summary, ""


def _on_user(summary: Dict[str, Any], scope: str) -> List[Dict[str, Any]]:
    """The blocker rows on the user that ``scope`` waits on."""
    rows = [r for r in summary.get("blockers") or [] if isinstance(r, dict) and r.get("who") == "user"]
    if scope == "start":
        rows = [r for r in rows if r.get("kind") == "decision"]
    return rows


def waiting_count(summary: Optional[Dict[str, Any]], scope: str = "settle") -> Optional[int]:
    """How many things ``scope`` waits on, or None when the summary cannot
    say. ``start`` reads ``waiting_decisions``; a row written before that
    count existed is counted from its decision blockers, and one with no
    blockers list either cannot tell (it has not shown the decisions are
    taken)."""
    if not isinstance(summary, dict) or "waiting" not in summary:
        return None
    key = "waiting_decisions" if scope == "start" else "waiting"
    if key in summary:
        try:
            return int(summary[key])
        except (TypeError, ValueError):
            return None
    if isinstance(summary.get("blockers"), list):
        return len(_on_user(summary, scope))
    return None


def verdict(summary: Optional[Dict[str, Any]], scope: str = "settle") -> int:
    """The exit code for one summary. An unreadable one is ``CANNOT_TELL``:
    it has not shown that the plan is clear."""
    if summary is None:
        return CANNOT_TELL
    count = waiting_count(summary, scope)
    if count is None:
        return CANNOT_TELL
    return WAITING if count > 0 else CLEAR


def _what_waits(summary: Dict[str, Any], scope: str = "settle") -> List[str]:
    """The titles the summary says ``scope`` waits on. The store keeps them
    in ``blockers`` with ``who: user``; a summary without them still counts."""
    out = []
    for row in _on_user(summary, scope):
        title = row.get("title") or row.get("id") or ""
        if title:
            out.append(f"{row['id']} {title}" if row.get("id") and row.get("id") != title else str(title))
    return out


def _render(summary: Optional[Dict[str, Any]], code: int, why: str = "", scope: str = "settle") -> str:
    """One line a reader (and the journal) can act on."""
    if summary is None:
        return f"cannot tell: {why or 'the plan could not be read'}"
    if code == CANNOT_TELL:
        return "cannot tell: the stored summary has no usable waiting count"
    if code == CLEAR:
        if scope == "start":
            rest = summary.get("waiting") or 0
            tail = f" ({rest} closure item(s) wait for the settle gate)" if rest else ""
            return f"plan clear: no decision waits on the user{tail}"
        return "plan clear: nothing waits on the user"
    titles = _what_waits(summary, scope)
    tail = f": {'; '.join(titles)}" if titles else ""
    what = "decision(s) waiting" if scope == "start" else "plan waiting"
    return f"{what}: {waiting_count(summary, scope)}{tail}"


NOTICE_TITLE = "plan 게이트 대기"
_NOTICE_FILE = "plan-gate-notice.json"


def _notice_state_path(session: str):
    from ..daemon import paths

    return paths.session_scratch_dir(session) / _NOTICE_FILE


def notice_key(summary: Dict[str, Any], scope: str) -> str:
    """What one stop is: the gate's scope and the ids it waits on."""
    ids = sorted(str(r.get("id") or r.get("title") or "") for r in _on_user(summary, scope))
    return scope + ":" + ",".join(ids)


def notice_text(summary: Dict[str, Any], scope: str) -> str:
    """The message a person reads: why the run stopped and on what."""
    rows = _on_user(summary, scope)
    gate = ("작업 시작 게이트(고르지 않은 결정만 센다)" if scope == "start"
            else "정산 게이트(결정·verify·followup을 모두 센다)")
    lines = [f"이 세션의 cflow 런이 {gate}에서 멈췄습니다. 계획 패널에서 아래 항목을 처리하면 이어 갑니다.", ""]
    why = {"decision not taken": "결정 대기", "not checked yet": "확인 대기", "not disposed of": "처리 대기"}
    for r in rows:
        lines.append(f"- `{r.get('id', '')}` {r.get('title', '')} — {why.get(r.get('why'), r.get('why', ''))}")
    return "\n".join(lines)


def notify_stall(session: str, summary: Optional[Dict[str, Any]], code: int, scope: str,
                 *, client=None, state_path=None) -> str:
    """Tell the person about a stop once per distinct set of items; forget
    it on CLEAR. Returns what happened ("posted", "same", "cleared", "skip",
    or "failed: <why>") -- for the tests and the journal, never the verdict."""
    path = state_path if state_path is not None else _notice_state_path(session)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    if code == CLEAR:
        if state.get(scope):
            state.pop(scope, None)
            _write_state(path, state)
            return "cleared"
        return "skip"
    if code != WAITING or summary is None:
        return "skip"
    key = notice_key(summary, scope)
    if (state.get(scope) or {}).get("key") == key:
        return "same"
    try:
        if client is None:
            from .. import daemon_client

            client = daemon_client.connect()
        if client is None:
            return "failed: no daemon"
        text = notice_text(summary, scope)
        tid = state.get("thread")
        res = None
        if tid:
            try:
                res = client.post(f"/api/threads/{tid}/messages", {"session": session, "text": text})
            except Exception:  # the thread may be gone; open a new one below
                res = None
        if not res or res.get("error"):
            res = client.post("/api/threads", {"session": session, "title": NOTICE_TITLE, "text": text})
            tid = ((res or {}).get("thread") or {}).get("id") or tid
    except Exception as exc:  # best effort: the verdict does not depend on it
        return f"failed: {exc}"
    state["thread"] = tid
    state[scope] = {"key": key}
    _write_state(path, state)
    return "posted"


def _write_state(path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--session",
        default=None,
        help="the session whose plan is read (default: this probe's session "
             "-- a v2 probe is given the run's driver)",
    )
    parser.add_argument(
        "--scope", choices=SCOPES, default="settle",
        help="what the gate waits on: 'start' only the unchosen decisions "
             "(the gates in front of work), 'settle' everything on the user "
             "(default)",
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="tell the person once, in a thread of this session, when the "
             "gate stops on a new set of items (best effort)",
    )
    parser.add_argument(
        "--run", default=None,
        help="the run the plan must belong to (default: $CFLOW2_RUN)")
    parser.add_argument(
        "--round", type=int, default=None,
        help="the round the plan must belong to (default: $CFLOW2_ROUND)")
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

    # Two independent axes: which round the plan must belong to (a plan of
    # another round answers nothing about this one -> 2) and what the gate
    # waits on (--scope). A plan of the right round is judged by the scope.
    run = (args.run if args.run is not None else os.environ.get("CFLOW2_RUN") or "").strip()
    rnd = args.round
    if rnd is None:
        try:
            rnd = int(os.environ.get("CFLOW2_ROUND") or "")
        except ValueError:
            rnd = None
    summary, why = read_summary(path, session, run=run, round=rnd)
    code = verdict(summary, args.scope)
    if args.notify:
        notify_stall(session, summary, code, args.scope)
    if args.json:
        print(json.dumps(summary if summary is not None else {"error": why},
                         ensure_ascii=False))
    else:
        print(_render(summary, code, why, args.scope),
              file=sys.stdout if code != CANNOT_TELL else sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised through cli_gate
    raise SystemExit(main())
