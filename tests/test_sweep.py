"""The batch sweep, split into a subagent that runs it and a gate that reads it.

``improv-leader``'s ``sweep`` step used to arm the whole suite as its
``verify``. The engine runs a verify synchronously as the run leaves the step,
so that one line meant the leader ran a 178-second sweep inside the very turn
its own workflow says it never sweeps in -- and because nobody typed the
command, nothing recorded that a sweep had happened at all.

``tools/sweep.py`` splits it. ``run`` executes the suite somewhere else (a
spawned subagent, in a clean tree) and leaves a receipt; ``check`` is the gate,
and only asks whether a green receipt exists for the branch's current tip.

These tests pin the four things that make the split safe rather than merely
smaller:

* the receipt is keyed by the commit it judged, so a stale one cannot be
  mistaken for a fresh one and no clock has to be trusted;
* it is keyed by the *repository*, not the working directory, so the leader's
  scratch sweep worktree answers for master in the main checkout;
* a dirty tree cannot silently produce one, because a sweep over somebody
  else's uncommitted files is a verdict about a different tree than the commit
  it claims (this repository closed six issues citing exactly such a number);
* a missing receipt is a *failure*, not a "cannot tell" -- the whole point is
  that a sweep which died leaves the gate red;
* and the board is subtracted from the tree before two commits are compared,
  because the step that arms this gate is told to commit ``.beads/`` *after*
  it sweeps -- so without that, following the instruction is what turns the
  step's own verify red. The cases below pin both directions of it: a
  board-only commit reuses the sweep, and anything else does not.

The suite is never actually run here: every case passes ``--command`` a cheap
stand-in. What is under test is the bookkeeping around the suite, which is
where the failure modes were.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SWEEP = Path(__file__).resolve().parents[1] / "tools" / "sweep.py"


def _load():
    """``tools/`` is not a package -- load the script the way a script is."""
    spec = importlib.util.spec_from_file_location("sweep", SWEEP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep = _load()

#: Stand-ins for the suite. The real command's cost is the point of the split,
#: so the tests must not pay it.
GREEN = f'"{sys.executable}" -c "print(\'12 passed, 1 skipped in 3.4s\')"'
RED = (
    f'"{sys.executable}" -c "'
    "print('FAILED tests/test_x.py::test_a - AssertionError'); "
    "print('1 failed, 11 passed in 3.4s'); "
    'raise SystemExit(1)"'
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repository with one commit on ``master``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "branch", "-M", "master")
    return repo


@pytest.fixture
def receipts(tmp_path) -> Path:
    return tmp_path / "receipts"


def _run(repo: Path, receipts: Path, *args: str) -> int:
    return sweep.main(
        ["run", "--repo", str(repo), "--receipts", str(receipts), *args]
    )


def _check(repo: Path, receipts: Path, *args: str) -> int:
    return sweep.main(
        ["check", "--repo", str(repo), "--receipts", str(receipts), *args]
    )


def test_a_green_run_writes_a_receipt_the_gate_then_accepts(repo, receipts):
    """The happy path end to end: subagent runs, gate reads, round proceeds."""
    assert _run(repo, receipts, "--command", GREEN) == 0
    assert _check(repo, receipts) == 0


def test_the_gate_fails_when_no_sweep_has_run(repo, receipts, capsys):
    """A missing receipt is the *observable* form of a sweep that never
    finished -- the subagent died, the daemon restarted under it, nobody
    spawned one. It must be red, and it must say what to do.

    "Cannot tell" would be the wrong answer here even though it is literally
    true: this gate exists precisely to stop a round closing on a sweep that
    did not happen.
    """
    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert "no sweep receipt" in err
    assert "subagent" in err  # names the remedy, not just the symptom


def test_a_red_run_is_recorded_and_the_gate_repeats_the_failing_names(
    repo, receipts, capsys
):
    """A red sweep has to survive into the gate's output.

    The leader's next move is to send the failure back to the branch that
    caused it, and a gate that only says "red" makes it re-run the suite to
    find out what broke -- in its own turn, which is the thing being removed.
    """
    assert _run(repo, receipts, "--command", RED) == 1
    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert "test_x.py::test_a" in err
    assert "1 failed" in err


def test_the_receipt_is_keyed_to_the_commit_it_judged(repo, receipts):
    """A sweep of the previous tip is not a verdict about this one.

    This is what replaces a freshness window. Nothing here reads a clock, so
    there is no "recent enough" to get wrong, and a receipt cannot drift into
    covering a commit it never saw.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    assert _check(repo, receipts) == 0

    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")

    assert _check(repo, receipts) == 1, (
        "the receipt for the old tip was accepted for a new one"
    )


def test_a_receipt_from_one_worktree_answers_for_another(repo, receipts):
    """The arrangement the leader actually runs in.

    The sweep happens in a short-pathed scratch worktree detached at the tip,
    because the main checkout has other sessions' uncommitted files in it.
    The gate, however, runs wherever the cflow run is pinned. Keying on the
    git *common* dir is what lets those be different directories.
    """
    linked = repo.parent / "scratch"
    _git(repo, "worktree", "add", "--detach", str(linked), "master")

    assert sweep.repo_key(linked) == sweep.repo_key(repo)
    assert _run(linked, receipts, "--command", GREEN) == 0
    assert _check(repo, receipts) == 0, (
        "a sweep in a linked worktree did not answer for the main checkout"
    )


# --------------------------------------------------------------------------- #
# One sweep per integration window. The leader batches every landing request
# that arrives inside five minutes into one merge, and the whole point of the
# batch is that the suite runs once for it. It ran twice: once on the
# integration preview (the candidates merged onto master in a scratch branch,
# swept before master moves) and again on the merge commit that lands exactly
# those candidates. Same content, different sha -- and the receipt was keyed
# only by sha, so the second run was structurally forced.
# --------------------------------------------------------------------------- #


def _feature(repo: Path, name: str, filename: str) -> None:
    """A feature branch off master with one commit on it."""
    _git(repo, "checkout", "-q", "-b", name, "master")
    (repo / filename).write_text(f"{name}{chr(10)}", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    _git(repo, "checkout", "-q", "master")


def test_the_preview_sweep_answers_for_the_merge_that_lands_the_same_batch(
    repo, receipts, capsys
):
    """The batch pays for the suite once, not twice.

    The preview is built the way the merge will be -- the same candidates,
    onto master, in the same order, ``--no-ff`` -- so the merge commit's tree
    is byte-identical to the preview's. The sha differs, and nothing about
    the suite depends on a sha, so the preview's green is a verdict about the
    merge commit too.
    """
    _feature(repo, "feat-a", "a-change.txt")
    _feature(repo, "feat-b", "b-change.txt")

    preview = repo.parent / "preview"
    _git(repo, "worktree", "add", "-q", "-b", "preview", str(preview), "master")
    _git(preview, "merge", "--no-ff", "-q", "-m", "preview a", "feat-a")
    _git(preview, "merge", "--no-ff", "-q", "-m", "preview b", "feat-b")
    assert _run(preview, receipts, "--command", GREEN, "--branch", "preview") == 0

    _git(repo, "merge", "--no-ff", "-q", "-m", "land a", "feat-a")
    _git(repo, "merge", "--no-ff", "-q", "-m", "land b", "feat-b")

    preview_tip = _git(repo, "rev-parse", "preview").strip()
    master_tip = _git(repo, "rev-parse", "master").strip()
    assert preview_tip != master_tip
    assert (
        _git(repo, "rev-parse", "preview^{tree}").strip()
        == _git(repo, "rev-parse", "master^{tree}").strip()
    ), "the preview was not built the way the merge was -- the test is wrong"

    assert not sweep.receipt_path(repo, master_tip, receipts).exists()
    assert _check(repo, receipts) == 0, (
        "the batch was made to sweep its own content twice"
    )
    out = capsys.readouterr().out
    assert preview_tip[:12] in out and "the same tree" in out, (
        f"the gate did not say whose receipt it stood on: {out}"
    )


def test_a_red_receipt_for_the_same_tree_is_not_laundered_into_a_pass(
    repo, receipts, capsys
):
    """Reuse carries verdicts, not just receipts.

    If a red preview could be skipped over -- "no receipt for this tip, so
    sweep again" -- the cheapest way past a red gate would be to merge and
    re-run. Only green stands in; a red preview leaves the merge commit with
    nothing, which is red as well, and the message says which of the two it
    is.
    """
    _feature(repo, "feat-a", "a-change.txt")

    preview = repo.parent / "preview"
    _git(repo, "worktree", "add", "-q", "-b", "preview", str(preview), "master")
    _git(preview, "merge", "--no-ff", "-q", "-m", "preview a", "feat-a")
    assert _run(preview, receipts, "--command", RED, "--branch", "preview") == 1

    _git(repo, "merge", "--no-ff", "-q", "-m", "land a", "feat-a")
    assert _check(repo, receipts) == 1
    assert "none green for its tree" in capsys.readouterr().err


def test_a_dirty_receipt_does_not_stand_in_for_another_commit(repo, receipts):
    """The contaminated-sweep rule survives the tree lookup.

    ``--allow-dirty`` records a verdict about "that commit plus whatever was
    lying around". It is already refused for the commit it names; reusing it
    by tree would let the same sweep answer for a commit it is even further
    from.
    """
    (repo / "scratch.txt").write_text("somebody else's work", encoding="utf-8")
    assert _run(repo, receipts, "--command", GREEN, "--allow-dirty") == 0
    _git(repo, "commit", "-q", "--allow-empty", "-m", "same tree, new sha")

    assert _check(repo, receipts) == 1


def test_reuse_needs_the_same_content_not_merely_the_same_files(repo, receipts):
    """A tree hash is the content, so an edit ends the reuse.

    The window batches by time; the receipt does not. A candidate that lands
    after the preview was swept changes the tree, and the gate goes back to
    demanding a sweep of what will actually be served.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    (repo / "a.txt").write_text(f"two{chr(10)}", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "late candidate")

    assert _check(repo, receipts) == 1


def _board(repo: Path, text: str) -> None:
    """A commit that touches nothing but the board -- the shape of the bug.

    ``improv-leader``'s sweep step prescribes exactly this after the sweep:
    close the issues with the numbers the sweep produced, then commit
    ``.beads/issues.jsonl``. It is not a stray commit somebody could stop
    making; it is the step.
    """
    board = repo / ".beads"
    board.mkdir(exist_ok=True)
    (board / "issues.jsonl").write_text(text + chr(10), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore(beads): close the round")


def test_the_board_commit_the_step_prescribes_does_not_undo_its_own_sweep(
    repo, receipts, capsys
):
    """The bug this axis exists for: obeying the step is what broke it.

    Sweep the tip, close the issues with the numbers, commit the board, leave
    the step -- and the tip the verify reads is now one the receipt cannot
    name. Five rounds ran that way. The tree really did change, so
    ``find_receipt_by_tree`` cannot help; what did not change is anything the
    suite reads.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    swept = _git(repo, "rev-parse", "master").strip()
    _board(repo, '{"id": "x", "status": "closed"}')

    tip = _git(repo, "rev-parse", "master").strip()
    assert tip != swept
    assert (
        _git(repo, "rev-parse", "master^{tree}").strip()
        != _git(repo, "rev-parse", swept + "^{tree}").strip()
    ), "the board commit did not move the tree -- the test is not testing this"

    assert _check(repo, receipts) == 0
    out = capsys.readouterr().out
    assert swept[:12] in out and "code tree" in out, (
        f"the gate did not say what it stood on, or why: {out}"
    )


def test_a_commit_outside_the_board_still_demands_its_own_sweep(
    repo, receipts, capsys
):
    """The negative control: being wrong here has to cost a sweep, not a verdict.

    One of three things holding the exemption up, not the whole of it --
    ``NON_CODE_ENTRIES`` lists the other two (the board is not a pytest
    input; no test reads the repository's own copy). What this case pins is
    the deny direction: a change to real content after the receipt was
    written is what the gate exists for, and the exemption must not reach it.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    (repo / "a.txt").write_text("two" + chr(10), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a real change")

    assert _check(repo, receipts) == 1
    assert "none green for its code tree" in capsys.readouterr().err


def test_the_board_and_a_code_change_in_one_commit_is_not_exempt(repo, receipts):
    """Exempt is a property of the *difference*, not of the board being in it.

    A round that closes issues and fixes a line in the same commit has
    changed something the suite reads. Subtracting ``.beads`` leaves that
    line behind, so the digest moves and the gate stays red.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    (repo / ".beads").mkdir()
    (repo / ".beads" / "issues.jsonl").write_text("{}" + chr(10), encoding="utf-8")
    (repo / "a.txt").write_text("two" + chr(10), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "board and code together")

    assert _check(repo, receipts) == 1


def test_the_exemption_is_the_exact_name_not_a_prefix(repo, receipts):
    """``.beads`` is a name, and the entries are matched whole.

    The shape this rule was first written in was a path prefix
    (``grep -v '^[.]beads/'``), which also swallows a top-level ``.beadsx``
    -- a file nobody has measured and the suite might well read. Matching the
    entry exactly costs nothing and keeps the exemption to what was measured.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    (repo / ".beadsx").write_text("not the board" + chr(10), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a name that merely starts the same way")

    assert _check(repo, receipts) == 1


def test_the_board_exemption_does_not_launder_a_red_sweep(repo, receipts, capsys):
    """Green is still the only verdict that carries.

    Otherwise the exemption becomes the cheapest way past a red gate: commit
    the board, and the tip that was measured red is answered for by nothing.
    """
    assert _run(repo, receipts, "--command", RED) == 1
    _board(repo, '{"id": "x"}')

    assert _check(repo, receipts) == 1
    assert "none green for its code tree" in capsys.readouterr().err


def test_a_dirty_receipt_does_not_stand_in_for_a_board_commit(repo, receipts):
    """The contaminated-sweep rule survives this rung too.

    ``--allow-dirty`` is a verdict about a commit plus whatever was lying
    around. It is refused for the commit it names, and reuse by code tree
    must not be the way back in.
    """
    (repo / "scratch.txt").write_text("somebody else's work", encoding="utf-8")
    assert _run(repo, receipts, "--command", GREEN, "--allow-dirty") == 0
    _board(repo, '{"id": "x"}')

    assert _check(repo, receipts) == 1


def test_a_receipt_written_before_this_axis_existed_is_not_reused(repo, receipts):
    """Old receipts have no ``code_tree``, and absence is not a match.

    Receipts outlive the tool that wrote them -- they are kept outside the
    repository precisely so a restart does not lose them. One from before
    this field existed says nothing about the board, so the gate goes red,
    which is the direction this whole axis is built to fail in.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    swept = _git(repo, "rev-parse", "master").strip()
    path = sweep.receipt_path(repo, swept, receipts)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    del receipt["code_tree"]
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    _board(repo, '{"id": "x"}')
    assert _check(repo, receipts) == 1


def test_the_board_may_be_added_for_the_first_time(repo, receipts):
    """A repository without a board yet is the same case.

    The digest is over the entries that are kept, so an entry that appears is
    as invisible as one that changes. This is not hypothetical: a fresh
    worktree of this repository has no ``.beads`` until the first board write
    lands in it.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    assert not (repo / ".beads").exists()
    _board(repo, '{"id": "x"}')

    assert _check(repo, receipts) == 0


def test_the_exemption_names_one_thing_and_widening_it_is_an_edit(repo):
    """Pinned, because the tempting change here is to generalise it.

    Every argument for this rung -- pytest does not collect the file, no test
    reads the repository's own copy -- was measured about ``.beads`` and
    nothing else. A second name needs its own two-sided measurement, and this
    assertion is where that is noticed.
    """
    assert sweep.NON_CODE_ENTRIES == frozenset({".beads"})

    tree = _git(repo, "rev-parse", "master^{tree}").strip()
    assert sweep.code_tree(repo, tree) != tree, (
        "the code tree must not be the tree hash -- one of them is a lie"
    )


def test_a_tree_standing_on_another_commit_refuses_to_produce_a_receipt(
    repo, receipts, capsys
):
    """The bug this file shipped with, and the one a clean tree hides.

    The suite runs in the working tree; the receipt is filed under
    ``--branch``'s sha. Nothing tied those together, so running from a
    feature branch filed a receipt naming ``master`` -- reporting master red
    over a failure that only existed on the branch. It happened for real on
    the round that wrote this tool, and the receipt looked entirely healthy:
    a commit, a tree, counts, a command.

    ``git status`` cannot catch this. The tree in that incident was clean;
    it was a clean checkout of a *different* commit. So HEAD is compared
    directly, and before the dirty check, because naming the wrong commit is
    worse than naming a smudged one.
    """
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "only on the branch")

    assert _run(repo, receipts, "--command", GREEN) == sweep.CANNOT_TELL
    err = capsys.readouterr().err
    assert "refusing to sweep" in err
    assert "nothing tested" in err  # says why, not just that

    commit = _git(repo, "rev-parse", "master").strip()
    assert not sweep.receipt_path(repo, commit, receipts).exists(), (
        "a receipt was filed for a commit the working tree was not on"
    )


def test_the_head_check_precedes_the_dirty_check(repo, receipts, capsys):
    """Both wrong at once must report the wrong *commit*.

    A message about uncommitted files sends someone to `git stash`, which
    does nothing about standing on the wrong branch -- they would clean the
    tree and file the same lie.
    """
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "only on the branch")
    (repo / "wip.py").write_text("x = 1\n", encoding="utf-8")

    assert _run(repo, receipts, "--command", GREEN) == sweep.CANNOT_TELL
    err = capsys.readouterr().err
    assert "nothing tested" in err
    assert "refusing to sweep" in err and "uncommitted" not in err


def test_a_dirty_tree_refuses_to_produce_a_receipt(repo, receipts, capsys):
    """The failure this repository actually shipped, made impossible.

    A leader swept the shared checkout while other sessions had uncommitted
    work in it, collected four foreign tests, and reported 1562/1563 where
    clean master had 1559. Six issues were closed citing that number. The
    counts were not wrong about anything real -- they were about a tree that
    was never committed and can never be reproduced.
    """
    (repo / "someone_elses_wip.py").write_text("x = 1\n", encoding="utf-8")

    assert _run(repo, receipts, "--command", GREEN) == sweep.CANNOT_TELL
    err = capsys.readouterr().err
    assert "refusing to sweep" in err
    assert "someone_elses_wip.py" in err  # names what is in the way
    assert _check(repo, receipts) == 1, "a refused sweep must leave the gate red"


def test_an_explicitly_dirty_receipt_is_still_rejected_by_the_gate(
    repo, receipts, capsys
):
    """``--allow-dirty`` is an escape hatch for looking, not for passing.

    Someone debugging wants to sweep a tree they are editing. That is fine;
    what must not happen is that run silently becoming the batch's verdict.
    So the receipt records the fact and the gate refuses it -- the flag buys
    output, never a green gate.
    """
    (repo / "wip.py").write_text("x = 1\n", encoding="utf-8")
    assert _run(repo, receipts, "--command", GREEN, "--allow-dirty") == 0

    commit = _git(repo, "rev-parse", "master").strip()
    receipt = json.loads(
        sweep.receipt_path(repo, commit, receipts).read_text(encoding="utf-8")
    )
    assert receipt["dirty"] is True

    assert _check(repo, receipts) == 1
    assert "allow-dirty" in capsys.readouterr().err


def test_the_receipt_carries_the_command_in_full(repo, receipts):
    """Numbers without their command have circulated here as if comparable.

    Two "reference" counts were live at once -- one from ``-m "not worktree"``
    (about 1450) and one unfiltered (1558) -- and both travelled as *the*
    baseline until someone noticed they were counting different things. A
    receipt that carries the command makes that impossible to repeat: the
    axis travels with the number.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    commit = _git(repo, "rev-parse", "master").strip()
    receipt = json.loads(
        sweep.receipt_path(repo, commit, receipts).read_text(encoding="utf-8")
    )
    assert receipt["command"] == GREEN
    assert receipt["counts"] == {"passed": 12, "skipped": 1}
    assert receipt["commit"] == commit
    assert receipt["tree"] == _git(repo, "rev-parse", "master^{tree}").strip()


def test_an_unreadable_branch_cannot_tell_rather_than_failing(tmp_path):
    """Outside a repository the honest answer is "cannot tell", not "red".

    A gate that reports its own broken plumbing as a failed sweep teaches
    people to route around it, which is how ``|| true`` gets appended to a
    verify. ``tools/deploy_check.py`` splits these the same way.
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert (
        sweep.main(["check", "--repo", str(plain), "--receipts", str(tmp_path)])
        == sweep.CANNOT_TELL
    )


def test_a_corrupt_receipt_cannot_tell_rather_than_passing(repo, receipts):
    """Half-written json is the shape a killed subagent leaves behind."""
    assert _run(repo, receipts, "--command", GREEN) == 0
    commit = _git(repo, "rev-parse", "master").strip()
    sweep.receipt_path(repo, commit, receipts).write_text("{trunc", encoding="utf-8")
    assert _check(repo, receipts) == sweep.CANNOT_TELL


def test_a_hand_written_receipt_outside_the_protocol_is_not_a_verdict(
    repo, receipts, capsys
):
    """The file this issue was filed over, given content that would pass.

    ``1fdc7a7-s127sweep3-receipt.json`` was a manual receipt: a name outside
    ``{sha}.json``, under the receipts directory. It happened to be red, so
    nothing was lost -- but a green one would have been the useful lie, a
    verdict nobody earned. So the scan refuses any name ``run`` does not
    write, on the name alone, and says so: a decision that exists on disk but
    is not recognised is a decision that may be about to be paid for again.
    """
    assert _check(repo, receipts) == 1          # baseline: nothing has run

    tip = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    manual = sweep.receipts_dir(repo, receipts) / "1fdc7a7-s127sweep3-receipt.json"
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text(
        json.dumps(
            {
                "commit": tip,
                "tree": tree,
                "code_tree": "x",
                "exit_code": 0,
                "counts": {"passed": 1},
                "dirty": False,
            }
        ),
        encoding="utf-8",
    )

    assert _check(repo, receipts) == 1, (
        "a hand-written receipt outside {sha}.json was accepted as a verdict"
    )
    err = capsys.readouterr().err
    assert manual.name in err
    assert "not a {sha}.json" in err


def test_a_corrupt_receipt_in_the_fallback_scan_warns(repo, receipts, capsys):
    """A standard-named receipt that cannot be parsed used to vanish.

    ``cmd_check`` reads the tip's own receipt directly, and that path already
    reported (test_a_corrupt_receipt_cannot_tell). The tree/code fallbacks
    went through ``_newest_green``, which skipped a broken file without a
    word -- so a green verdict that had been filed and then corrupted
    silently cost the whole sweep again. Now the scan names the file.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    swept = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "commit", "-q", "--allow-empty", "-m", "same tree, new tip")
    tip = _git(repo, "rev-parse", "HEAD").strip()

    assert tip != swept
    assert (
        _git(repo, "rev-parse", "HEAD^{tree}").strip()
        == _git(repo, "rev-parse", swept + "^{tree}").strip()
    ), "the empty commit did not keep the tree -- the case needs the fallback"
    sweep.receipt_path(repo, swept, receipts).write_text("{trunc", encoding="utf-8")

    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert f"{swept}.json" in err
    assert "cannot be read" in err


def test_run_files_the_receipt_under_the_standard_name_only(repo, receipts):
    """``run`` is the one writer, and the one name is ``{sha}.json``.

    The pin for "a receipt is only ever produced by 'python tools/sweep.py
    run'": a run leaves exactly one file, in the receipts directory, named
    after the full sha it judged -- nothing under a subdirectory, nothing
    with an added label. A verdict scan would accept nothing else
    (test_a_hand_written_receipt_outside_the_protocol).
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    tip = _git(repo, "rev-parse", "HEAD").strip()

    assert list(receipts.rglob("*.json")) == [sweep.receipt_path(repo, tip, receipts)]
    assert [p.name for p in sweep.receipts_dir(repo, receipts).glob("*.json")] == [
        f"{tip}.json"
    ]


# --------------------------------------------------------------------------- #
# The sweep command itself. These constraints used to be pinned against the
# workflow's verify line; they moved here with the command, because they are
# properties of running this suite and that is now the only place it is run.
# --------------------------------------------------------------------------- #

#: Windows refuses a path this long; the run that broke measured exactly 260.
MAX_PATH = 260

#: What the longest path under basetemp costs *besides* basetemp -- xdist's
#: ``popen-gwN/``, the test directory, the transcript layout and the
#: conversation id. Measured against the path that actually raised.
PATH_CONSTANT = 162

#: The longest session name to budget for.
LONGEST_SESSION = "w" * 16

#: Measured on this suite (32 cores): serial 532s, ``-n 4`` 230s, ``-n 8``
#: 178s, ``-n auto`` (= 32 here) 184s. Past a handful of workers the curve is
#: flat, because the wall clock belongs to daemons and PTYs starting up, not
#: to arithmetic.
MAX_USEFUL_WORKERS = 8


def _default_command() -> str:
    return sweep.DEFAULT_COMMAND.format(session=LONGEST_SESSION)


def test_the_sweep_runs_bounded_parallel():
    """Parallel, but with a ceiling -- and the ceiling is the point.

    ``-n auto`` reads as the obvious choice and is the wrong one here. It
    measured no faster than ``-n 8`` while running four times the processes,
    and that contention starved the PTY-timing tests: one sweep in three
    failed ``test_delivery_holds_while_a_human_is_typing``, whose 20-second
    wait for a screen to render is generous until 32 workers are spawning
    daemons at once. A check that fails one run in three teaches people to
    re-run it, which is worse than a slow one.
    """
    import re

    width = re.search(r" -n (\S+)", _default_command())
    assert width, "the sweep lost its -n; it is serial again"
    assert width.group(1) != "auto", (
        "-n auto is one worker per core (32 here): no faster than -n 8 and "
        "flaky with it -- see this test's docstring"
    )
    assert 2 <= int(width.group(1)) <= MAX_USEFUL_WORKERS


def test_the_sweep_basetemp_leaves_room_for_xdist():
    """A short basetemp is a correctness requirement here, not tidiness.

    xdist inserts ``popen-gwN/`` under basetemp, and the transcript tests
    re-encode their whole cwd into one filename -- so every character of
    basetemp is spent twice and the path grows as
    ``2 * len(basetemp) + PATH_CONSTANT``. Measured: a 48-character basetemp
    lands on 258 and passes, 49 lands on 260 and raises ``FileNotFoundError``.
    """
    command = _default_command()
    basetemp = command.split('--basetemp="')[1].split('"')[0]
    longest = 2 * len(basetemp) + PATH_CONSTANT
    assert longest < MAX_PATH, (
        f"basetemp {basetemp!r} builds paths up to {longest} characters, over "
        f"Windows' {MAX_PATH}: xdist's popen-gwN/ and the transcript slug "
        f"under it spend every character of it twice"
    )


def test_the_sweep_basetemp_is_per_session():
    """pytest empties its basetemp at startup, so a shared one is destructive.

    Two sweeps on one path do not merely collide: the later one deletes the
    earlier one's temp trees mid-run, and the failures that produces look
    like the code under test.
    """
    assert "{session}" in sweep.DEFAULT_COMMAND


def test_the_sweep_does_not_resync_the_environment():
    """``uv sync`` cannot replace the ``claunch.exe`` a live daemon holds open.

    It fails with os error 5, which turns a check into a blocker. Preparing
    the tree is the worktree's job, once, before the sweep.
    """
    assert "--no-sync" in sweep.DEFAULT_COMMAND


def test_the_sweep_covers_the_whole_suite():
    """The leader's half of the division of labour.

    The worker deliberately runs a narrow selection now, which only works if
    something eventually runs everything. This is that something, so it must
    not grow a marker filter: ``-m "not worktree"`` here would silently drop
    the slow tail from the only run that covers it.
    """
    assert " -m " not in sweep.DEFAULT_COMMAND, (
        "the batch sweep must stay unfiltered -- it is the only run that "
        "covers the whole suite"
    )
    assert "pytest tests" in sweep.DEFAULT_COMMAND


def test_the_script_runs_as_a_script_and_its_exit_status_reaches_the_shell():
    """The verify line runs this the long way round, and only that path
    proves the file is executable and its exit code survives."""
    proc = subprocess.run(
        [sys.executable, str(SWEEP), "check", "--repo", str(SWEEP.parent)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode in (0, 1, 2)
