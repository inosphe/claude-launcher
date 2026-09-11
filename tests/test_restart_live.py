"""``tools/restart_live.py`` restarts the daemon only when a restart would
change what it serves.

The RestartClock runs the ``restart:`` command the moment ``reflect`` is
entered, so the command itself has to ask whether there is anything to
deploy. It delegates that to ``tools/deploy_check.py`` and restarts on exactly
one of its four answers.
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

    def run(command):
        calls.append(list(command))
        return 0

    return calls, run


def test_older_code_is_restarted(verdict, restarts, capsys):
    verdict(deploy_check.NOT_RESTARTED)
    calls, run = restarts
    assert restart_live.main(["--branch", "master"], run=run) == 0
    assert calls == [["/fake/claunch", "daemon", "restart"]]
    assert "restarting" in capsys.readouterr().out


def test_code_already_served_is_left_alone(verdict, restarts, capsys):
    """The 2026-09-11 17:09 case: a tip that differs only in .beads."""
    verdict(deploy_check.SERVING)
    calls, run = restarts
    assert restart_live.main([], run=run) == 0
    assert calls == []
    assert "no restart needed" in capsys.readouterr().out


@pytest.mark.parametrize("code", [deploy_check.CANNOT_TELL, deploy_check.DIRTY])
def test_verdicts_a_restart_cannot_fix_do_not_restart(verdict, restarts, capsys, code):
    """A dirty checkout stays dirty across a restart; an unanswerable question
    stays unanswered. Both come back as the check's own exit code so the
    RestartClock's result block names them."""
    verdict(code)
    calls, run = restarts
    assert restart_live.main([], run=run) == code
    assert calls == []
    assert "not restarting" in capsys.readouterr().err


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
    """A verdict added to deploy_check must be classified here, not fall
    through to a restart by omission."""
    verdicts = {
        deploy_check.SERVING, deploy_check.NOT_RESTARTED,
        deploy_check.CANNOT_TELL, deploy_check.DIRTY,
    }
    handled = {deploy_check.SERVING, deploy_check.NOT_RESTARTED} | set(
        restart_live._NOT_FIXED_BY_RESTART
    )
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
