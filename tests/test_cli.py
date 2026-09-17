"""End-to-end CLI flows through main() (no subprocess / network commands)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claude_launcher import (
    bootstrap,
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


def test_public_cli_has_one_secret_writer():
    help_text = cli.build_parser().format_help()
    assert "set-token" in help_text
    assert "set-key" not in help_text


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


def test_install_codex_profile_targets_codex_home(home, capsys):
    run("create", "work", "--no-seed", "--harness", "codex")
    capsys.readouterr()
    assert run("install", "--profile", "work") == 0
    out = capsys.readouterr().out
    pdir = config.profiles_dir() / "work"
    codex_home = pdir / "codex"
    assert (codex_home / "skills" / "cflow" / "SKILL.md").is_file()
    assert not (pdir / "skills").exists()
    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.claunch]" in config_text
    assert 'env_vars = ["CLAUNCH_SESSION"]' in config_text
    assert "restart active agent sessions" in out


def test_cflow_archive_nudges_the_session_that_owned_the_run(
    home, tmp_path, monkeypatch, capsys
):
    from claude_launcher import cli_cflow
    from claude_launcher.cflow import engine as cflow_engine

    workflows = tmp_path / ".claunch" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "tiny.yaml").write_text(
        "name: tiny\nsteps:\n  one:\n    instructions: do one\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    cflow_engine.start("tiny")

    sent = []
    monkeypatch.setattr(
        cli_cflow,
        "_nudge_via_daemon",
        lambda message, scope, cwd: sent.append((message, scope, cwd)) or [scope],
    )

    assert run("cflow", "archive") == 0
    assert sent == [(cflow_engine.NUDGE_ARCHIVED, "s9", None)]
    assert "nudge accepted for session(s): s9" in capsys.readouterr().out


def test_install_all_routes_each_profile_to_its_harness_home(home, capsys):
    run("create", "claude-work", "--no-seed")
    run("create", "codex-work", "--no-seed", "--harness", "codex")
    capsys.readouterr()
    assert run("install", "--all") == 0
    root = config.profiles_dir()
    assert (root / "claude-work" / "skills" / "mesh" / "SKILL.md").is_file()
    assert (
        root / "codex-work" / "codex" / "skills" / "mesh" / "SKILL.md"
    ).is_file()
    assert (root / "codex-work" / "codex" / "config.toml").is_file()


@pytest.mark.parametrize("harness", ["pi", "kimi"])  # agent = kimi's path
def test_install_other_harnesses_use_their_native_home(home, capsys, harness):
    run("create", "work", "--no-seed", "--harness", harness)
    capsys.readouterr()
    assert run("install", "--profile", "work") == 0
    pdir = config.profiles_dir() / "work" / harness
    assert (pdir / "skills" / "cflow" / "SKILL.md").is_file()
    if harness == "pi":
        assert "does not support MCP" in capsys.readouterr().out
        assert not (pdir / "mcp.json").exists()
    else:
        import json

        assert "claunch" in json.loads(
            (pdir / "mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]
        assert not (config.profiles_dir() / "work" / ".claude.json").exists()


def test_install_profile_accepts_a_harness_selector(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("install", "--profile", "work:kimi") == 0
    assert (config.profiles_dir() / "work" / "kimi" / "mcp.json").is_file()


def test_install_all_profile_without_profiles_says_so(home, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("install", "--all-profile") == 0
    assert "no profiles exist" in capsys.readouterr().out


def test_a_project_install_hints_at_the_empty_global_layer(home, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("install") == 0
    assert "claunch install --global" in capsys.readouterr().out
    # once the layer is seeded, the hint goes away
    import os
    assert run("install", "--global") == 0
    assert "workflow ->" in capsys.readouterr().out
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    assert (cfg / "skills" / "cflow" / "SKILL.md").is_file()
    assert run("install") == 0
    assert "claunch install --global" not in capsys.readouterr().out


def test_create_registers_and_applies_template(home, capsys):
    assert run("create", "work", "--no-seed") == 0
    assert "work" in store.profiles()
    # The default template layer was applied into the store.
    entry = store.profile_entry("work")
    assert entry["auto_compact_at"] == 400000
    assert entry["harness_options"]["claude"]["env"] == {
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"
    }


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


def test_create_refuses_parent_denied_harness_before_making_profile(home, capsys):
    parent = profile.create("account")
    store.set_profile_field(parent.name, "allowed_harnesses", ["pi"])
    capsys.readouterr()

    assert run(
        "create", "blocked", "--no-seed", "--parent", "account",
        "--harness", "claude",
    ) == 1
    assert "allows only" in capsys.readouterr().err
    assert not profile.resolve("blocked").config_dir.exists()


def test_set_token_is_shared_and_pi_declares_its_projection(home, capsys):
    run("create", "pi-work", "--no-seed", "--harness", "pi")
    capsys.readouterr()

    assert run("set-token", "pi-work", "pi-secret") == 0
    p = profile.require("pi-work")
    assert credentials.stored_token(p) == "pi-secret"
    assert harnesses.get("pi").token_env == "ANTHROPIC_API_KEY"
    assert runner.harness_child_env(p, harnesses.get("pi"))["ANTHROPIC_API_KEY"] == "pi-secret"
    assert "pi-secret" not in store.path().read_text(encoding="utf-8")


def test_set_token_uses_packaged_claude_bearer_route_without_profile_metadata(
    home, capsys
):
    run("create", "gateway", "--no-seed")
    doc = store.load()
    doc["providers"] = {"kimi": {"env": {"ANTHROPIC_BASE_URL": "https://x"}}}
    store.save(doc)
    store.set_profile_field("gateway", "provider", "kimi")
    capsys.readouterr()

    assert run("set-token", "gateway", "kimi-secret") == 0
    p = profile.require("gateway")
    env = runner.child_env(p, with_token=True)

    assert env["ANTHROPIC_AUTH_TOKEN"] == "kimi-secret"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_set_harness_refuses_value_with_clear(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("set-harness", "work", "pi", "--clear") == 1
    assert "not both" in capsys.readouterr().err


def test_set_token_for_oauth_harness_is_stored_but_not_injected(home, capsys):
    run("create", "kimi-work", "--no-seed", "--harness", "kimi")
    capsys.readouterr()
    assert run("set-token", "kimi-work", "secret") == 0
    p = profile.require("kimi-work")
    env = runner.harness_child_env(p, harnesses.get("kimi"), base_env={})
    assert credentials.stored_token(p) == "secret"
    assert "KIMI_API_KEY" not in env


def test_set_and_get_token(home, capsys):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()
    assert run("get-token", "work") == 0
    assert capsys.readouterr().out.strip() == "sk-ant-oat01-X"


def test_token_commands_name_the_base_profile_because_variants_share_it(
    home, capsys
):
    run("create", "work", "--no-seed")
    capsys.readouterr()
    assert run("set-token", "work:pi", "secret") == 1
    assert "all harness variants share one token" in capsys.readouterr().err
    assert credentials.stored_token(profile.require("work")) is None


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


def test_path_qualified_selector_prints_namespaced_harness_home(home, capsys):
    p = profile.create("ds4")
    assert run("path", "ds4:pi") == 0
    assert Path(capsys.readouterr().out.strip()) == p.config_dir / "pi"


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
    run("create", "pi-work", "--no-seed")
    run("set-token", "pi-work", "pi-secret")
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
    assert run("run", "pi-work:pi", "--no-worktree", "--provider", "openai") == 0
    assert reached["cmd"][-2:] == ["--provider", "openai"]
    assert reached["env"]["ANTHROPIC_API_KEY"] == "pi-secret"
    assert lineage.effective_harness(profile.require("pi-work")) == "claude"


def test_pi_run_consumes_base_profile_borrow_as_a_launcher_flag(
    home, monkeypatch, capsys
):
    run("create", "pi-work", "--no-seed")
    run("create", "ds4", "--no-seed")
    run("set-token", "pi-work", "own-secret")
    run("set-token", "ds4", "lender-secret")
    reached = {}

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return _REAL_RUN(cmd, **kwargs)
        if kwargs.get("env") is not None:
            reached["cmd"] = list(cmd)
            reached["env"] = kwargs.get("env")
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    capsys.readouterr()

    assert run(
        "run", "pi-work:pi", "--no-worktree", "--borrow", "ds4", "--verbose"
    ) == 0
    assert reached["cmd"][-1] == "--verbose"
    assert "--borrow" not in reached["cmd"]
    assert reached["env"]["ANTHROPIC_API_KEY"] == "lender-secret"
    assert "exported as ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_run_rejects_a_qualified_borrow_selector(home, capsys):
    run("create", "work", "--no-seed")
    run("create", "ds4", "--no-seed")
    capsys.readouterr()

    assert run("run", "work", "--no-worktree", "--borrow", "ds4:claude") == 1
    assert "borrow targets a base profile" in capsys.readouterr().err


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


def test_provider_list_shows_allowed_harnesses_including_explicit_none(home, capsys):
    doc = store.load()
    doc.setdefault("providers", {}).update(
        {
            "claude-only": {"env": {}, "allowed_harnesses": ["claude"]},
            "disabled": {"env": {}, "allowed_harnesses": []},
        }
    )
    store.save(doc)
    capsys.readouterr()

    assert run("providers") == 0
    out = capsys.readouterr().out
    assert "claude-only  [harnesses: claude]" in out
    assert "disabled  [harnesses: (none)]" in out


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


def test_the_board_answers_are_mutually_exclusive_on_both_creation_commands():
    """One session, one board answer. ``--issue-text`` writes a new issue,
    ``--issue`` adopts one that exists, ``--no-issue`` and ``--no-issue-auto``
    ask for none — sent in pairs the daemon would have to guess, and the guess
    it makes today drops the written text without a word. argparse refuses the
    pair first, so the CLI and the API agree about what a request may mean.

    The two no-issue flags are in the group with the rest for a reason of
    their own: they leave the board identically empty and tell the session
    opposite things about that emptiness, so a request carrying both is not a
    preference to resolve either.
    """
    import itertools

    import pytest

    parser = cli.build_parser()
    answers = (
        ["--issue", "cl-1"], ["--no-issue"], ["--no-issue-auto"],
        ["--issue-text", "the spec"],
    )
    for cmd in (["new-session"], ["spawn"]):
        for one, two in itertools.combinations(answers, 2):
            with pytest.raises(SystemExit):
                parser.parse_args(cmd + one + two)
        # ...and each on its own parses, landing in its own field
        for flags in answers:
            parser.parse_args(cmd + flags)

    args = parser.parse_args(["new-session", "--issue-text", "the spec"])
    assert args.issue_text == "the spec"
    assert args.issue is None and args.no_issue is False
    assert args.no_issue_auto is False


def test_the_two_no_issue_answers_travel_as_two_values_of_one_key(
    home, monkeypatch, capsys,
):
    """The board switch is one key, ``beads``, and the answers are its values.

    ``False`` is the older spelling and has to keep meaning what it meant, or
    a script written before the answer was split would silently change what
    the session it creates is told to do. The auto answer is a string, which
    the payload's truthiness filter would keep on its own -- it is pinned
    here anyway, because "the filter happens to keep it" is not a contract.
    """
    from claude_launcher import daemon_client

    posts = []

    class FakeClient:
        base_url = "http://127.0.0.1:0"

        def get(self, path):
            return {}

        def post(self, path, payload):
            posts.append((path, payload))
            # the shape both commands print from: spawn reads the nested
            # session, new-session the flat record
            return {
                "session": {"name": "w9"},
                "name": "w9", "harness": "claude", "pid": 1,
            }

    monkeypatch.setattr(daemon_client, "ensure_running", lambda: FakeClient())
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")

    def body(cmd, *flags):
        posts.clear()
        run(cmd, "--task", "go", *flags)
        assert posts, (cmd, flags)
        return posts[-1][1]

    assert "beads" not in body("spawn")                 # did not say: mint one
    assert body("spawn", "--no-issue")["beads"] is False
    assert body("spawn", "--no-issue-auto")["beads"] == "none-auto"

    # the human's command builds its own body, so it is pinned separately --
    # the two have drifted before. It refuses to run from inside a managed
    # session, which is why the variable goes away first.
    monkeypatch.delenv("CLAUNCH_SESSION")
    new = ("new-session", "--profile", "work")
    assert "beads" not in body(*new)
    assert body(*new, "--no-issue")["beads"] is False
    assert body(*new, "--no-issue-auto")["beads"] == "none-auto"


def test_the_child_cap_flags_are_three_valued(home):
    """"Did not say" has to reach the daemon as an ABSENT key.

    The cap crosses by default, so a `False` default would send "hold me to
    the cap" on every plain `claunch spawn` — the exact refusal this stopped
    giving. Three states, three answers: None / True / False.
    """
    parser = cli.build_parser()
    assert parser.parse_args(["spawn"]).over_limit is None
    assert parser.parse_args(["spawn", "--over-limit"]).over_limit is True
    assert parser.parse_args(["spawn", "--within-limit"]).over_limit is False


def test_the_child_cap_answer_reaches_the_payload(home, monkeypatch, capsys):
    """...and survives the truthy filter that builds the request body.

    `over_limit: False` is a real answer and a falsy value at once, which is
    what the payload comprehension drops. It has to be put back, or the one
    flag that still asks for a refusal would silently do nothing.
    """
    from claude_launcher import cli_sessions, daemon_client

    posts = []

    class FakeClient:
        def post(self, path, payload):
            posts.append((path, payload))
            return {"session": {"name": "w9"}, "warnings": ["at the cap"]}

    monkeypatch.setattr(daemon_client, "ensure_running", lambda: FakeClient())
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")

    def body(*flags):
        posts.clear()
        run("spawn", *flags)
        return posts[-1][1]

    assert "over_limit" not in body()               # did not say
    assert body("--over-limit")["over_limit"] is True
    assert body("--within-limit")["over_limit"] is False
    assert body("--model", "terra")["model"] == "terra"
    assert body("--model", "")["model"] == ""
    # tools: absent inherits, `none` is the empty list the truthy filter
    # would otherwise drop, names travel as a list
    assert "tools" not in body()
    assert body("--tools", "none")["tools"] == []
    assert body("--tools", "full_read")["tools"] == ["full_read"]
    assert body("--tools", "full_read, other")["tools"] == ["full_read", "other"]

    # And the daemon's warning is printed, above the line it is about.
    out = capsys.readouterr().out
    assert "warning: at the cap" in out
    assert out.index("warning: at the cap") < out.index("spawned w9")


def test_a_transient_store_error_from_bootstrap_is_reported_not_a_traceback(
    home, capsys, monkeypatch
):
    """Every command runs ``bootstrap.run()`` before its own work (board
    claunch-qd9q). A bare ``OSError`` there used to reach nobody's ``except``
    and crash with a raw traceback -- exactly what made a transient Windows
    sharing conflict look like the run's own work was broken. Wrapped as
    ``store.TransientStoreError`` (a ``StoreError``), it now lands in the
    ``error: ...`` / exit 1 path every other store failure already uses."""

    def boom():
        raise store.TransientStoreError(
            "config file locked by another process; run the command again"
        )

    monkeypatch.setattr(bootstrap, "run", boom)

    code = run("providers")

    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "run the command again" in err
