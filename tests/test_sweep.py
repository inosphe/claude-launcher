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


@pytest.fixture(autouse=True)
def no_outer_test_window(monkeypatch):
    """Each cmd_run case acquires the class it is testing."""
    for key in (
        sweep.test_window.WINDOW_GRANT_ENV,
        sweep.test_window.WINDOW_CLASS_ENV,
        sweep.test_window.WINDOW_WORKERS_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


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

#: A red run shaped like the real one: a FAILURES section with a traceback in
#: it, then the short summary. Cut down from ``1fdc7a7``'s output, which is
#: the run the parked-output cases below are about.
RED_TRACEBACK = (
    f'"{sys.executable}" -c "'
    "print('=' * 31 + ' FAILURES ' + '=' * 31); "
    "print('________ test_borrow_lends ________'); "
    "print('[gw7] win32 -- Python 3.13.2'); "
    "print('    os.replace(tmp, path)'); "
    "print('E   PermissionError: [WinError 5] Access is denied'); "
    "print('src/claude_launcher/store.py:113: PermissionError'); "
    "print('FAILED tests/test_x.py::test_borrow_lends - PermissionError'); "
    "print('1 failed, 11 passed in 3.4s'); "
    'raise SystemExit(1)"'
)

#: The same, with 3,000 lines of warnings-summary filler wedged between the
#: traceback and the summary, so the whole thing clears ``_OUTPUT_LIMIT``.
#: The constants cannot be monkeypatched into place -- they are bound as
#: default arguments at import -- so the case has to produce output that is
#: really over the cap, which is also the only version of it worth trusting.
BIG_RED = (
    f'"{sys.executable}" -c "'
    "print('=' * 31 + ' FAILURES ' + '=' * 31); "
    "print('E   PermissionError: [WinError 5] Access is denied'); "
    "print(('warning line ' * 8 + chr(10)) * 3000, end=''); "
    "print('FAILED tests/test_x.py::test_borrow_lends - PermissionError'); "
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


def _build_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "branch", "-M", "master")


@pytest.fixture
def repo(tmp_path, repo_template) -> Path:
    """A repository with one commit on ``master``."""
    return repo_template("sweep", _build_repo, tmp_path / "repo")


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
# The suite output a red run parks beside its receipt
# (claunch-sweep-receipt-no-output-64hs).
#
# `failures` records node ids. Six red receipts into this repository, exactly
# one had its cause reconstructed, and only because somebody had saved the
# output by hand next to it -- what it showed was that two failures reading as
# unrelated (a PermissionError in a store, an HTTP 500 out of a sync server)
# were one cause, the second wrapped by the server it was raised in. Node ids
# cannot say that. So a red run now keeps the output it already had in hand,
# and the receipt names the file.
# --------------------------------------------------------------------------- #


def test_a_red_run_parks_the_suite_output_and_the_receipt_names_it(repo, receipts):
    """The mechanism survives the run that produced it, and can be found.

    Two halves, and neither is worth much alone: the output is on disk *and*
    the receipt points at it. A file with no pointer is the state the
    ``1fdc7a7`` output was in -- it was read once, by somebody scanning the
    directory by eye.
    """
    assert _run(repo, receipts, "--command", RED_TRACEBACK) == 1
    tip = _git(repo, "rev-parse", "HEAD").strip()

    parked = sweep.output_path(repo, tip, receipts)
    assert parked.is_file(), "a red run kept no output"
    text = parked.read_text(encoding="utf-8")
    assert "PermissionError: [WinError 5]" in text
    assert "store.py:113" in text

    receipt = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))
    assert receipt["output"] == f"{tip}.output.txt"


def test_a_green_run_parks_nothing(repo, receipts):
    """Green output is ~29,000 characters of warnings summary per sweep and
    carries no diagnosis. Keeping it would cost the directory and buy nothing.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    tip = _git(repo, "rev-parse", "HEAD").strip()

    assert not sweep.output_path(repo, tip, receipts).exists()
    receipt = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))
    assert "output" not in receipt


def test_a_green_run_clears_what_a_red_run_left_at_the_same_sha(repo, receipts):
    """The receipt is overwritten in place, so its companion must be too.

    Re-running a sweep at the same commit rewrites ``{sha}.json``. If the red
    run's ``{sha}.output.txt`` outlived it, the directory would hold a green
    verdict next to a red output under one sha -- and the way such a file gets
    read here is by eye, with no receipt consulted. That is the failure this
    change exists to remove, reintroduced from the other end.
    """
    assert _run(repo, receipts, "--command", RED_TRACEBACK) == 1
    tip = _git(repo, "rev-parse", "HEAD").strip()
    assert sweep.output_path(repo, tip, receipts).is_file()

    assert _run(repo, receipts, "--command", GREEN) == 0
    assert not sweep.output_path(repo, tip, receipts).exists()


def test_the_parked_output_is_invisible_to_the_receipt_scan(repo, receipts, capsys):
    """``.output.txt`` must not become the noise the name check was built for.

    ``_newest_green`` globs ``*.json`` and only then refuses names outside
    ``{sha}.json``, so this file is dropped at the glob -- before the check
    that would call it a hand-written receipt. Filing it under ``.json``
    instead would make every red run produce a warning about a file the run
    itself wrote, which is the case
    ``test_a_hand_written_receipt_outside_the_protocol`` reports and would
    then be reporting against us.

    This is the case that goes red if somebody changes the extension.
    """
    tip = _git(repo, "rev-parse", "HEAD").strip()
    parked = sweep.output_path(repo, tip, receipts)
    parked.parent.mkdir(parents=True, exist_ok=True)
    parked.write_text("E   PermissionError\n", encoding="utf-8")

    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert "no sweep receipt" in err, "the output file was read as a verdict"
    assert "not a {sha}.json" not in err, (
        f"{parked.name} tripped the hand-written-receipt warning; the "
        f"extension has to stay outside the *.json glob"
    )


def test_output_over_the_cap_is_cut_in_the_middle_and_says_so(repo, receipts):
    """The cut has one requirement: the tracebacks have to be on the surviving
    side of it, and so do the names of what failed.

    pytest puts them at opposite ends -- FAILURES before the warnings block,
    the short test summary after it -- which is why the middle is what goes.
    A cap alone would not give this: a plain head-truncation drops the failing
    names, a plain tail-truncation drops the tracebacks.
    """
    assert _run(repo, receipts, "--command", BIG_RED) == 1
    tip = _git(repo, "rev-parse", "HEAD").strip()

    text = sweep.output_path(repo, tip, receipts).read_text(encoding="utf-8")
    assert len(text) < 3000 * 105, "nothing was cut -- the case is not testing a cut"
    assert "PermissionError: [WinError 5]" in text, "the traceback was cut away"
    assert "FAILED tests/test_x.py::test_borrow_lends" in text, "the names were cut"
    assert "1 failed, 11 passed" in text
    assert "dropped" in text and "characters" in text, "the cut left no trace"


def test_a_receipt_filed_before_this_field_existed_still_reads(repo, receipts, capsys):
    """``output`` is added only to red receipts, so *every* other receipt is a
    receipt without the key -- every green one, and the five red ones this
    repository had already filed when the field did not exist.

    The gate is the only thing that reads these (``tools/changed_tests.py``
    borrows ``is_green``/``parse_counts`` but reads its own receipts out of a
    ``changed/`` subdirectory by exact path, and nothing under
    ``src/claude_launcher`` opens the sweeps directory at all). It must not
    require the key in either direction.
    """
    tip = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    filed = sweep.receipt_path(repo, tip, receipts)
    filed.parent.mkdir(parents=True, exist_ok=True)

    def file_receipt(**over):
        receipt = {
            "commit": tip,
            "tree": tree,
            "code_tree": sweep.code_tree(repo, tree),
            "branch": "master",
            "command": "pytest",
            "exit_code": 0,
            "counts": {"passed": 12},
            "failures": [],
            "dirty": False,
            "session": "before",
            "seconds": 3.4,
        }
        receipt.update(over)
        assert "output" not in receipt
        filed.write_text(json.dumps(receipt), encoding="utf-8")

    file_receipt()
    assert _check(repo, receipts) == 0, "a green receipt without the key was refused"

    file_receipt(
        exit_code=1,
        counts={"failed": 1, "passed": 11},
        failures=["FAILED tests/test_x.py::test_a - AssertionError"],
    )
    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert "test_x.py::test_a" in err, "the red report lost its failing names"
    assert "full output" not in err, "the gate named a file that was never written"


def test_a_green_run_that_cannot_clear_the_old_output_still_files(
    repo, receipts, capsys
):
    """The clearing must not be able to cost the verdict either.

    ``Path.unlink(missing_ok=True)`` swallows only FileNotFoundError. A lock,
    a permission, a directory in the path -- each propagated from here, which
    is *before* the receipt is written, so the suite ran to completion and
    filed nothing and the gate charged another full sweep. Measured on this
    repository, and the exception that did it was ``PermissionError [WinError
    5]``: the same failure whose mechanism this whole change exists to keep.
    """
    tip = _git(repo, "rev-parse", "HEAD").strip()
    sweep.output_path(repo, tip, receipts).mkdir(parents=True)  # unremovable

    assert _run(repo, receipts, "--command", GREEN) == 0, "a green run died"

    filed = sweep.receipt_path(repo, tip, receipts)
    assert filed.is_file(), "the suite ran and no receipt was filed"
    receipt = json.loads(filed.read_text("utf-8"))
    assert sweep.is_green(receipt)
    assert "does not belong to this green sweep" in receipt["output_error"]

    # Green still passes -- a bookkeeping failure is not a verdict about the
    # tree -- but the gate does not pass in silence.
    capsys.readouterr()
    assert _check(repo, receipts) == 0
    assert "could not be removed" in capsys.readouterr().err


def test_the_gate_tells_the_six_receipt_states_apart(repo, receipts, capsys):
    """``output_error`` split the receipt into more states than two, and a
    reader has to land on the right one.

    There are six, and the pairs that share a shape are what make it a test:
    two receipts carry *neither* key (a clean green one, and a red one filed
    before the field existed) and two carry ``output_error`` (a green run that
    could not clear the old file, and a red run that could not write its own).
    Neither pair may collapse. The old receipt matters most: it must read as
    "nothing was ever meant to be here", not as a loss.

    | state                        | keys          | gate                     |
    |------------------------------|---------------|--------------------------|
    | green, nothing beside it     | none          | green, no warning        |
    | green, stale file left over  | output_error  | green + WARNING          |
    | red, output saved            | output        | red, full output: <path> |
    | red, output named, not there | output        | red, full output: MISSING |
    | red, output lost             | output_error  | red, full output: NOT SAVED |
    | red, filed before this field | none          | red, neither line        |

    Note the two rows that carry the same key and differ only by what is on
    disk. That pair is not hypothetical: a later run at the same commit
    deletes the file and can then fail to file its own verdict, leaving this
    receipt standing and naming a path nothing is at (claunch-r103).

    (The count was four when this was first written, then five, then six --
    each step a state a fix had just introduced and the table had not caught
    up with. s181 found the fifth in review and the sixth by blocking the
    receipt write itself.)
    """
    tip = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    filed = sweep.receipt_path(repo, tip, receipts)
    filed.parent.mkdir(parents=True, exist_ok=True)
    GREEN_COUNTS = {"passed": 12}
    RED_COUNTS = {"failed": 1, "passed": 11}

    def gate(**over):
        receipt = {
            "commit": tip,
            "tree": tree,
            "code_tree": sweep.code_tree(repo, tree),
            "branch": "master",
            "command": "pytest",
            "exit_code": 1,
            "counts": RED_COUNTS,
            "failures": ["FAILED tests/test_x.py::test_a - AssertionError"],
            "dirty": False,
            "session": "t",
            "seconds": 3.4,
        }
        receipt.update(over)
        filed.write_text(json.dumps(receipt), encoding="utf-8")
        capsys.readouterr()
        code = _check(repo, receipts)
        return code, capsys.readouterr()

    green = dict(exit_code=0, counts=GREEN_COUNTS, failures=[])
    parked = sweep.output_path(repo, tip, receipts)
    states = {}
    states["green_clean"] = gate(**green)
    states["green_leftover"] = gate(
        **green, output_error=f"{tip}.output.txt: could not be removed"
    )
    parked.write_text("E   AssertionError\n", encoding="utf-8")
    states["red_saved"] = gate(output=f"{tip}.output.txt")
    parked.unlink()
    states["red_dangling"] = gate(output=f"{tip}.output.txt")
    states["red_lost"] = gate(output_error=f"{tip}.output.txt: [WinError 5]")
    states["red_old"] = gate()

    codes = {k: v[0] for k, v in states.items()}
    assert codes == {
        "green_clean": 0,
        "green_leftover": 0,   # bookkeeping is not a verdict about the tree
        "red_saved": 1,
        "red_dangling": 1,
        "red_lost": 1,
        "red_old": 1,
    }

    said = {k: (v[1].out + v[1].err) for k, v in states.items()}

    assert "WARNING" not in said["green_clean"]
    assert "could not be removed" in said["green_leftover"]
    assert f"{tip}.output.txt" in said["red_saved"]
    assert "NOT SAVED" not in said["red_saved"]
    assert "MISSING" not in said["red_saved"]
    assert "MISSING" in said["red_dangling"], (
        "the gate pointed at an output file that is not on disk"
    )
    assert "NOT SAVED" in said["red_lost"]
    assert "full output" not in said["red_old"], (
        "a receipt from before this field reads as a loss it never had"
    )

    assert len(set(said.values())) == 6, (
        "two receipt states are reported identically: "
        + repr({k: v[:60] for k, v in said.items()})
    )


def test_the_red_gate_names_the_parked_output(repo, receipts, capsys):
    """The gate's red message is where a reader meets this, so the path goes
    there. Without it the file is present and unfindable, which is how the one
    surviving output was nearly lost.
    """
    assert _run(repo, receipts, "--command", RED_TRACEBACK) == 1
    assert _check(repo, receipts) == 1

    tip = _git(repo, "rev-parse", "HEAD").strip()
    err = capsys.readouterr().err
    assert str(sweep.output_path(repo, tip, receipts)) in err


def test_the_receipt_is_still_filed_when_the_output_cannot_be_saved(
    repo, receipts, capsys
):
    """A companion file must not be able to cost the verdict.

    The sweep is 148 seconds of this machine. If parking its output threw --
    a full disk, a locked path, the directory taken by something else -- and
    that propagated, the run would die *after* the suite and before the
    receipt, and the gate would report the sweep as never having happened.
    So the write is allowed to fail, loudly, and the receipt is filed without
    the pointer.
    """
    tip = _git(repo, "rev-parse", "HEAD").strip()
    blocked = sweep.output_path(repo, tip, receipts)
    blocked.mkdir(parents=True)  # a directory where the file wants to go

    assert _run(repo, receipts, "--command", RED_TRACEBACK) == 1

    receipt = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))
    assert receipt["counts"] == {"failed": 1, "passed": 11}
    assert "output" not in receipt
    assert "could not save the suite output" in capsys.readouterr().err

    # ...and the loss is recorded, which is the half a warning cannot do. The
    # warning goes to a terminal; terminals end. What survives is this file,
    # and without the next two lines it is byte-for-byte a receipt from before
    # the field existed -- so a reader would conclude no output was ever meant
    # to exist, and stop looking. That is the failure this change removes,
    # rebuilt one level down.
    assert receipt["output_error"].startswith(f"{tip}.output.txt: ")

    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert "NOT SAVED" in err, "the gate hid a red run whose mechanism was lost"


def test_a_lost_output_does_not_read_like_a_receipt_that_never_had_one(
    repo, receipts, capsys
):
    """The pair that has to stay apart, stated directly.

    Both are red receipts with no ``output`` key, and before ``output_error``
    they were the same bytes:

    * one was filed before this field existed -- nothing was lost, there was
      never anything beside it;
    * one tried to save its output and could not -- the mechanism behind those
      failures is gone, and re-running the sweep is the only way back.

    A reader who cannot tell them apart treats the second as the first and
    stops looking. That is this issue's own failure mode, one level down.
    """
    tip = _git(repo, "rev-parse", "HEAD").strip()
    filed = sweep.receipt_path(repo, tip, receipts)

    sweep.output_path(repo, tip, receipts).mkdir(parents=True)
    assert _run(repo, receipts, "--command", RED_TRACEBACK) == 1
    lost = json.loads(filed.read_text("utf-8"))
    capsys.readouterr()

    old = {k: v for k, v in lost.items() if k != "output_error"}
    assert old != lost, "the two states are the same bytes"

    filed.write_text(json.dumps(old), encoding="utf-8")
    assert _check(repo, receipts) == 1
    before = capsys.readouterr().err

    filed.write_text(json.dumps(lost), encoding="utf-8")
    assert _check(repo, receipts) == 1
    after = capsys.readouterr().err

    assert before != after, "the gate reports both states identically"
    assert "NOT SAVED" in after and "NOT SAVED" not in before


# --------------------------------------------------------------------------- #
# Reading the suite back. The judge decodes two streams it did not write, and
# both cases below are about what happens when that decoding goes wrong: the
# receipt has to keep saying WHICH of "the suite printed no summary" and "the
# summary never reached me" it is looking at.
# --------------------------------------------------------------------------- #

#: A red run whose output carries bytes that are not valid in utf-8 *or* in
#: cp949, wrapped around an ordinary ASCII summary line. ``0xff`` is a lead
#: byte in neither encoding, so this stand-in fails a strict decode on any
#: machine rather than only on a Korean-locale one.
#:
#: This is the shape that took the judge's eyes out at 5516c651c732: a Korean
#: failure dump reached ``subprocess.run`` with no ``encoding=``, the reader
#: thread raised UnicodeDecodeError, ``proc.stdout`` came back ``None``, and a
#: receipt was filed with ``counts {}``, ``failures []`` and a 0-byte output
#: file -- red, but unable to say what was red.
UNDECODABLE = (
    f'"{sys.executable}" -c "'
    "import sys; "
    "sys.stdout.buffer.write(bytes([0xff, 0xfe, 0x81])); "
    "sys.stdout.buffer.flush(); "
    "print(); "
    "print('FAILED tests/test_x.py::test_a - AssertionError'); "
    "print('1 failed, 11 passed in 3.4s'); "
    'raise SystemExit(1)"'
)


def test_output_that_cannot_be_decoded_still_yields_counts(repo, receipts):
    """Undecodable bytes cost the bytes, not the verdict.

    The summary line and the ``FAILED`` line are ASCII and sit right beside
    the garbage. Reading the stream with ``errors="replace"`` keeps them; a
    strict decode loses the whole stream and files an empty receipt, which is
    the difference between "one test failed, here it is" and "something was
    red".
    """
    assert _run(repo, receipts, "--command", UNDECODABLE) == 1

    tip = _git(repo, "rev-parse", "HEAD").strip()
    receipt = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))

    assert receipt["counts"] == {"failed": 1, "passed": 11}
    assert receipt["failures"] == ["FAILED tests/test_x.py::test_a - AssertionError"]
    assert "stream_error" not in receipt, "nothing was lost, so nothing is claimed"

    parked = sweep.output_path(repo, tip, receipts)
    assert parked.name == receipt["output"]
    assert "1 failed, 11 passed" in parked.read_text(encoding="utf-8")


def test_a_lost_stream_is_said_rather_than_left_as_an_empty_summary(
    repo, receipts, capsys, monkeypatch
):
    """``counts {}`` has two causes and the receipt must name which one.

    A suite that printed no summary and a stream that never arrived both
    parse to nothing. The gate calls both red -- correctly -- but only one of
    them has a failing test to go and look at, and a reader who cannot tell
    them apart spends the search on the wrong half. ``errors="replace"``
    removes the cause measured at 5516c651c732; this case covers the state
    itself, whatever else ever produces it.
    """
    real = sweep.subprocess.run

    def lose_the_streams(command, *args, **kwargs):
        # Only the suite call. cmd_run's own git calls have to keep working,
        # or the run never reaches the line under test.
        if kwargs.get("shell"):
            return subprocess.CompletedProcess(command, 1, None, None)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(sweep.subprocess, "run", lose_the_streams)
    assert _run(repo, receipts, "--command", RED) == 1
    monkeypatch.undo()

    tip = _git(repo, "rev-parse", "HEAD").strip()
    receipt = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))
    assert receipt["counts"] == {}
    assert "stdout and stderr" in receipt["stream_error"]

    capsys.readouterr()
    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert "no counts" in err
    assert "streams: " in err and "could not be read back" in err


# --------------------------------------------------------------------------- #
# A run that never became a verdict does not overwrite the one that stands
# (claunch-hnjy).
#
# The receipt's name is the sha, so a second run at the same commit rewrites
# the first one's file. That is right when both runs judged something -- the
# newer verdict is the verdict. It is wrong when the second run never got a
# verdict to file: on 2026-08-31 a sweep of ``70e9505a`` finished 1 failed /
# 2963 passed / 1 skipped, a re-run at the same tip died in pytest's basetemp
# cleanup (``PermissionError: [WinError 32]``) before collection, and the
# ``counts {}`` receipt it filed anyway landed on top of the only full
# judgement that tree ever had. The output file went with it.
#
# So the rule the cases below pin: a run that parsed no counts never
# overwrites a receipt that has them. It is filed beside it instead, under
# ``{sha}.invalid.json``, and the standing verdict is left exactly as it was.
# --------------------------------------------------------------------------- #

#: A run that never became a verdict: the suite died before it could print a
#: summary line, so there is nothing for ``parse_counts`` to read. Shaped on
#: the real one -- pytest emptying its basetemp, exit 3, no counts.
NEVER_STARTED = (
    f'"{sys.executable}" -c "'
    "print('INTERNALERROR> PermissionError: [WinError 32] The process cannot "
    "access the file because it is being used by another process'); "
    'raise SystemExit(3)"'
)


def test_a_run_with_no_counts_does_not_overwrite_the_verdict_that_stands(
    repo, receipts, capsys
):
    """The incident, reproduced: red verdict first, broken run second.

    What is lost when this goes wrong is not a file but the judgement -- the
    gate reads the receipt, so an empty one in the verdict's place is the
    same state as no sweep at all. Both halves have to survive: the receipt
    and the parked output it names.
    """
    assert _run(repo, receipts, "--command", RED_TRACEBACK) == 1
    tip = _git(repo, "rev-parse", "HEAD").strip()
    verdict = sweep.receipt_path(repo, tip, receipts)
    parked = sweep.output_path(repo, tip, receipts)
    before, parked_before = (
        verdict.read_text(encoding="utf-8"),
        parked.read_text(encoding="utf-8"),
    )

    assert _run(repo, receipts, "--command", NEVER_STARTED) == 1
    assert verdict.read_text(encoding="utf-8") == before, (
        "a run that parsed no counts overwrote the verdict that stood"
    )
    assert parked.read_text(encoding="utf-8") == parked_before

    aside = sweep.receipts_dir(repo, receipts) / f"{tip}.invalid.json"
    assert aside.is_file(), "the broken run left no account of itself"
    filed = json.loads(aside.read_text(encoding="utf-8"))
    assert filed["counts"] == {}
    assert filed["exit_code"] == 3
    assert f"{tip}.json" in filed["not_a_verdict"]

    capsys.readouterr()
    assert _check(repo, receipts) == 1
    err = capsys.readouterr().err
    assert "test_x.py::test_borrow_lends" in err, "the standing verdict is gone"
    assert "not a {sha}.json" not in err, (
        "the aside file tripped the hand-written-receipt warning; run writes "
        "it, so the scan has to pass over it in silence"
    )


def test_a_run_with_no_counts_does_not_undo_a_green_verdict(repo, receipts):
    """The same rule from the other side, and the more expensive one.

    A broken run's receipt carries ``exit_code`` from the process that died,
    and ``is_green`` reads ``counts`` -- so an empty receipt whose command
    happened to exit 0 would pass as green over a tree nobody swept, while an
    empty one that exited 3 turns a real green red. Diverting the file leaves
    neither reading available.
    """
    assert _run(repo, receipts, "--command", GREEN) == 0
    tip = _git(repo, "rev-parse", "HEAD").strip()

    assert _run(repo, receipts, "--command", NEVER_STARTED) == 1
    receipt = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))
    assert receipt["counts"] == {"passed": 12, "skipped": 1}
    assert _check(repo, receipts) == 0


def test_a_run_with_no_counts_is_still_recorded_when_nothing_stands(repo, receipts):
    """The rule is about overwriting, not about refusing to write.

    With no receipt at the sha there is no judgement to protect, and the
    broken run is the only account of what happened there. It keeps the
    standard name, so ``check`` reads it and reports the state
    (test_a_lost_stream_is_said_rather_than_left_as_an_empty_summary depends
    on that path).
    """
    assert _run(repo, receipts, "--command", NEVER_STARTED) == 1
    tip = _git(repo, "rev-parse", "HEAD").strip()

    receipt = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))
    assert receipt["counts"] == {}
    assert "not_a_verdict" not in receipt
    assert not (sweep.receipts_dir(repo, receipts) / f"{tip}.invalid.json").exists()


def test_one_broken_run_replaces_another(repo, receipts):
    """What is protected is a verdict, and an empty receipt is not one.

    Two runs that both died leave one file, the newer -- otherwise the
    directory collects a copy per attempt and the rule stops being about
    verdicts at all.
    """
    assert _run(repo, receipts, "--command", NEVER_STARTED) == 1
    tip = _git(repo, "rev-parse", "HEAD").strip()
    first = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))

    assert _run(repo, receipts, "--command", NEVER_STARTED) == 1
    second = json.loads(sweep.receipt_path(repo, tip, receipts).read_text("utf-8"))

    assert second["started_at"] > first["started_at"]
    assert not (sweep.receipts_dir(repo, receipts) / f"{tip}.invalid.json").exists()


def test_git_output_is_read_as_utf8_not_as_the_locale(repo, receipts):
    """``_git`` decodes git, and git writes utf-8.

    Every commit this repository's sweep judges has a subject, and this
    repository writes them in Korean. With ``text=True`` alone the subject is
    decoded with the process locale -- cp949 on the machine this was measured
    on -- and the round trip either raises inside the reader thread or comes
    back as mojibake. Both answers arrive at the same place: a helper that
    every path through the gate calls, returning something that is not what
    git said.
    """
    subject = "다섯 줄을 도구로 낸다"
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)

    assert sweep._git(repo, "log", "-1", "--pretty=%s") == subject


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


def test_the_sweep_uses_the_window_worker_advice():
    command = sweep.default_command("s9", workers=5)
    assert " -n 5 " in command
    assert 'C:/t/s9w' in command


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
