"""Run the tests this branch's change can actually affect -- and only those.

``improv-worker``'s review step says, and has always said, that the worker
runs a *simplified* suite and that "the full sweep is not run here -- it is
long enough to block the round, and the rule of this formation is that the
leader runs it once per batch at integration time."

The project layer armed that step with
``pytest tests -q -m "not worktree" -n 8`` -- 1450 of the suite's 1558 tests.
The prose said "not the full sweep" and the command ran 93% of it. Six
sessions ran it concurrently on leaving a step, which the same workflow file
warns about in as many words: "the least visible sweep is the least recorded
sweep."

It also produced a specific, repeated confusion. A worker on a clean branch
counts fewer tests than a leader sweeping a shared checkout that holds other
sessions' uncommitted files, and reads its own smaller number as *I broke
something*. That happened; the contaminated 1562 and the clean 1559 were both
circulating as "the reference number" before the axes were named.

So the worker's gate stops being a suite and becomes what the fast-tests
guidance says to run while iterating: only what the change can affect.

Selection, in two rules, both checkable by eye:

1. **A changed test module is selected.** If you touched ``tests/test_x.py``,
   it runs.
2. **A changed module pulls in its same-named test module.** If you touched
   ``src/.../mesh.py`` and ``tests/test_mesh.py`` exists, it runs; likewise
   ``tools/deploy_check.py`` and ``tests/test_deploy_check.py``. This is a
   convention, not a guarantee -- 31 of 79 source modules have a same-named
   test -- so it only ever *widens* the selection and never narrows it.
3. **A changed file that is not python pulls in whatever guards it.** Rule 2
   can only follow a naming convention between ``.py`` files, so without this
   a round that edits only workflow yaml selects nothing -- even though
   several test modules exist for exactly those files. Two halves:

   a. any test module whose source *mentions the file's stem* (a test that
      pins a yaml names it in order to load it), and
   b. the few that find it by globbing its directory and so never name it,
      listed in :data:`GLOB_DISCOVERERS`.

   Rule 3 arrived in two goes, and the second one is the lesson. It was
   first written as a hand-kept list of the guarding tests -- and that list
   was wrong on its first outing, missing ``test_cflow_window``, whose
   assertion about the leader's sweep gate the very same round had broken.
   The gate passed; a full sweep found the regression. A relationship that
   is written down in the files should be read out of the files, so 3a reads
   it, and 3b is kept as small as the cases 3a genuinely cannot see.

Changes are read against the merge base with ``--base`` (default ``master``),
plus anything uncommitted, so the gate covers work that is staged, committed,
or still in the working tree.

**Selecting nothing is a pass, not a failure.** A round can legitimately
change only prose, workflow yaml, or docs -- a real one landed on the day
this was written, editing three yaml files and no tests at all. Making that
red would push workers to fake a test edit to get through the gate. What
stands behind an empty selection is the step's ``done_when``, which asks the
worker for numbers and scenarios in its report; this file's job is to make
sure that whatever *was* touched is green, not to be the whole review.

Exit codes match ``tools/deploy_check.py``: 0 = the selected tests passed (or
there were none), 1 = they failed, 2 = could not tell.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

CANNOT_TELL = 2

#: Parallelism is bounded by how little there is to do. Each xdist worker
#: costs a process spawn, and spawning eight of them to run one module is
#: slower than not spawning them: this suite's cost is processes, not CPU.
#: One module runs serially; more scale to a ceiling of four, well under the
#: measured n=8 optimum for the *whole* suite, because several of these run
#: concurrently across sessions.
MAX_WORKERS = 4

#: Rule 3: directories whose files have guardian tests that no naming
#: convention could find, because the files are not python.
#:
#: The workflow yaml is the case that forced this. Its two copies -- the
#: packaged canonical one and this repository's project layer -- have to stay
#: in step, and when they silently did not, the leader ran for days without a
#: preflight step the package had gained. Nothing was red, because each file
#: on its own was valid. ``test_sync_project_layer`` and
#: ``test_project_layer_override`` are what notice; they just have no
#: same-named source to be pulled in by.
#: Rule 3b: the few tests that reach a file without ever naming it, because
#: they discover it by globbing its directory. Rule 3a cannot see those, and
#: no amount of grepping will make it.
#:
#: This table is hand-maintained, so keep it as small as this. The first
#: version of rule 3 was a hand-written list of *all* the guarding tests, and
#: it was wrong in both directions on its first outing -- it named two tests
#: that do not touch the workflows and missed five that do, including the one
#: whose assertion the same round had just broken. That regression went
#: through the gate and was caught only by a full sweep. Hence 3a: the
#: relationship is in the files, so read it from the files.
GLOB_DISCOVERERS = (
    (
        ("src/claude_launcher/workflows/", ".claunch/workflows/"),
        ("tests/test_sync_project_layer.py",),
    ),
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise LookupError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.strip() or 'no output'}"
        )
    return proc.stdout.strip()


def changed_paths(repo: Path, base: str) -> List[str]:
    """Every path this branch touches: committed since the merge base, or not yet.

    Three sources, unioned. ``base...HEAD`` is the branch's own commits (three
    dots, so a base that moved ahead does not drag its commits in). ``diff
    HEAD`` is tracked-but-uncommitted. ``ls-files --others`` is new files that
    are not staged yet -- a new test module is exactly that, and leaving it
    out would let the gate miss the tests the round just wrote.
    """
    merge_base = _git(repo, "merge-base", base, "HEAD")
    out = set()
    out.update(_git(repo, "diff", "--name-only", f"{merge_base}...HEAD").splitlines())
    out.update(_git(repo, "diff", "--name-only", "HEAD").splitlines())
    out.update(
        _git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    )
    return sorted(p for p in out if p)


def select(repo: Path, paths: List[str]) -> List[str]:
    """The test modules those paths map to, by the two rules in the docstring."""
    picked = set()
    for rel in paths:
        p = Path(rel)
        name = p.name
        if p.parts[:1] == ("tests",) and name.startswith("test_") and name.endswith(".py"):
            if (repo / rel).is_file():  # a deleted test module is not runnable
                picked.add(rel)
            continue
        if p.suffix == ".py" and p.parts[:1] in (("src",), ("tools",)):
            twin = Path("tests") / f"test_{p.stem}.py"
            if (repo / twin).is_file():
                picked.add(twin.as_posix())
            continue
        posix = p.as_posix()
        picked.update(mentioning(repo, p.stem))          # 3a
        for prefixes, guards in GLOB_DISCOVERERS:        # 3b
            if posix.startswith(prefixes):
                picked.update(g for g in guards if (repo / g).is_file())
    return sorted(picked)


def mentioning(repo: Path, stem: str) -> List[str]:
    """Rule 3a: test modules whose source mentions ``stem``.

    Derived rather than remembered. A test that pins a yaml file names it to
    load it, so the reference is in the file and can be read out of it -- no
    table to forget to update when a fourth test starts pinning the same
    workflow.

    Widening only, like rule 2: a test that merely mentions the name in prose
    gets selected and costs a few seconds. The failure worth avoiding is the
    other direction.
    """
    if len(stem) < 4:  # too short to be a distinctive reference
        return []
    hits = []
    for path in sorted((repo / "tests").glob("test_*.py")):
        try:
            if stem in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(f"tests/{path.name}")
        except OSError:
            continue
    return hits


def build_command(files: List[str]) -> List[str]:
    """pytest over exactly ``files``, parallel only when it pays."""
    session = os.environ.get("CLAUNCH_SESSION", "worker")
    cmd = ["uv", "run", "--no-sync", "pytest", *files, "-q"]
    if len(files) > 1:
        cmd += ["-n", str(min(MAX_WORKERS, len(files)))]
    # Short and per-session for the same two reasons the sweep's is: xdist
    # nests popen-gwN/ under it against a 260-char ceiling, and pytest empties
    # its basetemp at startup, so a shared path has concurrent runs deleting
    # each other's temp trees.
    cmd += [f"--basetemp=C:/t/{session}c"]
    return cmd


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="changed_tests", description=__doc__.splitlines()[0]
    )
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument(
        "--base", default="master", help="ref to diff against (default: master)"
    )
    ap.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="print the selection and the command, run nothing",
    )
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    try:
        paths = changed_paths(repo, args.base)
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    files = select(repo, paths)
    if not files:
        print(
            f"no test modules map to this change ({len(paths)} path(s) touched "
            f"vs {args.base}) -- nothing for this gate to run. The step's "
            f"done_when still asks your report for numbers and scenarios."
        )
        return 0

    cmd = build_command(files)
    print(
        f"{len(files)} test module(s) selected from {len(paths)} changed path(s) "
        f"vs {args.base}:"
    )
    for f in files:
        print(f"  {f}")
    print(f"$ {' '.join(cmd)}", flush=True)
    if args.list_only:
        return 0

    return 0 if subprocess.run(cmd, cwd=str(repo)).returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
