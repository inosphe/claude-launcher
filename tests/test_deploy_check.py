"""The reflect gate: was the live daemon actually restarted onto this merge?

``improv-leader``'s ``reflect`` step used to be prose alone -- "restart and
confirm it is serving" -- with nothing behind it. A leader could file the
report without restarting anything, and the run had no way to notice; the
observed failure was quieter than that even: the daemon was not restarted,
the round could not close, and the run just repeated its 300-second reminder
while the leader waited for a signal that never comes on its own.

``tools/deploy_check.py`` turns that into a machine check the step's
``verify:`` runs: the daemon's recorded boot time must be strictly after the
tip of the branch it is supposed to be serving. These tests pin both
directions and the two ways the answer is "cannot tell" rather than "no",
because a gate that reports a missing file as a failed deploy teaches people
to pass it with ``|| true``.

Times are pinned with ``GIT_COMMITTER_DATE`` instead of the wall clock: the
whole subject is an ordering of two timestamps a second apart, and a test
that raced the clock for them would be flaky about exactly the thing it
checks.

The cases call ``main()`` in-process. One case goes the long way round, as
the ``verify:`` line does, because that is the part a python-level test
cannot see: the file has to be runnable as a script and its exit status has
to reach the shell.
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

#: The tip every case is measured against, and the two seconds around it.
TIP = "2026-08-25T12:00:00+09:00"  # 03:00:00Z
AFTER = "2026-08-25T03:00:01+00:00"
TIE = "2026-08-25T03:00:00+00:00"
BEFORE = "2026-08-25T02:59:59+00:00"


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
    """One repository whose ``master`` tip was committed at :data:`TIP`."""
    repo = tmp_path_factory.mktemp("deployed")
    _git(repo, "init", "-q")
    (repo / "served.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the merge being deployed", when=TIP)
    _git(repo, "branch", "-M", "master")
    return repo


def _daemon_json(tmp_path: Path, started_at: str, pid: int | None = None) -> Path:
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid() if pid is None else pid,
                "host": "127.0.0.1",
                "port": 8377,
                "instance": None,
                "version": "0.1.0",
                "started_at": started_at,
            }
        ),
        encoding="utf-8",
    )
    return path


def _check(repo: Path, doc: Path, *args: str) -> int:
    return deploy_check.main(
        ["--repo", str(repo), "--daemon-json", str(doc), *args]
    )


def test_a_daemon_that_booted_after_the_tip_passes(repo, tmp_path, capsys):
    """The deploy happened: the running daemon is younger than the merge."""
    assert _check(repo, _daemon_json(tmp_path, AFTER)) == 0
    out = capsys.readouterr().out
    assert AFTER in out and "12:00:00+09:00" in out


def test_a_daemon_older_than_the_tip_fails_and_says_both_times(
    repo, tmp_path, capsys
):
    """The exact state the step exists to catch -- merged, never restarted.

    The message has to carry both timestamps: the reader's next question is
    always "older than what?", and a gate that answers it in the failure is
    one nobody has to re-derive by hand.
    """
    assert _check(repo, _daemon_json(tmp_path, BEFORE)) == 1
    err = capsys.readouterr().err
    assert BEFORE in err and "12:00:00+09:00" in err
    assert "pre-merge" in err


def test_a_tie_inside_one_second_fails_closed(repo, tmp_path):
    """``started_at`` is second-precision, so a tie cannot be ordered.

    It resolves as "not proven", not as "probably fine" -- and the escape is
    the very thing being asked for, another restart, so failing closed costs
    the leader one command rather than a stuck run.
    """
    assert _check(repo, _daemon_json(tmp_path, TIE)) == 1


def test_a_leftover_daemon_json_does_not_pass_as_a_deploy(repo, tmp_path, capsys):
    """A recent ``started_at`` proves nothing if that daemon is gone.

    The file outlives the process it describes (a crash leaves it behind), so
    a boot time alone would let a dead daemon certify the deploy.
    """
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    assert _check(repo, _daemon_json(tmp_path, AFTER, pid=dead.pid)) == 1
    assert str(dead.pid) in capsys.readouterr().err


def test_a_missing_daemon_json_is_cannot_tell_not_a_verdict(repo, tmp_path, capsys):
    """No daemon file at all: exit 2, distinct from "did not restart"."""
    assert _check(repo, tmp_path / "absent.json") == 2
    assert "cannot tell" in capsys.readouterr().err


def test_an_unknown_branch_is_cannot_tell(repo, tmp_path, capsys):
    """A repository without the branch cannot answer the question either."""
    doc = _daemon_json(tmp_path, AFTER)
    assert _check(repo, doc, "--branch", "no-such-ref") == 2
    assert "cannot tell" in capsys.readouterr().err


def test_the_script_runs_as_the_verify_line_runs_it(repo, tmp_path):
    """The one case that goes through a shell, because ``verify:`` does.

    In-process calls cannot see a syntax error under ``__main__``, a missing
    ``SystemExit``, or an interpreter that cannot import what the script
    imports -- and all three would reach the gate as "verify failed" with no
    hint of why.
    """
    doc = _daemon_json(tmp_path, BEFORE)
    done = subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(repo), "--daemon-json", str(doc)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert done.returncode == 1, done.stderr
    assert "not restarted" in done.stderr
