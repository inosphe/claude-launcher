"""Would the parent accept this branch right now -- and if not, which fix?

The round trip this closes, as it actually runs. A worker rebases onto the
integration target, files ``LANDING REQUEST @ <tip>``, freezes its branch and
sits in ``await-landing`` doing nothing. Meanwhile the leader integrates in
*batches*: ``improv-leader`` opens a window every 300s and merges whatever has
collected in it, then sweeps. So the wait is long by design, and the target
moves during it -- other workers land. The leader's ``preflight`` then measures
the divergence by hand and sends the branch back with ``REBASE REQUESTED``.

Count what that rejection costs: a leader turn spent measuring, a mesh round
trip, a board transition (``in_review`` -> ``in_progress``) and back, and a
worker woken to redo a step it already passed. Now count what the rejection
*is*: two git commands. It is a measurement, not a judgement -- and cflow
already runs measurements without spending anyone's turn. ``verify`` is one
(the machine gate on leaving a step); ``awaits.probe`` is the other (the
daemon re-measures while the run sits still, and speaks only when the answer
changes). This script is the measurement both of them call, and the leader's
``preflight`` calls the same one, so the two sides cannot disagree about the
same branch any more.

Two verdicts, not one
---------------------
``improv-leader`` used to give one answer -- "rebase first" -- for two
different facts, and that conflation is the friction. They come apart:

* **Conflict.** ``git merge-tree`` says the two sides collide. Nothing but a
  rebase (or a merge somebody resolves) gets past that. Exit ``3``.
* **A moved baseline.** The target is ahead of the fork point but the merge is
  clean. The branch *merges*; what is stale is the **evidence**. A worker's
  targeted test numbers were taken on its own base, and if the base moved they
  do not describe the tree that will land. Exit ``1``.

Distance alone is not the first one. With ``behind == 0`` the target is an
ancestor of this branch and a ``--no-ff`` merge cannot collide with anything;
demanding a rebase there is an empty demand. That much is now settled between
worker and leader (board ``claunch-mde``, leader ruling of 2026-08-26).

But a clean ``merge-tree`` is not a pass either, and the measured case is from
this very round: ``s130``'s branch added a test requiring every gate under
``tools/`` to put this checkout's ``src`` first, while master meanwhile landed
a fifth gate (``landed_check.py``) that did not. Different files, so the text
does not collide and ``merge-tree`` reports clean -- and the merged tree is
red. It took a human rebasing to meet it; the fix is ``90a07a9``. A gate that
passed on "no conflict" would have shipped that red. So ``behind > 0`` demands
a re-measurement even when the merge is clean.

Re-measuring without rebasing
-----------------------------
The leader accepts the cheaper remedy: build the merge that will actually
happen, run the targets on *that*, and report the numbers -- the branch itself
never moves, so the tip the reviewer signed off on stays the tip that lands.
This script makes that remedy a **fact it can check** rather than a claim in a
report. The worker leaves the preview merge behind as a ref:

    git worktree add --detach <tmp> <target>
    git -C <tmp> merge --no-ff <branch>
    <run the targeted tests in <tmp>>
    git update-ref refs/claunch/preview/<branch> $(git -C <tmp> rev-parse HEAD)

and this asks one question of it: are its two parents exactly this tip and the
target's tip? If they are, the numbers were taken on the pair being judged
now. If the target moves again, the second parent stops matching and the
verdict goes back to ``1`` by itself -- which is the property the whole design
turns on, because that is the moment a stale baseline is created and nobody is
looking.

What it does not prove, kept honest in the same spirit as
``landed_check.py``: that anyone actually ran the tests on that preview. It
proves the tree existed and which two commits it merged. The numbers in the
report remain the worker's word, judged by the leader, as they always were.
Nor does it know whether the merged tree runs -- ``git`` being quiet means the
lines did not collide. The leader's post-merge sweep is the authority there,
and ``mergecheck.py`` covers the one silent case in between (one side deletes
a symbol the other started calling).

Ready on the branch side is not "the merge can run"
---------------------------------------------------
Everything above is about two commits. A merge happens in a **working tree**,
and git refuses to start one whose result would write over a file that tree is
dirty on -- before the merge begins, with no conflict and nothing for the
branch's owner to fix. This repository shares one checkout between many
sessions, so that is a normal state rather than an exception, and it caught
two consecutive integration rounds: the gate answered ``0``, the leader
entered its merge step, and ``git merge`` refused because another session's
uncommitted work sat in four of the files being merged. Both times a human
found it by running ``git status`` on a hunch.

``--checkout`` closes that: give it the tree the merge will run in (or pass it
bare and it finds whichever worktree has the target checked out), and it
intersects "files the merge writes" with "files that tree is dirty on". A
non-empty intersection is exit ``4``. It is **off by default** because this
same script is the worker's alignment gate, run from the worker's own
worktree, and a worker's worktree is dirty because it is working -- on by
default it would answer ``4`` for every worker doing its job.

Exit codes -- ``0`` ready, ``1`` re-measure, ``2`` could not tell, ``3``
rebase, ``4`` dirty checkout. Separate codes rather than one because
``awaits.probe`` compares **exit codes only** and deliberately ignores output
(``cflow/model.py``, :class:`Awaits`): folded together, a branch that went
from stale to conflicted would flip nothing and the daemon would say nothing.
``2`` keeps the meaning ``landed_check.py`` gave it so the two gates read
alike, and ``4`` is new rather than a reuse so that no existing caller's
reading of ``0``/``1``/``2``/``3`` changes.

What the output has to carry, per the board's convention on quiet empty
answers (``claunch-peyn``): an empty answer prints its denominator ("0 of 12
files the merge writes"), an input that cannot be read is refused rather than
matched to nothing, entries that did not parse are counted and keep the answer
off green, and a verdict that skipped the tree check says it skipped it.

Output is ASCII only, for the reason ``mergecheck`` gives: it prints into a
Windows console as often as a Unix one, and a character outside the code page
raises rather than garbles.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Every gate script under tools/ puts this checkout first. This one imports
# nothing from the package -- it asks git and nothing else -- and keeps the
# rule anyway, for the reason landed_check.py gives: the line that makes an
# import resolve against the tree being checked is worth more sitting here
# than remembered. tests/test_gates_run_this_checkout.py holds them all to it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

READY = 0
REMEASURE = 1
CANNOT_TELL = 2
REBASE = 3

#: The branch side is ready and the merge still cannot start, because the
#: working tree it would run in is dirty on a file the merge writes. A fifth
#: code rather than folding into ``1`` or ``3`` for the reason the other four
#: are separate: ``awaits.probe`` compares exit codes and ignores output, and
#: the remedy here is neither a rebase nor a re-measurement -- nothing the
#: branch's owner can do fixes it. Only reachable with ``--checkout``.
DIRTY_CHECKOUT = 4

#: The target a branch integrates into when nothing says otherwise. A nested
#: worker's target is its parent's branch, not this -- see :func:`_target`.
DEFAULT_TARGET = "master"

#: Where a preview merge is left for this gate to find. Under ``refs/claunch``
#: rather than ``refs/heads`` so it is not a branch: it must not show up in
#: ``git branch``, must not be pushed by default, and must never be something
#: the leader could merge by mistake.
PREVIEW_NS = "refs/claunch/preview"

#: ``--checkout`` with no path: find the tree from the target instead of being
#: told where it is. Passing a path means the caller remembers where the
#: target lives, and that memory is the thing that goes stale -- an
#: integration branch is often checked out nowhere at all, and master moves
#: between checkouts. git already knows; ask it.
DERIVE_FROM_TARGET = "@target"

#: How many blocking paths are printed before the rest are counted instead.
#: The count is printed either way: a gate that truncates without saying so
#: reads as "that was all of them".
BLOCKED_LISTED = 10


def _resolve_repo(explicit: Optional[str]) -> Tuple[Path, str]:
    """Which checkout to ask about, and how we came to think so.

    Same resolution as ``landed_check.py``, and for the same defect: the run's
    directory was assumed to be this session's own tree, and it is not
    whenever the run was keyed at the repository root while the worker stands
    in a worktree. This gate failed that shape in the other direction --
    standing on ``master`` it read ``tip == target_tip`` and answered
    ``ready: nothing to land``, exit 0, about a branch it never looked at.

    ``--repo`` wins outright so tests and hand-runs never depend on a daemon;
    that precedence lives in ``own_checkout`` so all three answers are decided
    in one place. Every failure of the lookup falls back to the working
    directory, the answer this always gave.
    """
    try:
        from claude_launcher.cflow import checkout

        where, how = checkout.own_checkout(explicit)
        return Path(where), how
    except Exception:
        # No package, no daemon, no managed session: the answer every caller
        # gave before this existed.
        return Path(explicit or ".").resolve(), "named" if explicit else "run cwd"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _target(repo: Path, branch: str) -> Tuple[str, str]:
    """The branch this one integrates into, and how we came to think so.

    A ``verify`` command is a static string in a workflow file, and the target
    is not static: a worker reporting to the leader integrates into master, a
    worker stacked under a middle worker integrates into its parent's branch.
    Git already has a name for "the branch mine is based on", so that is asked
    first -- ``git branch --set-upstream-to=<base>`` costs the worker one
    command and turns the base into a fact the machine can read instead of a
    sentence in a report.
    """
    up = _git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        f"{branch}@{{upstream}}",
    )
    if up.returncode == 0 and up.stdout.strip():
        return up.stdout.strip(), "upstream"
    return DEFAULT_TARGET, "default"


def _same_branch(branch: str, target: str) -> bool:
    """Are these two names the same branch?

    ``master`` and ``origin/master`` are one branch under two names, and
    :func:`_target` hands back whichever one git records as the upstream. The
    first version of this check compared the two names for equality and so
    missed exactly the case it was written for: standing on ``master`` with
    the upstream set, the target came back ``origin/master``, the check did
    not fire, and the gate answered ``ready: aligned`` exit 0 about a branch
    it had never looked at.

    The suffix test is not a convenience about remotes. Asking "is X ready to
    merge into X" has no answer whichever spelling each side arrives in, and a
    branch tracking its own remote counterpart is that question too.
    """
    return branch == target or target.endswith("/" + branch)


def _count(repo: Path, rng: str) -> Optional[int]:
    proc = _git(repo, "rev-list", "--count", rng)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _resolve(repo: Path, rev: str) -> Optional[str]:
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _conflicts(repo: Path, branch: str, target: str) -> Tuple[Optional[bool], str]:
    """Does merging the two collide? (``None`` = could not ask.)

    ``git merge-tree --write-tree`` (2.38+) answers with its exit code: 0
    clean, 1 conflicted, above that an error. Older git has only the
    three-argument form, which prints conflict hunks and exits 0 either way,
    so there the answer is read out of the output. Both are supported because
    the mesh is not one machine.
    """
    modern = _git(repo, "merge-tree", "--write-tree", branch, target)
    if modern.returncode in (0, 1):
        return bool(modern.returncode), "merge-tree --write-tree"
    base = _git(repo, "merge-base", branch, target)
    if base.returncode != 0:
        return None, "no merge base"
    legacy = _git(repo, "merge-tree", base.stdout.strip(), branch, target)
    if legacy.returncode != 0:
        return None, "merge-tree failed"
    return ("<<<<<<<" in legacy.stdout), "merge-tree (legacy)"


def _preview_covers(repo: Path, ref: str, tip: str, target_tip: str) -> Optional[str]:
    """The preview merge at ``ref`` if it merges exactly this pair, else None.

    Parents are compared as a set of two: which side was merged into which
    does not change what tree was measured, and pinning the order would make
    the gate depend on how the worker happened to spell the merge.
    """
    head = _resolve(repo, ref)
    if head is None:
        return None
    proc = _git(repo, "rev-list", "--parents", "--max-count=1", head)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    parents = proc.stdout.split()[1:]
    if len(parents) == 2 and set(parents) == {tip, target_tip}:
        return head
    return None


def _worktrees(repo: Path) -> Optional[List[Tuple[Path, Optional[str], str]]]:
    """Every checkout of this repository: ``(path, branch, HEAD)``.

    ``branch`` is ``None`` for a detached worktree, and that is not a gap to
    paper over: a detached checkout holds no branch, so no named target can be
    found in it and it can never be the tree a named merge runs in.
    """
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return None
    found: List[Tuple[Path, Optional[str], str]] = []
    path: Optional[str] = None
    head = ""
    branch: Optional[str] = None

    def flush() -> None:
        if path is not None:
            found.append((Path(path), branch, head))

    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            flush()
            path, head, branch = line[len("worktree ") :].strip(), "", None
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
    flush()
    return found


def _dirty(checkout: Path) -> Tuple[Optional[set], int]:
    """Paths this checkout is not clean on, and how many entries went unread.

    ``-z`` rather than plain ``--porcelain``: porcelain v1 quotes and escapes
    any path it cannot print raw, so the plain form hands back a spelling that
    does not match what ``git diff --name-only`` returns for the same file --
    an intersection computed across the two would silently miss it.

    The second number is why this returns a pair. The board's output
    convention (``claunch-peyn``, rule 4) is that a gate says how much it
    could not read: an unparsed status entry is a file this check is blind
    to, and dropping it quietly is how a check ends up green about a tree it
    did not finish reading.
    """
    proc = _git(checkout, "status", "--porcelain", "-z")
    if proc.returncode != 0:
        return None, 0
    fields = proc.stdout.split("\0")
    paths: set = set()
    unread = 0
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            unread += 1
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            # A rename or copy carries its source as the next NUL-separated
            # field. Both ends matter: the merge can be blocked by either.
            if i < len(fields) and fields[i]:
                paths.add(fields[i])
                i += 1
            else:
                unread += 1
    return paths, unread


def _merge_updates(repo: Path, base: str, tip: str) -> Tuple[Optional[set], str]:
    """Paths that differ between ``base`` and the tree that merges ``tip`` in.

    This is the set git actually checks before it will begin a merge, and it
    is **not** "the files the branch changed". What stops a merge is a path
    whose content the merge would write, which is a property of the *result*
    tree measured against the tree the checkout is sitting on.

    Measured here on git 2.48.1, three cases:

    * the merge updates ``a.py`` and ``a.py`` is locally modified -> refused,
      ``error: Your local changes to the following files would be overwritten
      by merge``
    * an untouched ``b.py`` is locally modified -> the merge runs
    * ``a.py`` is locally modified to byte-for-byte what the merge would
      write -> still refused; git compares the index, not the content

    The third case is why this compares path sets and never contents: there
    is no local edit to a written path that git lets through, so reading the
    files could only produce a false green.

    Old git without ``merge-tree --write-tree`` falls back to the fork-point
    diff, which is the branch-side path set -- close, and not the same
    measurement. The caller prints which of the two answered.
    """
    merged = _git(repo, "merge-tree", "--write-tree", base, tip)
    if merged.returncode == 0:
        lines = merged.stdout.strip().splitlines()
        tree = lines[0].strip() if lines else ""
        if not tree:
            return None, "merge-tree --write-tree returned no tree"
        diff = _git(repo, "diff", "--name-only", "-z", base, tree)
        if diff.returncode != 0:
            return None, "git could not diff the merge result"
        return {p for p in diff.stdout.split("\0") if p}, "merge-tree --write-tree"
    if merged.returncode == 1:
        return None, "merge-tree --write-tree reports the merge conflicts"
    fork = _git(repo, "merge-base", base, tip)
    if fork.returncode != 0 or not fork.stdout.strip():
        return None, "no merge base"
    diff = _git(repo, "diff", "--name-only", "-z", fork.stdout.strip(), tip)
    if diff.returncode != 0:
        return None, "git could not diff the fork point"
    return (
        {p for p in diff.stdout.split("\0") if p},
        "fork-point diff (git too old for merge-tree --write-tree)",
    )


def _checkout_check(
    repo: Path,
    checkout: str,
    branch: str,
    target: str,
    tip: str,
) -> Tuple[int, List[str], List[str]]:
    """Would the tree the merge runs in refuse to start it?

    Returns the verdict and the lines that justify it, rather than printing:
    the same measurement is reported two ways -- as the gate's own answer when
    the branch side is ready, and as a note beside a verdict it does not get
    to change when it is not.
    """
    out: List[str] = []
    err: List[str] = []

    if checkout == DERIVE_FROM_TARGET:
        trees = _worktrees(repo)
        if trees is None:
            err.append(
                f"cannot tell: git could not list the checkouts of {repo}, so "
                f"there is no way to find where {target} lives -- name the "
                f"tree with --checkout <path>"
            )
            return CANNOT_TELL, out, err
        holding = [t for t in trees if t[1] and _same_branch(t[1], target)]
        if not holding:
            # No checkout holds the target, so this gate has not found the
            # tree the merge will run in. The first version of this answered
            # READY with "no working tree can refuse this merge", and that
            # sentence claims more than was measured: a tree that does not
            # hold the target *now* can be switched onto it later and still
            # refuse. Rule 3 of ``claunch-peyn`` settles it -- a check that
            # did not run does not count towards a green.
            #
            # One case is a real pass rather than a shrug, and it is worth
            # separating because it is common: if the merge writes no files
            # at all (an already-landed branch, a branch with nothing on it),
            # then no tree anywhere can refuse it and the missing checkout
            # costs nothing.
            target_tip = _resolve(repo, target)
            if target_tip is None:
                err.append(
                    f"cannot tell: none of {len(trees)} worktree(s) has "
                    f"{target} checked out, and {target} does not resolve to "
                    f"a commit either"
                )
                return CANNOT_TELL, out, err
            updates, how = _merge_updates(repo, target_tip, tip)
            if updates is not None and not updates:
                out.append(
                    f"checkout: this merge writes 0 files, so no working "
                    f"tree can refuse it -- and none of {len(trees)} "
                    f"worktree(s) has {target} checked out [{how}]"
                )
                return READY, out, err
            writes = "an unknown number of" if updates is None else str(len(updates))
            err.append(
                f"cannot tell: none of {len(trees)} worktree(s) has {target} "
                f"checked out, so there is no tree to measure the {writes} "
                f"file(s) this merge writes against. Name the tree the merge "
                f"will run in with --checkout <path>, or check {target} out "
                f"first [{how}]"
            )
            return CANNOT_TELL, out, err
        where = holding[0][0]
    else:
        where = Path(checkout)

    head = _git(where, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head.returncode != 0 or not head.stdout.strip():
        err.append(
            f"cannot tell: --checkout {where} is not a git checkout with a "
            f"commit on HEAD. It wants the working tree the merge will run "
            f"in; pass --checkout with no value (or {DERIVE_FROM_TARGET!r}) "
            f"to find that tree from {target} instead"
        )
        return CANNOT_TELL, out, err
    head_sha = head.stdout.strip()
    on = _head_name(where) or "detached"

    updates, how = _merge_updates(repo, head_sha, tip)
    if updates is None:
        err.append(
            f"cannot tell: could not work out which files the merge writes in "
            f"{where} ({how})"
        )
        return CANNOT_TELL, out, err
    dirty, unread = _dirty(where)
    if dirty is None:
        err.append(f"cannot tell: git status failed in {where}")
        return CANNOT_TELL, out, err

    stamp = (
        f"{where} (on {on}, {head_sha[:12]}) -- {len(updates)} file(s) the "
        f"merge writes, {len(dirty)} dirty entry(ies) in that tree [{how}]"
    )
    if not _same_branch(on, target):
        # Answered anyway, because the intersection below IS the true answer
        # for that tree -- but said out loud, so this verdict cannot later be
        # quoted as an answer about the target.
        stamp += f"; note: that tree is on {on}, not the target {target}"

    blocked = sorted(updates & dirty)
    if blocked:
        out.append(f"dirty checkout: {len(blocked)} of {len(updates)} -- {stamp}")
        for path in blocked[:BLOCKED_LISTED]:
            out.append(f"    {path}")
        if len(blocked) > BLOCKED_LISTED:
            out.append(f"    ... and {len(blocked) - BLOCKED_LISTED} more, not listed")
        out.append(
            f"  git merge {branch} is refused there before it starts: "
            f'"Your local changes to the following files would be '
            f'overwritten by merge"'
        )
        out.append(
            "  a rebase does not fix this and neither does a re-measurement "
            "-- either that tree gets clean, or the merge runs somewhere that "
            "is"
        )
        return DIRTY_CHECKOUT, out, err

    if unread:
        # Rule 3 of claunch-peyn: what was not read does not count as green.
        # An unparsed status entry could be the one file that blocks.
        err.append(
            f"cannot tell: {unread} status entry(ies) in {where} did not "
            f"parse, so 0 blocked files is not an answer -- {stamp}"
        )
        return CANNOT_TELL, out, err

    out.append(f"checkout: 0 of {len(updates)} blocked -- {stamp}")
    return READY, out, err


def _ready(
    repo: Path,
    checkout: Optional[str],
    branch: str,
    target: str,
    tip: str,
) -> int:
    """``READY``, unless the tree the merge lands in would refuse to start it.

    The gap this closes was measured in two consecutive rounds, both times
    found by a human after the gate had already answered ``0``: the branch was
    ready, the checkout holding the target had another session's uncommitted
    work in files the merge writes, and ``git merge`` refused before it began.
    The gate had never looked at a working tree -- it asked only about the
    relationship between two commits, and that relationship was genuinely
    fine. Board ``claunch-merge-ready-dirty-checkout-7w7g``.

    **Off unless ``--checkout`` is given**, and the default is the design
    rather than caution. This same script is the *worker's* alignment gate,
    run from the worker's own worktree, and a worker's worktree is dirty
    because it is working. On by default it would answer ``4`` for every
    worker doing its job and wall off the step it exists to open. Only the
    side about to run the merge knows where the merge runs, so only that side
    asks.

    **This answer perishes in a way the others do not.** Every other verdict
    here is a function of commits, so quoting one later is quoting something
    that either still holds or is visibly stale from the hashes. This one is
    a function of live working trees -- who is standing where, and what they
    have not committed yet. Measured: the same code and the same two commits
    answered ``4``, then ``0``, because a worktree was removed in between.
    So a ``checkout:`` line is only true of the moment it was printed, and
    citing one as evidence means citing its timestamp with it.
    """
    if checkout is None:
        return READY
    code, out, err = _checkout_check(repo, checkout, branch, target, tip)
    for line in out:
        print(f"  {line}")
    for line in err:
        print(line, file=sys.stderr)
    return code


def _checkout_note(
    repo: Path,
    checkout: Optional[str],
    branch: str,
    target: str,
    tip: str,
) -> None:
    """Report the tree check beside a verdict it does not get to change.

    A conflict (``3``) and a moved baseline (``1``) have to be dealt with
    whatever the tree looks like, so they keep the exit code. The dirty tree
    is a different problem with a different owner -- the session whose
    uncommitted work is in the way -- and clearing it needs a human, so it is
    the slowest thing in the loop. Both times this gap bit, what it cost was
    the delay between the gate answering and somebody happening to run ``git
    status``; holding the finding back until the branch side is ready would
    rebuild exactly that delay one step further along.

    Silence would be the other error. A ``--checkout`` run that printed
    nothing about any tree reads as a tree that was looked at and found
    clean, which is rule 3 of ``claunch-peyn`` -- so this prints on every
    path, including the ones where it found nothing.

    All of it goes to **stdout**, including the lines saying the tree could
    not be read, and that is rule 7 of the same document: a warning written
    only to stderr is not a record, because the thing reading a gate does not
    read stderr. Here it would be worse than not recorded -- the header
    below is on stdout, so a reader who gets the header and nothing under it
    sees a tree that was examined and found clean. The gate's own verdict
    keeps stderr, because there the exit code is what distinguishes it.
    """
    if checkout is None:
        return
    code, out, err = _checkout_check(repo, checkout, branch, target, tip)
    print("  also, the working tree this merge would run in:")
    for line in out + err:
        print(f"    {line}")
    if code == DIRTY_CHECKOUT:
        print(
            "    that is a second, independent problem -- this branch cannot "
            "clear it, and the verdict above stays the exit code"
        )


def _recipe(branch: str, target: str, ref: str) -> str:
    """The cheaper remedy, spelled out where the verdict is read."""
    return (
        "  re-measure on the merge that will actually happen (the branch does "
        "not move):\n"
        f"    git worktree add --detach <tmp> {target}\n"
        f"    git -C <tmp> merge --no-ff {branch}\n"
        "    <run the targeted tests in <tmp>, report the numbers>\n"
        f"    git update-ref {ref} $(git -C <tmp> rev-parse HEAD)\n"
        f"  or rebase: git rebase {target}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Is this branch ready to be merged, or does it need a rebase "
            "(conflict) or a re-measurement (moved baseline) first?"
        )
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "repository to ask about. Omitted: this session's own checkout as "
            "the daemon records it, falling back to the working directory"
        ),
    )
    parser.add_argument(
        "--branch", default="HEAD", help="the branch asking to land (default: HEAD)"
    )
    parser.add_argument(
        "--target",
        default=None,
        help=(
            "the branch it integrates into. Default: this branch's git "
            f"upstream if one is set, else {DEFAULT_TARGET!r}. A worker "
            "stacked under a middle worker must name its parent's branch "
            "(or set it as the upstream), not master."
        ),
    )
    parser.add_argument(
        "--preview-ref",
        default=None,
        help=(
            f"where a preview merge is left for this gate to find (default: "
            f"{PREVIEW_NS}/<branch>). A merge commit whose two parents are "
            f"this tip and the target's tip answers the moved baseline."
        ),
    )
    parser.add_argument(
        "--checkout",
        nargs="?",
        const=DERIVE_FROM_TARGET,
        default=None,
        metavar="PATH",
        help=(
            "also ask whether the merge could start in the working tree it "
            "will run in. Give a path, or pass it bare (same as "
            f"{DERIVE_FROM_TARGET!r}) to use whichever worktree has the "
            "target checked out. A non-empty intersection of 'files the merge "
            f"writes' and 'files that tree is dirty on' answers "
            f"{DIRTY_CHECKOUT}, a code no other verdict uses. OFF BY DEFAULT: "
            "this same gate is the worker's alignment gate, and a worker's "
            "own worktree is dirty because it is working"
        ),
    )
    parser.add_argument(
        "--max-behind",
        type=int,
        default=None,
        metavar="N",
        help=(
            "escalate a moved baseline to a rebase once the target is more "
            "than N commits ahead of the fork point, clean merge or not. Off "
            "by default: distance alone does not force a rebase."
        ),
    )
    args = parser.parse_args(argv)
    repo, repo_how = _resolve_repo(args.repo)

    tip = _resolve(repo, args.branch)
    if tip is None:
        print(
            f"cannot tell: no commit {args.branch!r} in {repo} "
            f"(not a repository?)",
            file=sys.stderr,
        )
        return CANNOT_TELL

    target, how = (args.target, "named") if args.target else _target(repo, args.branch)
    target_tip = _resolve(repo, target)
    if target_tip is None:
        print(
            f"cannot tell: no branch {target!r} in {repo} ({how}) -- name the "
            f"integration target with --target, or set it as this branch's "
            f"upstream",
            file=sys.stderr,
        )
        return CANNOT_TELL

    branch_name = args.branch if args.branch != "HEAD" else _head_name(repo) or "HEAD"

    # Standing on the integration target is not a landing candidate, and the
    # ancestry shortcut below cannot tell that apart from "branch cut, nothing
    # committed yet": both reach tip == target_tip. The shortcut's own comment
    # says calling that "landed" would be a gate telling a lie, and it is the
    # same lie here with none of the same excuse -- this checkout holds no
    # branch that is asking to land. Measured at run cwd = repository root,
    # HEAD = master, with the worker's actual branch unexamined in its own
    # worktree: exit 0 either way, by two different routes -- "ready: nothing
    # to land" when the target resolved to `master`, and "ready: aligned --
    # target origin/master (upstream) +0 / branch +660" when it resolved
    # through the upstream. Hence _same_branch rather than ==.
    if _same_branch(branch_name, target):
        print(
            f"cannot tell: this checkout ({repo}, {repo_how}) is on "
            f"{branch_name}, which is the integration target itself "
            f"({target}, {how}) -- it is not a landing candidate, so there is "
            f"no readiness to report here. Ask about the candidate branch: "
            f"run this in that branch's worktree, or pass --repo <that "
            f"worktree> / --branch <it>.",
            file=sys.stderr,
        )
        return CANNOT_TELL

    # Ancestry first: after a --no-ff landing the target IS ahead of this tip,
    # and reading that as a moved baseline would wake a worker whose work
    # succeeded and send it to rebase onto a target that already contains it.
    if _git(repo, "merge-base", "--is-ancestor", tip, target_tip).returncode == 0:
        if tip == target_tip:
            # A branch cut and not yet committed on passes ancestry too, and
            # calling that "landed" would be a gate telling a lie -- the thing
            # these scripts exist not to do.
            print(f"ready: nothing to land -- {args.branch} is {target} ({tip[:12]})")
        else:
            print(f"ready: landed -- {target} already contains {tip[:12]}")
        # Both of these merge nothing, so the tree check runs against an
        # empty write set and says so with the denominator rather than being
        # special-cased out. "0 of 0" is a value.
        return _ready(repo, args.checkout, branch_name, target, tip)

    behind = _count(repo, f"{tip}..{target_tip}")
    ahead = _count(repo, f"{target_tip}..{tip}")
    if behind is None or ahead is None:
        print(
            f"cannot tell: git could not walk {args.branch}..{target}",
            file=sys.stderr,
        )
        return CANNOT_TELL

    where = f"target {target} ({how}) +{behind} / branch +{ahead}"
    if behind == 0:
        print(f"ready: aligned -- {where}")
        return _ready(repo, args.checkout, branch_name, target, tip)

    conflicted, asked_by = _conflicts(repo, tip, target_tip)
    if conflicted is None:
        print(f"cannot tell: {asked_by} -- {where}", file=sys.stderr)
        return CANNOT_TELL

    ref = args.preview_ref or f"{PREVIEW_NS}/{branch_name}"

    if conflicted:
        print(f"rebase: pre-merge conflicts -- {where} [{asked_by}]")
        print(f"  git rebase {target}")
        _checkout_note(repo, args.checkout, branch_name, target, tip)
        return REBASE
    if args.max_behind is not None and behind > args.max_behind:
        print(
            f"rebase: {behind} behind, over --max-behind {args.max_behind} -- "
            f"{where} (the merge itself is clean)"
        )
        print(f"  git rebase {target}")
        _checkout_note(repo, args.checkout, branch_name, target, tip)
        return REBASE

    preview = _preview_covers(repo, ref, tip, target_tip)
    if preview:
        print(
            f"ready: re-measured on {preview[:12]} -- {where} [{asked_by}, "
            f"preview {ref}]"
        )
        return _ready(repo, args.checkout, branch_name, target, tip)

    print(f"re-measure: baseline moved, merge is clean -- {where} [{asked_by}]")
    print(
        f"  the targeted numbers were taken on a base {behind} commit(s) "
        f"behind {target}, so they do not describe the tree that will land"
    )
    print(_recipe(branch_name, target, ref))
    _checkout_note(repo, args.checkout, branch_name, target, tip)
    return REMEASURE


def _head_name(repo: Path) -> Optional[str]:
    proc = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


if __name__ == "__main__":
    raise SystemExit(main())
