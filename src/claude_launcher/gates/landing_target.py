"""Is there a session that would take this worker's landing request?

``improv-worker-v2``'s landing gate used to look at everything except the
one fact that decides whether its request can go anywhere. Five machine
checks passed, the worker chose ``request``, and only then -- at
``integration-request``, as the daemon filed the push -- was the routing
attempted. When it failed, the board had already moved to ``in_review`` and
the failure arrived as a journal line:

    result: NOT queued -- parent `s1524` holds no `merger` role in the
    worker's mesh (`s1524`); mesh leader fallback: no leader in mesh `s1524`

and then as a nudge loop that reported ``outcome=ok`` twenty-five times over
about four hours without sending anything (claunch-od1mx; measured on run
``20261027-192120-21d28c``). The person learned about it from the worker's
prose, after the time had been spent.

The routing rule already existed and already answered the question. It is
:class:`claude_launcher.daemon.landing_route.Router`, the code the push clock
runs, so asking it before the request and then pushing through it cannot
disagree about who can merge. This gate is the asking: it adds no rule, and
its verdict is the Router's own.

Exit code 0 = there is a target, 1 = there is none, 2 = this could not be
told. The three values are the convention ``landed_check`` established for
this workflow's other gates, and they mean what they mean there -- except
that here the middle one has a consequence: it is the branch that turns a
round away from ``request`` and toward a person, so "could not tell" must
never be spelled as "none". That distinction is carried by
:attr:`~claude_launcher.daemon.landing_route.Target.undecidable`, set by the
Router itself where its walk stopped on a fact it could not read -- not by
matching the wording of a reason, which a reworded sentence would silently
reclassify as a definite nobody.

It runs the Router where the Router lives. A worker's routing reads the
daemon's session table, its mesh rosters and its run slots; none of that is
in this process, so the gate asks ``GET /api/cflow2/landing-target`` and the
daemon's handler builds the answer from the same objects the clock pushes
through (see the handler's docstring for why its directory set matches the
trigger path's). The gate therefore holds the standard library plus
:mod:`claude_launcher.daemon_client`, which is stdlib-only by design -- the
same constraint every gate here is under (``uv run --no-sync`` never
populates a worktree's ``.venv``).

The direction comes from the run, not from the caller: ``relation`` is the
start input ``integration-request``'s own triggers branch on
(``stacked`` -> ``parent:landing``, ``independent`` -> ``leader:landing``).
Taking it as an argument would put a second reading of the same value in the
gate's path, where it could differ from the push's. ``--relation`` exists
only for a caller asking about a run it has no scope for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

#: There is somebody to take the request.
TARGET = 0
#: There is nobody -- a definite answer.
NO_TARGET = 1
#: The question could not be answered (no daemon, an unreadable run, a walk
#: that stopped on a fact it could not read). Never folded into NO_TARGET.
CANNOT_TELL = 2

#: The path the daemon serves the answer on.
PATH = "/api/cflow2/landing-target"

#: How long the daemon call may take. A gate sits on the way out of a step,
#: so this is the ceiling on what it may add; the Router walk itself is
#: local reads.
TIMEOUT = 30.0


def _session_from_env() -> str:
    """The session this probe acts as. A v2 probe is given the run's driver
    (``cflow2.runtime._probe_session``), never the daemon's own ambient
    name, so reading the environment is reading the run."""
    return (os.environ.get("CLAUNCH_SESSION") or "").strip()


def ask(cwd: str, scope: str, relation: str = "",
        timeout: float = TIMEOUT) -> Tuple[Optional[Dict[str, Any]], str]:
    """(the daemon's answer, why there is none). A None answer is never an
    empty one: it says the question did not reach the place that answers it,
    which the caller reports as :data:`CANNOT_TELL`.

    The two reasons are kept apart because they lead a reader to different
    places. A daemon that did not answer may be restarting; a daemon that
    answered ``404`` for this path is running a build that does not have it
    -- the workflow came from a newer package than the process serving it --
    and no amount of retrying will change that. Both are ``2`` here; the
    difference is in the sentence, and in what the reader has to do.
    """
    from .. import daemon_client

    client = daemon_client.connect()
    if client is None:
        return None, "the daemon is not reachable"
    query = f"cwd={_quote(cwd)}&scope={_quote(scope)}"
    if relation:
        query += f"&relation={_quote(relation)}"
    try:
        doc = client.get(f"{PATH}?{query}", timeout=timeout)
    except daemon_client.DaemonClientError as exc:
        if getattr(exc, "status", None) == 404:
            return None, (
                f"this daemon has no {PATH} route -- it is running a build older "
                f"than the workflow asking, so the routing cannot be measured "
                f"until a daemon running this code serves it (a person restarts "
                f"it; a session may not)"
            )
        return None, f"the daemon did not answer ({exc})"
    return (doc if isinstance(doc, dict) else None), (
        "" if isinstance(doc, dict) else "the daemon's answer was not an object")


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value or ""), safe="")


def verdict(doc: Optional[Dict[str, Any]]) -> int:
    """The exit code for one answer. ``undecidable`` wins over everything:
    a walk that could not read a roster has not shown that nobody can
    merge, and the workflow acts on the difference."""
    if doc is None:
        return CANNOT_TELL
    if doc.get("undecidable"):
        return CANNOT_TELL
    return TARGET if doc.get("holder") else NO_TARGET


def _render(doc: Optional[Dict[str, Any]], code: int, why: str = "") -> str:
    """One line a reader (and the agent's journal) can act on: the Router's
    own sentences, not a paraphrase of them."""
    if doc is None:
        return f"cannot tell: {why or 'the routing could not be measured'}"
    if code == TARGET:
        h = doc.get("holder") or {}
        where = f"{h.get('cwd')} {h.get('run') or 'main'} ({h.get('version')})"
        note = ""
        if doc.get("passed"):
            note = f" -- passed over: {'; '.join(doc['passed'])}"
        return f"target: {doc.get('owner')!r} at {where}{note}"
    if code == CANNOT_TELL:
        return f"cannot tell: {doc.get('why') or 'the walk could not be read'}"
    return f"no target: {doc.get('why') or 'nobody can merge this request'}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        default=None,
        help="the checkout the question is asked from (default: this "
             "session's own, as the daemon records it; else the working "
             "directory)",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="the run's scope (default: this probe's session -- a v2 probe "
             "is given the run's driver)",
    )
    parser.add_argument(
        "--relation",
        default=None,
        help="stacked | independent. Omitted: the run's own inputs.relation, "
             "which is what the push branches on -- pass this only when "
             "asking about a run you have no scope for",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print the daemon's whole answer (owner, holder, why, passed, "
             "refused, undecidable) instead of the one-line verdict",
    )
    parser.add_argument("--timeout", type=float, default=TIMEOUT,
                        help=f"how long the daemon call may take (default {TIMEOUT:g}s)")
    args = parser.parse_args(argv)

    cwd = args.repo or _own_checkout()
    scope = args.scope or _session_from_env()
    doc, why = ask(cwd, scope, (args.relation or "").strip(), args.timeout)
    code = verdict(doc)
    if args.json:
        print(json.dumps(doc if doc is not None else {"error": why}, ensure_ascii=False))
    else:
        print(_render(doc, code, why), file=sys.stdout if code != CANNOT_TELL else sys.stderr)
    return code


def _own_checkout() -> str:
    """The session's own tree, resolved the way every gate in this package
    resolves it: ``--repo`` never comes here, so a run keyed elsewhere than
    the session stands still asks about the session's tree.

    Anything that goes wrong falls back to the working directory -- the
    answer every caller gave before ``own_checkout`` existed."""
    try:
        from ..cflow import checkout

        return checkout.own_checkout(None)[0]
    except Exception:  # noqa: BLE001 - no package, no daemon, no session
        return os.getcwd()


if __name__ == "__main__":
    raise SystemExit(main())
