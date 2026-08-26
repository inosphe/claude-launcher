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
packaged file, graft in each project-layer field the project copy carries —
together with the run of ``#`` comment lines directly above it, which is
where the reasons for the command live — and write that as the project
copy. Nothing else from the project copy survives, on purpose: anything
else there is drift.

The grafted fields are ``verify:`` and ``awaits:`` (see ``GRAFT_RE``), and
they are chosen by name rather than by shape. Both answer "what does *this*
repository check, and with which tool" — a question the packaged copy cannot
answer because it ships to every repository — and ``awaits`` may run to
several lines where ``verify`` is usually one, so a block is the field's line
plus everything indented under it.

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
#: The step-level fields the graft keys on (four-space indent). Named as a
#: tuple as well as a pattern because the set is not only this file's: the
#: drift test in ``tests/test_project_layer_override.py`` compares the two
#: copies of a workflow with exactly these fields excluded, and it imports the
#: tuple rather than restating it. Two lists would agree right up until
#: somebody widened one, and that failure is silent in the direction that
#: matters — a field this tool stops grafting, while the test still ignores
#: it, simply drops out of both.
#:
#: Keyed by field NAME, not by shape: what belongs to the project layer is
#: whatever answers "what does THIS repository check" — the commands, not the
#: prose — and that is a property of the field, not of how many lines it takes
#: to write. ``awaits`` is here for the same reason ``verify`` is: what a step
#: waits for is this repository's business, and the packaged copy that ships
#: everywhere cannot name a tool that only exists here.
GRAFT_FIELDS = ("verify", "awaits")
GRAFT_RE = re.compile(r"^    (?:" + "|".join(GRAFT_FIELDS) + r"):")
COMMENT_RE = re.compile(r"^    #")
NEXT_RE = re.compile(r"^    next:")
#: A continuation of a grafted field: indented deeper than the field itself.
CONTINUATION_RE = re.compile(r"^     +\S")


def field_blocks(project_text: str) -> Dict[str, List[str]]:
    """Each step's grafted fields, with their comment runs, by step id.

    A block is the field's line, any lines indented deeper than it (so a
    mapping written over several lines survives, not only a one-liner), and
    the run of ``#`` comments directly above — which is where the reason for
    the command lives, and the reason is the half that is worth keeping.

    A step may carry more than one grafted field, so blocks accumulate in file
    order rather than the last one winning.
    """
    lines = project_text.splitlines(keepends=True)
    blocks: Dict[str, List[str]] = {}
    step = None
    i = 0
    while i < len(lines):
        m = STEP_RE.match(lines[i])
        if m:
            step = m.group(1)
            i += 1
            continue
        if not GRAFT_RE.match(lines[i]):
            i += 1
            continue
        if step is None:
            raise ValueError(f"a {lines[i].strip()} line before any step")
        start = i
        while start > 0 and COMMENT_RE.match(lines[start - 1]):
            start -= 1
        end = i + 1
        while end < len(lines) and _continues(lines, end):
            end += 1
        blocks.setdefault(step, []).extend(lines[start:end])
        i = end
    return blocks


def _continues(lines: List[str], i: int) -> bool:
    """Whether ``lines[i]`` still belongs to the grafted field above it.

    A blank line counts only when something deeper-indented follows it — a
    block scalar may contain one, and the blank line between two steps must
    not be swallowed along with it.
    """
    if CONTINUATION_RE.match(lines[i]):
        return True
    if lines[i].strip():
        return False
    for line in lines[i + 1 :]:
        if not line.strip():
            continue
        return bool(CONTINUATION_RE.match(line))
    return False


def graft(bundled_text: str, blocks: Dict[str, List[str]]) -> str:
    """The packaged text with each block placed inside its step.

    Before the step's ``next:`` where there is one. A **select** step has none
    — the engine forbids it, because a select routes through its options — and
    such a step can still carry an ``awaits``: ``verify`` is the field a select
    may not have (``model.py``: "'verify' is not allowed on a select step"),
    while ``awaits`` only has to name its probe explicitly there. That is not a
    corner: the steps where a run *waits* are the ones written as selects, so
    "what is this standing still for" belongs to them more than to anywhere
    else. Placing at the end of the step body covers them.

    The packaged copy may already end a step with a one-line pointer comment
    ("the verify lives in the project layer"); a block that begins with the
    same lines replaces them rather than stacking on top.
    """
    pending = dict(blocks)
    out: List[str] = []
    step = None

    def close(step_id) -> None:
        """Place a block for a step that had no ``next:`` to hang it on."""
        if step_id not in pending:
            return
        block = pending.pop(step_id)
        # Behind any blank lines that separate this step from the next one:
        # the block is a field of the step above them, not of the step below.
        end = len(out)
        while end > 0 and not out[end - 1].strip():
            end -= 1
        start = end
        while start > 0 and COMMENT_RE.match(out[start - 1]):
            start -= 1
        trailing = out[start:end]
        if trailing and block[: len(trailing)] == trailing:
            del out[start:end]
            end = start
        out[end:end] = block

    for line in bundled_text.splitlines(keepends=True):
        m = STEP_RE.match(line)
        if m:
            close(step)
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
    close(step)
    if pending:
        raise ValueError(
            "grafted field blocks for steps the packaged copy does not have: "
            + ", ".join(sorted(pending))
        )
    return "".join(out)


def regenerate(name: str, bundled_dir: Path = BUNDLED, project_dir: Path = PROJECT) -> str:
    bundled = (bundled_dir / f"{name}.yaml").read_text(encoding="utf-8")
    project = (project_dir / f"{name}.yaml").read_text(encoding="utf-8")
    return graft(bundled, field_blocks(project))


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
