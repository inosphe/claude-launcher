"""Has this session's sub run finished? — the gate-shaped twin of
``claunch cflow sub-done``.

A main run's step waits on its sub runs through ``awaits`` (``improv-worker``'s
``work`` step: the ``found-issue`` side tracks it opened must be over before
the report is written), and this repository's gate rule says a probe reaches
this checkout through a ``tools/`` script under ``uv run --no-sync`` — never
a bare ``claunch`` off PATH, which is an installed copy and not this tree
(``tests/test_gates_run_this_checkout.py``). Other repositories write the
same wait as ``awaits: {sub: all}``, which spells ``claunch cflow sub-done
--all``; this file is that command with the tree pinned.

It reads the run state directly rather than importing the engine: a gate
must hold on the standard library alone (``uv run --no-sync`` never populates
a fresh worktree's ``.venv``), and the fact is one JSON field per slot —
``status`` in ``.cflow/runs/<session>/sub/<name>/state.json``. The slot
layout is ``cflow/state.py``'s (``SUB_DIR``); ``tests/test_cflow_subflow.py``
pins that this script and the engine read the same directory.

    uv run --no-sync python tools/sub_done.py --all         # $CLAUNCH_SESSION
    uv run --no-sync python tools/sub_done.py <name>
    uv run --no-sync python tools/sub_done.py --all -t <session>

Exit codes, the shape ``claunch cflow sub-done`` uses:

    --all:  0  no sub run of the session is still running (none, or every
               one is done/aborted)
            1  at least one is still running
            2  cannot tell (no session name, no run directory found)
    <name>: 0  that sub run is done
            1  it is running, or was aborted
            2  no such sub run stands (never started, or archived)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Same as the other gate tools: run from a checkout, against that checkout.
# Nothing is imported from it here (the fact is a JSON file), but the line
# keeps this script on the same footing as its neighbours if that changes.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FINISHED = ("done", "aborted")


def _cflow_dir(start: Path) -> Path | None:
    """The nearest ``.cflow`` at or above ``start`` — how the CLI finds a run
    from a shell pinned inside a worktree under the project root."""
    for cwd in (start, *start.parents):
        if (cwd / ".cflow").is_dir():
            return cwd / ".cflow"
    return None


def _sub_states(scope_dir: Path) -> dict[str, dict]:
    base = scope_dir / "sub"
    out: dict[str, dict] = {}
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        state = entry / "state.json"
        if not state.is_file():
            continue
        try:
            out[entry.name] = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[entry.name] = {"status": "unreadable"}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="has this session's sub run finished? (exit code is the answer)")
    ap.add_argument("name", nargs="?", help="the sub run's name")
    ap.add_argument("--all", action="store_true", help="every sub run finished?")
    ap.add_argument("-t", "--session", help="whose sub runs (default: $CLAUNCH_SESSION)")
    ap.add_argument("--cwd", help="where to look for .cflow (default: here, then upward)")
    args = ap.parse_args(argv)

    scope = args.session or os.environ.get("CLAUNCH_SESSION", "")
    if not scope:
        print("cannot tell: no session named (pass -t, or set CLAUNCH_SESSION)")
        return 2
    if not args.all and not args.name:
        print("sub_done: give a sub run NAME, or --all")
        return 2
    root = _cflow_dir(Path(args.cwd or os.getcwd()).resolve())
    if root is None:
        print("cannot tell: no .cflow directory here or above")
        return 2
    states = _sub_states(root / "runs" / scope)

    if args.all:
        running = {n: s for n, s in states.items() if s.get("status") not in FINISHED}
        if running:
            for name, st in running.items():
                print(f"sub run {name!r}: {st.get('status')} (step {st.get('current')})  run: {st.get('run_id')}")
            return 1
        print(f"sub runs: none active ({len(states)} finished)")
        return 0

    st = states.get(args.name)
    if st is None:
        print(f"sub run {args.name!r}: no such sub run in this scope")
        return 2
    status = st.get("status")
    step = st.get("current")
    print(f"sub run {args.name!r}: {status}" + (f" (step {step})" if step else "") + f"  run: {st.get('run_id')}")
    return 0 if status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
