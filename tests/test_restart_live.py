"""``tools/restart_live.py`` restarts the daemon only when a restart would
change what it serves.

The RestartClock runs the ``restart:`` command the moment ``reflect`` is
entered, so the command itself has to ask whether there is anything to
deploy. It delegates that to ``tools/deploy_check.py`` and skips the restart on
exactly one of its four answers -- "already serving" -- because every other
answer is either fixed by a restart or not safe to skip on (see the module
docstring for the boot-time-dirt deadlock that a skip on 3 produced).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load():
    spec = importlib.util.spec_from_file_location("restart_live", TOOLS / "restart_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


restart_live = _load()
deploy_check = restart_live.deploy_check


@pytest.fixture
def verdict(monkeypatch):
    """Pin ``deploy_check.main``'s answer and record what it was asked."""
    asked = {}

    def install(code: int):
        def fake_main(argv):
            asked["argv"] = list(argv)
            return code

        monkeypatch.setattr(deploy_check, "main", fake_main)
        return asked

    return install


@pytest.fixture
def restarts(monkeypatch):
    """A ``claunch`` on PATH and a recorder for the command that runs."""
    calls = []
    monkeypatch.setattr(restart_live.shutil, "which", lambda name: f"/fake/{name}")
    # The reason's commit list reads the real daemon.json and git; pinned
    # here so these tests see only the decision. Tested on its own below.
    monkeypatch.setattr(restart_live, "_unserved", lambda repo, branch, dj: [])

    def run(command):
        calls.append(list(command))
        return 0

    return calls, run


def test_older_code_is_restarted(verdict, restarts, capsys):
    verdict(deploy_check.NOT_RESTARTED)
    calls, run = restarts
    assert restart_live.main(["--branch", "master"], run=run) == 0
    assert calls == [[
        "/fake/claunch", "daemon", "restart",
        "--reason", restart_live._RESTARTING_BECAUSE[deploy_check.NOT_RESTARTED],
    ]]
    got = capsys.readouterr()
    assert "restarting" in got.err and "running:" in got.out


def test_dry_run_decides_without_restarting(verdict, restarts, capsys):
    """``--dry-run`` exists so the decision can be checked against a live
    checkout without taking the daemon down (running the script bare against
    this repository on 2026-09-11 18:12 did exactly that)."""
    verdict(deploy_check.NOT_RESTARTED)
    calls, run = restarts
    assert restart_live.main(["--dry-run"], run=run) == 0
    assert calls == []
    assert "dry run" in capsys.readouterr().out


def test_code_already_served_is_left_alone(verdict, restarts, capsys):
    """The 2026-09-11 17:09 case: a tip that differs only in .beads."""
    verdict(deploy_check.SERVING)
    calls, run = restarts
    assert restart_live.main([], run=run) == 0
    assert calls == []
    assert "no restart needed" in capsys.readouterr().out


@pytest.mark.parametrize("code", [deploy_check.CANNOT_TELL, deploy_check.DIRTY])
def test_unsure_and_dirty_answers_still_restart(verdict, restarts, capsys, code):
    """The 2026-09-11 18:03 deadlock: deploy_check keeps answering 3 for dirt
    the daemon *booted* with until a restart clears it, so a skip on 3 could
    not be undone by committing. Both answers restart; the reason is said."""
    verdict(code)
    calls, run = restarts
    assert restart_live.main([], run=run) == 0
    assert [c[:4] for c in calls] == [["/fake/claunch", "daemon", "restart", "--reason"]]
    assert calls[0][4] == restart_live._RESTARTING_BECAUSE[code]
    assert "restarting" in capsys.readouterr().err


def test_the_check_is_asked_about_the_same_branch_and_daemon(verdict, restarts):
    asked = verdict(deploy_check.SERVING)
    calls, run = restarts
    restart_live.main(
        ["--branch", "release", "--repo", "here", "--daemon-json", "d.json",
         "--allow-dirty", "sha1:abc"],
        run=run,
    )
    assert asked["argv"] == [
        "--repo", "here", "--branch", "release",
        "--daemon-json", "d.json", "--allow-dirty", "sha1:abc",
    ]


def test_the_restart_exit_status_is_passed_through(verdict, restarts):
    verdict(deploy_check.NOT_RESTARTED)
    calls, _ = restarts
    assert restart_live.main([], run=lambda command: 7) == 7


def test_no_claunch_on_path_is_an_error_not_a_silent_green(verdict, monkeypatch, capsys):
    verdict(deploy_check.NOT_RESTARTED)
    monkeypatch.setattr(restart_live.shutil, "which", lambda name: None)
    assert restart_live.main([], run=lambda command: 0) == 1
    assert "cannot restart" in capsys.readouterr().err


def test_every_verdict_of_deploy_check_has_an_answer_here():
    """A verdict added to deploy_check must be classified here -- as the one
    skip or as a named restart reason -- not fall through to a KeyError at
    the moment the daemon runs this."""
    verdicts = {
        deploy_check.SERVING, deploy_check.NOT_RESTARTED,
        deploy_check.CANNOT_TELL, deploy_check.DIRTY,
    }
    handled = {deploy_check.SERVING} | set(restart_live._RESTARTING_BECAUSE)
    assert handled == verdicts


@pytest.mark.parametrize("wrapper", ["restart_live.ps1", "restart_live.sh"])
def test_the_platform_wrappers_defer_to_the_python_decision(wrapper):
    """Neither wrapper runs ``claunch daemon restart`` itself any more."""
    text = (TOOLS / wrapper).read_text(encoding="utf-8")
    code_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert any("tools/restart_live.py" in line for line in code_lines), wrapper
    assert not any(re.search(r"claunch\s+daemon\s+restart", line) for line in code_lines), wrapper


def test_the_reason_lists_the_commits_the_daemon_does_not_serve(verdict, restarts, monkeypatch):
    """The approval card shows this reason (claunch-mm8zv): the person asked
    to cut every attached turn sees which merges are not live yet."""
    verdict(deploy_check.NOT_RESTARTED)
    calls, run = restarts
    unserved = [f"{i:07x} merge: change {i}" for i in range(12)]
    monkeypatch.setattr(restart_live, "_unserved", lambda repo, branch, dj: unserved)
    assert restart_live.main([], run=run) == 0
    reason = calls[0][calls[0].index("--reason") + 1]
    assert reason.startswith(restart_live._RESTARTING_BECAUSE[deploy_check.NOT_RESTARTED])
    assert "does not serve yet (12)" in reason
    assert "0000000 merge: change 0" in reason
    assert f"{restart_live.UNSERVED_SHOWN - 1:07x} merge" in reason
    assert f"{restart_live.UNSERVED_SHOWN:07x} merge" not in reason
    assert "... and 2 more" in reason


def test_unserved_reads_the_range_from_the_daemons_head(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        deploy_check, "_daemon_doc",
        lambda dj: (Path("d.json"), {"started_at": "x", "code": {"head": "abc123"}}),
    )

    def fake_git(repo, *args):
        seen["args"] = args
        return "1111111 merge: one\n2222222 merge: two\n"

    monkeypatch.setattr(deploy_check, "_git", fake_git)
    assert restart_live._unserved(Path("."), "master", None) == [
        "1111111 merge: one", "2222222 merge: two",
    ]
    assert seen["args"][-1] == "abc123..master"
    assert "--first-parent" in seen["args"]


def test_unserved_is_empty_when_it_cannot_tell(monkeypatch):
    """The reason is for a person; failing to build it must not stop the
    restart."""
    def no_doc(dj):
        raise LookupError("no daemon.json")

    monkeypatch.setattr(deploy_check, "_daemon_doc", no_doc)
    assert restart_live._unserved(Path("."), "master", None) == []
    monkeypatch.setattr(
        deploy_check, "_daemon_doc",
        lambda dj: (Path("d.json"), {"started_at": "x", "code": {}}),
    )
    assert restart_live._unserved(Path("."), "master", None) == []
