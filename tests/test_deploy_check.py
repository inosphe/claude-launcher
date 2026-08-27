"""The reflect gate: is the live daemon serving the code of this branch?

``improv-leader``'s ``reflect`` step used to be prose alone -- "restart and
confirm it is serving" -- with nothing behind it. A leader could file the
report without restarting anything, and the run had no way to notice; the
observed failure was quieter than that even: the daemon was not restarted,
the round could not close, and the run just repeated its 300-second reminder
while the leader waited for a signal that never comes on its own.

``tools/deploy_check.py`` is what the step's ``verify:`` runs. Its first
version compared two timestamps -- the daemon's boot time against the branch
tip's committer date -- and both of its answers were measured wrong on
2026-08-27, in opposite directions:

* ``claunch-tig1``: exit 0 while the served checkout differed from ``master``
  in nineteen tracked paths, seven under ``src/claude_launcher/``. The green
  sentence claimed the merge was being served; the step's ``done_when`` cites
  that green as its evidence.
* ``claunch-33id``: exit 1 for a tip whose only changed path was
  ``.beads/issues.jsonl``. No code had moved, ``tools/sweep.py`` passed the
  same commit at the same moment, and the red forced a daemon restart that had
  nothing new to serve.

So the check moved from *when* to *what*, and these tests are arranged around
that: the boot times here are deliberately older than the tips they are
measured against, because a test that let the old rule pass by accident would
not notice it coming back.

**Every case is a different answer, and one test says so.**
:func:`test_no_two_answers_are_the_same_string` drives all fifteen reachable
states in one go and refuses duplicates. That is the failure this repository
has paid for most often: two different facts arriving as one string and one
exit code, so "I could not look" and "there was nothing to see" are read as
the same thing (protocol 10, ``claunch-peyn``). Four exit codes carry the four
kinds of fact; the strings separate the cases inside each. The cases before it
each pin one answer's content, and it pins that the set stays separable.

Times are pinned with ``GIT_COMMITTER_DATE`` instead of the wall clock, and
the daemon documents are written by hand rather than by booting a daemon --
the subject is what a reader does with a recorded fact, and booting one would
put the machine's real repository into every case.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[1] / "tools" / "deploy_check.py"

#: The two commits the fixture repository is built from, and a boot that
#: happened *before* both of them as far as the recorded dates go. Ordering by
#: time would fail every green below; ordering by content passes the ones that
#: should pass.
CODE_AT = "2026-08-25T12:00:00+09:00"
BEADS_AT = "2026-08-25T13:00:00+09:00"
BOOTED = "2026-08-25T01:00:00+00:00"


def _load():
    """``tools/`` is not a package -- load the script the way a script is."""
    spec = importlib.util.spec_from_file_location("deploy_check", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy_check = _load()


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo), capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """A repository whose last two commits differ only in ``.beads``.

    That is not a contrived shape: ``improv-leader``'s sweep step prescribes
    committing ``.beads/issues.jsonl`` after the sweep, so it is the shape
    ``master`` is left in at the end of every round, and it is the one
    ``claunch-33id`` was filed about.
    """
    repo = tmp_path_factory.mktemp("deployed")
    _git(repo, "init", "-q")
    (repo / "served.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "before the merge", when=CODE_AT)
    (repo / "served.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the merge being deployed", when=CODE_AT)
    (repo / ".beads").mkdir()
    (repo / ".beads" / "issues.jsonl").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore(beads): close the round", when=BEADS_AT)
    _git(repo, "branch", "-M", "master")
    return repo


@pytest.fixture(scope="module")
def shas(repo) -> dict:
    """``old`` (pre-merge code), ``code`` (the merge), ``tip`` (board close)."""
    log = _git(repo, "log", "--format=%H", "master").split()
    return {"tip": log[0], "code": log[1], "old": log[2]}


def _doc(tmp_path: Path, repo: Path, head: str | None, **over) -> Path:
    """A ``daemon.json`` as ``runtime_state.write_daemon_json`` writes one."""
    code = {
        "root": str(repo / "src" / "claude_launcher"),
        "repo": str(repo),
        "head": head,
        "dirty": [],
        "dirty_more": 0,
    }
    given = over.pop("code", {})
    if given is not None:
        code.update(given)
    doc = {
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": 8377,
        "instance": None,
        "version": "0.1.0",
        "started_at": BOOTED,
        "code": code,
    }
    if given is None:
        doc.pop("code")
    doc.update(over)
    path = tmp_path / "daemon.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _check(repo: Path, doc: Path, *args: str) -> int:
    return deploy_check.main(
        ["--repo", str(repo), "--daemon-json", str(doc), *args]
    )


def _answer(capsys, code: int) -> str:
    """The verdict line, from whichever stream the exit code sends it to."""
    got = capsys.readouterr()
    text = got.out if code == 0 else got.err
    assert text.strip(), f"exit {code} said nothing on its own stream"
    assert not (got.out.strip() and got.err.strip()), "a verdict on both streams"
    return text.strip()


# --------------------------------------------------------------- serving (0)


def test_the_tip_itself_is_being_served(repo, shas, tmp_path, capsys):
    """The plain green: the daemon booted on this very commit, tree clean."""
    code = _check(repo, _doc(tmp_path, repo, shas["tip"]))
    message = _answer(capsys, code)
    assert code == 0
    assert shas["tip"][:12] in message


def test_a_board_only_commit_does_not_need_a_restart(repo, shas, tmp_path, capsys):
    """``claunch-33id``: the tip changed ``.beads`` and nothing else.

    The daemon booted on the commit *below* the tip, and it booted (by the
    recorded dates) an hour before that tip was even committed. Under the old
    rule this was exit 1 and "the live server is still serving pre-merge code",
    which was false: the code it serves is the tip's code, to the byte. The
    cost of that red was a real restart, and a restart takes down every
    attached session's drive to serve nothing new.
    """
    code = _check(repo, _doc(tmp_path, repo, shas["code"]))
    message = _answer(capsys, code)
    assert code == 0
    assert shas["code"][:12] in message and shas["tip"][:12] in message
    assert ".beads" in message


def test_a_declared_dirty_set_can_be_exempted(repo, shas, tmp_path, capsys):
    """The escape hatch, and it takes a value rather than being a switch.

    A bare "ignore the dirt" flag would put back exactly the state
    :func:`test_a_dirty_checkout_is_serving_no_commit` catches, under another
    name. Declaring the paths means the exemption is checked against what is
    actually there, and the verdict says how many were waived.
    """
    dirty = {"dirty": ["src/other.py"]}
    code = _check(
        repo, _doc(tmp_path, repo, shas["tip"], code=dirty),
        "--allow-dirty", "src/other.py",
    )
    message = _answer(capsys, code)
    assert code == 0
    assert "src/other.py" in message and "1 declared" in message


def test_the_digest_form_of_a_declaration_is_accepted(repo, shas, tmp_path, capsys):
    """Nineteen paths is the real case, and nineteen paths on a command line
    is a declaration nobody checks. The check prints the digest of what it
    found; that digest is a legal declaration."""
    paths = [f"src/mod{n}.py" for n in range(19)]
    digest = deploy_check.dirty_digest(paths)
    code = _check(
        repo, _doc(tmp_path, repo, shas["tip"], code={"dirty": paths}),
        "--allow-dirty", digest,
    )
    message = _answer(capsys, code)
    assert code == 0
    assert "19 declared" in message


# --------------------------------------------------- not restarted / gone (1)


def test_older_code_is_a_restart_the_leader_has_not_done(repo, shas, tmp_path, capsys):
    """The state the step exists to catch: merged, never restarted.

    The message carries both code trees, because the reader's next question is
    always "different from what", and a gate that answers it in the failure is
    one nobody has to re-derive by hand.
    """
    code = _check(repo, _doc(tmp_path, repo, shas["old"]))
    message = _answer(capsys, code)
    assert code == 1
    assert shas["old"][:12] in message and "pre-merge" in message


def test_a_leftover_daemon_json_does_not_pass_as_a_deploy(repo, shas, tmp_path, capsys):
    """A recorded boot proves nothing if that daemon is gone.

    The file outlives the process it describes (a crash leaves it behind), so
    the snapshot alone would let a dead daemon certify the deploy.
    """
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    code = _check(repo, _doc(tmp_path, repo, shas["tip"], pid=dead.pid))
    message = _answer(capsys, code)
    assert code == 1
    assert str(dead.pid) in message


# -------------------------------------------------- not any commit's code (3)


def test_a_dirty_checkout_is_serving_no_commit(repo, shas, tmp_path, capsys):
    """``claunch-tig1``, and the exit code the leader ruled on.

    Same commit, same boot time, same everything as
    :func:`test_the_tip_itself_is_being_served` -- one field differs. That is
    the control the issue asked for: a pair whose true answers are different,
    so a check that could not tell them apart would fail here rather than
    agree with itself.

    3 rather than 1 because the fix is different. 1 is one command; this needs
    somebody's uncommitted work committed or set aside, and possibly somebody
    else's. Folding them loses which of the two happened, and the automatic
    consumers of this gate compare exit codes only -- ``awaits.probe`` takes
    the code and drops the output.
    """
    dirty = {"dirty": ["src/claude_launcher/cli.py", "src/claude_launcher/usage.py"]}
    code = _check(repo, _doc(tmp_path, repo, shas["tip"], code=dirty))
    message = _answer(capsys, code)
    assert code == 3
    assert "src/claude_launcher/cli.py" in message
    assert "--allow-dirty sha1:" in message, "the failure has to name its own cure"


def test_dirt_appearing_after_boot_is_caught_too(repo, shas, tmp_path, capsys):
    """The boot snapshot is not the whole of what is served.

    Modules are loaded once, but ``harnesses.yaml`` and the workflow YAMLs are
    read at runtime, so a checkout that goes dirty *after* boot still changes
    what the daemon hands out. The boot list here is empty and the working tree
    is not.
    """
    (repo / "served.txt").write_text("edited after boot\n", encoding="utf-8")
    try:
        code = _check(repo, _doc(tmp_path, repo, shas["tip"]))
        message = _answer(capsys, code)
    finally:
        _git(repo, "checkout", "--", "served.txt")
    assert code == 3
    assert "served.txt" in message


def test_a_declaration_that_does_not_match_still_fails(repo, shas, tmp_path, capsys):
    """The condition on the escape hatch: it is checked, not believed.

    Declaring one path while two are dirty exempts neither. The message prints
    the digest of what is actually there, so the declaration can be corrected
    rather than widened to "everything".
    """
    dirty = {"dirty": ["src/a.py", "src/b.py"]}
    code = _check(
        repo, _doc(tmp_path, repo, shas["tip"], code=dirty),
        "--allow-dirty", "src/a.py",
    )
    message = _answer(capsys, code)
    assert code == 3
    assert "src/b.py" in message and deploy_check.dirty_digest(
        ["src/a.py", "src/b.py"]
    ) in message


def test_the_board_file_is_not_dirt_that_blocks_a_deploy(repo, shas, tmp_path, capsys):
    """``.beads`` is subtracted from the dirty set as well as from the trees.

    The same file that must not force a restart when it is *committed* must
    not block one when it is *uncommitted* -- it is the most frequently written
    file in this arrangement, and the leader's own step leaves it modified.
    """
    dirty = {"dirty": [".beads/issues.jsonl"]}
    code = _check(repo, _doc(tmp_path, repo, shas["tip"], code=dirty))
    assert code == 0, _answer(capsys, code)
    capsys.readouterr()


def test_a_leading_status_field_is_not_eaten_from_the_first_path(repo, tmp_path):
    """A path shifted by one character silently changed a verdict.

    ``git status --porcelain`` puts a two-character status field in front of
    every path, so stripping the output eats the space before the *first*
    entry only: nineteen paths arrive intact and one arrives as
    ``beads/issues.jsonl``, which no longer matches ``.beads`` and counts as a
    code change. No error, one wrong path, a verdict on top of it. Measured
    against this repository's own checkout before it was fixed.
    """
    (repo / ".beads" / "issues.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    try:
        assert deploy_check._porcelain(repo) == [".beads/issues.jsonl"]
    finally:
        _git(repo, "checkout", "--", ".beads/issues.jsonl")


# ------------------------------------------------------------ cannot tell (2)


def test_a_missing_daemon_json_is_cannot_tell_not_a_verdict(repo, tmp_path, capsys):
    """No daemon file at all: exit 2, distinct from "did not restart"."""
    code = _check(repo, tmp_path / "absent.json")
    message = _answer(capsys, code)
    assert code == 2 and "cannot tell" in message


def test_an_unknown_branch_is_cannot_tell(repo, shas, tmp_path, capsys):
    """A repository without the branch cannot answer the question either."""
    code = _check(repo, _doc(tmp_path, repo, shas["tip"]), "--branch", "no-such-ref")
    message = _answer(capsys, code)
    assert code == 2 and "cannot tell" in message


def test_a_daemon_that_recorded_no_code_is_cannot_tell(repo, tmp_path, capsys):
    """The bootstrap state: a daemon older than the field this gate reads.

    It resolves itself with one restart, and the step that runs this gate
    restarts the daemon anyway. What it must not do is guess -- falling back
    to the timestamp rule here would keep the two wrong answers alive for
    exactly the daemons that cannot be checked.
    """
    code = _check(repo, _doc(tmp_path, repo, None, code=None))
    message = _answer(capsys, code)
    assert code == 2 and "no code snapshot" in message


def test_an_installed_copy_cannot_be_compared_with_a_branch(repo, tmp_path, capsys):
    """A daemon running from a wheel has no commit to compare, and says so."""
    code = _check(repo, _doc(tmp_path, repo, None, code={"repo": None}))
    message = _answer(capsys, code)
    assert code == 2 and "checkout" in message


def test_a_served_checkout_that_is_gone_is_cannot_tell(repo, shas, tmp_path, capsys):
    """The daemon booted from a directory nobody can read now."""
    absent = {"repo": str(tmp_path / "was-here"), "root": str(tmp_path / "was-here")}
    code = _check(repo, _doc(tmp_path, repo, shas["tip"], code=absent))
    message = _answer(capsys, code)
    assert code == 2 and "no longer on disk" in message


def test_a_daemon_serving_another_repository_is_cannot_tell(
    repo, shas, tmp_path_factory, tmp_path, capsys
):
    """Two checkouts, one branch name. The branch here is not the one it booted
    on, and answering about it would be answering a different question."""
    other = tmp_path_factory.mktemp("elsewhere")
    _git(other, "init", "-q")
    (other / "f.txt").write_text("x\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "unrelated", when=CODE_AT)
    code = _check(
        repo,
        _doc(tmp_path, repo, shas["tip"], code={"repo": str(other)}),
    )
    message = _answer(capsys, code)
    assert code == 2 and "different repository" in message


def test_a_head_this_repository_does_not_have_is_cannot_tell(repo, tmp_path, capsys):
    """The daemon booted on a commit that never reached here (a worktree that
    was thrown away, a branch that was never pushed)."""
    code = _check(repo, _doc(tmp_path, repo, "0" * 40))
    message = _answer(capsys, code)
    assert code == 2 and "does not have" in message


def test_a_boot_that_could_not_read_its_tree_is_cannot_tell(repo, shas, tmp_path, capsys):
    """``dirty: null`` is "git did not answer", and it is not ``[]``.

    The daemon writes ``None`` when its ``git status`` failed or timed out.
    Reading that as "clean" is the exact mistake this gate is a correction of:
    it would turn "I could not look" into a green.
    """
    code = _check(repo, _doc(tmp_path, repo, shas["tip"], code={"dirty": None}))
    message = _answer(capsys, code)
    assert code == 2 and "could not read its checkout" in message


def test_a_tree_that_cannot_be_read_now_is_cannot_tell(
    repo, shas, tmp_path, capsys, monkeypatch
):
    """The same distinction on the other side: git failing here is not clean."""
    monkeypatch.setattr(deploy_check, "_porcelain", lambda repo: None)
    code = _check(repo, _doc(tmp_path, repo, shas["tip"]))
    message = _answer(capsys, code)
    assert code == 2 and "cannot read the state" in message


# ------------------------------------------------------- writer/reader, shell


def test_the_daemon_writes_the_shape_this_gate_reads(repo, tmp_path, capsys):
    """One format, two files -- so one test stands across the seam.

    ``daemon/runtime_state.py`` writes the snapshot and this script reads it.
    Nothing else connects them, and a field renamed on one side would show up
    as "cannot tell" on the other -- which is a quiet way for the gate to stop
    checking anything, since 2 is not a verdict and reads as a tooling problem.

    Every other case in this file hand-writes the document, so this is the only
    place the two halves are joined. It runs the writer against the fixture
    repository rather than this one: reading this checkout's own HEAD is what
    ``tests/_repo_history_guard.py`` refuses, and it refuses it for a reason
    that lands on this gate (two commits with one tree share a sweep receipt).
    """
    from claude_launcher.daemon import runtime_state

    package = repo / "src" / "claude_launcher"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    try:
        snap = runtime_state.code_snapshot(root=package)
        assert set(snap) == {"root", "repo", "head", "dirty", "dirty_more"}
        assert Path(snap["repo"]) == repo.resolve()
        assert snap["dirty"], "an untracked package is dirt, not an empty list"

        doc = tmp_path / "written.json"
        doc.write_text(
            json.dumps({"pid": os.getpid(), "started_at": BOOTED, "code": snap}),
            encoding="utf-8",
        )
        got = deploy_check.main(
            ["--repo", str(repo), "--daemon-json", str(doc),
             "--allow-dirty", deploy_check.dirty_digest(snap["dirty"])]
        )
        message = _answer(capsys, got)
        assert got == 0, message
        assert snap["head"][:12] in message, "the reader did not read what was written"
    finally:
        (package / "__init__.py").unlink()
        package.rmdir()
        (repo / "src").rmdir()


def test_the_script_runs_as_the_verify_line_runs_it(repo, shas, tmp_path):
    """The one case that goes through a shell, because ``verify:`` does.

    In-process calls cannot see a syntax error under ``__main__``, a missing
    ``SystemExit``, or an interpreter that cannot import what the script
    imports -- and all three would reach the gate as "verify failed" with no
    hint of why. This one also covers the ``import sweep`` at module scope,
    which resolves through a path this file's loader sets up differently.
    """
    doc = _doc(tmp_path, repo, shas["old"])
    done = subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(repo), "--daemon-json", str(doc)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert done.returncode == 1, done.stderr
    assert "not restarted" in done.stderr


def test_no_two_answers_are_the_same_string(
    repo, shas, tmp_path_factory, tmp_path, capsys
):
    """Every state this gate can reach, side by side, checked for collisions.

    A gate whose answers collapse into each other cannot be read: "I could not
    look" and "there was nothing to see" arriving as one string and one exit
    code is the failure this repository has paid for most often, and no test
    that looks at one case at a time can catch it. The cases above each pin one
    answer's content; this one pins that the *set* stays separable.

    It builds its own set rather than collecting what the other cases recorded.
    Accumulating in a module global looks equivalent and is not: the suite runs
    under ``pytest -n``, which puts these tests on different worker processes,
    and each worker would then judge the three or four answers that happened to
    land on it -- a test that passes by not seeing the collision.
    """
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    _git(elsewhere, "init", "-q")
    (elsewhere / "f.txt").write_text("x\n", encoding="utf-8")
    _git(elsewhere, "add", "-A")
    _git(elsewhere, "commit", "-q", "-m", "unrelated", when=CODE_AT)

    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    two = ["src/a.py", "src/b.py"]

    #: name -> (daemon.json overrides, extra argv). One per reachable answer.
    cases = {
        "tip": ({"head": shas["tip"]}, []),
        "board-only": ({"head": shas["code"]}, []),
        "exempted": (
            {"head": shas["tip"], "code": {"dirty": two}},
            ["--allow-dirty", deploy_check.dirty_digest(two)],
        ),
        "older-code": ({"head": shas["old"]}, []),
        "dead-pid": ({"head": shas["tip"], "pid": dead.pid}, []),
        "dirty": ({"head": shas["tip"], "code": {"dirty": two}}, []),
        "declaration-mismatch": (
            {"head": shas["tip"], "code": {"dirty": two}},
            ["--allow-dirty", "src/a.py"],
        ),
        "no-code-block": ({"head": None, "code": None}, []),
        "no-repo": ({"head": shas["tip"], "code": {"repo": None}}, []),
        "repo-gone": (
            {"head": shas["tip"], "code": {"repo": str(tmp_path / "was-here")}},
            [],
        ),
        "other-repo": (
            {"head": shas["tip"], "code": {"repo": str(elsewhere)}},
            [],
        ),
        "unknown-head": ({"head": "0" * 40}, []),
        "boot-unread": ({"head": shas["tip"], "code": {"dirty": None}}, []),
    }

    answers = {}
    for name, (over, argv) in cases.items():
        head = over.pop("head")
        got = _check(repo, _doc(tmp_path, repo, head, **over), *argv)
        answers[name] = (got, _answer(capsys, got))

    # The two that need the world moved rather than the document rewritten.
    got = _check(repo, tmp_path / "absent.json")
    answers["no-daemon-json"] = (got, _answer(capsys, got))
    got = _check(repo, _doc(tmp_path, repo, shas["tip"]), "--branch", "no-such-ref")
    answers["unknown-branch"] = (got, _answer(capsys, got))

    seen: dict = {}
    for name, (code, message) in answers.items():
        assert message not in seen, f"{name} says exactly what {seen[message]} says"
        seen[message] = name
    codes = {code for code, _ in answers.values()}
    assert codes == {0, 1, 2, 3}, f"the four facts are not all reachable: {codes}"
    assert len(answers) == 15
