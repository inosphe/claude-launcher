"""End-to-end CLI flows through main() (no subprocess / network commands)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from claude_launcher import (
    cli,
    config,
    credentials,
    harnesses,
    lineage,
    profile,
    runner,
    store,
)


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
    with pytest.raises(SystemExit):
        run("install", "--all", "--profile", "work")


def test_install_global_through_the_cli(home, capsys, tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    assert run("install", "--global") == 0
    out = capsys.readouterr().out
    assert "workflow ->" in out
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    assert (cfg / "skills" / "cflow" / "SKILL.md").is_file()


def test_install_all_profile_covers_every_profile_but_not_global(home, capsys, tmp_path, monkeypatch):
    import json
    import os

    monkeypatch.chdir(tmp_path)
    run("create", "work", "--no-seed")
    run("create", "play", "--no-seed")
    capsys.readouterr()
    assert run("install", "--all-profile") == 0
    out = capsys.readouterr().out
    # each profile got its own MCP registration and skills
    for name in ("work", "play"):
        pdir = config.profiles_dir() / name
        assert (pdir / "skills" / "cflow" / "SKILL.md").is_file()
        servers = json.loads((pdir / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
        assert "claunch" in servers
    # the machine-wide workflow layer is seeded, but the user's global
    # setup is left alone — that stays --global's job
    assert "workflow ->" in out
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    assert not (cfg / "skills" / "cflow" / "SKILL.md").exists()


def test_install_all_is_an_alias_of_all_profile(home, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("install", "--all") == 0
    pdir = config.profiles_dir() / "work"
    assert (pdir / "skills" / "cflow" / "SKILL.md").is_file()


def test_install_all_profile_without_profiles_says_so(home, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("install", "--all-profile") == 0
    assert "no profiles exist" in capsys.readouterr().out


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


def test_profile_harness_is_created_inherited_pinned_and_cleared(home, capsys):
    assert run("create", "base", "--no-seed", "--harness", "pi") == 0
    assert run("create", "child", "--no-seed", "--parent", "base") == 0
    child = profile.require("child")
    assert lineage.effective_harness(child) == "pi"

    assert run("set-harness", "child", "kimi") == 0
    assert lineage.effective_harness(child) == "kimi"
    assert store.profile_entry("child")["harness"] == "kimi"

    assert run("set-harness", "child", "--clear") == 0
    assert lineage.effective_harness(child) == "pi"
    assert "harness" not in store.profile_entry("child")


def test_set_key_uses_the_harness_declared_route(home, capsys):
    run("create", "pi-work", "--no-seed", "--harness", "pi")
    store.set_profile_field("pi-work", "api_key_env", "OPENAI_API_KEY")
    capsys.readouterr()

    assert run("set-key", "pi-work", "pi-secret") == 0
    p = profile.require("pi-work")
    assert credentials.stored_api_key(p) == "pi-secret"
    assert credentials.stored_token(p) is None
    assert "api_key_env" not in store.profile_entry("pi-work")
    assert harnesses.get("pi").api_key_env == "ANTHROPIC_API_KEY"
    assert "pi-secret" not in store.path().read_text(encoding="utf-8")


def test_set_key_uses_packaged_claude_bearer_route_without_profile_metadata(
    home, capsys
):
    run("create", "gateway", "--no-seed")
    doc = store.load()
    doc["providers"] = {"kimi": {"env": {"ANTHROPIC_BASE_URL": "https://x"}}}
    store.save(doc)
    store.set_profile_field("gateway", "provider", "kimi")
    capsys.readouterr()

    assert run("set-key", "gateway", "kimi-secret") == 0
    p = profile.require("gateway")
    env = runner.child_env(p, with_token=True)

    assert env["ANTHROPIC_AUTH_TOKEN"] == "kimi-secret"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert "api_key_env" not in store.profile_entry("gateway")


def test_set_harness_refuses_value_with_clear(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("set-harness", "work", "pi", "--clear") == 1
    assert "not both" in capsys.readouterr().err


def test_set_key_is_refused_for_oauth_harnesses(home, capsys):
    run("create", "kimi-work", "--no-seed", "--harness", "kimi")
    capsys.readouterr()
    assert run("set-key", "kimi-work", "secret") == 1
    assert "declares no API-key route" in capsys.readouterr().err


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


def test_non_claude_run_does_not_steal_the_harness_provider_flag(
    home, monkeypatch, capsys
):
    run("create", "pi-work", "--no-seed", "--harness", "pi")
    run("set-key", "pi-work", "pi-secret")
    reached = {}

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return _REAL_RUN(cmd, **kwargs)
        if "env" not in kwargs:
            return type("Done", (), {"returncode": 0, "stdout": ""})()
        reached["cmd"] = list(cmd)
        reached["env"] = kwargs["env"]
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    capsys.readouterr()
    assert run("run", "pi-work", "--no-worktree", "--provider", "openai") == 0
    assert reached["cmd"][-2:] == ["--provider", "openai"]
    assert reached["env"]["ANTHROPIC_API_KEY"] == "pi-secret"


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
