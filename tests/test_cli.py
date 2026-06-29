"""End-to-end CLI flows through main() (no subprocess / network commands)."""

from __future__ import annotations

from claude_launcher import cli, config, store


def run(*argv):
    return cli.main(list(argv))


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
