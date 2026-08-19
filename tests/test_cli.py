"""End-to-end CLI flows through main() (no subprocess / network commands)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from claude_launcher import cli, config, runner, store


def run(*argv):
    return cli.main(list(argv))


def test_install_scopes_are_mutually_exclusive(home, capsys, tmp_path, monkeypatch):
    import pytest

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        run("install", "--global", "--project")
    # ...on the aliases too, since they share the parser helper
    with pytest.raises(SystemExit):
        run("cflow", "install", "--global", "--profile", "work")
    with pytest.raises(SystemExit):
        run("mesh", "install", "--global", "--project")


def test_install_global_through_the_cli(home, capsys, tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    assert run("install", "--global") == 0
    out = capsys.readouterr().out
    assert "workflow ->" in out
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    assert (cfg / "skills" / "cflow" / "SKILL.md").is_file()


def test_a_project_install_hints_at_the_empty_global_layer(home, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("install") == 0
    assert "claunch install --global" in capsys.readouterr().out
    # once the layer is seeded, the hint goes away
    run("install", "--global")
    capsys.readouterr()
    assert run("install") == 0
    assert "claunch install --global" not in capsys.readouterr().out


def test_create_registers_and_applies_template(home, capsys):
    assert run("create", "work", "--no-seed") == 0
    assert "work" in store.profiles()
    # Default template env was applied into the store.
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in store.profile_entry("work")["env"]


def test_env_set_and_show(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("env", "work", "FOO=bar") == 0
    capsys.readouterr()
    assert run("env", "work") == 0
    out = capsys.readouterr().out
    assert "FOO=bar" in out


def test_create_child_inherits_env(home, capsys):
    run("create", "work", "--no-seed")
    run("env", "work", "FOO=bar")
    run("create", "dev", "--no-seed", "--parent", "work")
    capsys.readouterr()
    assert run("env", "dev", "--effective") == 0
    out = capsys.readouterr().out
    assert "FOO=bar" in out


def test_set_and_get_token(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()
    assert run("get-token", "work") == 0
    assert capsys.readouterr().out.strip() == "sk-ant-oat01-X"


def test_get_token_own_requires_own(home, capsys):
    run("create", "base", "--no-seed")
    run("create", "child", "--no-seed", "--parent", "base")
    run("set-token", "base", "sk-ant-oat01-P")
    capsys.readouterr()
    # Inherited resolution works...
    assert run("get-token", "child") == 0
    assert capsys.readouterr().out.strip() == "sk-ant-oat01-P"
    # ...but --own has nothing to print and errors out.
    assert run("get-token", "child", "--own") == 1


def test_prune_removes_orphan(home, capsys):
    run("create", "keep", "--no-seed")
    (config.profiles_dir() / "orphan").mkdir(parents=True)
    capsys.readouterr()
    assert run("prune") == 0
    out = capsys.readouterr().out
    assert "orphan" in out
    assert not (config.profiles_dir() / "orphan").exists()


def test_unknown_profile_errors(home, capsys):
    assert run("env", "ghost") == 1


_REAL_RUN = subprocess.run


def _capture_launch(monkeypatch):
    """A ``subprocess.run`` that records the claude launch and really runs git."""
    captured = {}

    def fake_launch(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return _REAL_RUN(cmd, **kwargs)
        if cmd and cmd[0] == config.claude_bin():
            captured["args"] = list(cmd[1:])
            captured["env"] = kwargs.get("env")
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(runner.subprocess, "run", fake_launch)
    return captured


def test_run_null_launches_without_oauth_token(home, monkeypatch, capsys):
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-shell-token")
    captured = _capture_launch(monkeypatch)
    capsys.readouterr()
    assert run("run", "work", "--null", "--no-worktree", "--resume") == 0
    # Neither the stored token nor the shell leftover reaches claude.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["env"]
    assert captured["args"] == ["--resume"]
    assert "no OAuth token" in capsys.readouterr().err


def test_run_without_null_still_injects_token(home, monkeypatch, capsys):
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    captured = _capture_launch(monkeypatch)
    capsys.readouterr()
    assert run("run", "work", "--no-worktree") == 0
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-X"


def test_run_null_conflicts_with_borrow(home, capsys):
    run("create", "work", "--no-seed")
    run("create", "other", "--no-seed")
    capsys.readouterr()
    assert run("run", "work", "--null", "--borrow", "other") == 1
    assert "cannot be combined with --borrow" in capsys.readouterr().err


def test_extract_null_stops_at_separator():
    # A literal --null after `--` belongs to claude, not the launcher.
    found, rest = cli._extract_null(["--", "--null"])
    assert found is False
    assert rest == ["--", "--null"]


def test_set_provider_and_list(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("providers") == 0
    out = capsys.readouterr().out
    assert "default" in out


def test_set_provider_pin_and_clear(home, capsys):
    # Define a provider directly in the store, set it globally.
    doc = store.load()
    doc.setdefault("providers", {})["glm"] = {"env": {"ANTHROPIC_BASE_URL": "https://x"}}
    store.save(doc)
    run("create", "work", "--no-seed")
    assert run("set-provider", "glm") == 0  # global
    # Pin the profile back to default over the global provider.
    assert run("set-provider", "work", "default") == 0
    assert store.profile_entry("work")["provider"] == "default"
    # Clear the override -> inherits global again.
    assert run("set-provider", "work", "--clear") == 0
    assert "provider" not in store.profile_entry("work")


def test_set_provider_clear_with_value_errors(home, capsys):
    run("create", "work", "--no-seed")
    assert run("set-provider", "work", "glm", "--clear") == 1
