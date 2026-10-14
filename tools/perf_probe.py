"""Measure the live daemon's CPU the same way every round, and say whether two
measurements can be compared.

``perf-monitor-v2`` runs rounds of measure -> analyse -> propose -> (the user
approves) -> delegate -> land -> remeasure. The measurement is the part that
must not drift between rounds: one round sampled 15 seconds at 50 Hz, the next
60 seconds at 100 Hz, one three minutes after a restart with the machine at
load 40 and one fifty minutes after with the machine idle -- and the before /
after numbers were then set side by side as if they were one axis. Each of
those was found and written down by hand (claunch-2b734, claunch-xrrn0
comments). This tool takes the choice away: the round runs ``record`` with
the run's inputs, ``summarize`` reduces the raw profile to the same three
views every time, and ``compare`` refuses to call a delta a delta without
printing the condition differences beside it.

    uv run --no-sync python tools/perf_probe.py record --label baseline
        [--seconds 60] [--rate 100] [--probe-path /api/sessions]
        [--probe-every 1.5] [--min-uptime 900] [--out DIR]
    uv run --no-sync python tools/perf_probe.py summarize [DIR | --label L]
    uv run --no-sync python tools/perf_probe.py compare [BEFORE AFTER]
    uv run --no-sync python tools/perf_probe.py check [DIR | --label L] [--max-age S]
    uv run --no-sync python tools/perf_probe.py warm [--min-uptime S]
    uv run --no-sync python tools/perf_probe.py issues-closed [--from-env NAME]
    uv run --no-sync python tools/perf_probe.py serving [--from-env NAME]

``record`` writes four files into one directory: ``profile.raw`` (py-spy's
collapsed stacks with counts), ``summary.json`` (the three views below),
``meta.json`` (the conditions: daemon pid, code head, uptime, load average,
sample and error counts, session counts, open connections, the probe's
timings) and, with ``--probe-path``, ``probe.txt`` (one line per request:
start time, seconds, outcome). Without ``--out`` the directory is a new
``perf-monitor/<UTC stamp>-<label>`` under the session's scratch directory
(``$CLAUNCH_SCRATCH`` -- ``/tmp`` is one directory for every session on the
machine, AGENTS.md), and ``perf-monitor/last-<label>`` names it. That pointer
is how the rest finds a measurement without being handed a path: a workflow's
``verify`` cannot take the run's values (the engine refuses ``${...}`` in a
command), so ``check --label baseline`` and a bare ``compare`` (last
``baseline`` against last ``after``) read it instead.

The last four subcommands are gates, in the exit-code shape the other gate
tools use: 0 true, 1 not yet, 2 cannot tell. ``check`` is a step's ``verify``
(a measurement under that label, summarized, recorded within ``--max-age``
seconds -- so last round's file does not pass for this round's); ``warm``,
``issues-closed`` and ``serving`` are ``wait`` probes the daemon's clock
polls. ``--from-env NAME`` reads the bound data the engine exports as
``CFLOW_DATA_<NAME>`` (JSON for a mapping, docs/cflow-v2.md 2.3); the issue
ids are its ``issues`` list.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

#: A stack is the event loop's when it passes through asyncio's dispatcher.
#: That thread also pumps every terminal, so its share is the number that says
#: whether the dashboard and the terminals wait (claunch-c9evc).
LOOP_MARK = "_run_once (asyncio/base_events.py"

#: Two measurements whose load averages differ by more than this factor are
#: not one axis: on 2026-10-14 the "after" ran at load 40 against a "before"
#: at load 1 and its sample count came out nine times higher.
LOAD_RATIO_LIMIT = 2.0

FRAME = re.compile(r"\s*\((?:/|[A-Za-z]:\\).*?(claude_launcher[/\\]|lib[/\\]python3\.\d+[/\\]|site-packages[/\\])")


def _short(frame: str) -> str:
    return FRAME.sub(" (", frame)


def _noline(frame: str) -> str:
    return re.sub(r":\d+\)$", ")", frame)


def _scratch() -> Optional[Path]:
    """This session's scratch directory (AGENTS.md: never a fixed /tmp name)."""
    env = os.environ.get("CLAUNCH_SCRATCH")
    if env:
        return Path(env)
    name = os.environ.get("CLAUNCH_SESSION")
    if not name:
        return None
    try:
        from claude_launcher.daemon import paths
        return paths.session_scratch_dir(name)
    except Exception:
        return None


def _pointer(label: str) -> Optional[Path]:
    base = _scratch()
    return None if base is None else base / "perf-monitor" / f"last-{label}"


def _resolve(dir_: Optional[str], label: Optional[str]) -> Optional[Path]:
    """An explicit directory, else the one the last ``record --label`` named."""
    if dir_:
        return Path(dir_)
    ptr = _pointer(label or "latest")
    if ptr is None or not ptr.is_file():
        return None
    return Path(ptr.read_text(encoding="utf-8").strip())


def _daemon_doc() -> Optional[dict]:
    try:
        from claude_launcher.daemon import runtime_state
    except Exception:
        return None
    try:
        return runtime_state.read_daemon_json()
    except Exception:
        return None


def _uptime(doc: dict) -> Optional[float]:
    started = str(doc.get("started_at") or "")
    try:
        when = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.datetime.now(dt.timezone.utc) - when).total_seconds()


def _loadavg() -> Optional[list]:
    try:
        return [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        return None


def _client():
    try:
        from claude_launcher import daemon_client
        return daemon_client.connect()
    except Exception:
        return None


def _session_counts(client) -> Optional[dict]:
    if client is None:
        return None
    try:
        doc = client.get("/api/sessions", timeout=60)
    except Exception:
        return None
    rows = doc.get("sessions", doc) if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        return None
    counts = collections.Counter(str(r.get("status") or "?") for r in rows)
    return {"total": len(rows), **dict(counts)}


def _connections(client) -> Optional[dict]:
    if client is None:
        return None
    try:
        doc = client.get("/api/connections", timeout=10)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    held = doc.get("open")
    count = doc.get("open_count")
    if count is None and isinstance(held, list):
        count = len(held)
    return {"open": count, "http": doc.get("http_connections")}


def _probe_loop(client, path: str, every: float, stop: threading.Event,
                out: Path) -> None:
    with out.open("w", encoding="utf-8") as fh:
        while not stop.is_set():
            t0 = time.time()
            outcome = "ok"
            try:
                client.get(path, timeout=30)
            except Exception as exc:  # noqa: BLE001 -- the outcome is the record
                outcome = type(exc).__name__
            fh.write(f"{t0:.3f} {time.time() - t0:.3f} {outcome}\n")
            fh.flush()
            stop.wait(every)


def _probe_stats(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    secs, fails = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        secs.append(float(parts[1]))
        fails += parts[2] != "ok"
    if not secs:
        return {"n": 0}
    secs.sort()
    return {
        "n": len(secs), "failed": fails,
        "avg": round(sum(secs) / len(secs), 3),
        "p50": secs[len(secs) // 2],
        "p95": secs[min(len(secs) - 1, int(len(secs) * 0.95))],
        "max": secs[-1],
    }


def cmd_record(args) -> int:
    if args.out:
        out = Path(args.out)
    else:
        base = _scratch()
        if base is None:
            print("no --out and no session scratch (CLAUNCH_SCRATCH / CLAUNCH_SESSION)",
                  file=sys.stderr)
            return 2
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = base / "perf-monitor" / f"{stamp}-{args.label or 'run'}"
    out.mkdir(parents=True, exist_ok=True)
    doc = _daemon_doc()
    if not doc or not doc.get("pid"):
        print("no daemon.json with a pid -- is the daemon running?", file=sys.stderr)
        return 2
    spy = shutil.which("py-spy")
    if not spy:
        print("py-spy is not on PATH", file=sys.stderr)
        return 2
    uptime = _uptime(doc)
    if args.min_uptime and uptime is not None and uptime < args.min_uptime:
        print(f"daemon has been up {uptime:.0f}s, under --min-uptime "
              f"{args.min_uptime}s: a measurement now carries its start-up "
              "catch-up (unread transcripts) -- wait, or pass --min-uptime 0",
              file=sys.stderr)
        return 3
    client = _client()
    load_before = _loadavg()
    stop = threading.Event()
    prober = None
    if args.probe_path and client is not None:
        prober = threading.Thread(
            target=_probe_loop, daemon=True,
            args=(client, args.probe_path, args.probe_every, stop,
                  out / "probe.txt"))
        prober.start()
    started = time.time()
    proc = subprocess.run(
        [spy, "record", "-p", str(doc["pid"]), "-d", str(args.seconds),
         "-r", str(args.rate), "-f", "raw", "--nonblocking",
         "-o", str(out / "profile.raw")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    stop.set()
    if prober is not None:
        prober.join(timeout=40)
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    m = re.search(r"Samples:\s*(\d+)\s+Errors:\s*(\d+)", proc.stdout + proc.stderr)
    meta = {
        "label": args.label,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "seconds": args.seconds, "rate": args.rate,
        "wall": round(time.time() - started, 1),
        "daemon": {
            "pid": doc.get("pid"), "port": doc.get("port"),
            "head": (doc.get("code") or {}).get("head"),
            "started_at": doc.get("started_at"),
            "uptime": None if uptime is None else round(uptime),
        },
        "load_before": load_before, "load_after": _loadavg(),
        "samples": int(m.group(1)) if m else None,
        "errors": int(m.group(2)) if m else None,
        "sessions": _session_counts(client),
        "connections": _connections(client),
        "probe": None if not args.probe_path else {
            "path": args.probe_path, "every": args.probe_every,
            **(_probe_stats(out / "probe.txt") or {}),
        },
        "py_spy_rc": proc.returncode,
        "py_spy_tail": tail[-3:],
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))
    if proc.returncode != 0 or not (out / "profile.raw").is_file():
        return 2
    (out / "summary.json").write_text(
        json.dumps(_aggregate(out / "profile.raw", args.top), indent=2),
        encoding="utf-8")
    for label in {"latest", args.label or "latest"}:
        ptr = _pointer(label)
        if ptr is not None:
            ptr.parent.mkdir(parents=True, exist_ok=True)
            ptr.write_text(str(out), encoding="utf-8")
    print(f"recorded: {out}")
    return 0


def _aggregate(raw: Path, top: int) -> dict:
    total = loop = 0
    outer, deep, leaf = (collections.Counter() for _ in range(3))
    with raw.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stack, _, count = line.rstrip().rpartition(" ")
            try:
                n = int(count)
            except ValueError:
                continue
            total += n
            frames = stack.split(";")
            if any(LOOP_MARK in f for f in frames):
                loop += n
            ours = [_short(f) for f in frames if "claude_launcher" in f]
            if ours:
                outer[ours[0]] += n
                deep[ours[-1]] += n
            leaf[_short(frames[-1])] += n

    def rows(counter):
        return [{"frame": k, "samples": v,
                 "share": round(v * 100 / total, 1) if total else 0.0}
                for k, v in counter.most_common(top)]

    return {"total": total, "event_loop": loop,
            "outermost": rows(outer), "deepest": rows(deep), "leaf": rows(leaf)}


def cmd_summarize(args) -> int:
    d = _resolve(args.dir, args.label)
    if d is None:
        print("no measurement directory (give one, or record with --label first)",
              file=sys.stderr)
        return 2
    raw = d / "profile.raw"
    if not raw.is_file():
        print(f"{raw} is missing", file=sys.stderr)
        return 2
    summary = _aggregate(raw, args.top)
    (d / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    t = summary["total"] or 1
    print(f"samples {summary['total']}, event loop {summary['event_loop']} "
          f"({summary['event_loop'] * 100 / t:.1f}%)")
    for view, head in (("outermost", "outermost claude_launcher frame"),
                       ("deepest", "deepest claude_launcher frame"),
                       ("leaf", "leaf frame")):
        print(f"\n| {head} | samples | share |\n|---|---|---|")
        for r in summary[view]:
            print(f"| `{r['frame'][:120]}` | {r['samples']} | {r['share']}% |")
    return 0


def _conditions_differ(a: dict, b: dict) -> list:
    notes = []
    for key in ("seconds", "rate"):
        if a.get(key) != b.get(key):
            notes.append(f"{key} differs: {a.get(key)} vs {b.get(key)}")
    la, lb = (a.get("load_before") or [None])[0], (b.get("load_before") or [None])[0]
    if la and lb and max(la, lb) / max(min(la, lb), 0.1) > LOAD_RATIO_LIMIT:
        notes.append(f"1-minute load average differs: {la} vs {lb}")
    ua = (a.get("daemon") or {}).get("uptime")
    ub = (b.get("daemon") or {}).get("uptime")
    for label, up in (("before", ua), ("after", ub)):
        if up is not None and up < 900:
            notes.append(f"{label} daemon was up {up}s (< 900s: start-up catch-up included)")
    sa, sb = a.get("sessions") or {}, b.get("sessions") or {}
    if sa.get("total") and sb.get("total") and sa["total"] != sb["total"]:
        notes.append(f"session rows differ: {sa['total']} vs {sb['total']}")
    ca, cb = a.get("connections") or {}, b.get("connections") or {}
    if ca.get("open") is not None and ca.get("open") != cb.get("open"):
        notes.append(f"open daemon connections differ: {ca.get('open')} vs {cb.get('open')}")
    return notes


def cmd_compare(args) -> int:
    dirs = [_resolve(args.before, "baseline"), _resolve(args.after, "after")]
    if None in dirs:
        print("need two measurements: give BEFORE AFTER, or record with "
              "--label baseline and --label after first", file=sys.stderr)
        return 2
    metas, sums = [], []
    for d in dirs:
        try:
            metas.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
            sums.append(json.loads((d / "summary.json").read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"{d}: {exc} (run record and summarize first)", file=sys.stderr)
            return 2
    a, b = sums
    print("| item | before | after |\n|---|---|---|")
    for label, key in (("daemon head", "head"), ("daemon uptime (s)", "uptime")):
        print(f"| {label} | {(metas[0]['daemon'] or {}).get(key)} | {(metas[1]['daemon'] or {}).get(key)} |")
    print(f"| load average (1m) | {(metas[0].get('load_before') or ['?'])[0]} | {(metas[1].get('load_before') or ['?'])[0]} |")
    print(f"| samples | {a['total']} | {b['total']} |")
    print(f"| event-loop samples | {a['event_loop']} | {b['event_loop']} |")
    pa, pb = metas[0].get("probe") or {}, metas[1].get("probe") or {}
    if pa or pb:
        for key in ("avg", "p95", "max", "failed"):
            print(f"| probe {key} | {pa.get(key)} | {pb.get(key)} |")
    # Keyed without the line number: the two measurements usually run on two
    # commits, and a line that moved is still the same function.
    before, after = collections.Counter(), collections.Counter()
    for side, rows in ((before, a["outermost"]), (after, b["outermost"])):
        for r in rows:
            side[_noline(r["frame"])] += r["samples"]
    print("\n| outermost frame (line numbers dropped) | before | after |\n|---|---|---|")
    for frame in sorted(set(before) | set(after),
                        key=lambda f: -(before.get(f, 0) + after.get(f, 0))):
        print(f"| `{frame[:110]}` | {before.get(frame, '-')} | {after.get(frame, '-')} |")
    notes = _conditions_differ(metas[0], metas[1])
    print("\nconditions:", "same axis" if not notes else "NOT one axis")
    for n in notes:
        print(f"- {n}")
    return 0


def _from_env(name: str) -> Optional[dict]:
    raw = os.environ.get(f"CFLOW_DATA_{name.upper()}")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def cmd_check(args) -> int:
    d = _resolve(args.dir, args.label)
    if d is None:
        print("no measurement recorded under that label")
        return 1
    missing = [f for f in ("meta.json", "profile.raw", "summary.json")
               if not (d / f).is_file()]
    if missing:
        print(f"{d}: missing {', '.join(missing)}")
        return 1
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    try:
        when = dt.datetime.fromisoformat(meta["recorded_at"])
    except (KeyError, ValueError):
        print(f"{d}: meta.json has no recorded_at")
        return 2
    age = (dt.datetime.now(dt.timezone.utc) - when).total_seconds()
    if age > args.max_age:
        print(f"{d}: recorded {age:.0f}s ago, older than --max-age {args.max_age}s "
              "(a previous round's measurement, not this one)")
        return 1
    print(f"{d}: measurement and summary present, {age:.0f}s old")
    return 0


def cmd_warm(args) -> int:
    floor = args.min_uptime
    if floor is None:
        inputs = _from_env("inputs") or {}
        floor = inputs.get("warmup_seconds")
    floor = 900 if floor is None else int(floor)
    doc = _daemon_doc()
    up = _uptime(doc) if doc else None
    if up is None:
        print("daemon start time unreadable")
        return 2
    print(f"daemon up {up:.0f}s, floor {floor}s")
    return 0 if up >= floor else 1


def _issues(args) -> Optional[list]:
    value = _from_env(args.from_env) if args.from_env else None
    ids = (value or {}).get("issues") if value else args.issue
    if not ids:
        return None
    return [str(i) for i in ids]


def _show(issue: str) -> Optional[dict]:
    exe = shutil.which("claunch")
    if not exe:
        return None
    proc = subprocess.run([exe, "beads", "show", issue, "--json"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=25)
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        return None
    if isinstance(doc, list):
        doc = doc[0] if doc else None
    return doc if isinstance(doc, dict) else None


def cmd_issues_closed(args) -> int:
    ids = _issues(args)
    if not ids:
        print("no issue ids")
        return 2
    open_ = []
    for issue in ids:
        doc = _show(issue)
        if doc is None:
            print(f"{issue}: unreadable")
            return 2
        if doc.get("status") != "closed":
            open_.append(f"{issue}={doc.get('status')}")
    print("all closed" if not open_ else "waiting: " + ", ".join(open_))
    return 0 if not open_ else 1


def cmd_serving(args) -> int:
    ids = _issues(args)
    doc = _daemon_doc()
    head = ((doc or {}).get("code") or {}).get("head")
    if not ids or not head:
        print("no issue ids or no daemon code head")
        return 2
    behind = []
    for issue in ids:
        row = _show(issue)
        reason = str((row or {}).get("close_reason") or "")
        m = re.search(r"merged\s+([0-9a-f]{7,40})", reason)
        if not m:
            print(f"{issue}: no 'merged <hash>' close reason")
            return 2
        rc = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor",
                             m.group(1), head], capture_output=True).returncode
        if rc == 1:
            behind.append(f"{issue}@{m.group(1)[:8]}")
        elif rc != 0:
            # 128: a hash this checkout's object store does not have (or a
            # malformed one). Undecidable, not "behind": the workflow's wait
            # hands three of these in a row to the user. The leader merges in
            # this checkout's master, so a real merge hash is normally present.
            print(f"{issue}: git cannot compare {m.group(1)} with {head}")
            return 2
    print(f"daemon head {head[:8]}: " + ("serves every merge" if not behind
                                         else "missing " + ", ".join(behind)))
    return 0 if not behind else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--out")
    rec.add_argument("--label", help="baseline | after: the pointer compare/check read")
    rec.add_argument("--top", type=int, default=15)
    rec.add_argument("--seconds", type=int, default=60)
    rec.add_argument("--rate", type=int, default=100)
    rec.add_argument("--probe-path")
    rec.add_argument("--probe-every", type=float, default=1.5)
    rec.add_argument("--min-uptime", type=int, default=900)
    summ = sub.add_parser("summarize")
    summ.add_argument("dir", nargs="?")
    summ.add_argument("--label")
    summ.add_argument("--top", type=int, default=15)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("before", nargs="?")
    cmp_.add_argument("after", nargs="?")
    chk = sub.add_parser("check")
    chk.add_argument("dir", nargs="?")
    chk.add_argument("--label")
    chk.add_argument("--max-age", type=int, default=3600)
    warm = sub.add_parser("warm")
    warm.add_argument("--min-uptime", type=int)
    for name in ("issues-closed", "serving"):
        p = sub.add_parser(name)
        p.add_argument("--from-env")
        p.add_argument("--issue", action="append")
    args = ap.parse_args(argv)
    return {
        "record": cmd_record, "summarize": cmd_summarize, "compare": cmd_compare,
        "check": cmd_check, "warm": cmd_warm, "issues-closed": cmd_issues_closed,
        "serving": cmd_serving,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
