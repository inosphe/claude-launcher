"""Codex full-access defaults across direct and wizard session launches."""

from __future__ import annotations

import argparse
import sys

import pytest

from claude_launcher import cli, harnesses, lineage, profile, runner, spawn, wizard
from claude_launcher.daemon import harness as daemon_harness
from claude_launcher.daemon.harness import SessionDef


FULL_ACCESS_FLAG = "--dangerously-bypass-approvals-and-sandbox"

#: How many leading argv elements name the program in the case being run.
#: The ``program_prefix`` fixture sets this to the length its own forced
#: branch produces, so assertions never have to guess it and never have to
#: hardcode one machine's answer.
PREFIX_LEN = 1


class CodexWizardSources(wizard.Sources):
    """A daemon view with one Codex profile and one Codex parent session."""

    def __init__(self, cwd: str):
        self.cwd = cwd

    def harnesses(self):
        return [{"name": "codex", "available": True, "description": "Codex"}]

    def profiles(self):
        return ["codex"]

    def profile_selectors(self):
        return ["codex:codex"]

    def profile_options(self):
        return [
            {"value": "codex:codex", "label": "codex/codex", "harness": "codex"}
        ]

    def profile_details(self):
        return [
            {
                "name": "codex:codex",
                "harness": "codex",
                "harness_available": True,
                "borrow_allowed": False,
                "borrow_mode": "none",
            }
        ]

    def sessions(self):
        return [
            {
                "name": "lead",
                "status": "idle",
                "harness": "codex",
                "profile": "codex:codex",
                "cwd": self.cwd,
                "args": ["--model", "test-model"],
            }
        ]

    def spawn_report(self, parent: str):
        assert parent == "lead"
        return {
            "can_spawn": True,
            "blocked_by": [],
            "depth": 0,
            "max_depth": 3,
            "children_used": 0,
            "children_remaining": 4,
            "may_choose": [],
            "spawnable_harnesses": ["codex"],
            "workspaces": [],
            "child_cflow": "",
        }


def codex_profile():
    selected = profile.create("codex")
    lineage.set_harness(selected, "codex")
    return selected


def assert_full_access(argv, *remaining, prefix=None):
    """Full access leads the harness's own arguments, and nothing precedes it.

    The program prefix is not always one argv element. On Windows the Codex
    npm ``.CMD`` shim is bypassed by invoking Node on the package's entry
    point, so ``argv`` opens with ``[node.exe, codex.js]`` and the flag sits
    at index 2. The length is taken from the case being run rather than
    written in here: anchoring on the flag's own index would accept an
    argument inserted ahead of it, and hardcoding either length would fail
    on the machines that produce the other one.
    """
    at = PREFIX_LEN if prefix is None else prefix
    assert FULL_ACCESS_FLAG not in argv[:at], argv
    assert argv[at:] == [FULL_ACCESS_FLAG, *remaining], argv


@pytest.fixture(params=["one element", "two elements"], autouse=True)
def program_prefix(request, monkeypatch):
    """Run every case in this module under both program prefix shapes.

    ``launch_command`` opens the argv with the declared executable, except
    on Windows with Codex installed through npm globally: there the ``.CMD``
    shim is bypassed and the prefix becomes ``[node.exe, codex.js]``. Which
    shape a run sees therefore depends on the machine, not on the tree --
    the same commit is green on a machine without that install and red on
    one with it, and that is how the regression this module now pins reached
    master unseen.

    Forcing the branch here takes the machine out of the answer: both shapes
    are exercised on every machine and on every platform, because the
    decision is patched rather than measured from the filesystem.
    """
    if request.param == "one element":
        monkeypatch.setattr(
            harnesses.Harness, "_windows_codex_npm_command", lambda self, first: None
        )
        monkeypatch.setattr(sys.modules[__name__], "PREFIX_LEN", 1)
    else:
        monkeypatch.setattr(
            harnesses.Harness,
            "_windows_codex_npm_command",
            lambda self, first: ["node.exe", "codex.js"],
        )
        monkeypatch.setattr(sys.modules[__name__], "PREFIX_LEN", 2)


def test_the_full_access_assertion_holds_for_either_program_prefix():
    """Both prefix shapes are accepted, and an inserted argument is not.

    Which shape ``build_command`` returns depends on what is installed, so a
    regression pinned to only one of them would go unseen on the machines
    that produce the other. The third case is the one an index-anchored
    assertion lets through: a positional argument sitting between the
    program and the flag leaves the flag present and everything after it
    intact, so only a boundary that does not come from the flag's own
    position rejects it.
    """
    assert_full_access(
        ["codex.cmd", FULL_ACCESS_FLAG, "--model", "m"], "--model", "m", prefix=1
    )
    assert_full_access(
        ["node.exe", "codex.js", FULL_ACCESS_FLAG, "--model", "m"],
        "--model",
        "m",
        prefix=2,
    )
    with pytest.raises(AssertionError):
        assert_full_access(
            ["codex.cmd", "resume", FULL_ACCESS_FLAG, "--model", "m"],
            "--model",
            "m",
            prefix=1,
        )
    with pytest.raises(AssertionError):
        assert_full_access(
            ["codex.cmd", "--model", "m"], "--model", "m", prefix=1
        )


def test_claunch_run_uses_the_codex_full_access_default(
    home, tmp_path, monkeypatch
):
    codex_profile()
    reached = {}

    def capture(command, **kwargs):
        if command and command[0] == "git":
            return type("Done", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        if FULL_ACCESS_FLAG in command:
            reached["command"] = list(command)
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(runner.subprocess, "run", capture)
    monkeypatch.chdir(tmp_path)

    assert cli.main(
        ["run", "codex:codex", "--no-worktree", "--model", "test-model"]
    ) == 0
    assert_full_access(reached["command"], "--model", "test-model")


def test_new_wizard_codex_launch_uses_the_full_access_default(
    home, tmp_path, monkeypatch
):
    codex_profile()
    monkeypatch.setattr(harnesses.Harness, "available", lambda self: True)
    form = wizard.Wizard(
        CodexWizardSources(str(tmp_path)),
        cwd=str(tmp_path),
        defaults=argparse.Namespace(args=["--", "--model", "test-model"]),
    )
    answers = argparse.Namespace()
    form.apply(answers)

    assert answers.profile == "codex:codex"
    session = daemon_harness.normalize(
        SessionDef(
            name="new-codex",
            profile=answers.profile,
            cwd=str(tmp_path),
            args=answers.args,
        )
    )
    argv, _, _ = daemon_harness.build_command(session)
    assert_full_access(argv, "--model", "test-model")


def test_spawn_wizard_codex_launch_uses_the_full_access_default(
    home, tmp_path, monkeypatch
):
    codex_profile()
    monkeypatch.setattr(harnesses.Harness, "available", lambda self: True)
    parent = daemon_harness.normalize(
        SessionDef(
            name="lead",
            profile="codex:codex",
            cwd=str(tmp_path),
            args=("--model", "test-model"),
        )
    )
    form = wizard.SpawnWizard(CodexWizardSources(str(tmp_path)), cwd=str(tmp_path))
    answers = argparse.Namespace()
    form.apply(answers)

    assert answers.parent == "lead"
    child_fields = spawn.check(
        spawn.SpawnPolicy(),
        {"profile": answers.profile, "args": answers.args},
        parent=parent.to_dict(),
        depth=0,
        children=0,
    )
    child = daemon_harness.normalize(SessionDef(name="child", **child_fields))
    argv, _, _ = daemon_harness.build_command(child)
    assert_full_access(argv, "--model", "test-model")
