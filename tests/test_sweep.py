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
  that a sweep which died leaves the gate red.

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
