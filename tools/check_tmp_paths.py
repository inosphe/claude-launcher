"""Refuse a fixed machine-wide temp path in a file this repository executes.

``/tmp`` is one directory for the whole machine here: ``cygpath -w /tmp``
answers the user's Temp directory, and so do ``TMP`` and ``TEMP``. Two
sessions that pick the same file name under it overwrite each other with no
error -- the second reader takes the first writer's bytes for its own, and a
verdict built on those values is wrong in a way that still reads as
reasonable. That happened; ``claunch-shared-tmp-clobber-xjn`` holds the
measurement.

What this check covers, and what it cannot
------------------------------------------
Most of that exposure is in commands an agent types, and no check in this
repository can see those. What it can see is the part the repository owns:
the scripts it runs and the commands its workflows declare. Those run on
every machine and in every session, so a fixed temp path written into one is
a collision that ships. The check refuses it there and leaves prose alone --
``AGENTS.md`` and the workflow rules quote the incident's real file names on
purpose, and a check that flagged the documentation of the bug would be
answered by deleting the documentation.

What passes
-----------
A path that carries the session in it (``$CLAUNCH_SESSION``, ``%s``), a
placeholder in angle brackets, or the exported ``CLAUNCH_SCRATCH`` directory.
Everything a session writes should go to the last of those: the daemon sets
it per session (``daemon/paths.session_scratch_dir``) and creates it before
the session's first command.

Exit codes: 0 nothing to report, 1 at least one fixed path, 2 the repository
could not be read.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

#: Files whose contents are run rather than read. A fixed temp path in one of
#: these is executed on every machine, by every session, with the same name.
CODE_SUFFIXES = {".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".mjs"}

#: Directories searched for those files. ``tests`` is out: a test that names
#: ``/tmp/a.png`` as data never creates it, and the ones here do not.
CODE_ROOTS = ("tools", "src/claude_launcher")

#: Workflow layers. Only the lines that DECLARE A COMMAND are read -- a
#: workflow's prose is where the rule against fixed temp paths is written, and
#: it quotes the form it forbids.
WORKFLOW_ROOTS = ("src/claude_launcher/workflows", ".claunch/workflows")

#: The YAML keys whose value is a command line the daemon or the run executes.
COMMAND_KEYS = ("verify", "restart", "probe", "command", "run", "check")

_COMMAND_LINE = re.compile(
    r"^\s*(?:-\s*)?(" + "|".join(COMMAND_KEYS) + r")\s*:\s*(?P<value>\S.*)$"
)

#: A concrete name under the machine-wide temp root: at least one path
#: character, and nothing in it that varies per session or marks a placeholder.
#: The lookbehind is what keeps a project-relative directory out of it --
#: ``.claunch/tmp/e2e-roundtrip.json`` lives in the checkout and is per
#: project, so it is not this problem and must not be reported as it.
_FIXED_TMP = re.compile(r"(?<![\w.\-/])/tmp/(?P<name>[A-Za-z0-9._-]+)")

#: What makes an occurrence acceptable, checked on the surrounding token.
_VARIES = ("$", "%", "<", "{", "CLAUNCH_SCRATCH")


def _token_around(line: str, start: int, end: int) -> str:
    """The whitespace-delimited token the match sits in.

    The decision is about the whole path, not the matched fragment:
    ``/tmp/$CLAUNCH_SESSION-mine.txt`` matches at ``mine.txt`` and is fine,
    and only the token shows why.
    """
    left = start
    while left > 0 and not line[left - 1].isspace():
        left -= 1
    right = end
    while right < len(line) and not line[right].isspace():
        right += 1
    return line[left:right]


def findings_in(text: str, *, path: str, workflow: bool) -> list[tuple[int, str]]:
    """Every fixed temp path in ``text``, as ``(line number, the line)``."""
    out: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        subject = line
        if workflow:
            matched = _COMMAND_LINE.match(line)
            if not matched:
                continue
            subject = matched.group("value")
        for found in _FIXED_TMP.finditer(subject):
            token = _token_around(subject, found.start(), found.end())
            if any(mark in token for mark in _VARIES):
                continue
            out.append((number, line.strip()))
            break
    return out


def scan(repo: Path) -> list[tuple[str, int, str]]:
    """Walk the covered files and return every finding, path-sorted."""
    found: list[tuple[str, int, str]] = []
    here = Path(__file__).resolve()
    for root in CODE_ROOTS:
        base = repo / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in CODE_SUFFIXES:
                continue
            if path.resolve() == here:
                # This file spells out the form it refuses.
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in findings_in(text, path=str(path), workflow=False):
                found.append((str(path.relative_to(repo)), number, line))
    for root in WORKFLOW_ROOTS:
        base = repo / root
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.yaml")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in findings_in(text, path=str(path), workflow=True):
                found.append((str(path.relative_to(repo)), number, line))
    return found


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--repo",
        default="",
        help="repository root to scan (default: the tree this file is in)",
    )
    args = ap.parse_args(argv)

    # A finding is printed with the offending line in it, and this repository's
    # workflow files are Korean. On a console whose encoding is not UTF-8 the
    # print would raise and the check would report a crash instead of its
    # verdict -- the one outcome a gate must not have.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent
    if not (repo / "src" / "claude_launcher").is_dir():
        print(f"not a claude-launcher checkout: {repo}", file=sys.stderr)
        return 2

    findings = scan(repo)
    if not findings:
        print(
            "no fixed machine-wide temp path in the files this repository "
            "executes (scanned "
            + ", ".join(CODE_ROOTS + WORKFLOW_ROOTS)
            + ")"
        )
        return 0
    print("fixed machine-wide temp paths -- two sessions would share these:")
    for rel, number, line in findings:
        print(f"  {rel}:{number}: {line}")
    print(
        "\nWrite to the session's own directory instead: the daemon exports it "
        "as $CLAUNCH_SCRATCH (see daemon/paths.session_scratch_dir). If a "
        "machine-wide path is genuinely meant, put the session in the name."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
