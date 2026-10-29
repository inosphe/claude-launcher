"""Rank test modules and tests by the time a sweep spent in them. Read-only.

``python tools/sweep.py run`` keeps the junit report of each sweep beside its
receipt as ``<sha>.junit.xml.gz``. This reads one of those (or a plain junit
XML) and prints modules by cumulative time, then the slowest tests.

    python tools/durations_report.py                # newest report of this repository
    python tools/durations_report.py <sha-prefix>   # that sweep's report
    python tools/durations_report.py path/to/junit.xml[.gz] --top 30 --json

A test's time is setup + call + teardown as junit records it. With ``-n``
workers the total exceeds the run's wall time; ``share`` is each module's part
of that total.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep  # noqa: E402


def find_report(spec, repo: Path, receipts=None) -> Path:
    """``spec`` is a file, a sha prefix, or empty for the newest report."""
    if spec and Path(spec).is_file():
        return Path(spec)
    directory = sweep.receipts_dir(repo, receipts)
    found = sorted(directory.glob("*.junit.xml.gz"), key=lambda p: p.stat().st_mtime) if directory.is_dir() else []
    if spec:
        found = [p for p in found if p.name.startswith(spec)]
    if not found:
        raise SystemExit(f"no junit report in {directory}" + (f" for {spec!r}" if spec else ""))
    return found[-1]


def load(path: Path):
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return ET.fromstring(data)


def collect(root) -> list:
    """``[{"nodeid", "module", "seconds"}]`` for every testcase."""
    cases = []
    for case in root.iter("testcase"):
        file = case.get("file") or ""
        classname = case.get("classname") or ""
        module = file or classname.rsplit(".", 1)[0]
        parts = [module] if file else []
        inner = classname[len(file[:-3].replace("/", ".")) + 1:] if file.endswith(".py") else ""
        parts += [p for p in inner.split(".") if p] + [case.get("name", "")]
        cases.append({"nodeid": "::".join(parts), "module": module,
                      "seconds": float(case.get("time") or 0.0)})
    return cases


def rank(cases: list, top: int) -> dict:
    total = sum(c["seconds"] for c in cases)
    modules: dict = {}
    for c in cases:
        row = modules.setdefault(c["module"], {"module": c["module"], "seconds": 0.0, "tests": 0})
        row["seconds"] += c["seconds"]
        row["tests"] += 1
    ranked = sorted(modules.values(), key=lambda r: r["seconds"], reverse=True)
    for row in ranked:
        row["seconds"] = round(row["seconds"], 2)
        row["share"] = round(row["seconds"] / total, 4) if total else 0.0
    slowest = sorted(cases, key=lambda c: c["seconds"], reverse=True)[:top]
    return {
        "tests": len(cases),
        "modules": len(ranked),
        "sum_seconds": round(total, 1),
        "top_modules": ranked[:top],
        "slowest_tests": [{"nodeid": c["nodeid"], "seconds": round(c["seconds"], 2)} for c in slowest],
    }


def render(report: dict, source: Path) -> str:
    lines = [f"{source.name}: {report['tests']} tests in {report['modules']} modules, "
             f"{report['sum_seconds']}s summed over workers", "", "module  (cumulative seconds, share, tests)"]
    for r in report["top_modules"]:
        lines.append(f"  {r['seconds']:9.2f}s {r['share']:6.1%} {r['tests']:5d}  {r['module']}")
    lines += ["", "slowest tests"]
    for t in report["slowest_tests"]:
        lines.append(f"  {t['seconds']:9.2f}s  {t['nodeid']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("report", nargs="?", default="", help="junit file, sha prefix, or empty for the newest")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--receipts", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    path = find_report(args.report, args.repo.resolve(), args.receipts)
    report = rank(collect(load(path)), args.top)
    print(json.dumps({"source": str(path), **report}, indent=2) if args.json else render(report, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
