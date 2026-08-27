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
DEFAULT_COMMAND = 'uv run --no-sync pytest tests -q -n 8 --basetemp="C:/t/{session}w"'

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
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
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

    Unreadable files are skipped rather than fatal: every caller is a fallback,
    and is already on its way to a red gate without one.
    """
    directory = receipts_dir(repo, override)
    if not directory.is_dir():
        return None
    best = None
    for path in sorted(directory.glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
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
    command = args.command or DEFAULT_COMMAND.format(session=session)

    started = datetime.now(timezone.utc)
    print(f"sweep: {command}\n  repo={repo}\n  {args.branch}={commit}", flush=True)
    proc = subprocess.run(
        command, shell=True, cwd=str(repo), capture_output=True, text=True
    )
    finished = datetime.now(timezone.utc)
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
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "seconds": round((finished - started).total_seconds(), 1),
    }
    dest = receipt_path(repo, commit, args.receipts)
    dest.parent.mkdir(parents=True, exist_ok=True)
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
        print(
            f"sweep of {commit[:12]} was red: {summary}, exit "
            f"{receipt.get('exit_code')}\n  {failures}\n"
            f"command: {receipt.get('command')}",
            file=sys.stderr,
        )
        return 1

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
        p.add_argument("--repo", type=Path, default=Path("."))
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
