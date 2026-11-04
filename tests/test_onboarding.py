"""claunch onboard / validate / run around Claude Code's first-run flag (claunch-1lpzm.1)."""

from __future__ import annotations

import json
import os

import pytest

from claude_launcher import cli, onboarding, profile, runner


def run(*argv):
    return cli.main(list(argv))


def _config(name):
    return onboarding.config_path(profile.require(name))


def _write(name, data):
    path = _config(name)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _capture_launch(monkeypatch):
    captured = {}

    def fake_spawn(p, args, **kwargs):
        captured["profile"] = p.name
        return 0

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    return captured


# ------------------------------------------------------------------ module


def test_mark_keeps_every_other_key_and_the_file_mode(home):
    run("create", "work", "--no-seed")
    path = _write("work", {"theme": "dark", "numStartups": 3})
    os.chmod(path, 0o644)
    p = profile.require("work")
    assert onboarding.state(p) == onboarding.MISSING
    assert onboarding.mark(p) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"theme": "dark", "numStartups": 3, "hasCompletedOnboarding": True}
    if os.name != "nt":  # Windows has no POSIX permission bits to keep
        assert path.stat().st_mode & 0o777 == 0o644
    assert onboarding.state(p) == onboarding.DONE
    # No temp file is left beside it.
    assert [f.name for f in path.parent.glob(".claude.json.*")] == []


def test_mark_is_a_no_op_when_already_done(home):
    run("create", "work", "--no-seed")
    path = _write("work", {"hasCompletedOnboarding": True, "theme": "light"})
    before = path.read_text(encoding="utf-8")
    assert onboarding.mark(profile.require("work")) is False
    assert path.read_text(encoding="utf-8") == before


def test_mark_creates_the_file_when_absent(home):
    run("create", "work", "--no-seed")
    path = _config("work")
    path.unlink(missing_ok=True)
    assert onboarding.mark(profile.require("work")) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "hasCompletedOnboarding": True
    }


def test_mark_refuses_a_broken_file_and_leaves_it(home):
    run("create", "work", "--no-seed")
    path = _config("work")
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(onboarding.OnboardingError):
        onboarding.mark(profile.require("work"))
    assert path.read_text(encoding="utf-8") == "{not json"
    assert onboarding.state(profile.require("work")) == onboarding.MISSING


# ------------------------------------------------------------------ onboard


def test_onboard_writes_the_flag_for_a_profile_with_a_token(home, capsys):
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()
    assert run("onboard", "work") == 0
    assert "done" in capsys.readouterr().out
    assert json.loads(_config("work").read_text(encoding="utf-8"))[
        "hasCompletedOnboarding"
    ] is True


def test_onboard_refuses_a_profile_without_a_credential(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("onboard", "work") == 1
    assert "claunch login work" in capsys.readouterr().out
    assert onboarding.state(profile.require("work")) == onboarding.MISSING


def test_onboard_check_only_reads(home, capsys):
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()
    assert run("onboard", "work", "--check") == 1
    assert "missing" in capsys.readouterr().out
    assert onboarding.state(profile.require("work")) == onboarding.MISSING
    run("onboard", "work")
    capsys.readouterr()
    assert run("onboard", "work", "--check") == 0
    assert "done" in capsys.readouterr().out


def test_onboard_all_marks_those_it_can_and_fails_the_rest(home, capsys):
    run("create", "work", "--no-seed")
    run("create", "play", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()
    assert run("onboard", "--all") == 1
    out = capsys.readouterr().out
    assert "claunch login play" in out
    assert onboarding.state(profile.require("work")) == onboarding.DONE
    assert onboarding.state(profile.require("play")) == onboarding.MISSING


def test_onboard_needs_exactly_one_target(home, capsys):
    run("create", "work", "--no-seed")
    assert run("onboard") != 0
    assert run("onboard", "work", "--all") != 0


# ------------------------------------------------------------------ validate


def test_validate_reports_the_flag_without_writing_it(home, monkeypatch, capsys):
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    monkeypatch.setattr(
        runner,
        "heartbeat",
        lambda p, **kw: runner.Heartbeat(ok=True, code=0, reason="", output="pong"),
    )
    capsys.readouterr()
    assert run("validate", "work") == 0
    assert "onboarding: missing (run 'claunch onboard work')" in capsys.readouterr().out
    assert onboarding.state(profile.require("work")) == onboarding.MISSING
    run("onboard", "work")
    capsys.readouterr()
    assert run("validate", "work") == 0
    assert "onboarding: done" in capsys.readouterr().out


# ------------------------------------------------------------------ run


def test_run_hints_at_onboard_and_does_not_write(home, monkeypatch, capsys):
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    _capture_launch(monkeypatch)
    capsys.readouterr()
    assert run("run", "work", "--no-worktree") == 0
    assert "claunch onboard work" in capsys.readouterr().err
    assert onboarding.state(profile.require("work")) == onboarding.MISSING


def test_run_is_quiet_once_onboarded(home, monkeypatch, capsys):
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    run("onboard", "work")
    _capture_launch(monkeypatch)
    capsys.readouterr()
    assert run("run", "work", "--no-worktree") == 0
    assert "claunch onboard" not in capsys.readouterr().err


def test_run_without_auth_or_with_null_does_not_hint(home, monkeypatch, capsys):
    run("create", "work", "--no-seed")
    _capture_launch(monkeypatch)
    capsys.readouterr()
    # No credential: the first-run login is the way in, so no hint.
    assert run("run", "work", "--no-worktree") == 0
    assert "claunch onboard" not in capsys.readouterr().err
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()
    assert run("run", "work", "--null", "--no-worktree") == 0
    assert "claunch onboard" not in capsys.readouterr().err
