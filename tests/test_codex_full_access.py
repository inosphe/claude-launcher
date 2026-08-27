"""Codex full-access defaults across direct and wizard session launches."""

from __future__ import annotations

import argparse

from claude_launcher import cli, harnesses, lineage, profile, runner, spawn, wizard
from claude_launcher.daemon import harness as daemon_harness
from claude_launcher.daemon.harness import SessionDef


FULL_ACCESS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


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


def assert_full_access(argv, *remaining):
    assert argv[1:] == [FULL_ACCESS_FLAG, *remaining]


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
