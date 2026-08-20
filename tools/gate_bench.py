"""Measure whether the -n 8 gate is deterministic, quiet and under load.

The gate command is the thing every worker's cflow run has to pass, so the
question is not "is it fast" but "does it give the same answer every time" —
and specifically whether it still does when several sessions run their gates
at once. A wall-clock assumption in the suite (a 0.5s idle threshold) reacts
to how starved the machine is, not to how many xdist workers this particular
run asked for, so a quiet-machine repeat cannot answer it.

Two phases, same command:

* **quiet**   — repeats with nothing else of ours running.
* **loaded**  — repeats while a synthetic load stands in for the other
  sessions' gates. The load is not picked by taste: ``--emulate`` says how
  many *other* gates to imitate and the burner count is derived from a gate's
  own measured width (``characterize``), so the number in the report has a
  provenance rather than a vibe.

Every run records the counts, the duration, and the identity of anything that
failed — a run that "passed 942 of 943" tells you almost nothing without the
name of the one that did not.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: pytest's summary line, e.g. "942 passed, 1 skipped, 105 deselected in 178.42s".
_COUNT = re.compile(r"(\d+) (passed|failed|skipped|deselected|error|xfailed|xpassed)")
_ELAPSED = re.compile(r"in ([\d.]+)s")
#: "FAILED tests/test_x.py::test_y - AssertionError: ..." — the id is what matters.
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
#: How much of a failing run's tail to keep — enough for the tracebacks.
_FAILTEXT = 20000


def burner(seconds: float) -> None:
    """One CPU-hungry process: the unit the synthetic load is built from."""
    end = time.monotonic() + seconds
    x = 0
    while time.monotonic() < end:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    return


def _python_procs() -> int:
    """How many python processes exist right now.

    Counted rather than pattern-matched: an xdist worker is spawned through
    execnet as ``python -c "import sys;exec(eval(sys.stdin.readline()))"``,
    so it carries neither "pytest" nor "xdist" on its command line — a filter
    for either undercounts an -n 8 run to about half and makes the load look
    smaller than it is. A plain count against a baseline taken before the run
    has no such blind spot.
    """
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process python -ErrorAction SilentlyContinue | "
             "Measure-Object).Count"],
            capture_output=True, text=True, timeout=30,
        )
        return int((ps.stdout or "0").strip() or 0)
    except Exception:
        return -1


def _cpu_pct(samples: int = 3, gap: float = 2.0) -> float:
    """Machine-wide CPU, averaged — one instantaneous read of the perf
    counter swings wildly enough to be worthless as evidence."""
    vals = []
    for i in range(samples):
        try:
            ps = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor | "
                 "Where-Object { $_.Name -eq '_Total' }).PercentProcessorTime"],
                capture_output=True, text=True, timeout=30,
            )
            vals.append(float((ps.stdout or "").strip() or 0))
        except Exception:
            pass
        if i < samples - 1:
            time.sleep(gap)
    return round(sum(vals) / len(vals), 1) if vals else -1.0


def run_gate(cmd: list, cwd: Path, basetemp: str) -> dict:
    """One gate run, reduced to the facts a verdict needs."""
    full = list(cmd) + [f"--basetemp={basetemp}"]
    # Before, so the count during the run can be read as "this gate's own
    # processes plus whatever was already there".
    base_procs = _python_procs()
    started = time.monotonic()
    mid = {}
    proc = subprocess.Popen(
        full, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    # Sample once the run is properly under way: the first seconds are import
    # and collection, which is not the load the suite is being judged under.
    time.sleep(20)
    if proc.poll() is None:
        procs = _python_procs()
        mid = {
            "python_procs": procs,
            "python_procs_base": base_procs,
            "python_procs_delta": (procs - base_procs) if procs >= 0 else None,
            "cpu_pct": _cpu_pct(),
        }
    out, _ = proc.communicate()
    elapsed = time.monotonic() - started

    counts = {k: int(n) for n, k in _COUNT.findall(out or "")}
    m = _ELAPSED.search(out or "")
    return {
        "exit": proc.returncode,
        "wall_s": round(elapsed, 2),
        "pytest_s": float(m.group(1)) if m else None,
        "counts": counts,
        "failed": sorted(set(_FAILED.findall(out or ""))),
        # The names alone cannot say WHY: a deadline helper that gives up may
        # raise AssertionError rather than TimeoutError (see
        # tests/test_daemon_e2e.py::_wait_screen), so "assertion vs timeout"
        # read off the exception type misclassifies a wait failure as a value
        # failure. Keep the text and let the reader see the sentence.
        "failure_text": (out or "")[-_FAILTEXT:] if proc.returncode else "",
        "during": mid,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        # NOT __doc__: argparse writes it to stdout, and a cp949 console
        # raises UnicodeEncodeError on the dashes this file is written
        # with. --help must work everywhere the tool runs.
        description="Measure whether the -n 8 gate is deterministic, "
                    "quiet and under load.")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--phase", choices=["quiet", "loaded", "characterize"],
                    required=True)
    ap.add_argument("--emulate", type=int, default=0,
                    help="how many OTHER sessions gates the load stands in for")
    ap.add_argument("--per-gate", type=int, default=8,
                    help="busy processes one gate contributes (from characterize)")
    ap.add_argument("--max-seconds", type=float, default=1800.0,
                    help="hard ceiling on the load window: burners die on "
                         "their own when it elapses. A saturated machine can "
                         "keep the mesh from delivering a 'hold', so the load "
                         "must not depend on being told to stop.")
    # NOT the repo root: the record is machine-local measurement data, and
    # a default that dirties `git status` collides with the very workflow
    # this gate belongs to (wrapup requires a clean tree). Printed on exit
    # so a temp path does not mean a lost file.
    ap.add_argument("--out", default=str(
        Path(tempfile.gettempdir()) / "gate_bench.jsonl"))
    ap.add_argument("--basetemp-root", default="C:/t/s24b")
    ap.add_argument("--gate", default=(
        'uv run --no-sync pytest tests -q -m "not worktree" -n 8'))
    args = ap.parse_args()

    cwd = Path(__file__).resolve().parents[1]
    # shlex, not str.split: the gate carries -m "not worktree", and naive
    # splitting hands pytest a -m of '"not' and a path of 'worktree"' — which
    # collects nothing and exits 5, i.e. a green-looking run that ran nothing.
    cmd = shlex.split(args.gate)

    burners = []
    load_desc = {"emulate": 0, "burners": 0}
    if args.phase == "loaded":
        n = args.emulate * args.per_gate
        load_desc = {"emulate": args.emulate, "burners": n,
                     "per_gate": args.per_gate, "cpus": os.cpu_count()}
        print(f"[load] {n} busy processes standing in for {args.emulate} gates "
              f"x {args.per_gate} workers on {os.cpu_count()} cpus", flush=True)
        for _ in range(n):
            burners.append(subprocess.Popen(
                [sys.executable, "-c",
                 f"import time\nend=time.monotonic()+{args.max_seconds}\nx=0\n"
                 "while time.monotonic()<end: x=(x*1103515245+12345)&0x7FFFFFFF"],
            ))
        time.sleep(5)  # let the load actually land before the gate starts

    try:
        if args.phase == "characterize":
            # One gate, sampled: this is where --per-gate comes from.
            r = run_gate(cmd, cwd, f"{args.basetemp_root}c")
            print(json.dumps({"phase": "characterize", **r}, ensure_ascii=False))
            return 0
        results = []
        for i in range(args.repeats):
            r = run_gate(cmd, cwd, f"{args.basetemp_root}{args.phase}{i}")
            rec = {"phase": args.phase, "i": i, "load": load_desc, **r}
            results.append(rec)
            with open(args.out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{args.phase} {i}] exit={r['exit']} {r['counts']} "
                  f"pytest={r['pytest_s']}s failed={r['failed']} "
                  f"during={r['during']}", flush=True)
        same = {json.dumps(r["counts"], sort_keys=True) for r in results}
        print(f"[{args.phase}] identical result sets: {len(same) == 1} "
              f"({len(same)} distinct)")
        print(f"[{args.phase}] records: {args.out}")
        return 0
    finally:
        for b in burners:
            try:
                b.kill()
            except Exception:
                pass
        for b in burners:
            try:
                b.wait(timeout=10)
            except Exception:
                pass
        if burners:
            print(f"[load] {len(burners)} burners stopped", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
