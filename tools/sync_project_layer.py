"""Regenerate this repository's project-layer workflows from the package.

The packaged workflows under ``src/claude_launcher/workflows/`` are the only
place a workflow is written. This repository also carries a *project layer*
(``.claunch/workflows/``) for two of them, and that layer wins for every run
here — it exists for exactly one reason: to add the ``verify:`` commands
that arm the gates with this repository's own test suite, which the
packaged copy cannot carry (it ships to every repository).

For a long time the project copies were maintained by hand — a packaged
edit re-typed into the override, or not. Twice it was not: the leader ran
for days without the preflight step the package had gained, and the worker
pair drifted far enough that nobody wanted to touch it. ``claunch cflow
update`` does not help; it refreshes the *global* layer. ``claunch cflow add
--project --force`` copies the packaged bytes and throws the ``verify:``
lines away with them.

So this script does the one mechanical thing the layer needs: take the
packaged file, graft in each ``verify:`` line the project copy carries —
together with the run of ``#`` comment lines directly above it, which is
where the reasons for the command live — and write that as the project
copy. Nothing else from the project copy survives, on purpose: anything
else there is drift.

    uv run --no-sync python tools/sync_project_layer.py            # rewrite
    uv run --no-sync python tools/sync_project_layer.py --check    # exit 1 on drift

``--check`` is what the test suite runs, so a packaged edit that is not
followed by a regeneration is red, not silent.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "claude_launcher" / "workflows"
PROJECT = ROOT / ".claunch" / "workflows"

#: A step header at the workflow's ``steps:`` level (two-space indent).
STEP_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
#: The step-level fields the graft keys on (four-space indent).
VERIFY_RE = re.compile(r"^    verify:")
COMMENT_RE = re.compile(r"^    #")
NEXT_RE = re.compile(r"^    next:")


def verify_blocks(project_text: str) -> Dict[str, List[str]]:
    """Each step's ``verify:`` line plus the comment run above it, by step id."""
    lines = project_text.splitlines(keepends=True)
    blocks: Dict[str, List[str]] = {}
    step = None
    for i, line in enumerate(lines):
        m = STEP_RE.match(line)
        if m:
            step = m.group(1)
            continue
        if VERIFY_RE.match(line):
            if step is None:
                raise ValueError("a verify: line before any step")
            j = i
            while j > 0 and COMMENT_RE.match(lines[j - 1]):
                j -= 1
            blocks[step] = lines[j : i + 1]
    return blocks


def graft(bundled_text: str, blocks: Dict[str, List[str]]) -> str:
    """The packaged text with each block placed before its step's ``next:``.

    The packaged copy may already end a step with a one-line pointer comment
    ("the verify lives in the project layer"); a block that begins with the
    same lines replaces them rather than stacking on top.
    """
    pending = dict(blocks)
    out: List[str] = []
    step = None
    for line in bundled_text.splitlines(keepends=True):
        m = STEP_RE.match(line)
        if m:
            step = m.group(1)
        if NEXT_RE.match(line) and step in pending:
            block = pending.pop(step)
            k = len(out)
            while k > 0 and COMMENT_RE.match(out[k - 1]):
                k -= 1
            trailing = out[k:]
            if trailing and block[: len(trailing)] == trailing:
                del out[k:]
            out.extend(block)
        out.append(line)
    if pending:
        raise ValueError(
            "verify blocks for steps the packaged copy does not have: "
            + ", ".join(sorted(pending))
        )
    return "".join(out)


def regenerate(name: str, bundled_dir: Path = BUNDLED, project_dir: Path = PROJECT) -> str:
    bundled = (bundled_dir / f"{name}.yaml").read_text(encoding="utf-8")
    project = (project_dir / f"{name}.yaml").read_text(encoding="utf-8")
    return graft(bundled, verify_blocks(project))


def overrides(bundled_dir: Path = BUNDLED, project_dir: Path = PROJECT) -> List[str]:
    """The workflows this project overrides — project files the package also ships."""
    if not project_dir.is_dir():
        return []
    return sorted(
        p.stem for p in project_dir.glob("*.yaml") if (bundled_dir / p.name).is_file()
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name", nargs="*", help="override(s) to regenerate; default: all")
    ap.add_argument(
        "--check", action="store_true",
        help="do not write; exit 1 if any override differs from its regeneration",
    )
    args = ap.parse_args(argv)
    names = args.name or overrides()
    drifted = []
    for name in names:
        try:
            wanted = regenerate(name)
        except (OSError, ValueError) as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            return 2
        dest = PROJECT / f"{name}.yaml"
        current = dest.read_text(encoding="utf-8")
        if current == wanted:
            print(f"{name}: current")
            continue
        if args.check:
            drifted.append(name)
            print(f"{name}: differs from packaged + verify", file=sys.stderr)
            continue
        dest.write_text(wanted, encoding="utf-8", newline="\n")
        print(f"{name}: regenerated from the package (verify blocks kept)")
    if drifted:
        print(
            "project layer drifted from the package — run "
            "tools/sync_project_layer.py to regenerate",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
