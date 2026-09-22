"""Run this repository's full sweep somewhere else, and gate on the receipt it leaves.

``improv-leader``'s ``sweep`` step judges the batch's merge result with the
whole suite. That run used to *be* the step's ``verify``, which meant the
cflow engine ran it (``cflow/engine.py`` ``_run_verify``) synchronously while
the leader's ``next`` call blocked on it. Three things were wrong with that:

* The leader's own workflow says it does not run sweeps or merges in its
  turn -- and then a single ``verify:`` line did exactly that, invisibly.
  ``improv-worker``'s review step already warned about this shape: "the step
  payload's ``verify`` field means leaving the step *starts a sweep* ... the
  least visible sweep is the least recorded sweep."
* Six sessions each running ``-n 8`` on leaving a step is 48 processes that
  contaminate each other's timings, and nothing in the journal says a sweep
  was even running.
* A verify's result lives only in the run. When the daemon restarts
  mid-sweep -- four spontaneous restarts were observed in one shift
  (``claunch-3k9``) -- the sweep is gone and so is the verdict.

So the run and the verdict are split, and a file on disk joins them:

    subagent:  python tools/sweep.py run   --branch master   # minutes
    the gate:  python tools/sweep.py check --branch master   # milliseconds

``run`` executes the suite and writes a **receipt** naming the commit it
judged, the command in full, and the counts. ``check`` is what the step arms
as its ``verify``: it looks up the receipt for the branch's *current* tip and
passes only if that sweep was green.

Both halves live in one file on purpose. The receipt is a format shared
between a writer and a reader, and a format split across two files drifts.

A receipt is written by ``run`` and by nothing else. The one name it may be
filed under is ``{commit}.json`` -- the full sha the run judged, nothing
added (``claunch-sweep-receipt-single-writer-mxv0`` was filed over a manual
receipt whose name violated exactly that). ``_newest_green`` refuses any
other name before parsing: a hand-written receipt is a verdict nobody
earned, and no matter how well-formed its content it is not this tool's
decision to reuse.

**Keyed by commit, not by clock.** The receipt's name is the sha it judged,
so there is no "is this recent enough" guess: a receipt either belongs to the
tip being gated or it does not. This is also what survives the restarts
above. ``claunch-bg-sweep-lost-on-restart-hq7`` separates two failure axes,
and the receipt covers both -- if the sweep subagent dies, no receipt is
written and the gate is red (producer); if the *leader* dies, the receipt is
still on disk when it comes back (consumer).

**Keyed by repository, not by working directory.** The identity is the git
*common* dir, which every worktree of one repository shares. That is
deliberate: the leader sweeps in a throwaway worktree (a short path, and one
nobody else has uncommitted files in) and the receipt still answers for
master in the main checkout.

**The tree must actually BE the commit the receipt names.** Two ways it can
fail to be, and both were seen for real:

* *Dirty.* A sweep in a checkout holding somebody else's uncommitted work is
  a verdict about that commit plus whatever was lying around. A leader's
  sweep in the shared checkout collected four foreign tests and reported
  1562/1563 where clean master had 1559; six issues were closed citing the
  contaminated number before another session caught it.
* *The wrong commit entirely.* The suite runs in the working tree while the
  receipt is filed under ``--branch``'s sha, so running it from a feature
  branch files a receipt naming a commit nothing tested. That happened on
  the round that wrote this file: a red receipt against ``master``, from a
  tree that was not master, while master was fine. A clean ``git status``
  does not catch it -- the tree was a perfectly clean checkout of something
  else.

So ``run`` refuses both, and checks HEAD first, because standing on the
wrong commit is the worse of the two.

**A commit is the key; a tree is the verdict.** The sweep judges the working
tree, not the history above it, so a green receipt for tree *T* is a verdict
about every commit whose tree is *T*. ``check`` uses that: if the tip has no
receipt of its own it will accept a green one recorded for a different commit
with the same tree, and says so, naming both shas.

That is not a convenience -- it is what makes ``improv-leader``'s five-minute
integration window cost one sweep instead of two. The leader sweeps the
integration *preview* (the batch's candidates merged onto master in a scratch
branch) before committing to the merge, so a broken combination is caught
while master is still clean. Merging those same candidates in the same order
with ``--no-ff`` then produces a different commit with **byte-identical
content**, and without this the batch would pay for the whole suite twice
over one tree.

**And a tree is not quite the verdict either -- the board is not code.** The
same step that arms this gate also commits ``.beads/issues.jsonl``, and the
order it prescribes puts that commit *after* the sweep: sweep the tip, close
the issues with the numbers the sweep produced, commit the board, leave the
step. So master moves to a commit no receipt names, and the step's own
``verify`` goes red **because the step was followed**. That is not a race
somebody lost; it is what obeying the instruction does, every round.

Re-ordering the step does not fix it, and this is the part that took five
rounds to see. Board commits do not all come from here -- a crew on another
mesh integrates into the same master, and one of its board commits (``e6e3ef9``,
12:47:37) landed *inside* a re-sweep that ran 12:46:24-12:49:30. Nothing this
workflow can reorder controls when somebody else commits, so with the gate
keyed on the whole tree there is no serial sweep that reliably satisfies it.
The failure stopped being waste and became non-termination; the roughly
eighteen minutes of re-swept-for-nothing was the cheaper half.

So ``check`` has a third and last rung, ``code_tree``: the tip's tree with
``NON_CODE_ENTRIES`` removed. A green receipt recorded for the same code tree
answers, and it says so, naming both trees. What makes that sound is measured
on both sides -- pytest never collects the board file (it is outside
``testpaths``, and three consecutive board-only commits gave 1759/1/0 to the
digit), and no test reads the repository's own copy. What makes it *safe* is
that it subtracts rather than selects: anything outside those names still
changes the digest, so an unrecognised path costs one sweep, never a verdict.
Keying on the inputs instead (``src/``, ``tests/``, ``pyproject.toml``) folds
harder and fails the other way round, which is the trade this file already
refuses everywhere else.

Safe while nothing in this suite reads the repository's own HEAD or refs --
and that is now watched rather than asserted. ``tests/_repo_history_guard.py``
refuses such a read at the ``subprocess`` call that makes it, and
``tests/test_repo_history_guard.py`` is the broken variant proving the guard
is armed. This paragraph used to claim that every git-touching test digs a
throwaway repository under ``tmp_path``; it was already wrong when it was
written -- ``tests/test_mergecheck.py::test_the_real_commits`` reads this
repository on purpose -- and wrong in a way only a machine was ever going to
keep track of. It is safe there because it names its commits by *hash*, which
is the line the guard actually draws: the object database is shared by every
commit, so two same-tree commits cannot disagree about an object, while they
disagree about HEAD by construction. A suite that asserted on ``git log``
would go red at that guard, which is the point of it.
Only *green* receipts carry over; a red one is a verdict the gate refuses to
launder, and the tip is simply left without a receipt, which is red anyway.

Exit codes match ``tools/deploy_check.py``: 0 = confirmed, 1 = no, 2 = could
not tell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# A gate runs the tree it is checking. This checkout's ``src`` goes in front of
# every installed copy, so the ``claude_launcher`` imports below resolve HERE --
# whatever the worktree's .venv holds (``uv run --no-sync`` promises never to
# populate it) and whatever else on the path answers to the same name.
# Pinned by tests/test_gates_run_this_checkout.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_launcher import test_window

CANNOT_TELL = 2

#: This repository's sweep: the whole suite, no marker filter. The worker's
#: gate is deliberately *not* this (see ``tools/changed_tests.py``) -- only
#: the leader's batch sweep runs everything.
#:
#: ``--no-sync``: uv would otherwise re-resolve the venv before running, and
#: the daemon holds ``.venv/Scripts/claunch.exe`` open, so the replace fails
#: with os error 5. Prepare the tree once with ``uv sync --extra test``; that
#: one sync also installs the xdist ``-n`` needs.
#:
#: ``-n 8`` is measured on this machine, not chosen: serial 532s, n=4 230s,
#: n=8 178s, n=auto (32 here) 184s -- and 32 workers starved the PTY timing
#: tests into intermittent failures. The suite is parallel safe (the heavy
#: e2e tests take OS-assigned ports; conftest gives each test its own tmp
#: home and env).
#:
#: The basetemp is short and per-session because both are hard limits, not
#: taste: xdist inserts ``popen-gwN/`` under it and the transcript tests fold
#: an absolute cwd back into a filename, so the path is 2*basetemp+162 and
#: 260 chars (MAX_PATH) is a FileNotFoundError. Measured: 48 chars passes at
#: 258, 49 fails at 260. And pytest empties its basetemp at startup, so two
#: concurrent runs sharing one path delete each other's temp trees mid-run.
#:
#: ``--deselect ...test_mesh_federation_over_real_relay``: the one named
#: exception to "no marker filter" above, and it stays a single ``--deselect``
#: rather than a marker for exactly that reason -- a marker is a knob anyone
#: can widen by adding a name to it later, a literal node id is not.
#: ``claunch-rl2x.1``: under ``-n 8`` this test's xdist *worker process*
#: crashes (Windows ``0xc0000374`` heap corruption, cause unknown) at a rate
#: that reached 13/13 across two batches (2026-09-18, sessions s469/s572/
#: s582), reproducing identically on plain master and on every branch tested
#: against it -- it is a property of running this test under parallel
#: workers on this machine, not of any change. A crashed xdist worker does
#: not report "this test failed"; it reports nothing, and whatever *other*
#: test that worker was mid-run on also gets no verdict (the varying second
#: failure in the same batches' receipts). Leaving it in the parallel run
#: therefore does not buy coverage of it -- it spends the batch sweep's one
#: shot on a coin flip that also endangers an unrelated test. The node still
#: runs in every plain ``pytest tests`` (a developer's default, and CI's own
#: suite) and passed 3/3 standalone in the same measurements; only this one
#: parallel gate skips it. See ``tests/test_sweep.py::
#: test_the_sweep_covers_the_whole_suite`` for the test that pins this to
#: exactly one exception.
DEFAULT_COMMAND = (
    'uv run --no-sync pytest tests -q -n 8 --basetemp="C:/t/{session}w" '
    "--deselect tests/test_federation_integration.py::test_mesh_federation_over_real_relay"
)


def default_command(session: str, workers: int = 8) -> str:
    """The repository sweep command at the width granted by the arbiter."""
    command = DEFAULT_COMMAND.format(session=session)
    return re.sub(r"(?<!\S)-n\s+8(?=\s|$)", f"-n {max(1, workers)}", command, count=1)

#: ``123 passed``, ``2 failed``, ``1 skipped`` ... from pytest's summary line.
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|error|errors|xfailed|xpassed)")

#: Top-level entries the gate subtracts from a tree before asking whether two
#: commits are the same thing. Exactly one, and adding a second needs the same
#: two-sided measurement this one has:
#:
#: * *not an input*  -- ``.beads/issues.jsonl`` is outside ``testpaths =
#:   ["tests"]``, so pytest does not collect it, and three rounds of the same
#:   board-only commit produced counts that did not differ by one digit
#:   (1759/1/0 three times over trees ``dc534d0``/``cc3dc69``/``9a76122``);
#: * *not consumed* -- no test reads the repository's own copy. Every
#:   ``.beads`` path under ``tests/`` is built under a ``tmp_path`` fixture
#:   and written by the test itself. What to count, if you are checking:
#:   ``.beads`` paths under ``tests/*.py`` that are **not** rooted in a
#:   fixture. What to leave out, because none of it is a path -- ``.js``
#:   property access (``body.beads``), the ``daemon.beads`` module, prose,
#:   and this file's own cases (``tests/test_sweep.py`` commits a board into
#:   a throwaway repository to test exactly this rung).
#:
#:   A bare ``grep -rn '[.]beads' tests/`` is *not* that count, and is worth
#:   one sentence because the difference bit its author: it matches all of
#:   the above, and it also matches ``tests/__pycache__/*.pyc``, so its line
#:   total depends on whether this tree has ever run the suite. Two readers
#:   at the same revision get different numbers from it -- which is the one
#:   failure a revision stamp cannot fix, since what varies is not the
#:   commit but the working tree's history.
#: * *not shipped* -- nothing under it is run or installed by anyone outside
#:   the suite. The board is a record this workflow writes and reads back;
#:   ``stubs/claunch.bat`` is a shim a user's PATH executes, which is why it
#:   fails here *after passing both bullets above* -- nothing under
#:   ``tests/``, ``src/`` or ``tools/`` references it except the example two
#:   lines above, which this bullet needed in order to be legible, so the two
#:   tests that admitted ``.beads`` admit it too. (Grep and you get exactly
#:   one hit -- that example, not this claim. It said "zero" until the
#:   paragraph supplied its own counterexample.) This bullet is the one
#:   doing that work, and it was argued in review before it was written
#:   here, which is the same gap in miniature.
#:
#:   The narrowness of "run or installed" is load-bearing. Shipped code reads
#:   this repository's own ``.beads/`` at runtime -- one constant and four
#:   call sites across ``cli_beads.py`` and ``daemon/beads.py`` -- so a
#:   broader "used by" or "read by" would disqualify the single entry this
#:   list holds. Widen the wording and the rule deletes itself.
#:
#: **This is a deny list, and that is the whole safety argument.** The digest
#: below is the *whole* tree minus these names, so a path that appears
#: anywhere else -- a new root ``conftest.py``, a data directory, ``uv.lock``
#: -- still changes it and the gate still goes red. The tempting shape is the
#: opposite one, keying on the inputs (``src/``, ``tests/``, ``pyproject.toml``):
#: it folds harder, and it fails in the direction that costs a verdict rather
#: than a sweep -- one input path left off the list and the gate is green over
#: a tree nobody judged. This repository has already paid for a green over an
#: unswept tree once (six issues closed citing a contaminated 1562), so the
#: rule is that being wrong here must cost time, never correctness.
#:
#: **The bill for keeping it to one name is measured, not assumed.** Five
#: other top-level entries are non-code in the same way the board is --
#: ``docs/``, ``docs4users/``, ``README.md``, ``AGENTS.md``, ``.gitignore``
#: -- and **as of f76aa08** this history holds 15 commits that touched
#: nothing but those, every one of which moves the digest and costs a sweep
#: it did not need. (Against 21 that touched nothing but ``.beads/``, which
#: is what this rung now reuses a receipt for. Count with ``git rev-list
#: --no-merges``: a merge's combined diff can list only doc files, which
#: inflates the first figure to 16.) Both numbers are derived from history
#: rather than observed once, so they move as master grows -- and they
#: already differ off master: one live branch, four commits behind, read 20
#: for the second -- which four commits is what decides that, so treat this
#: as an instance and not a rule. Recount at your own revision.
#:
#: Those 15 are the price of the paragraph above, paid on
#: purpose, and the number is here so that whoever weighs an extension
#: starts from it instead of from an impression. What argues against
#: extending is not the size of that bill but the paragraph below: each
#: added name widens a surface nothing guards.
#:
#: **What is prose here and what is enforced.** The pin in
#: ``tests/test_sweep.py`` catches a name being *added*, and the cases around
#: it catch the digest ceasing to discriminate. Neither catches the other
#: direction: a test added later that reads the repository's own ``.beads/``
#: would make the second bullet false, and nothing would go red -- the gate
#: would be green over a tree it did not judge, which is the one failure this
#: whole axis is arranged to avoid. That is a rule about what may be written,
#: not a rule the code keeps, and it is written down rather than guarded
#: because guarding it needs the machinery of
#: ``tests/_repo_history_guard.py``. Read this paragraph as the limit it is,
#: not as an assurance.
NON_CODE_ENTRIES = frozenset({".beads"})


def _git(repo: Path, *args: str) -> str:
    """``git -C repo args...``, stripped. Raises LookupError on failure."""
    # ``encoding``/``errors`` rather than a bare ``text=True``: git writes
    # utf-8 (commit subjects, paths, its own messages) and ``text=True``
    # decodes with the process locale, which is cp949 on this machine. The two
    # disagree the moment a non-ASCII byte appears, and the disagreement does
    # not surface as a decode error here -- it kills the reader thread inside
    # subprocess.run, so ``proc.stdout`` arrives as ``None`` and the next line
    # raises about ``NoneType``. See the same pair at the suite call below.
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise LookupError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.strip() or 'no output'}"
        )
    return proc.stdout.strip()


def repo_key(repo: Path) -> str:
    """Stable id for the *repository*, shared by all of its worktrees.

    ``--git-common-dir`` is the one path every worktree of a repository
    agrees on, which is what lets a sweep run in a scratch worktree answer
    for the branch in the main checkout.
    """
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    resolved = str(Path(common).resolve()).lower().replace("\\", "/")
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]


def code_tree(repo: Path, tree: str) -> str:
    """`tree` with :data:`NON_CODE_ENTRIES` removed, as a stable digest.

    The gate's question is "was the whole suite run over this content", and
    ``find_receipt_by_tree`` answers it exactly when two commits share a tree.
    Plenty of things break that: this tree has twelve top-level entries and
    eleven of them move the digest, ``docs/`` and ``README.md`` included,
    which the suite reads no more than it reads the board. The board is not
    singled out for being harmless -- it is singled out because
    ``improv-leader``'s sweep step *prescribes* committing it, and does so
    after the sweep it also prescribes: sweep the tip, close the issues with
    the numbers, ``git add .beads/issues.jsonl`` (that step commits no other
    path -- ``improv-leader.yaml``, the ``sweep`` step), leave the step. So
    master moves to a commit the receipt cannot name, and the step's own
    ``verify`` is red *because* the step was obeyed. That, plus the
    two-sided measurement in ``NON_CODE_ENTRIES``, is the whole case for
    subtracting it; neither half reaches the other eleven, and a change
    to any of them still costs a sweep, on purpose. Five
    rounds ran that way before it was written down, and the wasted re-sweeps
    came to about eighteen minutes; a second integrator committing the same
    file from another mesh made it worse than waste, because master moved
    again while a re-sweep was still running (``e6e3ef9`` landed at 12:47:37
    inside a run spanning 12:46:24-12:49:30). Re-ordering the step cannot fix
    that -- nothing in this workflow controls when somebody else commits --
    which is why the axis moves instead.

    Not a git object: this hashes ``git ls-tree``'s own lines (mode, type,
    sha, name) for the entries that are kept. Building a real tree would mean
    writing objects into the repository a gate is only supposed to read.
    """
    listing = _git(repo, "ls-tree", tree)
    kept = [
        line
        for line in listing.splitlines()
        if line.split("\t", 1)[-1] not in NON_CODE_ENTRIES
    ]
    return hashlib.sha1("\n".join(kept).encode("utf-8")).hexdigest()


def receipts_dir(repo: Path, override: Optional[Path] = None) -> Path:
    """Where this repository's receipts live -- outside the repository.

    Receipts are evidence about a tree, not part of it: keeping them in the
    worktree would put them in front of every ``git status`` the workflow
    reads, and the dirty-tree rule above would then trip over its own output.
    """
    if override is not None:
        return override / repo_key(repo)
    from claude_launcher import config

    return config.launcher_home() / "sweeps" / repo_key(repo)


def receipt_path(repo: Path, commit: str, override: Optional[Path] = None) -> Path:
    return receipts_dir(repo, override) / f"{commit}.json"


def output_path(repo: Path, commit: str, override: Optional[Path] = None) -> Path:
    """Where a *red* run parks the suite output the receipt summarises.

    The extension is load-bearing twice over, and both layers are checked by
    tests below. :func:`_newest_green` scans ``directory.glob("*.json")`` and
    only then refuses names outside :data:`_RECEIPT_NAME_RE`, so
    ``.output.txt`` is dropped at the glob, before the name check could call
    it a hand-written receipt. Filing this under ``.json`` would make every
    red run emit a "not a {sha}.json receipt" warning about a file the run
    itself wrote -- the gate warning about its own evidence.
    """
    return receipts_dir(repo, override) / f"{commit}.output.txt"


#: The one name a receipt may be filed under -- the full sha it judged, plus
#: ``.json``. ``run`` constructs it via :func:`receipt_path`; ``_newest_green``
#: refuses any other name *before* parsing, because a file here that ``run``
#: did not write is a hand-written receipt, a verdict nobody earned. Pinned to
#: SHA-1's 40 hex digits, which is what ``git rev-parse`` emits here; a
#: repository on SHA-256 would cost a sweep, never a verdict -- the denial
#: direction this file refuses to trade (see ``NON_CODE_ENTRIES``).
_RECEIPT_NAME_RE = re.compile(r"^[0-9a-f]{40}\.json$")

#: The one other name ``run`` files under: the record of an attempt that
#: parsed no counts and therefore refused to overwrite the verdict standing at
#: ``{sha}.json`` (``claunch-hnjy``). It is written by ``run``, so
#: :func:`_newest_green` passes over it in silence instead of reporting it as
#: a hand-written receipt, and it can never be reused as one -- a run that
#: produced no counts judged nothing.
_INVALID_NAME_RE = re.compile(r"^[0-9a-f]{40}\.invalid\.json$")


def invalid_run_path(repo: Path, commit: str, override: Optional[Path] = None) -> Path:
    """Where a run that produced no counts is filed when a verdict stands."""
    return receipts_dir(repo, override) / f"{commit}.invalid.json"


def invalid_output_path(
    repo: Path, commit: str, override: Optional[Path] = None
) -> Path:
    """The companion output for :func:`invalid_run_path`, kept out of the
    ``*.json`` glob for the same reason :func:`output_path` is."""
    return receipts_dir(repo, override) / f"{commit}.invalid.output.txt"


def standing_verdict(path: Path) -> Optional[dict]:
    """The receipt already filed at `path`, if it is a judgement to protect.

    Three states are *not*: no file, a file that cannot be parsed (which
    ``_newest_green`` already refuses to treat as a decision), and a receipt
    whose ``counts`` are empty. Those are what a new run may replace.
    """
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or not (receipt.get("counts") or {}):
        return None
    return receipt


def is_green(receipt: dict) -> bool:
    """A receipt that judged a clean tree and found nothing wrong."""
    counts = receipt.get("counts") or {}
    return (
        not receipt.get("dirty")
        and receipt.get("exit_code") == 0
        and not counts.get("failed")
        and not counts.get("error")
    )


def _newest_green(repo: Path, override: Optional[Path], matches) -> Optional[tuple]:
    """The newest green receipt `matches` accepts, as ``(path, receipt)``.

    Two kinds of file are refused *loudly* rather than skipped silently, and
    the silence was the bug: a verdict that quietly cannot be reused costs the
    whole sweep a second time, which is the failure
    ``claunch-sweep-receipt-single-writer-mxv0`` filed over.

    * a name outside :data:`_RECEIPT_NAME_RE` was not written by ``run``.
      A hand-written receipt is a verdict nobody earned, and it is refused
      on the name alone, before anything is parsed;
    * a standard-named receipt that cannot be parsed is a filed verdict that
      nobody can read.

    Both are warned to stderr and ignored. A *valid* receipt that simply does
    not match the caller's ``matches`` is neither -- it is a real decision
    about a different tree, and is passed over silently.
    """
    directory = receipts_dir(repo, override)
    if not directory.is_dir():
        return None
    best = None
    for path in sorted(directory.glob("*.json")):
        if _INVALID_NAME_RE.match(path.name) is not None:
            # ``run``'s own file, and never a verdict: it exists precisely
            # because that attempt had none to file. The warning below is
            # about receipts nobody earned, so it must not fire on this one.
            continue
        if _RECEIPT_NAME_RE.match(path.name) is None:
            print(
                f"WARNING: {path} is not a {{sha}}.json receipt, the one name "
                f"'python tools/sweep.py run' files receipts under. A file "
                f"here with any other name was hand-written, not filed -- it "
                f"is not a verdict about any tree. Ignoring it.",
                file=sys.stderr,
            )
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(
                f"WARNING: {path} cannot be read as a receipt ({exc}); a "
                f"sweep outcome that was filed and then lost is not a "
                f"verdict. Ignoring it.",
                file=sys.stderr,
            )
            continue
        if not is_green(receipt) or not matches(receipt):
            continue
        if best is None or (receipt.get("finished_at") or "") > (
            best[1].get("finished_at") or ""
        ):
            best = (path, receipt)
    return best


def find_receipt_by_tree(
    repo: Path, tree: str, override: Optional[Path] = None
) -> Optional[tuple]:
    """The newest green receipt recorded for `tree`, whatever commit it named.

    The sweep runs against a working tree, so the sha in the receipt's name is
    only an address -- the thing judged is the content. This is what lets one
    sweep of an integration preview answer for the merge commit that lands the
    same candidates: same tree, different sha.
    """
    return _newest_green(repo, override, lambda r: r.get("tree") == tree)


def find_receipt_by_code_tree(
    repo: Path, code: str, override: Optional[Path] = None
) -> Optional[tuple]:
    """The newest green receipt whose tree matched `code` outside the board.

    One rung weaker than :func:`find_receipt_by_tree` and deliberately last:
    the trees really do differ, and what is being claimed is that they differ
    only in :data:`NON_CODE_ENTRIES`. See :func:`code_tree` for why that claim
    is safe to make and where it came from.

    A receipt written before this field existed has no ``code_tree`` and is
    skipped, which leaves the gate red -- the direction this whole axis is
    built to fail in.
    """
    return _newest_green(
        repo, override, lambda r: bool(code) and r.get("code_tree") == code
    )


def parse_counts(output: str) -> dict:
    """Counts from pytest's summary line -- the last one, when xdist repeats it."""
    counts: dict = {}
    for line in output.splitlines():
        found = _COUNT_RE.findall(line)
        if found and ("passed" in line or "failed" in line or "error" in line):
            counts = {kind.rstrip("s"): int(n) for n, kind in found}
    return counts


def _failure_lines(output: str, limit: int = 40) -> list:
    """The ``FAILED``/``ERROR`` short-summary lines, so a red gate can name names."""
    lines = [
        ln.strip()
        for ln in output.splitlines()
        if ln.startswith("FAILED ") or ln.startswith("ERROR ")
    ]
    return lines[:limit]


#: The cap on the suite output a red run parks beside its receipt, and how
#: much is kept from each end. Both are sized against this repository's last
#: red sweep (``1fdc7a7``: 2 failed, 2319 passed, 2085 warnings), whose
#: captured output measured 36,072 characters in this layout:
#:
#: ===========  =====================================================
#: character    section
#: ===========  =====================================================
#: 0            uv's banner, then xdist's progress dots
#: 3,135        ``=== FAILURES ===`` -- the tracebacks, 9,710 long
#: 12,845       ``=== warnings summary ===`` -- 22,758, 63% of the file
#: 35,603       ``=== short test summary info ===`` + the counts line
#: ===========  =====================================================
#:
#: That is the shape the split exploits: pytest prints the failures *before*
#: the warnings block and the failing names *after* it, so cutting the middle
#: keeps both things a reader needs and drops the part that is 63% of the
#: bytes and none of the diagnosis. The head is 15x the 12,845 characters that
#: run needed to reach the end of its tracebacks and the tail 139x its 469
#: characters of summary, so the whole 36,072 fits seven times over under the
#: cap -- a real sweep here is not truncated at all. The limit bounds the case
#: this repository has not hit yet and has no other bound for: a failing test
#: printing without limit into its own captured output.
_OUTPUT_LIMIT = 256 * 1024
_OUTPUT_HEAD = 192 * 1024


def _truncated(
    output: str, limit: int = _OUTPUT_LIMIT, head: int = _OUTPUT_HEAD
) -> str:
    """`output`, cut in the middle to `limit` characters, saying that it was.

    The note goes in the file rather than the receipt because the file is what
    a reader has open when the question arises. A cut that left no trace would
    be the worse half of the failure this whole change is about: evidence that
    looks complete and is not.
    """
    if len(output) <= limit:
        return output
    tail = limit - head
    dropped = len(output) - limit
    return (
        output[:head]
        + f"\n\n[... tools/sweep.py dropped {dropped} characters here: the "
        f"suite printed {len(output)}, over this file's {limit} cap. The "
        f"{head} characters above and the {tail} below are kept, which is "
        f"where pytest puts the tracebacks and the failing names "
        f"respectively ...]\n\n"
        + output[-tail:]
    )


def cmd_run(args) -> int:
    repo = args.repo.resolve()
    try:
        commit = _git(repo, "rev-parse", args.branch)
        tree = _git(repo, "rev-parse", args.branch + "^{tree}")
        code = code_tree(repo, tree)
        head = _git(repo, "rev-parse", "HEAD")
        dirty = _git(repo, "status", "--porcelain")
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    # The suite runs in this working tree, but the receipt is filed under
    # --branch's sha. If those are different commits the receipt is a lie in
    # the most useful-looking form: it names a commit nobody tested. This
    # happened on the very round that wrote this file -- a run in a feature
    # worktree filed a red receipt against master, and master was fine.
    #
    # A clean tree is not enough to catch it: `git status` was empty, because
    # the tree was a perfectly clean checkout of a *different* commit. So the
    # identity has to be checked directly, and it is checked before the
    # dirty test because standing on the wrong commit is the worse error.
    if head != commit:
        print(
            f"refusing to sweep: {repo} is at {head[:12]}, but this run would "
            f"file its receipt against {args.branch} = {commit[:12]}. The "
            f"suite runs in the working tree, so the receipt would name a "
            f"commit nothing tested. Check out {args.branch} (a scratch "
            f"worktree detached at it is the cheap way) and run this there.",
            file=sys.stderr,
        )
        return CANNOT_TELL

    if dirty and not args.allow_dirty:
        print(
            f"refusing to sweep: {repo} has {len(dirty.splitlines())} uncommitted "
            f"path(s), so a run here judges {args.branch} plus whatever is lying "
            f"around, not {commit[:12]}. Sweep a clean tree -- a scratch worktree "
            f"at the tip is the cheap way -- or pass --allow-dirty to record an "
            f"explicitly untrusted receipt.\n{dirty}",
            file=sys.stderr,
        )
        return CANNOT_TELL

    session = os.environ.get("CLAUNCH_SESSION", "sweep")
    try:
        grant = test_window.acquire(
            test_window.SWEEP,
            label=f"sweep.py run --branch {args.branch}",
        )
    except test_window.WindowUnavailable as exc:
        print(f"cannot sweep: {exc}", file=sys.stderr)
        return CANNOT_TELL

    command = args.command or default_command(session, grant.advisory_n)

    started = datetime.now(timezone.utc)
    print(f"sweep: {command}\n  repo={repo}\n  {args.branch}={commit}", flush=True)
    # The suite's output is read as utf-8 with undecodable bytes replaced,
    # never with the locale ``text=True`` picks. Measured at 5516c651c732 on a
    # cp949 machine: the first Korean byte in a failure dump
    # (``ec 9d bd``, position 3772) raised UnicodeDecodeError inside
    # subprocess.run's reader thread, which left ``proc.stdout`` at ``None``
    # and filed a receipt with ``counts {}``, ``failures []`` and a 0-byte
    # output file. The gate reads that as red -- correctly -- but nothing on
    # disk could say WHAT was red, so a real failure and a broken judge looked
    # identical. ``errors="replace"`` is what keeps the summary line (ASCII)
    # readable even when the bytes around it are not.
    #
    # The child's own encoding is deliberately left alone. Handing the suite
    # ``PYTHONIOENCODING``/``PYTHONUTF8`` would make the sweep judge a process
    # the operator's own ``pytest`` never runs, which is how a sweep goes green
    # over a tip that is red in the default environment.
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=grant.child_env(),
        )
    finally:
        grant.release()
    finished = datetime.now(timezone.utc)
    # A stream that came back as ``None`` is not an empty stream: the reader
    # died and took the output with it. Both states parse to ``counts {}``, and
    # a receipt that cannot tell them apart sends its reader to look for a
    # failing test that was never named. Say which one this was.
    lost = [n for n in ("stdout", "stderr") if getattr(proc, n) is None]
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output)

    counts = parse_counts(output)
    receipt = {
        "commit": commit,
        "tree": tree,
        "code_tree": code,
        "branch": args.branch,
        "repo": str(repo),
        "command": command,
        "exit_code": proc.returncode,
        "counts": counts,
        "failures": _failure_lines(output),
        "dirty": bool(dirty),
        "session": session,
        "window": grant.receipt(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "seconds": round((finished - started).total_seconds(), 1),
    }
    if lost:
        receipt["stream_error"] = (
            f"{' and '.join(lost)} could not be read back from the suite "
            f"process, so this receipt describes only what survived; empty "
            f"counts here mean the output was lost, not that the suite "
            f"printed no summary"
        )
        print(f"WARNING: {receipt['stream_error']}", file=sys.stderr)
    dest = receipt_path(repo, commit, args.receipts)
    saved = output_path(repo, commit, args.receipts)

    # A receipt is named after the commit alone, so a second run at the same
    # tip rewrites the first one's file. That is right while both runs judged
    # something -- the newer verdict is the verdict. It is wrong when this run
    # never got a judgement to file, and the difference cost a real one
    # (``claunch-hnjy``, 2026-08-31): a sweep of ``70e9505a`` finished 1 failed
    # / 2963 passed / 1 skipped, a re-run at that same tip died inside pytest's
    # basetemp cleanup with ``PermissionError: [WinError 32]`` before
    # collection, and the ``counts {}`` receipt it filed anyway landed on top
    # of the only full judgement that tree ever had. The output file went with
    # it.
    #
    # What that costs is not the file. ``cmd_check`` stands on the receipt, so
    # an empty one in a verdict's place reads afterwards as "no sweep ran
    # here" -- the green and the red are lost the same way, and the round pays
    # for the suite again to learn what it already knew. It also runs the other
    # direction: ``is_green`` reads ``counts``, so an empty receipt whose
    # process happened to exit 0 would answer as green for every commit
    # sharing the tree.
    #
    # So a run that parsed no counts never overwrites one that has them. It is
    # filed beside instead rather than dropped: "this tree measures X" and
    # "this attempt could not measure it" are two facts, and the second is the
    # one the next person needs in order not to repeat the attempt.
    standing = None if counts else standing_verdict(dest)
    if standing is not None:
        stood = ", ".join(
            f"{v} {k}" for k, v in sorted((standing.get("counts") or {}).items())
        )
        receipt["not_a_verdict"] = (
            f"this run parsed no counts, so the suite produced no judgement to "
            f"file; {dest.name} ({stood}, exit {standing.get('exit_code')}) is "
            f"the verdict that stands at this commit and was left untouched"
        )
        dest = invalid_run_path(repo, commit, args.receipts)
        saved = invalid_output_path(repo, commit, args.receipts)
        print(
            f"WARNING: this run produced no counts, so it is not a verdict "
            f"about {commit[:12]}; it is filed as {dest.name} and the receipt "
            f"already there ({stood}) is left as it was.",
            file=sys.stderr,
        )

    dest.parent.mkdir(parents=True, exist_ok=True)

    # The suite output is already in hand and, until now, was dropped at
    # exactly this line: the receipt kept `failures`, a list of node ids.
    # Node ids do not carry a mechanism. Of the six red receipts this
    # repository has filed, the one whose cause could be reconstructed was the
    # one where somebody happened to save the output by hand beside it, and
    # what that output showed was that two failures which read as unrelated (a
    # PermissionError in a store, an HTTP 500 out of a sync server) had one
    # cause, the second having been raised inside the server and come back
    # wrapped. `failures` cannot express that; the output can, so it is kept.
    #
    # Only for red, and red as `not is_green(...)` rather than a test of its
    # own, so that a later widening of green -- errors, skips -- carries here
    # without a second definition to keep in step. A green run's output is
    # ~29,000 characters of warnings summary and no diagnosis, once per sweep,
    # so it is not kept; and a green run *deletes* what an earlier red run at
    # this same sha left, because the pair is rewritten together and a stale
    # red output beside a green receipt reads as this sweep's.
    if is_green(receipt):
        try:
            saved.unlink(missing_ok=True)
        except OSError as exc:
            # ``missing_ok`` covers only FileNotFoundError. Anything else --
            # a lock, a permission, a directory standing in the path -- used
            # to propagate from here, which is *before* the receipt is
            # written: the suite would run to completion and file nothing,
            # and the gate reads that as "no sweep receipt" and charges
            # another full run. Measured, and the exception that did it was
            # ``PermissionError: [WinError 5]`` -- the same failure this
            # change exists to make legible.
            #
            # Recorded as well as survived, for the same reason as the red
            # branch: what is left on disk is a green receipt with a *red*
            # run's output file beside it, and the one way such a file has
            # ever been read here is by eye. Unexplained, it reads as this
            # sweep's own output.
            receipt["output_error"] = (
                f"{saved.name}: a previous red run's output could not be "
                f"removed and does not belong to this green sweep ({exc})"
            )
            print(
                f"WARNING: {saved} is left over from an earlier red run at "
                f"this commit and could not be removed ({exc}); it is not "
                f"this sweep's output. The receipt is filed and says so.",
                file=sys.stderr,
            )
    else:
        try:
            saved.write_text(_truncated(output), encoding="utf-8", errors="replace")
        except OSError as exc:
            # A companion file that cannot be written must not cost the verdict
            # the suite just spent minutes earning -- so the failure is caught
            # and the receipt is still filed.
            #
            # But it is recorded, and that half is not optional. Warning to
            # stderr alone would put the only account of the loss in a terminal,
            # which is the exact failure this whole change exists to remove: the
            # process that knows dies, and what is left on disk is a red receipt
            # with no output -- indistinguishable, byte for byte, from one filed
            # before this field existed. A reader would conclude nothing was
            # ever meant to be there. `output_error` is what makes "there is
            # nothing beside this receipt" and "something should be beside this
            # receipt and here is why it is not" different states on disk.
            receipt["output_error"] = f"{saved.name}: {exc}"
            print(
                f"WARNING: could not save the suite output to {saved} ({exc}); "
                f"the receipt is filed without it, so this red run keeps only "
                f"the failing names.",
                file=sys.stderr,
            )
        else:
            receipt["output"] = saved.name

    dest.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts"
    print(
        f"receipt: {dest}\n  {summary} in {receipt['seconds']}s, "
        f"exit {proc.returncode}"
    )
    return 0 if proc.returncode == 0 else 1


def cmd_check(args) -> int:
    repo = args.repo.resolve()
    try:
        commit = _git(repo, "rev-parse", args.branch)
        tree = _git(repo, "rev-parse", args.branch + "^{tree}")
        code = code_tree(repo, tree)
        path = receipt_path(repo, commit, args.receipts)
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    stood_in_for = None
    if path.is_file():
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot tell: {path} is unreadable: {exc}", file=sys.stderr)
            return CANNOT_TELL
    else:
        # No receipt under this sha. The sweep judges content, not history, so
        # a green receipt for the same tree is a verdict about this commit --
        # the integration preview's sweep answering for the merge that lands
        # the same candidates is exactly that case, and it is what keeps a
        # batch to one sweep. Nothing is inferred: the sha it was recorded
        # under is printed alongside.
        found = find_receipt_by_tree(repo, tree, args.receipts)
        matched = "tree"
        if found is None:
            # Last rung, and the weakest: the trees really do differ, and the
            # claim is that they differ only in NON_CODE_ENTRIES. This is the
            # rung that stops the sweep step from breaking its own verify by
            # obeying itself -- the board commit it is told to make lands
            # after the sweep it is told to run, every round, by construction.
            found = find_receipt_by_code_tree(repo, code, args.receipts)
            matched = "code"
        if found is None:
            print(
                f"no sweep receipt for {args.branch} tip {commit[:12]} at {path}, "
                f"none green for its tree {tree[:12]}, and none green for its "
                f"code tree {code[:12]} (that tree without "
                f"{', '.join(sorted(NON_CODE_ENTRIES))}) -- spawn a subagent to "
                f"run 'python tools/sweep.py run --branch {args.branch}' in a "
                f"clean tree at that commit, then leave this step again. Do not "
                f"run the suite in this turn.",
                file=sys.stderr,
            )
            return 1
        path, receipt = found
        stood_in_for = (receipt.get("commit") or "?", matched)

    if receipt.get("dirty"):
        print(
            f"receipt {path} was recorded with --allow-dirty: it judged "
            f"{commit[:12]} plus uncommitted files, which is not a verdict about "
            f"the commit. Re-sweep a clean tree.",
            file=sys.stderr,
        )
        return 1

    counts = receipt.get("counts") or {}
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts"
    via = ""
    if stood_in_for is not None:
        sha, matched = stood_in_for
        via = (
            f"\nvia the receipt for {sha[:12]}: a different commit, the "
            f"same tree {tree[:12]}"
            if matched == "tree"
            else (
                f"\nvia the receipt for {sha[:12]}: a different tree "
                f"({(receipt.get('tree') or '?')[:12]}), identical outside "
                f"{', '.join(sorted(NON_CODE_ENTRIES))} -- code tree {code[:12]}"
            )
        )
    if receipt.get("exit_code") != 0 or counts.get("failed") or counts.get("error"):
        failures = "\n  ".join(receipt.get("failures") or []) or "(none recorded)"
        # Naming the file is the half that makes writing it worth anything: it
        # sits in a directory nobody browses, and the one time such a file was
        # read, it was found by eye. A reader of this message should not have
        # to guess that something is lying next to the receipt.
        parked = receipt.get("output")
        if parked and not (path.parent / parked).is_file():
            # The receipt names a file that is not there. Saying the path
            # plainly would be this line telling a lie it invented: measured on
            # a receipt left standing by a later run that deleted the file and
            # then failed to file its own verdict (claunch-r103), where the
            # gate named a path nothing was at. A pointer that cannot be
            # followed is also the loudest evidence that this receipt is not
            # what it looks like, so it is said rather than hidden.
            where = (
                f"\nfull output: {path.parent / parked} -- MISSING. This "
                f"receipt names an output file that is not on disk, so it may "
                f"not describe the last run of this commit."
            )
        elif parked:
            where = f"\nfull output: {path.parent / parked}"
        elif receipt.get("output_error"):
            # Say that the output is missing rather than absent. Otherwise this
            # message reads exactly like a receipt from before the field
            # existed, and the reader spends the search this line exists to
            # save them.
            where = (
                f"\nfull output: NOT SAVED -- {receipt['output_error']}"
                f"\n  (the mechanism behind these failures was not recorded; "
                f"re-running the sweep is the only way to get it back)"
            )
        else:
            where = ""
        # A receipt whose streams were lost says "no counts" for a reason that
        # has nothing to do with the suite, and the reader of a red gate is
        # about to go looking for a failing test it does not name. Put the
        # reason next to the emptiness it explains rather than leaving it in
        # the json for somebody who already stopped reading.
        blind = (
            f"\nstreams: {receipt['stream_error']}"
            if receipt.get("stream_error")
            else ""
        )
        print(
            f"sweep of {commit[:12]} was red: {summary}, exit "
            f"{receipt.get('exit_code')}\n  {failures}\n"
            f"command: {receipt.get('command')}{blind}{where}",
            file=sys.stderr,
        )
        return 1

    # A green verdict still passes -- the sweep judged the tree and found
    # nothing wrong, and a bookkeeping failure beside it does not change that.
    # But it is said out loud rather than swallowed: the file it is about is
    # sitting in the receipts directory looking like this sweep's output.
    if receipt.get("output_error"):
        print(f"WARNING: {receipt['output_error']}", file=sys.stderr)
    if receipt.get("stream_error"):
        print(f"WARNING: {receipt['stream_error']}", file=sys.stderr)
    print(
        f"sweep of {args.branch} tip {commit[:12]} green: {summary} "
        f"in {receipt.get('seconds')}s\ncommand: {receipt.get('command')}\n"
        f"swept by {receipt.get('session')} at {receipt.get('finished_at')}"
        f"{via}"
    )
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="sweep", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)

    for name, help_text in (
        ("run", "run the full suite and write a receipt (for a subagent)"),
        (
            "check",
            "pass only if a green receipt exists for the branch tip (for a gate)",
        ),
    ):
        p = sub.add_parser(name, help=help_text)
        # The working directory, and deliberately NOT the session's checkout.
        # Three gates under tools/ resolve this through
        # ``cflow.checkout.own_checkout`` instead (changed_tests, landed_check,
        # merge_ready), and the hole here looks like the fourth. It is not
        # (claunch-7sj.1).
        #
        # ``run`` sweeps the tree it is standing in -- ``cmd_run`` says so and
        # refuses when HEAD is not --branch -- and the whole point of the
        # design is that the tree is a scratch worktree detached at the tip,
        # which is neither the session's checkout nor the run's. Measured
        # 2026-09-22: the leader's sweep ran in C:/cl-sweep-93 (detached at
        # d5461356) while its session's recorded cwd was the main checkout,
        # which held an uncommitted path at that moment. ``own_checkout``
        # prefers the session over the working directory, so it would have
        # aimed the sweep at the main checkout -- the "master plus whatever is
        # lying around" receipt that ``cmd_run``'s refusal exists to stop.
        #
        # ``check`` reads a receipt keyed by ``repo_key`` (--git-common-dir)
        # and resolves --branch through shared refs, so every worktree of this
        # repository answers identically and there is nothing for the lookup
        # to correct.
        p.add_argument(
            "--repo",
            type=Path,
            default=Path("."),
            help=(
                "the checkout to work in (default: the working directory). "
                "For 'run' this is the tree the suite executes in, so point "
                "it at a clean checkout of --branch."
            ),
        )
        p.add_argument("--branch", default="master")
        p.add_argument(
            "--receipts",
            type=Path,
            default=None,
            help="override the receipt root (tests)",
        )
        if name == "run":
            p.add_argument(
                "--command", default=None, help="sweep command (default: this repo's)"
            )
            p.add_argument(
                "--allow-dirty",
                action="store_true",
                help="sweep anyway, and mark the receipt untrusted",
            )

    args = ap.parse_args(argv)
    return cmd_run(args) if args.mode == "run" else cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
