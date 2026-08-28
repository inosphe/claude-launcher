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
   convention, not a guarantee -- 47 of 107 source modules have a same-named
   test -- so it only ever *widens* the selection and never narrows it.

   The convention standing alone was the second hole this file grew, and it
   was measured four times before it was closed. ``daemon/cflow_clock.py`` has
   no ``tests/test_cflow_clock.py``, so a round that rewrote it selected
   **nothing** and exited 0; ten test modules import it. Three more rounds
   found the same shape on ``daemon/mesh.py`` (1 selected, 31 import it -- and
   ``test_mesh_wire.py``, which was not selected, held three real failures),
   ``daemon/screen.py`` (1 against 6) and ``cli_sessions.py`` (1 against 7).
   So rule 2 has two more halves, and both derive rather than remember:

   a. **2b -- whoever imports it.** :func:`importers` parses the test modules
      and selects the ones whose imports name the changed module. Direct
      imports only; :func:`importers` carries the measurement for why the
      transitive closure is not an option.
   b. **2c -- whoever names it.** :func:`mentioning`, rule 3a's text search,
      applied to python too. It is what catches a guard that pins a file by
      string rather than importing it -- ``test_delivery_contract`` keys a
      table on ``("cli_sessions.py", "_cmd_send_keys")`` and imports nothing.
      Dunder files are excluded, see :func:`_is_dunder`.
3. **A changed file that is not python pulls in whatever guards it.** Rules 2
   and 2b need a python module to follow, so without this a round that edits
   only workflow yaml selects nothing -- even though several test modules
   exist for exactly those files. Two halves:

   a. any test module whose source *refers to the file* (a test that pins a
      yaml names it in order to load it) -- by stem when the stem looks like
      a filename, by full basename otherwise, see :func:`needle`, and
   b. the few that find it by globbing its directory, or drive it across a
      language boundary, and so never name it: :data:`EXPLICIT_GUARDS`.

   Rule 3 arrived in two goes, and the second one is the lesson. It was
   first written as a hand-kept list of the guarding tests -- and that list
   was wrong on its first outing, missing ``test_cflow_window``, whose
   assertion about the leader's sweep gate the very same round had broken.
   The gate passed; a full sweep found the regression. A relationship that
   is written down in the files should be read out of the files, so 3a reads
   it, and 3b is kept as small as the cases 3a genuinely cannot see.

**A selection that is not empty can still be missing the module that
matters**, and that is the one failure none of the devices below sees: they
all ask whether the selection came back *empty*. ``cli_sessions.py`` selects
nine test modules and not ``test_daemon_wedge``, which is the one that guards
it, because ``test_daemon_wedge`` imports ``claude_launcher.cli`` and ``cli``
is what imports ``cli_sessions`` -- one hop past rule 2b. A red batch landed
through exactly that (``claunch-uf7m``). :func:`reached_indirectly` names those
modules without running them; widening the rule to select them was measured
and costs a median 26 of 119 modules against the current 3.

**A path no rule can map is reported, never passed over** --
:func:`unmapped`, derived from :func:`map_one` so that it cannot answer
differently from the selection. An empty selection must be able to mean "I
could not tell" instead of "there is nothing", and for python it could not:
``unmapped`` skipped every ``.py`` on the grounds that rules 1 and 2 owned
them, while rule 2 only speaks when a same-named test happens to exist. A
source module without one was selected by nothing and reported by nothing. A
batch landed three red modules through that gap and two more batches shipped
on top before a bisect found them; :func:`map_one` carries the case.

Changes are read against the merge base with ``--base`` (default ``master``),
plus anything uncommitted, so the gate covers work that is staged, committed,
or still in the working tree. ``--base auto`` reads the branch's upstream
instead of a fixed ref, which is what a worker stacked on an integration
branch needs: measured against master such a branch counts the entire batch
as its own change, and three sessions in one day measured that at 78-93% of
the suite -- the sweep this gate exists not to be. An upstream that is only
this branch's own remote copy is not such a branch and is skipped, because
measuring against it counts everything unpushed as this round's change. See
:func:`resolve_base`.
The runner's own scratch lock is not counted as a change; see
:data:`RUNNER_LOCK_RE`.

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

**Both caveats are stated either way.** "Nothing went unmapped" and "nothing
sits one hop out" are printed as lines rather than left as absent blocks,
because an absent block also means "this build has no such check" -- and the
two readings are the difference between a swept axis and one nobody ran. The
gate exists to keep exactly that pair apart, so it should not reproduce it in
its own output.

**stdout carries the verdict and its caveats; stderr carries only this
tool's own trouble.** See :data:`STREAMS`, which ``--help`` prints. The two
caveats -- paths no rule could map, and modules one hop past rule 2b -- are
the part a landing request has to quote, so they go where the answer goes.

That holds for every exit code, and the rule was written wrong the first time:
both of ``CANNOT_TELL``'s paths were on stderr, so an abstaining ``--check``
put its *selection* on stdout and its reason for abstaining on the other
stream -- and its own green twin, ``green receipt for tree ...``, was already
on stdout. One verdict split across two streams by outcome. A procedure
reading stdout alone saw a healthy-looking selection line and exit 2 with
nothing to say why (merger-r5, 2026-08-27, measured on ``54f537b``).

Exit codes match ``tools/deploy_check.py``: 0 = the selected tests passed (or
there were none), 1 = they failed, 2 = could not tell -- which is what
``--check`` returns when no receipt answers, because "nobody has run this"
and "this is fine" are the two things a gate must never spell the same way.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
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

#: Which stream carries what, printed by ``--help`` because a reader who
#: keeps only one of them reads the other's contents as "none".
#:
#: A landing procedure that split the streams to avoid a pipe was reading
#: ``out.txt`` alone and treating ``err.txt`` as discardable -- reasonably, on
#: the evidence, since the only thing that had ever been in it was uv's
#: ``VIRTUAL_ENV`` line. Both of this gate's caveats were on the discarded
#: side, and the caveat the leader now requires a landing request to quote is
#: one of them. Unread and absent spell the same, which is this file's whole
#: subject (worker-select / merger-r5, 2026-08-27, ``claunch-a9t``).
STREAMS = """\
streams: stdout carries the verdict and every caveat on it -- for all three
exit codes. That is the selection, the pytest command, the paths no rule could
map, the modules one hop out that were not selected, and both halves of what
--check answers (a green receipt, or why it is abstaining). stderr carries only
this tool's own trouble, which never decides the exit code: a receipt it could
not read or write, a working tree it could not hash, an old basetemp it pruned.
Read stdout to learn what the gate did and did not cover; a procedure that
keeps only stdout loses nothing it needs.
"""

#: What ``--base`` falls back to when nothing better is known.
DEFAULT_BASE = "master"

#: ``--base auto``: ask git for the branch this one integrates into instead of
#: pinning one -- which is not the same as asking for the upstream, since a
#: branch's own remote copy is an upstream and is no such branch. See
#: :func:`resolve_base`.
BASE_AUTO = "auto"

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

#: ``uv run`` is how every gate in this repository is invoked, and while it
#: holds the project lock it writes a zero-byte ``uv-<hash>.lock`` into the
#: project root. It is untracked, so ``ls-files --others`` counts it as a
#: changed path and the gate then warns that a file *its own launcher* made
#: one second ago is guarded by no test.
#:
#: Measured by worker-64hs (2026-08-27, ``claunch-uv-lock-counted-as-changed-
#: path-62yg``): the same tree at the same base read **4** changed paths under
#: ``uv run`` and **3** under ``.venv/Scripts/python.exe``, the warning present
#: in the first and absent in the second. It reproduces on a checkout that
#: ``git status --porcelain`` calls clean, and deleting the file does not help
#: -- the next ``uv run`` writes another one.
#:
#: The cost of leaving it in is not a wrong selection (no test maps to it
#: either way) but a warning that is always there. The warning's next line
#: tells the reader to check the path by hand and consider asking for a full
#: sweep, and a warning that fires every single run stops being read -- which
#: is the state a genuinely unguarded path would arrive into.
#:
#: Only this one shape is dropped, and only at the root. Untracked files stay
#: in the changed set otherwise: a test module written this round and not yet
#: added is exactly an untracked file, and is the case :func:`changed_paths`
#: was widened to catch.
RUNNER_LOCK_RE = re.compile(r"^uv-[0-9a-f]+\.lock$")

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


def resolve_base(repo: Path, base: str) -> tuple:
    """The ref to measure against, and how we came to think so.

    ``--base master`` is right for a branch cut from master and wrong for a
    branch cut from an integration branch, which is the shape this formation
    puts every worker in. Against master such a branch reads the whole batch
    as its own change: three sessions measured the same gate on the same day
    at 93/119 modules, 95/119 and 1945 tests in 221s, for rounds that had
    touched four to six files (``claunch-eghh``). The gate's own step forbids
    running the suite; with a fixed base the gate was how the suite got run.

    The branch a worker integrates into is not something the workflow file can
    write down -- it differs per worker and per round -- but it is something
    git records, under the name the alignment gate already reads:
    ``tools/merge_ready.py::_target`` resolves ``<branch>@{upstream}`` for
    exactly this question. Reading the same ref means the selection and the
    alignment check describe the same target instead of two.

    Not every upstream is an integration branch, though. ``master@{upstream}``
    resolves to ``origin/master``, which is not a branch master integrates
    into -- it is where master is pushed. Reading it as the axis makes the
    selection "everything the repository changed since the last push", and on
    this repository that measured **102 changed paths -> 114 modules** against
    an ``origin/master`` 110 commits behind the local branch, for a session
    that had committed nothing (``claunch-d4yo``, s262/s256/s259). Both
    remotes held the same commit, so no fetch reaches it.

    Git already records the difference, and it needs no threshold and no
    ancestry test. ``branch.<X>.merge`` is the ref on the far side that ``<X>``
    is paired with: ``refs/heads/<X>`` means the pairing is this branch's own
    remote copy, anything else names a different branch. Both alternatives
    were measured over all 285 local branches and both misread this repository:

    * ancestry ("upstream is an ancestor of HEAD") also catches three real
      stacked branches -- ``s127-7w7g-gate-dirty``, ``s127-qj03-doc-body`` and
      ``s217-wf-followup`` -- because a parent being an ancestor of its child
      is what a stack *is*.
    * a threshold on how far behind the upstream is separates them today
      (111 against 1-5) but makes the number the reason, and the number moves.

    ``branch.<X>.merge`` catches ``master`` alone and leaves all eleven
    stacked branches on their upstream.

    Four answers, and two of them have to be loud:

    ``given``
        an explicit ``--base X``. Unchanged, and still the default.
    ``upstream``
        ``--base auto`` and the branch has one that names a different branch.
    ``self-tracking``
        ``--base auto`` and the upstream is this branch's own remote copy. That
        is a push destination, not something to measure against, so it falls
        back to master and says so -- otherwise the output reads ``vs master``
        with no way to tell that ``auto`` was even asked. Exit code unchanged.
    ``no-upstream``
        ``--base auto`` and it does not. Falling back to master is right for a
        branch cut from master and is the original defect for a stacked one,
        and nothing here can tell those apart -- so it falls back and says so,
        naming the one command that settles it. Exit code is unchanged: a
        missing upstream is a thing to fix, not a reason to refuse a verdict.
    """
    if base != BASE_AUTO:
        return base, "given"
    try:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    except LookupError:
        return DEFAULT_BASE, "no-upstream"
    # Not through _git: no upstream is an ordinary answer here, not a failure.
    proc = subprocess.run(
        [
            "git", "-C", str(repo), "rev-parse", "--abbrev-ref",
            "--symbolic-full-name", f"{branch}@{{upstream}}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        # Same reason: a branch with no ``merge`` configured is an ordinary
        # answer, and reading it must not turn into a failure.
        paired = subprocess.run(
            ["git", "-C", str(repo), "config", f"branch.{branch}.merge"],
            capture_output=True,
            text=True,
        )
        if paired.stdout.strip() == f"refs/heads/{branch}":
            return DEFAULT_BASE, "self-tracking"
        return proc.stdout.strip(), "upstream"
    return DEFAULT_BASE, "no-upstream"


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
        rel
        for rel in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
        if not RUNNER_LOCK_RE.match(rel)   # the runner's own scratch, see above
    )
    return sorted(p for p in out if p)


def _is_test_module(p: Path) -> bool:
    """``tests/test_x.py`` -- rule 1's shape, asked from two places."""
    return (
        p.parts[:1] == ("tests",)
        and p.name.startswith("test_")
        and p.name.endswith(".py")
    )


def map_one(repo: Path, rel: str) -> List[str]:
    """The test modules **one** changed path maps to, by the rules above.

    :func:`select` is the union of this over the change and :func:`unmapped`
    is the paths for which it comes back empty. They read one mapping rather
    than keeping two, because for four rounds they kept two and the two did
    not agree.

    ``unmapped`` answered for python by assuming: ``if p.suffix == ".py":
    continue  # rules 1 and 2 own python``. Ownership needs rule 2 to always
    produce something, and rule 2 is a naming convention -- 54 of this
    repository's 102 source modules have no same-named test. Where the
    convention was absent the selection was empty *and* the report was silent,
    so the path appeared in neither half of the output. Nothing said the gate
    had not looked at it.

    That combination landed a red batch. ``bd87fdc`` changed
    ``cli_sessions.py``, which has no ``tests/test_cli_sessions.py``; the gate
    selected nothing for it and printed nothing about it; the batch carried
    three red modules and two further batches shipped on top before a bisect
    found them (``claunch-uf7m``). Rules 2b and 2c have since closed the
    *selection* half for that particular file -- seven test modules import it
    -- but they close it by convention too, and eight of this repository's
    source modules still map to nothing at all. This is what makes those eight
    audible.

    Deriving both halves from one mapping makes the silent case unreachable:
    a path either names test modules here, or it is reported.
    """
    p = Path(rel)
    if _is_test_module(p):                                # 1
        return [rel] if (repo / rel).is_file() else []    # deleted: not runnable
    picked = set()
    if p.suffix == ".py" and p.parts[:1] in (("src",), ("tools",)):
        twin = Path("tests") / f"test_{p.stem}.py"
        if (repo / twin).is_file():                       # 2
            picked.add(twin.as_posix())
        mod = module_name(rel)                            # 2b
        if mod:
            picked.update(importers(repo, mod))
        if not _is_dunder(p):                             # 2c
            picked.update(mentioning(repo, p.name))
        return sorted(picked)
    posix = p.as_posix()
    picked.update(mentioning(repo, p.name))               # 3a
    for prefixes, guards in EXPLICIT_GUARDS:              # 3b
        if posix.startswith(prefixes):
            picked.update(g for g in guards if (repo / g).is_file())
    return sorted(picked)


def select(repo: Path, paths: List[str]) -> List[str]:
    """The test modules those paths map to, by the rules in the docstring."""
    picked = set()
    for rel in paths:
        picked.update(map_one(repo, rel))
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

    **Python is in scope, and used not to be** -- see :func:`map_one` for the
    round that cost and the eight modules it still applies to. The one python
    path still passed over is a *deleted* test module: rule 1 answered for it,
    there is nothing left to run, and "grep for what guards it" is not advice
    about a file the round removed on purpose.
    """
    loose = []
    for rel in paths:
        p = Path(rel)
        if _is_test_module(p) and not (repo / rel).is_file():
            continue                      # rule 1 answered; the file is gone
        if map_one(repo, rel):
            continue                      # some rule spoke for it
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
    return [rel for rel, text in test_texts(repo).items() if term in text]


def module_name(rel: str) -> Optional[str]:
    """``src/claude_launcher/daemon/screen.py`` -> ``claude_launcher.daemon.screen``.

    ``None`` for anything outside ``src`` -- ``tools/*.py`` is not importable
    under a package name (its tests load it by path with
    ``spec_from_file_location``), so rule 2c's text search is what speaks for
    those, and this returns nothing rather than inventing a name.

    A package's ``__init__.py`` is named by its *package*
    (``claude_launcher.daemon``), which is what a test actually writes when it
    imports from it.
    """
    p = Path(rel)
    if p.suffix != ".py" or p.parts[:1] != ("src",):
        return None
    parts = list(p.with_suffix("").parts[1:])
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


def _is_dunder(p: Path) -> bool:
    """``__init__.py`` / ``__main__.py`` -- excluded from rule 2c, measured.

    ``needle("__init__.py")`` is the compound stem ``__init__``, which is not a
    reference to anything: it appears in 35 of this suite's 118 test modules,
    none of which are about the package's three-line ``__init__``. Rule 2b
    already selects that file's real dependents by import, correctly and
    without the noise.
    """
    return p.stem.startswith("__") and p.stem.endswith("__")


def _imported_names(path: Path, pkg: str = "") -> set:
    """Every module name ``path`` imports, as written -- and their parents.

    ``import a.b.c`` and ``from a.b import c`` are both recorded as
    ``a.b.c`` (plus ``a.b``), because a test reaches a module either way and
    the selection must not care which it chose. Relative imports are resolved
    against ``pkg`` -- ``src`` uses them exclusively (this repository has zero
    absolute ``claude_launcher`` imports inside ``src``), so a resolver that
    skipped them would read the package as importing nothing.

    A file that will not parse contributes nothing rather than raising: this
    gate runs on working trees, and a half-typed module is a normal state for
    one. The tree it cannot parse is the tree pytest is about to reject
    anyway.
    """
    out = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".") if pkg else []
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                mod = ".".join([b for b in base if b] + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if not mod:
                continue
            out.add(mod)
            for alias in node.names:
                out.add(f"{mod}.{alias.name}")
    return out


#: ``repo`` -> {test module -> its source text}. :func:`mentioning` is asked
#: once per changed path by :func:`map_one`, and :func:`map_one` is now asked
#: by both :func:`select` and :func:`unmapped` -- so a thirty-path round would
#: read the same ~4MB of test sources sixty times over. Read once instead. Same
#: shape and same lifetime as :data:`_TEST_IMPORTS` below, keyed by repository
#: because the test suite drives several within one process.
_TEST_TEXTS: dict = {}


def test_texts(repo: Path) -> dict:
    """Each test module's source, read once per repository."""
    key = str(repo.resolve())
    cached = _TEST_TEXTS.get(key)
    if cached is None:
        cached = {}
        for path in sorted((repo / "tests").glob("test_*.py")):
            try:
                cached[f"tests/{path.name}"] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
        _TEST_TEXTS[key] = cached
    return cached


#: ``repo`` -> {test module -> the module names it imports}. Parsing 118 test
#: modules costs ~0.1s, and :func:`select` asks once per changed path.
_TEST_IMPORTS: dict = {}


def test_imports(repo: Path) -> dict:
    """What each test module imports, parsed once per repository."""
    key = str(repo.resolve())
    cached = _TEST_IMPORTS.get(key)
    if cached is None:
        cached = {
            f"tests/{path.name}": _imported_names(path)
            for path in sorted((repo / "tests").glob("test_*.py"))
        }
        _TEST_IMPORTS[key] = cached
    return cached


#: ``repo`` -> {module name -> the ``src`` modules that import it directly}.
#: The other direction from :data:`_TEST_IMPORTS`, and the input to
#: :func:`reached_indirectly`.
_SRC_IMPORTERS: dict = {}


def src_importers(repo: Path) -> dict:
    """Which ``src`` modules import which, parsed once per repository.

    A module's own package is what its relative imports resolve against, and
    for ``__init__.py`` that package is the module itself -- ``from . import
    x`` inside ``daemon/__init__.py`` means ``daemon.x``, not
    ``claude_launcher.x``. Getting that wrong silently loses a package's whole
    re-export list, which is the exact edge this map exists to see.
    """
    key = str(repo.resolve())
    cached = _SRC_IMPORTERS.get(key)
    if cached is None:
        cached = {}
        for path in sorted((repo / "src").rglob("*.py")):
            rel = path.relative_to(repo).as_posix()
            mod = module_name(rel)
            if not mod:
                continue
            if path.stem == "__init__":
                pkg = mod
            else:
                pkg = mod.rsplit(".", 1)[0] if "." in mod else ""
            for target in _imported_names(path, pkg):
                cached.setdefault(target, set()).add(mod)
        _SRC_IMPORTERS[key] = cached
    return cached


def reached_indirectly(repo: Path, paths: List[str], picked) -> List[tuple]:
    """Test modules one hop further out than rule 2b reaches -- named, not run.

    Rule 2b selects the tests that import the changed module. A test that
    imports something *else* which imports it is not selected, deliberately:
    the transitive closure was measured on this suite at a median 42% of the
    tests per source module, which is the full sweep under another name.

    That limit has a cost, and unlike the others it does not look like one.
    ``bd87fdc`` changed ``cli_sessions.py``; the selection came back with
    **nine** modules, which reads as a gate that worked. The module that
    actually guards the changed behaviour --
    ``tests/test_daemon_wedge.py`` -- was not among them, because it writes
    ``from claude_launcher import cli`` and ``cli.py`` is what imports
    ``cli_sessions``. One hop. The batch landed three red modules and two more
    batches shipped on top before a bisect found them (``claunch-uf7m``).

    Every device this file has for that failure asks whether the selection is
    *empty*: :func:`unmapped` reports a path no rule could map, and the
    landing-request rule asks the worker to name what the selection left out.
    Neither fires here. Nine is not empty and no path went unmapped, so the
    round reads as covered and the missing module is named nowhere.

    So this names them. Two things it deliberately is not:

    * **Not selected.** Running them would take the median selection from 3 of
      119 modules to 26, ``store.py`` from 59 to 93, and this round already
      has 78-96% measurements on record for what a gate that wide costs
      (``claunch-eghh``). The direct-import limit is measured and stays.
    * **Not truncated.** A capped list reads as a complete one. If the number
      is large that is the finding, and the count is printed with it.

    One hop only, and by ``src``: it is the distance a re-export or an
    aggregator adds, which is the shape ``cli.py`` has and the shape this
    package uses throughout. Returns ``(test module, changed path, via)``, so
    the reader can check the claim rather than take it.
    """
    picked = set(picked)
    found: dict = {}
    graph = src_importers(repo)
    for rel in paths:
        mod = module_name(rel)
        if not mod:
            continue
        for via in sorted(graph.get(mod, ())):
            for name in importers(repo, via):
                if name in picked or name in found:
                    continue
                found[name] = (rel, via)
    return [(name, *found[name]) for name in sorted(found)]


def importers(repo: Path, mod: str) -> List[str]:
    """Rule 2b: test modules that import the changed module.

    Rule 2 maps a source file to its same-named test and stops there, which
    is a naming convention standing in for a dependency. Where the two agree
    it is right by luck; where they do not it selects nothing at all, and 60
    of this repository's 107 source modules have no same-named test.

    Four rounds measured the gap before this was written, each one the same
    shape -- selection strictly smaller than the set of modules that actually
    read the file:

    ======================  =========  ============  ==================
    changed                 rule 2     imports it    what it cost
    ======================  =========  ============  ==================
    ``daemon/cflow_clock``  0 modules  10 modules    nothing ran; exit 0
    ``daemon/mesh``         1 module   31 modules    3 real failures in
                                                     ``test_mesh_wire``
    ``daemon/screen``       1 module   6 modules     no regression
    ``cli_sessions``        1 module   7 modules     no regression
    ======================  =========  ============  ==================

    Only the ``mesh`` row was a caught regression; the others say the
    selection was narrow without proving anything was broken. That is the
    honest reading, and it is still the reason to widen -- a gate whose
    coverage is decided by whether someone happened to name a file
    ``test_<x>.py`` is not measuring what it claims to.

    This is the same principle rule 3a already applies to non-python files:
    **a relationship written down in the files is read out of the files.**
    An import is that relationship, written in a place that cannot go stale
    the way a hand-kept table does -- and ``EXPLICIT_GUARDS``'s own history
    is what a hand-kept table costs.

    Widening only, like rules 2 and 3a. The limit is deliberate: this reads
    what a test *imports*, not what its imports transitively reach. The
    transitive closure was measured on this suite and selects a median of
    1054 of 2489 tests -- 42% of the suite, for the median source module,
    and 25% or more for 92 of 107 of them. That is the full sweep under
    another name, and the whole premise of this gate is that the worker's
    run is cheap. Direct imports are a median of 88 tests.
    """
    return sorted(name for name, imps in test_imports(repo).items() if mod in imps)


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


def relations_tried(rel: str) -> str:
    """Which relationships were looked for on this path, and came back empty.

    "Nothing maps to it" is not a size until the reader knows what was
    searched for. A path that no *import* reaches is a different statement
    from a path that no test *names*, and from one no table drives -- and the
    fix differs in each case. Naming the relations turns a bare "none" into
    something a reviewer can disagree with.

    Required by the landing-request rule as of this round: the line that says
    what the selection did not cover also says which relation was swept for
    (reference, import, execution). Pointed out by s181; ``claunch-uf7m``
    carries the ruling.
    """
    p = Path(rel)
    if p.suffix == ".py" and p.parts[:1] in (("src",), ("tools",)):
        tried = [f"same-named test (tests/test_{p.stem}.py)"]
        if module_name(rel):
            tried.append("direct import by a test")
        if not _is_dunder(p):
            tried.append(f"a test naming {needle(p.name)!r}")
        return ", ".join(tried)
    return f"a test naming {needle(p.name)!r}, EXPLICIT_GUARDS"


def _base_note(how: str) -> str:
    """How the base was arrived at, for the line that reports the selection.

    A number is only readable next to the axis it was measured on, and this
    tool's axis moved -- so the output says which one it stood on rather than
    leaving the reader to infer it from a command line they may not have.
    """
    return " (upstream)" if how == "upstream" else ""


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="changed_tests",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=STREAMS,
    )
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=(
            f"ref to diff against (default: {DEFAULT_BASE!r}). "
            f"{BASE_AUTO!r} reads the branch's upstream -- what a stacked "
            f"worker branch integrates into -- and falls back to "
            f"{DEFAULT_BASE!r}, loudly, when none is set or when the only "
            f"upstream is this branch's own remote copy"
        ),
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
    base, how = resolve_base(repo, args.base)
    if how == "self-tracking":
        print(
            f"WARNING: --base {BASE_AUTO} found this branch's own remote copy "
            f"as its upstream, which is a push destination and not a branch to "
            f"measure against; measuring against {DEFAULT_BASE!r} instead.\n"
            f"  Against that copy the selection is everything the repository "
            f"changed since the last push, not this round: 102 paths -> 114 "
            f"modules here, for a session that had committed nothing "
            f"(claunch-d4yo).\n"
            f"  If this branch does integrate into another one, name it:\n"
            f"    git branch --set-upstream-to=<integration branch>"
        )
    if how == "no-upstream":
        print(
            f"WARNING: --base {BASE_AUTO} found no upstream for this branch; "
            f"measuring against {DEFAULT_BASE!r}.\n"
            f"  That is right for a branch cut from {DEFAULT_BASE}, and is the "
            f"wrong axis for one stacked on an integration branch -- there it "
            f"counts the whole batch as this round's change (claunch-eghh: "
            f"93/119, 95/119 and 1945 tests in 221s, for rounds of four to six "
            f"files).\n"
            f"  Name the branch you integrate into, which the alignment gate "
            f"reads from the same place:\n"
            f"    git branch --set-upstream-to=<integration branch>"
        )
    try:
        paths = changed_paths(repo, base)
    except LookupError as exc:
        print(f"cannot tell: {exc}")
        return CANNOT_TELL

    files = select(repo, paths)
    loose = unmapped(repo, paths)
    if loose:
        # Printed whether or not anything was selected: a change can map
        # partly, and the mapped part passing is exactly what makes the
        # unmapped part easy to miss.
        print(
            f"WARNING: {len(loose)} changed path(s) map to no test module. "
            f"This gate says nothing about them -- not that they are fine:"
        )
        for rel in loose:
            print(f"  {rel}  (searched: {relations_tried(rel)})")
        print(
            "  Check by hand (grep -rl '<filename>' tests/) and, if something "
            "guards them, add it to EXPLICIT_GUARDS in this file. If the "
            "change is broad, ask the leader for a full sweep."
        )
    else:
        # Said out loud, because the alternative is saying it by staying
        # silent -- and silence here is indistinguishable from a build of
        # this tool that had no such check. A landing request has to carry
        # this line either way; it should be able to quote it rather than
        # infer it from an absent block (merger-r5, 2026-08-27).
        print(
            f"all {len(paths)} changed path(s) map to at least one test module."
        )

    hops = reached_indirectly(repo, paths, files)
    if hops:
        # After the unmapped warning and before the selection, because it is
        # about what the selection does NOT say. See reached_indirectly.
        print(
            f"NOTE: {len(hops)} test module(s) reach a changed file one import "
            f"hop further out than rule 2b follows, and were NOT selected. This "
            f"gate did not run them:"
        )
        for name, rel, via in hops:
            print(f"  {name}  <- {rel} via {via}")
        print(
            "  Direct imports only is measured, not an oversight: the "
            "transitive closure selects a median 42% of this suite per source "
            "module. This list is here so that a selection which looks healthy "
            "cannot hide the module that actually guards the change "
            "(claunch-a9t). Judge it, or run one of them by hand."
        )
    else:
        print("no test module sits one import hop outside this selection.")

    if not files:
        print(
            f"no test modules map to this change ({len(paths)} path(s) touched "
            f"vs {base}{_base_note(how)}) -- nothing for this gate to run. The step's "
            f"done_when still asks your report for numbers and scenarios."
        )
        return 0                          # a pass, for --check too: see below

    cmd = build_command(files, basetemp=args.basetemp)
    print(
        f"{len(files)} test module(s) selected from {len(paths)} changed path(s) "
        f"vs {base}{_base_note(how)}:"
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
                f"counted."
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
