"""Is this session's keep-alive flag set?

The worker workflow's ``end-hold`` step exists because an overseer refused
the ending. Refusing is not what keeps the session: the daemon reaps a
finished one-shot run's session on sight of ``done``, and the only thing
that stops it is ``session.sdef.keep_alive``
(``daemon/cflow_clock.py``: ``if session.sdef.keep_alive: ... return``).
So the step tells the agent to run ``claunch keep-alive $CLAUNCH_SESSION``,
and until this script existed nothing checked that it did — the run reached
``done`` either way and the refusal changed nothing. A gate whose refusal
path is prose is the shape of defect the checklist work removed from this
same file; this closes it with an exit code instead.

Reading the flag needs a *read* path, and the CLI has none: ``claunch
keep-alive <session>`` with no ``off`` SETS it (``cli_sessions.py`` posts
unconditionally), so probing with the CLI would make the answer true. The
daemon's session record carries it, though: ``harness.py`` puts
``keep_alive`` into ``sdef.to_dict()``, ``session.info()`` spreads that, and
``GET /api/sessions/{name}`` returns the whole thing — a plain flat dict,
and a read that leaves the value alone.

    uv run --no-sync python tools/keepalive_check.py            # $CLAUNCH_SESSION
    uv run --no-sync python tools/keepalive_check.py <session>

Exit codes, in the shape the other gate tools use:

    0  the flag is set — the session survives the run's close
    1  the flag is NOT set — the refusal has no effect yet; run the command
    2  cannot tell (no session name, daemon unreachable, no such session)

Two and one are kept apart on purpose. Both refuse to advance, but "I looked
and it is false" and "I could not look" are fixed by different actions, and
a tool that collapses them sends the reader to the wrong one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Same as the other gate tools: run from a checkout, against that checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher import daemon_client  # noqa: E402


def main(argv: list[str]) -> int:
    name = argv[1] if len(argv) > 1 else os.environ.get("CLAUNCH_SESSION", "")
    if not name:
        print(
            "cannot tell: no session named. Pass one, or run this where "
            "CLAUNCH_SESSION is set (inside the session itself)."
        )
        return 2

    # connect(), not ensure_running(): a check must not start a daemon. If
    # nothing is running there is no session to be kept alive either, and
    # starting one here would answer a question nobody asked.
    client = daemon_client.connect()
    if client is None:
        print(
            "cannot tell: the daemon is not reachable, so the flag cannot be "
            "read. Start it (`claunch sessions`) and run this again."
        )
        return 2

    try:
        info = client.get(f"/api/sessions/{name}")
    except daemon_client.DaemonClientError as exc:
        print(f"cannot tell: {exc}")
        return 2

    if not isinstance(info, dict) or "keep_alive" not in info:
        # The field is part of the session record, so its absence means the
        # record is not the shape this tool was written against — say that
        # rather than reading a missing key as false.
        print(
            f"cannot tell: the record for {name!r} carries no 'keep_alive' "
            f"field (keys: {sorted(info) if isinstance(info, dict) else type(info).__name__})"
        )
        return 2

    if info.get("keep_alive"):
        print(f"keep-alive: SET for {name} -- the session survives this run's close")
        return 0

    print(
        f"keep-alive: NOT set for {name}. The ending was refused, but nothing "
        f"is stopping it: when this run reaches 'done' the daemon ends the "
        f"session anyway. Run `claunch keep-alive {name}` and try again."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
