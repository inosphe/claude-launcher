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

**The same tree is judged once.** Before running anything the gate hashes the
content pytest is about to read (:func:`worktree_tree` -- the working tree,
not ``HEAD``, because uncommitted work is the point of this tool) and looks
for a green receipt filed under that hash *and* this selection. If one is
there it prints what it stood on and runs nothing; when it does run, it
leaves the receipt behind.

That is the largest measured waste in a worker's round, and it is not an
estimate. s150 ran sixteen modules by hand in 578s and the same round's gate
ran the same selection over the same tree again for 656s. s147 ran one
fourteen-module selection five times in a single round. s144 saw the same
doubling. The mechanism to stop it was already in ``tools/sweep.py``
(``find_receipt_by_tree``, keyed by tree so a different sha with identical
content is the same verdict) and this file simply did not call it -- ``grep
receipt tools/changed_tests.py`` returned nothing.

Three things make the reuse safe, and each is load-bearing:

* **The key is the tree, and the tree includes uncommitted files.** A sweep
  can key by commit because it refuses to run on a dirty checkout; this gate
  exists to judge dirty checkouts, so the dirt has to be *inside* the key
  instead of excluded from it. Any edit changes the hash and the receipt no
  longer answers.
* **The key includes the selection.** Same tree, fewer modules, is a
  different verdict and must not stand in for a larger one (``claunch-p5n``).
* **Only green carries over**, by ``sweep.is_green``, so the two gates cannot
  drift on what green means. A red receipt is not a verdict to launder and
  not a reason to refuse to re-run either: the fix loop needs the re-run, and
  the moment the fix is typed the tree is different anyway.

And the premise underneath both gates -- that no test can tell two same-tree
commits apart -- stopped being a hand-grepped sentence:
``tests/_repo_history_guard.py`` refuses a read of this repository's HEAD or
refs at the ``subprocess`` call that makes it.

``--check`` reads the receipt and never runs. That is the door for a
**peer reviewer**, who is a different session, past its own intake scan, and
whose confirming run would therefore be concurrent load that no scan counted
-- which is where two sessions have already met xdist node-down and OSError
22 and come away with no verdict at all. A reviewer reads the receipt, or
abstains; either is cheaper than a run nobody budgeted.

What the reviewer has to stand on for that to work is the *same content*, and
the key says so rather than trusting it: a clean checkout of the author's
commit hashes to what the author's gate hashed, and anything uncommitted on
either side hashes to something else and abstains. That is the right answer,
not a limitation -- a reviewer looking at a different tree than the one that
was judged has no verdict, and should say so.

Exit codes match ``tools/deploy_check.py``: 0 = the selected tests passed (or
there were none), 1 = they failed, 2 = could not tell -- which is what
``--check`` returns when no receipt answers, because "nobody has run this"
and "this is fine" are the two things a gate must never spell the same way.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# A gate runs the tree it is checking. This checkout's ``src`` goes in front of
# every installed copy, so the ``claude_launcher`` imports below resolve HERE --
# whatever the worktree's .venv holds (``uv run --no-sync`` promises never to
# populate it) and whatever else on the path answers to the same name.
# Pinned by tests/test_gates_run_this_checkout.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _load_sweep():
    """``tools/sweep.py``, by path -- ``tools`` is not on ``pythonpath``.

    Imported rather than copied because a receipt is a *format*, and
    ``sweep.py`` says in as many words why its two halves live in one file:
    "a format split across two files drifts". This is a third half, so it
    borrows the same ``repo_key``/``receipts_dir``/``is_green``/``parse_counts``
    instead of growing its own opinion about any of them.
    """
    spec = importlib.util.spec_from_file_location(
        "sweep", Path(__file__).resolve().parent / "sweep.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep = _load_sweep()

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


def _git(repo: Path, *args: str, env: Optional[dict] = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env
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
    + ten digits is 21 for a five-character session, against the old fixed
    name's 11 -- the generation spends ten of the 37 characters that were
    spare, and pinned tests hold both numbers.
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

    Ordering is by name, which is chronological because the suffix leads with
    the month -- except across a new year, where January sorts below the
    December it follows. The cost of that is bounded and self-clearing: at
    worst the first few runs of a year keep stale trees and drop fresh ones,
    and the window walks itself straight again within ``keep`` runs. Sorting
    by mtime would be correct there and wrong here, where several of these
    are created inside one second.
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


# --------------------------------------------------------------------------- #
# receipts: so the same tree is not judged twice
# --------------------------------------------------------------------------- #
def worktree_tree(repo: Path) -> str:
    """The tree hash of the content pytest is about to read.

    Not ``HEAD^{tree}``. This gate's whole job is to judge work that is
    *uncommitted* -- ``changed_paths`` unions three sources for exactly that
    reason -- so a key that ignored the working tree would hand one edit's
    verdict to the next one, which is worse than running twice.

    Built in a scratch index so the real one is untouched: ``add -A`` stages
    tracked and untracked-but-not-ignored content into it, and ``write-tree``
    names the result. That is the same content pytest collects, minus what
    ``.gitignore`` already excludes from both.

    The scratch index starts EMPTY -- this used to copy the repository's
    index for its stat cache (claunch-fej8), and that cache is exactly what
    must not be trusted here. Git calls a file clean without reading it when
    the cached mtime second and size both match, so a rewrite inside the
    cached stat's second, at the same size, is invisible and the tree
    carries the PRE-edit blob (claunch-vuta: a green verdict filed under the
    tree of the edit *before* the one being judged; the two writes landed
    0.46s apart, ``x = 1`` and ``x = 2`` both seven bytes with CRLF, and the
    gate's own ``git status`` had refreshed the real index past the second
    boundary, lifting the racy guard that would otherwise have caught it).
    Nudging the copied index's mtime to force every entry racy did not hold
    up under test -- git's behaviour there did not match the model -- so the
    stat cache is not distrusted, it is absent: with no entries at all,
    ``add`` hashes every file from disk. Measured on this repository:
    0.21s against 0.12s with the copy -- the files are warm, because pytest
    is about to read exactly these.

    Why not keep the copy and preserve its mtime (claunch-vuta vs
    claunch-gate-receipt-key-mismatch-t6zp): that half-measure is real and
    it is not enough. Keeping the stat cache is keeping git's licence to
    answer "unchanged" from ``lstat`` alone -- and that comparison is
    weaker than it looks: here it is size plus mtime at one-second
    granularity (``st_ino`` is 0 and ``st_ctime`` is the *creation* time,
    so neither discriminates). Git's one guard is the racy-clean rule: an
    entry whose cached mtime is not older than the index file's own mtime
    may not be trusted on stat and has its content re-read
    (``read-cache.c``, ``is_racy_timestamp``). The guard is dated relative
    to *the index*, so a copy stamped with ``now`` drops every entry into
    the copy's past and switches the guard off wholesale -- measured, git
    2.48.1.windows.1, 24 runs across four timings: ``copyfile`` alone wrong
    23/24 and wrong by returning exactly ``HEAD^{tree}``, mtime-preserving
    copy right 24/24 against a from-empty-index tree.

    But the guard it re-arms only fires while the entry is racy, and an
    entry can be stale without being racy. Git protects the common path
    itself -- writing the index smudges a racily-clean entry's cached size
    to 0, so it is re-read forever after -- and that protection is bypassed
    by any restore that preserves mtime (``cp -p``, ``tar -x``, ``rsync
    -t``, ``unzip``). Measured on this repository with the mtime-preserving
    copy in place: after ``add``/``commit``, a one-second wait, and a plain
    ``git status`` (which rehashes the entry and rewrites the index a
    second later, so nothing is racy any more), restoring different content
    of the same size under the old mtime leaves ``git status --porcelain``
    empty and this function returning ``HEAD^{tree}`` -- the value the
    first paragraph forbids. From an empty index, the same scenario returns
    the working tree's real hash. The stat cache is therefore not
    distrusted here, it is absent.

    That is not a test artefact. A same-size edit landing in the wrong
    second is handed the previous tree's key, and the previous tree's green
    receipt is then reused for content nobody ran -- the false green the
    whole filing scheme exists to make impossible, and a strictly worse
    failure than the mismatched lookup that led here: a wrong key costs one
    extra run, this costs the run itself.

    Side effect worth knowing: ``add -A`` writes blobs for uncommitted files
    into the object database, the way ``git stash create`` does. They are
    loose objects nothing references, and gc collects them.
    """
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"      # absent on purpose: see above
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        _git(repo, "add", "-A", env=env)
        return _git(repo, "write-tree", env=env)


def selection_key(files: List[str]) -> str:
    """Which modules a receipt speaks for -- part of the key, not a detail.

    Two runs over the same tree that selected different modules are two
    different verdicts, and the smaller one must never answer for the larger
    (``claunch-p5n`` names this explicitly). Folding the selection into the
    key makes that impossible rather than merely discouraged.
    """
    return hashlib.sha1("\n".join(sorted(files)).encode("utf-8")).hexdigest()[:12]


def receipts_dir(repo: Path, override: Optional[Path] = None) -> Path:
    """Where a *targeted* receipt lives -- deliberately not where sweeps live.

    ``sweep.find_receipt_by_tree`` globs ``receipts_dir/*.json`` and accepts
    any green receipt whose ``tree`` matches. Filing sixteen modules' verdict
    in that directory would therefore let ``sweep.py check`` read it as a
    verdict about the whole suite: a false green of exactly the shape this
    repository keeps having to relearn. A subdirectory is invisible to a
    non-recursive glob, so the two kinds cannot be confused by accident.
    """
    return sweep.receipts_dir(repo, override) / "changed"


def receipt_path(
    repo: Path, tree: str, files: List[str], override: Optional[Path] = None
) -> Path:
    return receipts_dir(repo, override) / f"{tree}-{selection_key(files)}.json"


def find_receipt(
    repo: Path, tree: str, files: List[str], override: Optional[Path] = None
) -> Optional[dict]:
    """The green receipt that already answers for this tree and selection.

    An exact key, so there is no "is this close enough" judgement and no
    newest-wins scan: a receipt either was recorded for this content and this
    selection or it was not. A receipt whose selection is a strict *superset*
    would also answer, and is deliberately not looked for -- the win this
    exists to collect is the same round running the same thing twice, where
    the selections are equal by construction.

    Red receipts are not reused. ``sweep.is_green`` is the judge, so the two
    gates cannot drift on what "green" means -- except on one axis, on
    purpose: ``is_green`` also refuses a receipt marked ``dirty``, and a
    targeted receipt is *always* dirty in the sweep's sense. That refusal
    exists because a sweep is keyed by commit, so uncommitted files are
    outside its key; here they are inside it, hashed into ``tree``. So the
    field is recorded for the reader and left out of the verdict.

    A receipt at the exact path that cannot be *read* is warned to stderr and
    treated as absent -- the changed_tests echo of ``sweep``'s rule on the
    same shape, see ``sweep._newest_green``. A silent skip here is a round
    re-measuring what it had already measured.
    """
    path = receipt_path(repo, tree, files, override)
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # The exact receipt that would answer for this tree and selection
        # exists but cannot be read. That used to be a silent None -- the same
        # defect ``sweep._newest_green`` had, in the same shape: a verdict
        # quietly thrown away, forcing a round to re-measure what it had
        # already measured, with no way to see why.
        print(
            f"WARNING: {path} cannot be read as a receipt ({exc}); the exact "
            f"receipt that would answer for this tree and selection is "
            f"unusable. Treating this tree as unswept.",
            file=sys.stderr,
        )
        return None
    if receipt.get("tree") != tree or sorted(receipt.get("selection") or []) != sorted(
        files
    ):
        return None                       # a hash collision, or a hand-edited file
    return receipt if sweep.is_green({**receipt, "dirty": False}) else None


def describe(receipt: dict) -> str:
    """What was stood on, named -- never inferred.

    ``sweep.py``'s ``check`` prints the sha of the receipt it accepted when
    that sha is not the one being gated, and this does the same for the same
    reason: a gate that passes without running has to say what it passed on,
    or it is indistinguishable from a gate that did nothing.
    """
    counts = receipt.get("counts") or {}
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts"
    return (
        f"{summary} in {receipt.get('seconds')}s, run by "
        f"{receipt.get('session')} at {receipt.get('finished_at')} "
        f"on commit {(receipt.get('commit') or '?')[:12]}"
    )


def build_command(files: List[str], *, basetemp: Optional[str] = None) -> List[str]:
    """pytest over exactly ``files``, parallel only when it pays."""
    session = os.environ.get("CLAUNCH_SESSION", "worker")
    cmd = ["uv", "run", "--no-sync", "pytest", *files, "-q"]
    if len(files) > 1:
        cmd += ["-n", str(min(MAX_WORKERS, len(files)))]
    cmd += [f"--basetemp={basetemp or session_basetemp(session)}"]
    return cmd


def run_and_record(
    repo: Path,
    files: List[str],
    cmd: List[str],
    tree: Optional[str],
    base: str,
    override: Optional[Path] = None,
) -> int:
    """Run the selection, and leave a receipt whatever the outcome.

    The receipt is written for red runs too. ``claunch-p5n`` is the case: a
    session restarted, the background shell's record went with it, the pytest
    process had run to completion and the verdict was unrecoverable -- so the
    round re-measured, and the re-measurement collided with the generation
    before it. A verdict that only exists in a terminal is a verdict one
    process death away from costing another full run.

    Output is teed rather than captured. ``sweep.py`` can capture because
    nobody watches a sweep; a worker watches this one, and taking its
    progress away to gain a count would trade the thing it is for the thing
    it records.
    """
    started = datetime.now(timezone.utc)
    lines: List[str] = []
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    code = proc.wait()
    finished = datetime.now(timezone.utc)

    if tree is None:
        return 0 if code == 0 else 1      # no key, so nothing to file it under

    output = "".join(lines)
    receipt = {
        "kind": "changed_tests",
        "tree": tree,
        "selection": sorted(files),
        "base": base,
        "repo": str(repo),
        "command": cmd,
        "exit_code": code,
        "counts": sweep.parse_counts(output),
        "failures": sweep._failure_lines(output),
        "session": os.environ.get("CLAUNCH_SESSION", "worker"),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "seconds": round((finished - started).total_seconds(), 1),
    }
    try:
        receipt["commit"] = _git(repo, "rev-parse", "HEAD")
    except LookupError:
        receipt["commit"] = None          # an address for the reader, not the key
    dest = receipt_path(repo, tree, files, override)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(f"receipt: {dest}")
    except OSError as exc:
        # Never turn a finished run into a failure over its bookkeeping.
        print(f"WARNING: could not write the receipt: {exc}", file=sys.stderr)
    return 0 if code == 0 else 1


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
    ap.add_argument(
        "--check",
        action="store_true",
        help="read the receipt only, never run: 0 green, 2 no receipt (abstain)",
    )
    ap.add_argument(
        "--no-reuse",
        action="store_true",
        help="run even if a green receipt already answers for this tree",
    )
    ap.add_argument(
        "--receipts",
        type=Path,
        default=None,
        help="override the receipt root (tests)",
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
        return 0                          # a pass, for --check too: see below

    cmd = build_command(files, basetemp=args.basetemp)
    print(
        f"{len(files)} test module(s) selected from {len(paths)} changed path(s) "
        f"vs {args.base}:"
    )
    for f in files:
        print(f"  {f}")

    # The key, before anything is run. A failure here costs the reuse, never
    # the gate: standing on no receipt is the behaviour this tool has always
    # had, and turning a runnable selection into "cannot tell" over a git
    # call would be a worse gate than the one being improved.
    tree: Optional[str] = None
    try:
        tree = worktree_tree(repo)
    except (LookupError, OSError) as exc:
        print(
            f"WARNING: no tree hash for this working tree ({exc}); running "
            f"without receipt reuse.",
            file=sys.stderr,
        )

    found = (
        find_receipt(repo, tree, files, args.receipts) if tree is not None else None
    )

    if args.check:
        # The reviewer's door. A peer-review responder is a different session
        # past its own intake scan, so a confirming run of theirs is load no
        # scan counted -- which is where s155 and s148 met xdist node-down and
        # OSError 22, i.e. no verdict at all. Reading the receipt costs them
        # nothing and costs the machine nothing.
        if found is None:
            print(
                f"no green receipt for tree {(tree or '?')[:12]} and this "
                f"{len(files)}-module selection -- abstain, or ask the author "
                f"to run 'python tools/changed_tests.py --base {args.base}'. "
                f"Do not run the selection yourself: it is load nobody's scan "
                f"counted.",
                file=sys.stderr,
            )
            return CANNOT_TELL
        print(f"green receipt for tree {tree[:12]}: {describe(found)}")
        return 0

    if args.list_only:
        print(f"$ {' '.join(cmd)}", flush=True)
        return 0

    if found is not None and not args.no_reuse:
        # The measured waste this exists to remove: s150 ran these same
        # modules by hand for 578s and the gate immediately ran them again
        # for 656s over an identical tree; s147 ran one selection five times
        # in a round. Nothing is inferred here -- what was stood on is named.
        print(
            f"not running: tree {tree[:12]} with this selection is already "
            f"green.\n  {describe(found)}\n"
            f"  receipt: {receipt_path(repo, tree, files, args.receipts)}\n"
            f"  (--no-reuse runs it anyway; any edit changes the tree and so "
            f"the key)"
        )
        return 0

    # Reclaim before the run, not after: the directory this run is about to
    # create is the newest and so is never a candidate, and a run that dies
    # still leaves its own tree behind to be read. Below the reuse check, not
    # above it: a round that stands on a receipt starts no run, so it has no
    # claim on anybody's timeline -- least of all its own live sibling's.
    session = os.environ.get("CLAUNCH_SESSION", "worker")
    for gone in prune_basetemps(session):
        print(f"pruned old basetemp: {gone}", file=sys.stderr)

    print(f"$ {' '.join(cmd)}", flush=True)
    return run_and_record(repo, files, cmd, tree, args.base, args.receipts)


if __name__ == "__main__":
    raise SystemExit(main())
