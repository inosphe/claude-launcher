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

   a. any test module whose source *refers to the file* (a test that pins a
      yaml names it in order to load it) -- by stem when the stem looks like
      a filename, by full basename otherwise, see :func:`needle`, and
   b. the few that find it by globbing its directory, or drive it across a
      language boundary, and so never name it: :data:`EXPLICIT_GUARDS`.

   Anything no rule can map is **reported** rather than passed over --
   see :func:`unmapped`. An empty selection must be able to mean "I could
   not tell" instead of "there is nothing".

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
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

# A gate runs the tree it is checking. This checkout's ``src`` goes in front of
# every installed copy, so the ``claude_launcher`` imports below resolve HERE --
# whatever the worktree's .venv holds (``uv run --no-sync`` promises never to
# populate it) and whatever else on the path answers to the same name.
# Pinned by tests/test_gates_run_this_checkout.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CANNOT_TELL = 2

#: Parallelism is bounded by how little there is to do. Each xdist worker
#: costs a process spawn, and spawning eight of them to run one module is
#: slower than not spawning them: this suite's cost is processes, not CPU.
#: One module runs serially; more scale to a ceiling of four, well under the
#: measured n=8 optimum for the *whole* suite, because several of these run
#: concurrently across sessions.
MAX_WORKERS = 4

#: Rule 3b: guards that rule 3a provably cannot find, because the reference
#: it would grep for is not there to grep. Two shapes so far:
#:
#: * The test discovers the file by globbing its directory and so never
#:   writes its name (``test_sync_project_layer`` and the workflow layers).
#: * The file is not python at all and its guard drives it from the other
#:   side of a language boundary. ``test_web_topology`` boots ``app.js``
#:   against a stub browser and runs ~40 ``tests/web/*_check.js`` files; no
#:   python imports the asset, so nothing links them but this line.
#:
#: Keep this table small, and add to it only when 3a demonstrably cannot see
#: the pair. The first version of rule 3 was a hand-written list of *all* the
#: guarding tests and was wrong both ways on its first outing -- it named two
#: tests that do not touch the workflows and missed five that do, one of them
#: the test whose assertion that same round had broken. That regression went
#: through the gate; a full sweep found it. So the default is to derive, and
#: this is the documented exception list.
EXPLICIT_GUARDS = (
    (
        ("src/claude_launcher/workflows/", ".claunch/workflows/"),
        ("tests/test_sync_project_layer.py",),
    ),
    (
        ("src/claude_launcher/web/static/", "tests/web/"),
        ("tests/test_web_topology.py",),
    ),
)

#: Shortest stem :func:`needle` will search for on its own. Below this a stem
#: is not a reference, it is a substring -- ``app`` appears in most files in
#: this repository -- so the full basename is used instead.
MIN_STEM = 4


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
        picked.update(mentioning(repo, p.name))          # 3a
        for prefixes, guards in EXPLICIT_GUARDS:          # 3b
            if posix.startswith(prefixes):
                picked.update(g for g in guards if (repo / g).is_file())
    return sorted(picked)


def unmapped(repo: Path, paths: List[str]) -> List[str]:
    """Changed paths no rule could map to a test -- reported, never silent.

    The distinction this exists to keep is the one this whole mesh kept
    relearning today: **an empty result must be able to say "I could not
    tell" rather than "there is nothing".** A gate that answers both with a
    green exit teaches people that green means checked.

    The case that forced it: ``app.js``. Rule 2 skips it (not python), and
    rule 3a will not grep for a three-letter stem that appears in most files
    in the repository, so the selection came back empty and the gate passed
    having run nothing. A whole round of web work would have gone through it
    that way. ``EXPLICIT_GUARDS`` now covers that directory, but the next
    unmapped asset is not covered by anything, and this is what makes it
    visible instead of letting it look like a clean bill of health.
    """
    loose = []
    for rel in paths:
        p = Path(rel)
        if p.suffix == ".py":
            continue                      # rules 1 and 2 own python
        posix = p.as_posix()
        if any(posix.startswith(prefixes) for prefixes, _ in EXPLICIT_GUARDS):
            continue                      # 3b speaks for it
        if mentioning(repo, p.name):
            continue                      # 3a found something
        loose.append(rel)
    return loose


def needle(name: str) -> str:
    """What to grep the tests for, given a changed file's name.

    A bare stem is only a usable search term when it looks like a filename
    rather than a word. ``improv-worker`` does -- a separator or a digit is
    the tell, and a test that pins that yaml writes exactly that string in
    order to load it. ``whatever`` does not: matching it selected fourteen
    modules that merely used the word in a docstring, which is the full suite
    creeping back in by another route.

    So compound names are searched by stem, and everything else by its full
    basename (``style.css``), which only appears where the file is genuinely
    referenced.
    """
    stem = Path(name).stem
    compound = "-" in stem or "_" in stem or any(c.isdigit() for c in stem)
    return stem if (compound and len(stem) >= MIN_STEM) else name


def mentioning(repo: Path, name: str) -> List[str]:
    """Rule 3a: test modules whose source refers to the changed file.

    Derived rather than remembered. A test that pins a yaml file names it in
    order to load it, so the reference is in the file and can be read out of
    it -- there is no table to forget to update when a fourth test starts
    pinning the same workflow.

    Widening only, like rule 2: a test that mentions the name in passing gets
    selected and costs a few seconds. The failure worth avoiding is the other
    direction -- but see :func:`needle` for why "widening only" still has to
    have a limit.
    """
    term = needle(name)
    hits = []
    for path in sorted((repo / "tests").glob("test_*.py")):
        try:
            if term in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(f"tests/{path.name}")
        except OSError:
            continue
    return hits


#: How many of this session's own past basetemps :func:`prune_basetemps`
#: leaves standing. The timeline only has to outlive the round that reads it,
#: and eight is well above the two or three a round ever quotes -- but it is
#: also the guard against pruning a *live* sibling: a hand run and a gate run
#: of the same session overlap routinely (measured by s150), and the live one
#: is always among the newest, so anything at or above two is safe.
KEEP_BASETEMPS = 8


def basetemp_root() -> Path:
    """Where the per-run scratch trees live. Short on purpose -- see below."""
    return Path("C:/t")


def session_basetemp(session: str, *, now: Optional[float] = None) -> str:
    """``C:/t/<session>c<MMDDHHMMSS>`` -- identity in the prefix, generation
    in the suffix.

    The string does three jobs at once and the split is what makes them fit:

    * **scratch** -- pytest's ``tmp_path`` root. pytest *empties* an explicit
      basetemp when the run starts (``_pytest/tmpdir.py`` ``getbasetemp`` ->
      ``rm_rf``), so any two runs that share the path delete each other's
      fixtures. Measured: a run holding a fixture lost it 7.6s in, exactly
      when a second run began; under ``-n`` the wipe lands earlier still,
      at node startup, because xdist's controller calls ``getbasetemp``
      before any test does (``xdist/workermanage.py``).
    * **identity** -- a process scan sees the command line and nothing else,
      so this is how you tell whose pytest that is. That needs the session
      to be *recognisable*, which a stable prefix gives; it never needed the
      whole string to be stable, and treating those as the same thing is
      what cost the third job.
    * **timeline** -- a gate leaves no stdout, so the directory's
      CreationTime, recursive LastWriteTime and ``popen-gw*`` count are the
      only surviving record of when a dead run started, how long it took and
      how wide it ran. A repeated name means the *next* round erases the
      previous round's, at its startup, before anyone reads it.

    A per-session-fixed name solved the first job against concurrent runs and
    silently broke the third against consecutive ones. A generation suffix
    settles both, and costs the second nothing.

    Length: measured ceiling is 48 characters (xdist nests ``popen-gwN/`` and
    the transcript tests fold an absolute cwd back into a filename, so the
    path is ``2*basetemp+162`` against MAX_PATH). ``C:/t/`` + session + ``c``
    + ten digits is 28 for a five-character session -- the suffix spends ten
    of the twenty-odd characters that were spare.
    """
    stamp = time.strftime("%m%d%H%M%S", time.localtime(now))
    return f"{basetemp_root().as_posix()}/{session}c{stamp}"


def prune_basetemps(session: str, *, keep: int = KEEP_BASETEMPS) -> List[Path]:
    """Drop this session's oldest scratch trees, keeping the newest ``keep``.

    Giving every run its own name means nothing ever reuses -- and therefore
    nothing ever reclaims -- a directory: with an explicit basetemp pytest
    skips its own end-of-session cleanup too (``_pytest/tmpdir.py``
    ``pytest_sessionfinish`` requires ``_given_basetemp is None``). Measured
    on this machine: 431 top-level directories under ``C:/t``, the oldest six
    days old, none of which anything was ever going to remove.

    Only ``<session>c*`` is considered. Another session's timeline is that
    session's evidence and is never this function's to delete. Failures are
    swallowed: a locked directory is a live run or an open handle, and a gate
    that goes red because housekeeping lost a race is worse than one that
    leaves a directory behind.
    """
    root = basetemp_root()
    try:
        mine = sorted(p for p in root.glob(f"{session}c*") if p.is_dir())
    except OSError:
        return []
    dropped = []
    for path in mine[: max(0, len(mine) - keep)]:
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        dropped.append(path)
    return dropped


def build_command(files: List[str], *, basetemp: Optional[str] = None) -> List[str]:
    """pytest over exactly ``files``, parallel only when it pays."""
    session = os.environ.get("CLAUNCH_SESSION", "worker")
    cmd = ["uv", "run", "--no-sync", "pytest", *files, "-q"]
    if len(files) > 1:
        cmd += ["-n", str(min(MAX_WORKERS, len(files)))]
    cmd += [f"--basetemp={basetemp or session_basetemp(session)}"]
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
    ap.add_argument(
        "--basetemp",
        default=None,
        help=(
            "pytest scratch root for this run (default: a fresh "
            "C:/t/<session>c<MMDDHHMMSS>). Pass one to pin a run's timeline "
            "to a name you chose; it must not be a path another run is using, "
            "because pytest empties it at startup"
        ),
    )
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    try:
        paths = changed_paths(repo, args.base)
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    files = select(repo, paths)
    loose = unmapped(repo, paths)
    if loose:
        # Printed whether or not anything was selected: a change can map
        # partly, and the mapped part passing is exactly what makes the
        # unmapped part easy to miss.
        print(
            f"WARNING: {len(loose)} changed path(s) map to no test module. "
            f"This gate says nothing about them -- not that they are fine:",
            file=sys.stderr,
        )
        for rel in loose:
            print(f"  {rel}", file=sys.stderr)
        print(
            "  Check by hand (grep -rl '<filename>' tests/) and, if something "
            "guards them, add it to EXPLICIT_GUARDS in this file. If the "
            "change is broad, ask the leader for a full sweep.",
            file=sys.stderr,
        )

    if not files:
        print(
            f"no test modules map to this change ({len(paths)} path(s) touched "
            f"vs {args.base}) -- nothing for this gate to run. The step's "
            f"done_when still asks your report for numbers and scenarios."
        )
        return 0

    cmd = build_command(files, basetemp=args.basetemp)
    print(
        f"{len(files)} test module(s) selected from {len(paths)} changed path(s) "
        f"vs {args.base}:"
    )
    for f in files:
        print(f"  {f}")
    print(f"$ {' '.join(cmd)}", flush=True)
    if args.list_only:
        return 0

    # Reclaim before the run, not after: the directory this run is about to
    # create is the newest and so is never a candidate, and a run that dies
    # still leaves its own tree behind to be read.
    session = os.environ.get("CLAUNCH_SESSION", "worker")
    for gone in prune_basetemps(session):
        print(f"pruned old basetemp: {gone}", file=sys.stderr)

    return 0 if subprocess.run(cmd, cwd=str(repo)).returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
