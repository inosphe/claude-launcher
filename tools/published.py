"""Has a run published a milestone this step has not consumed? — the
gate-shaped twin of ``claunch cflow published``.

A step's ``awaits: {sub: <name>, at: <milestone>}`` (a main run waiting on a
sub run) or ``awaits: {main: <milestone>}`` (a sub run waiting on its main
run) spells ``claunch cflow published ...``, and this repository's gate rule
says a probe reaches this checkout through a ``tools/`` script under ``uv run
--no-sync`` — never a bare ``claunch`` off PATH, which is an installed copy
and not this tree (``tests/test_gates_run_this_checkout.py``). The project
layer therefore writes the same await with ``probe:`` naming this file; the
milestone still decides what leaving the step consumes.

It reads the run state directly rather than importing the engine, for the
reason ``tools/sub_done.py`` gives: a gate must hold on the standard library
alone. The facts are two JSON fields — the source run's
``milestones.<name>.count`` and the asking run's ``consumed.<step>`` — in the
slot layout of ``cflow/state.py`` (``runs/<session>/state.json`` for the main
run, ``runs/<session>/sub/<name>/state.json`` for a sub run).
``tests/test_cflow_milestones.py`` pins that this script and
``engine.published`` answer alike.

    uv run --no-sync python tools/published.py stack cut --step stack-merge
    uv run --no-sync python tools/published.py main cut-wanted --step cut

The asking run is ``--run``, else ``$CLAUNCH_CFLOW_RUN`` (the daemon sets it
when the probe is a sub run's), else the main run. Exit codes, the shape
``claunch cflow published`` uses:

    0  the source published the milestone since the step last consumed it
    1  nothing new
    2  cannot tell: the source run does not stand, no asking run, no session
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Same as the other gate tools: run from a checkout, against that checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MAIN = "main"
RUN_ENV = "CLAUNCH_CFLOW_RUN"


def _cflow_dir(start: Path) -> Path | None:
    for cwd in (start, *start.parents):
        if (cwd / ".cflow").is_dir():
            return cwd / ".cflow"
    return None


def _state(slot: Path) -> dict | None:
    path = slot / "state.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _slot(scope_dir: Path, run: str) -> Path:
    return scope_dir if not run or run == MAIN else scope_dir / "sub" / run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="has SOURCE published MILESTONE since STEP consumed it? (exit code is the answer)"
    )
    ap.add_argument("source", help="'main', or the sub run's name")
    ap.add_argument("milestone")
    ap.add_argument("--step", help="the asking step (default: the asking run's current step)")
    ap.add_argument("--run", help=f"the asking run (default: ${RUN_ENV}, else the main run)")
    ap.add_argument("-t", "--session", help="whose runs (default: $CLAUNCH_SESSION)")
    ap.add_argument("--cwd", help="where to look for .cflow (default: here, then upward)")
    args = ap.parse_args(argv)

    scope = args.session or os.environ.get("CLAUNCH_SESSION", "")
    if not scope:
        print("cannot tell: no session named (pass -t, or set CLAUNCH_SESSION)")
        return 2
    root = _cflow_dir(Path(args.cwd or os.getcwd()).resolve())
    if root is None:
        print("cannot tell: no .cflow directory here or above")
        return 2
    scope_dir = root / "runs" / scope
    asking = _state(_slot(scope_dir, args.run or os.environ.get(RUN_ENV, "")))
    if asking is None:
        print("cannot tell: no asking run stands here")
        return 2
    source = _state(_slot(scope_dir, args.source))
    if source is None:
        print(f"run {args.source!r}: not running in this scope")
        return 2
    step = args.step or asking.get("current")
    consumed = int((asking.get("consumed") or {}).get(step) or 0)
    count = int(((source.get("milestones") or {}).get(args.milestone) or {}).get("count") or 0)
    new = count > consumed
    print(
        f"{args.source} {args.milestone}: published {count}x, step {step} "
        f"consumed {consumed}: " + ("new" if new else "nothing new")
    )
    return 0 if new else 1


if __name__ == "__main__":
    # 1 is an answer ("nothing new"), and Python exits 1 on an uncaught
    # exception: a crash must not read as a wait still standing.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure is "cannot tell"
        print(f"cannot tell: {type(exc).__name__}: {exc}")
        sys.exit(2)
