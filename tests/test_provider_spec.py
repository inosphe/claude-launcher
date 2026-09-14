"""The harness-neutral provider spec, its per-harness translators, and the
config migration from the Claude-vocabulary ``env`` form."""

from __future__ import annotations

import json

import pytest

from claude_launcher import (
    credentials,
    harnesses,
    lineage,
    migrate_config,
    pi_provider,
    profile,
    provider_spec,
    providers,
    runner,
    settings,
    store,
    translators,
)
from claude_launcher.cli import main as cli_main

DEEPSEEK = {
    "api_key": "sk-deepseek",
    "endpoints": {
        "anthropic": "https://api.example.com/anthropic",
        "openai": "https://api.example.com",
    },
    "models": {"default": "flash", "small": "flash", "large": "pro"},
    "context_window": 1_000_000,
    "auto_compact_at": 900_000,
}

EXPLICIT_REASONING = {
    "reasoning_effort": "high",
    "openai_reasoning_format": "deepseek",
}

LEGACY_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_AUTH_TOKEN": "sk-legacy",
    "ANTHROPIC_BASE_URL": "https://api.example.com/anthropic",
    "ANTHROPIC_MODEL": "flash[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "flash[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "flash[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "pro[1m]",
    "CLAUDE_CODE_SUBAGENT_MODEL": "flash[1m]",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
}


@pytest.fixture(autouse=True)
def _scrub_ambient(monkeypatch):
    for key in (
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_CODE_EFFORT_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)


def _provider(name: str, entry: dict) -> None:
    store.update(lambda doc: doc.setdefault("providers", {}).update({name: entry}))


def _profile_on(name: str, provider: str, *, harness: str | None = None):
    p = profile.create(name)
    store.set_profile_field(p.name, "provider", provider)
    if harness:
        lineage.set_harness(p, harness)
    return p


# --- spec parsing ------------------------------------------------------------


def test_spec_reads_schema_fields_and_role_fallbacks(home):
    _provider("ds", DEEPSEEK)
    spec = providers.spec("ds")
    assert spec.api_key == "sk-deepseek"
    assert spec.endpoint("openai") == "https://api.example.com"
    assert spec.model("subagent") == "flash"  # falls back to small
    assert spec.model_list() == ("flash", "pro")
    assert spec.legacy_env is None


def test_spec_rejects_unknown_role_channel_and_bad_numbers(home):
    _provider("bad-role", {"models": {"sonnet": "x"}})
    with pytest.raises(providers.ProviderError, match="models.sonnet"):
        providers.spec("bad-role")
    _provider("bad-channel", {"harness_options": {"claude": {"config": {}}}})
    with pytest.raises(providers.ProviderError, match="claude.config"):
        providers.spec("bad-channel")
    _provider("bad-window", {"context_window": "lots"})
    with pytest.raises(providers.ProviderError, match="context_window"):
        providers.spec("bad-window")
    _provider("inverted", {"context_window": 100, "auto_compact_at": 200})
    with pytest.raises(providers.ProviderError, match="exceeds"):
        providers.spec("inverted")
    _provider("bad-effort", {"reasoning_effort": "maximum"})
    with pytest.raises(providers.ProviderError, match="reasoning_effort"):
        providers.spec("bad-effort")
    _provider("bad-format", {"openai_reasoning_format": "guessed"})
    with pytest.raises(providers.ProviderError, match="openai_reasoning_format"):
        providers.spec("bad-format")


def test_profile_overlay_covers_models_window_and_options_not_endpoints(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds")
    store.set_profile_field(p.name, "models", {"default": "flash-pinned"})
    store.set_profile_field(p.name, "auto_compact_at", 600_000)
    store.set_profile_field(p.name, "reasoning_effort", "high")
    store.set_profile_field(
        p.name, "harness_options", {"claude": {"env": {"EXTRA": "1"}}}
    )
    spec = providers.spec_for(p)
    assert spec.model("default") == "flash-pinned"
    assert spec.model("large") == "pro"
    assert spec.auto_compact_at == 600_000
    assert spec.context_window == 1_000_000
    assert spec.reasoning_effort == "high"
    assert spec.option_env("claude") == {"EXTRA": "1"}
    store.set_profile_field(p.name, "endpoints", {"openai": "https://elsewhere"})
    with pytest.raises(providers.ProviderError, match="belongs on the provider"):
        providers.spec_for(p)
    store.set_profile_field(p.name, "endpoints", None)
    store.set_profile_field(p.name, "openai_reasoning_format", "deepseek")
    with pytest.raises(providers.ProviderError, match="belongs on the provider"):
        providers.spec_for(p)


# --- legacy env ---------------------------------------------------------------


def test_legacy_env_is_reverse_translated_and_kept_verbatim_for_claude(home):
    _provider("old", {"env": LEGACY_ENV})
    spec = providers.spec("old")
    assert spec.api_key == "sk-legacy"
    assert spec.endpoint("anthropic") == "https://api.example.com/anthropic"
    assert spec.endpoint("openai") is None  # path present: not assumed
    assert spec.models == {"default": "flash", "small": "flash", "large": "pro"}
    assert spec.harness_options["claude"]["model_tag"] == "[1m]"
    assert providers.provider_env("old") == LEGACY_ENV


def test_legacy_env_with_mixed_tags_keeps_bare_ids_and_pins_the_tagged_vars(home):
    env = {**LEGACY_ENV, "ANTHROPIC_DEFAULT_HAIKU_MODEL": "tiny"}  # untagged
    _provider("old", {"env": env})
    spec = providers.spec("old")
    assert spec.models == {"default": "flash", "small": "tiny", "large": "pro", "subagent": "flash"}
    assert "model_tag" not in spec.options("claude")
    doc, _ = migrate_config.convert(store.load())
    after = translators.claude(provider_spec.from_entry(doc["providers"]["old"], "t")).env
    for key, value in env.items():
        if value != "":
            assert after[key] == value, key


def test_legacy_env_without_a_path_fills_the_openai_endpoint(home):
    env = {**LEGACY_ENV, "ANTHROPIC_BASE_URL": "https://host.example/"}
    _provider("old", {"env": env})
    assert providers.spec("old").endpoint("openai") == "https://host.example/"


# --- claude translation ---------------------------------------------------------


def test_claude_translation_emits_roles_tag_and_compaction():
    spec = provider_spec.from_entry(DEEPSEEK, "t")
    env = translators.claude(spec).env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-deepseek"
    assert env["ANTHROPIC_MODEL"] == "flash[1m]"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "flash[1m]"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "flash[1m]"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "pro[1m]"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "pro[1m]"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "flash[1m]"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "900000"
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_translation_emits_explicit_reasoning_effort(home):
    _provider("ds", {**DEEPSEEK, **EXPLICIT_REASONING})
    p = _profile_on("work", "ds")
    env = runner.child_env(p, with_token=True, base_env={})
    assert env[provider_spec.CLAUDE_REASONING_EFFORT] == "high"

    store.set_profile_field(p.name, "reasoning_effort", "low")
    env = runner.child_env(p, with_token=True, base_env={})
    assert env[provider_spec.CLAUDE_REASONING_EFFORT] == "low"


def test_claude_model_tag_declared_beats_window_and_empty_disables():
    base = provider_spec.from_entry(DEEPSEEK, "t")
    tagged = provider_spec.from_entry(
        {**DEEPSEEK, "harness_options": {"claude": {"model_tag": "[x]"}}}, "t"
    )
    assert translators.claude(tagged).env["ANTHROPIC_MODEL"] == "flash[x]"
    off = provider_spec.from_entry(
        {**DEEPSEEK, "harness_options": {"claude": {"model_tag": ""}}}, "t"
    )
    assert translators.claude(off).env["ANTHROPIC_MODEL"] == "flash"
    small = provider_spec.from_entry({**DEEPSEEK, "context_window": 128_000, "auto_compact_at": None}, "t")
    assert translators.claude(small).env["ANTHROPIC_MODEL"] == "flash"
    assert translators.claude(base).env["ANTHROPIC_MODEL"] == "flash[1m]"


def test_claude_runner_uses_translation_and_profile_option_env(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds")
    store.set_profile_field(
        p.name, "harness_options", {"claude": {"env": {"CLAUDE_CODE_FLAG": "1"}}}
    )
    settings.set_env(p, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000"})
    env = runner.child_env(p, with_token=True, base_env={})
    assert env["ANTHROPIC_MODEL"] == "flash[1m]"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-deepseek"
    assert env["CLAUDE_CODE_FLAG"] == "1"
    # The legacy profile env is still the raw override channel and wins.
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "500000"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# --- codex translation ------------------------------------------------------------


def test_codex_translation_emits_window_limit_and_config_overrides():
    spec = provider_spec.from_entry(
        {
            "context_window": 1_000_000,
            "auto_compact_at": 900_000,
            "harness_options": {
                "codex": {
                    "env": {"OPENAI_LOG": "debug"},
                    "config": {"model_reasoning_effort": "xhigh", "flag": True, "n": 3},
                }
            },
        },
        "t",
    )
    out = translators.codex(spec)
    assert out.args == [
        "-c", "model_context_window=1000000",
        "-c", "model_auto_compact_token_limit=900000",
        "-c", 'model_reasoning_effort="xhigh"',
        "-c", "flag=true",
        "-c", "n=3",
    ]
    assert out.env == {"OPENAI_LOG": "debug"}
    assert out.notes == []


def test_codex_translation_emits_explicit_reasoning_effort():
    spec = provider_spec.from_entry({"reasoning_effort": "high"}, "t")
    assert translators.codex(spec).args == [
        "-c",
        'model_reasoning_effort="high"',
    ]


def test_codex_launch_args_precede_session_args(home):
    p = profile.create("cx")
    lineage.set_harness(p, "codex")
    store.set_profile_field(p.name, "context_window", 1_000_000)
    store.set_profile_field(p.name, "auto_compact_at", 900_000)
    entry = harnesses.get("codex")
    cmd = runner.harness_launch_args(p, entry, ["-c", "model=gpt-x"])
    assert cmd[:4] == ["-c", "model_context_window=1000000", "-c", "model_auto_compact_token_limit=900000"]
    assert cmd[-2:] == ["-c", "model=gpt-x"]


# --- pi translation -----------------------------------------------------------------


def test_pi_registration_reads_openai_endpoint_models_and_window(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds", harness="pi")
    credentials.save_token(p, "stored")
    env = runner.harness_child_env(p, harnesses.get("pi"), base_env={})
    assert env[pi_provider.ENV_BASE_URL] == "https://api.example.com/v1"
    assert json.loads(env[pi_provider.ENV_MODELS]) == ["flash", "pro"]
    assert env[pi_provider.ENV_CONTEXT_WINDOW] == "1000000"
    assert env["ANTHROPIC_API_KEY"] == "stored"
    assert "ANTHROPIC_BASE_URL" not in env
    cmd = runner.harness_launch_args(p, harnesses.get("pi"), [])
    assert cmd[cmd.index("--model") + 1] == "flash"
    # settings.json got the compaction reserve: window - compact_at
    written = json.loads((p.config_dir / "pi" / "settings.json").read_text())
    assert written["compaction"]["reserveTokens"] == 100_000


def test_pi_reasoning_is_explicit_in_env_args_settings_and_model(home):
    _provider("ds", {**DEEPSEEK, **EXPLICIT_REASONING})
    p = _profile_on("work", "ds", harness="pi")
    env = runner.harness_child_env(p, harnesses.get("pi"), base_env={})
    assert env[pi_provider.ENV_REASONING_EFFORT] == "high"
    assert env[pi_provider.ENV_REASONING_FORMAT] == "deepseek"

    cmd = runner.harness_launch_args(p, harnesses.get("pi"), ["--continue"])
    assert cmd[cmd.index("--thinking") + 1] == "high"
    assert cmd.index("--thinking") < cmd.index("--continue")

    written = json.loads((p.config_dir / "pi" / "settings.json").read_text())
    assert written["defaultThinkingLevel"] == "high"


@pytest.mark.parametrize(
    ("entry", "missing"),
    [
        ({"reasoning_effort": "high"}, "openai_reasoning_format"),
        ({"openai_reasoning_format": "deepseek"}, "reasoning_effort"),
    ],
)
def test_pi_rejects_incomplete_reasoning_declaration(home, entry, missing):
    _provider("ds", {**DEEPSEEK, **entry})
    p = _profile_on("work", "ds", harness="pi")
    with pytest.raises(runner.RunnerError, match=missing):
        runner.harness_child_env(p, harnesses.get("pi"), base_env={})


def test_harness_without_reasoning_translation_rejects_the_field(home):
    p = profile.create("km")
    lineage.set_harness(p, "kimi")
    store.set_profile_field(p.name, "reasoning_effort", "high")
    with pytest.raises(runner.RunnerError, match="cannot translate.*reasoning_effort"):
        runner.harness_child_env(p, harnesses.get("kimi"), base_env={})


def test_pi_refuses_a_provider_without_an_openai_endpoint(home):
    _provider("old", {"env": LEGACY_ENV})  # /anthropic path: openai unknown
    p = _profile_on("work", "old", harness="pi")
    with pytest.raises(runner.RunnerError, match="endpoints.openai"):
        runner.harness_child_env(p, harnesses.get("pi"), base_env={})


def test_pi_falls_back_to_provider_api_key_without_stored_token(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds", harness="pi")
    env = runner.harness_child_env(p, harnesses.get("pi"), base_env={})
    assert env["ANTHROPIC_API_KEY"] == "sk-deepseek"


def test_pi_option_env_and_settings_channel(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds", harness="pi")
    store.set_profile_field(
        p.name,
        "harness_options",
        {"pi": {"env": {"PI_FLAG": "1"}, "settings": {"hideThinkingBlock": True}}},
    )
    home_dir = p.config_dir / "pi"
    home_dir.mkdir(parents=True)
    (home_dir / "settings.json").write_text(json.dumps({"keep": "me"}))
    env = runner.harness_child_env(p, harnesses.get("pi"), base_env={})
    assert env["PI_FLAG"] == "1"
    written = json.loads((home_dir / "settings.json").read_text())
    assert written == {
        "keep": "me",
        "hideThinkingBlock": True,
        "compaction": {"reserveTokens": 100_000},
    }


def test_pi_extension_reads_the_context_window_variable():
    text = pi_provider.extension_path().read_text(encoding="utf-8")
    assert "CLAUNCH_PI_CONTEXT_WINDOW" in text
    assert "contextWindow: 128000" not in text


def test_pi_reasoning_extension_contract():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the Pi extension contract")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("pi_reasoning.test.mjs"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- migration -----------------------------------------------------------------------


def test_migrate_provider_env_reproduces_the_claude_env(home):
    _provider("old", {"env": LEGACY_ENV, "allowed_harnesses": ["claude", "pi"]})
    doc, report = migrate_config.convert(store.load())
    entry = doc["providers"]["old"]
    assert "env" not in entry
    assert entry["allowed_harnesses"] == ["claude", "pi"]
    assert entry["api_key"] == "sk-legacy"
    assert entry["endpoints"] == {"anthropic": "https://api.example.com/anthropic"}
    assert entry["models"] == {"default": "flash", "small": "flash", "large": "pro"}
    assert entry["harness_options"] == {"claude": {"model_tag": "[1m]"}}
    assert doc["version"] == 2
    assert any("endpoints.openai left empty" in w for w in report.warnings)
    # Same variables reach Claude, minus the dropped empty pins, plus FABLE.
    after = translators.claude(provider_spec.from_entry(entry, "t")).env
    expected = {k: v for k, v in LEGACY_ENV.items() if v != ""}
    expected["ANTHROPIC_DEFAULT_FABLE_MODEL"] = "pro[1m]"
    assert after == expected


def test_migrate_keeps_unreproducible_keys_under_claude_options(home):
    env = {**LEGACY_ENV, "ANTHROPIC_DEFAULT_SONNET_MODEL": "other[1m]", "CLAUDE_CODE_X": "1"}
    _provider("old", {"env": env})
    doc, _ = migrate_config.convert(store.load())
    kept = doc["providers"]["old"]["harness_options"]["claude"]["env"]
    assert kept == {"ANTHROPIC_DEFAULT_SONNET_MODEL": "other[1m]", "CLAUDE_CODE_X": "1"}
    after = translators.claude(provider_spec.from_entry(doc["providers"]["old"], "t")).env
    for key, value in env.items():
        if value != "":
            assert after[key] == value


def test_migrate_lifts_claude_reasoning_effort_and_keeps_backend_format(home):
    _provider(
        "old",
        {
            "env": {**LEGACY_ENV, "CLAUDE_CODE_EFFORT_LEVEL": "high"},
            "openai_reasoning_format": "deepseek",
        },
    )
    doc, _ = migrate_config.convert(store.load())
    entry = doc["providers"]["old"]
    assert entry["reasoning_effort"] == "high"
    assert entry["openai_reasoning_format"] == "deepseek"
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in (
        entry.get("harness_options", {}).get("claude", {}).get("env", {})
    )
    assert translators.claude(provider_spec.from_entry(entry, "t")).env[
        "CLAUDE_CODE_EFFORT_LEVEL"
    ] == "high"


def test_migrate_absorbs_round1_pi_block_and_profile_env(home):
    _provider(
        "old",
        {
            "env": LEGACY_ENV,
            "harnesses": {"pi": {"base_url": "https://api.example.com", "models": ["flash", "pro"], "default_model": "flash"}},
        },
    )
    p = _profile_on("work", "old")
    settings.set_env(
        p,
        {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "ANTHROPIC_MODEL": "flash-pinned[1m]",
        },
    )
    doc, _ = migrate_config.convert(store.load())
    prov = doc["providers"]["old"]
    assert "harnesses" not in prov
    assert prov["endpoints"]["openai"] == "https://api.example.com"
    prof = doc["profiles"]["work"]
    assert "env" not in prof
    assert prof["auto_compact_at"] == 600_000
    assert prof["models"] == {"default": "flash-pinned"}
    # The profile pinned ANTHROPIC_MODEL alone; the role would also move
    # SONNET, so the old SONNET value is pinned back to keep the outcome.
    assert prof["harness_options"]["claude"] == {
        "model_tag": "[1m]",
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "flash[1m]",
        },
    }
    before = {**LEGACY_ENV, **settings.get_env(p)}
    after = providers.claude_env(p, "old", doc)
    for key, value in before.items():
        if value != "":
            assert after[key] == value, key
    # Idempotent.
    again, report = migrate_config.convert(doc)
    assert again == doc and not report.changed


def test_migrate_command_writes_backup_and_is_repeatable(home, capsys):
    _provider("old", {"env": LEGACY_ENV})
    assert cli_main(["migrate-config", "--dry-run"]) == 0
    assert "env" in store.load()["providers"]["old"]
    assert cli_main(["migrate-config"]) == 0
    doc = store.load()
    assert "env" not in doc["providers"]["old"] and doc["version"] == 2
    backup = store.path().with_name(store.path().name + migrate_config.BACKUP_SUFFIX)
    assert backup.is_file() and "sk-legacy" in backup.read_text(encoding="utf-8")
    capsys.readouterr()
    assert cli_main(["migrate-config"]) == 0
    assert "already at the current schema" in capsys.readouterr().out


def test_store_refuses_a_newer_schema(home):
    store.path().write_text("version: 99\n", encoding="utf-8")
    with pytest.raises(store.StoreError, match="upgrade claunch"):
        store.load()


def test_compact_window_for_another_harness_comes_from_the_spec_only(home, monkeypatch):
    from claude_launcher.daemon import ctxsize
    from claude_launcher.daemon.session import SessionDef

    p = profile.create("cx")
    lineage.set_harness(p, "codex")
    monkeypatch.setenv(ctxsize.COMPACT_WINDOW_ENV, "111000")
    settings.set_env(p, {ctxsize.COMPACT_WINDOW_ENV: "222000"})
    ctxsize.forget()
    assert ctxsize.compact_window_of(SessionDef(name="s", harness="codex", profile="cx")) is None
    store.set_profile_field(p.name, "auto_compact_at", 900_000)
    ctxsize.forget()
    assert ctxsize.compact_window_of(SessionDef(name="s", harness="codex", profile="cx")) == 900_000


def test_providers_listing_shows_the_spec(home, capsys):
    _provider("ds", {**DEEPSEEK, **EXPLICIT_REASONING})
    _provider("old", {"env": LEGACY_ENV})
    assert cli_main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "openai: https://api.example.com" in out
    assert "models: default=flash, small=flash, large=pro" in out
    assert "context_window=1000000  auto_compact_at=900000" in out
    assert (
        "reasoning_effort=high  openai_reasoning_format=deepseek" in out
    )
    assert "legacy env (run `claunch migrate-config`)" in out


# --- template ----------------------------------------------------------------------


def test_template_layer_is_copied_into_new_profiles_and_keeps_own_fields(home):
    from claude_launcher import template

    store.update(lambda doc: doc.update({"template": {
        "auto_compact_at": 400_000,
        "harness_options": {"claude": {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"}}},
    }}))
    p = profile.create("fresh")
    template.apply_to(p)
    entry = store.profile_entry("fresh")
    assert entry["auto_compact_at"] == 400_000
    assert entry["harness_options"]["claude"]["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "0"
    env = runner.child_env(p, with_token=True, base_env={})
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"
    assert env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "0"
    # A field the profile already sets is kept; option maps merge.
    q = profile.create("own")
    store.set_profile_field("own", "auto_compact_at", 100_000)
    store.set_profile_field("own", "harness_options", {"claude": {"env": {"MINE": "1"}}})
    template.apply_to(q)
    entry = store.profile_entry("own")
    assert entry["auto_compact_at"] == 100_000
    assert entry["harness_options"]["claude"]["env"] == {
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
        "MINE": "1",
    }


def test_migrate_converts_the_template_env_block(home):
    store.update(lambda doc: doc.update({"template": {"env": {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
    }}}))
    doc, report = migrate_config.convert(store.load())
    assert doc["template"] == {
        "auto_compact_at": 400_000,
        "harness_options": {"claude": {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"}}},
    }
    again, rep2 = migrate_config.convert(doc)
    assert again == doc and not rep2.changed


# --- pi builtin tools --------------------------------------------------------------


def test_pi_launch_loads_the_tools_extension_on_every_provider(home):
    _provider("ds", DEEPSEEK)
    custom = _profile_on("work", "ds", harness="pi")
    plain = profile.create("plain")
    lineage.set_harness(plain, "pi")
    entry = harnesses.get("pi")
    tools = str(pi_provider.tools_extension_path())
    cmd = runner.harness_launch_args(custom, entry, [])
    assert cmd[:2] == ["--extension", tools]
    assert cmd[2:4] == ["--extension", str(pi_provider.extension_path())]
    cmd = runner.harness_launch_args(plain, entry, ["-p", "hi"])
    assert cmd == ["--extension", tools, "-p", "hi"]
    env = runner.harness_child_env(custom, entry, base_env={pi_provider.ENV_TOOLS: "stale"})
    assert pi_provider.ENV_TOOLS not in env  # all tools: nothing to narrow


def test_pi_tools_can_be_switched_off_per_profile(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds", harness="pi")
    store.set_profile_field(p.name, "harness_options", {"pi": {"tools": {"full_read": False}}})
    entry = harnesses.get("pi")
    cmd = runner.harness_launch_args(p, entry, [])
    assert str(pi_provider.tools_extension_path()) not in cmd
    env = runner.harness_child_env(p, entry, base_env={})
    assert env[pi_provider.ENV_TOOLS] == ""
    store.set_profile_field(p.name, "harness_options", {"pi": {"tools": {"full_read": "no"}}})
    with pytest.raises(runner.RunnerError, match="true/false"):
        runner.harness_launch_args(p, entry, [])


def test_pi_tools_extension_registers_full_read():
    text = pi_provider.tools_extension_path().read_text(encoding="utf-8")
    assert 'name: "full_read"' in text
    assert "CLAUNCH_PI_TOOLS" in text
    assert "registerTool" in text


# --- per-session tools (SessionDef, spawn policy, run --tools, `claunch tools`) ---


def test_sessiondef_tools_round_trip_and_validation(home):
    from claude_launcher.daemon import harness as dh

    sdef = dh.SessionDef.from_dict({"name": "s", "harness": "pi", "tools": "full_read, full_read"})
    assert sdef.tools == ("full_read",)
    assert dh.SessionDef.from_dict(sdef.to_dict()).tools == ("full_read",)
    assert dh.SessionDef.from_dict({"name": "s", "harness": "pi", "tools": []}).tools == ()
    assert "tools" not in dh.SessionDef.from_dict({"name": "s"}).to_dict()
    assert dh.SessionDef.from_dict({"name": "s"}).tools is None
    _provider("ds", DEEPSEEK)
    _profile_on("work", "ds", harness="pi")
    with pytest.raises(dh.HarnessError, match="unknown tool 'nope'"):
        dh.normalize(dh.SessionDef.from_dict({"name": "s", "profile": "work", "tools": ["nope"]}))
    with pytest.raises(dh.HarnessError, match="no builtin tools"):
        dh.normalize(dh.SessionDef.from_dict({"name": "s", "profile": "work:claude", "tools": ["full_read"]}))


def test_session_tools_override_the_profile_default(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds", harness="pi")
    store.set_profile_field(p.name, "harness_options", {"pi": {"tools": {"full_read": False}}})
    entry = harnesses.get("pi")
    tools_ext = str(pi_provider.tools_extension_path())
    # Profile default: off.
    assert tools_ext not in runner.harness_launch_args(p, entry, [])
    # Session says on: the extension loads and the env names the set.
    cmd = runner.harness_launch_args(p, entry, [], tools=["full_read"])
    assert tools_ext in cmd
    env = runner.harness_child_env(p, entry, base_env={}, tools=["full_read"])
    assert pi_provider.ENV_TOOLS not in env  # the full set: nothing to narrow
    # Session says none while the profile default is on.
    store.set_profile_field(p.name, "harness_options", {})
    env = runner.harness_child_env(p, entry, base_env={}, tools=[])
    assert env[pi_provider.ENV_TOOLS] == ""
    assert tools_ext not in runner.harness_launch_args(p, entry, [], tools=[])


def test_spawn_inherits_gates_and_resets_tools(home):
    from claude_launcher import spawn as spawn_mod

    _provider("ds", DEEPSEEK)
    _profile_on("work", "ds", harness="pi")
    parent = {"name": "p", "harness": "pi", "profile": "work", "cwd": str(home), "tools": ["full_read"]}
    policy = spawn_mod.SpawnPolicy(allow_args=False)
    child = spawn_mod.check(policy, {"name": "c"}, parent=parent, depth=0, children=0)
    assert child["tools"] == ["full_read"]
    with pytest.raises(spawn_mod.SpawnDenied):
        spawn_mod.check(policy, {"name": "c", "tools": []}, parent=parent, depth=0, children=0)
    policy = spawn_mod.SpawnPolicy(allow_args=True, allow_profile=True)
    child = spawn_mod.check(policy, {"name": "c", "tools": "none"}, parent=parent, depth=0, children=0)
    assert child["tools"] == []
    child = spawn_mod.check(policy, {"name": "c", "tools": ["full_read"]}, parent=parent, depth=0, children=0)
    assert child["tools"] == ["full_read"]
    # A harness flip drops the inherited choice unless the request set one.
    profile.create("cl")
    child = spawn_mod.check(policy, {"name": "c", "profile": "cl"}, parent=parent, depth=0, children=0)
    assert "tools" not in child


def test_run_tools_flag_and_profile_default_command(home, monkeypatch, capsys):
    _provider("ds", DEEPSEEK)
    _profile_on("work", "ds", harness="pi")
    reached = {}

    def fake_plain_spawn(prof, entry, args, *, cwd=None, borrow=None, tools=None):
        reached["cmd"] = runner.harness_launch_args(prof, entry, list(args), tools=tools)
        reached["env"] = runner.harness_child_env(prof, entry, base_env={}, tools=tools)
        return 0

    monkeypatch.setattr(runner, "_plain_spawn", fake_plain_spawn)
    monkeypatch.setattr(harnesses.Harness, "available", lambda self: True)
    assert cli_main(["run", "work:pi", "--no-worktree", "--tools", "none"]) == 0
    assert str(pi_provider.tools_extension_path()) not in reached["cmd"]
    assert reached["env"][pi_provider.ENV_TOOLS] == ""
    assert cli_main(["run", "work:pi", "--no-worktree", "--tools", "full_read"]) == 0
    assert str(pi_provider.tools_extension_path()) in reached["cmd"]
    assert cli_main(["run", "work:pi", "--no-worktree", "--tools", "bogus"]) == 1
    capsys.readouterr()
    # `claunch tools` writes the profile default and shows it.
    assert cli_main(["tools", "work", "--off", "full_read"]) == 0
    out = capsys.readouterr().out
    assert "full_read    off" in out
    assert store.profile_entry("work")["harness_options"]["pi"]["tools"] == {"full_read": False}
    assert cli_main(["tools", "work", "--on", "full_read"]) == 0
    assert "harness_options" not in store.profile_entry("work")
    assert "full_read    on" in capsys.readouterr().out


# --- the xlarge tier (Fable) above large (Opus) ---------------------------------------


def test_xlarge_follows_large_unless_set():
    spec = provider_spec.from_entry({"models": {"default": "flash", "large": "pro"}}, "p")
    assert spec.model("xlarge") == "pro"
    assert spec.model_list() == ("flash", "pro")
    env = translators.claude(spec).env
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "pro"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "pro"
    spec = provider_spec.from_entry(
        {"models": {"default": "flash", "large": "pro", "xlarge": "max"}}, "p"
    )
    assert spec.model_list() == ("flash", "pro", "max")
    env = translators.claude(spec).env
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "pro"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "max"


def test_legacy_env_keeps_xlarge_only_when_it_differs():
    same = provider_spec.from_legacy_env(
        {"ANTHROPIC_DEFAULT_OPUS_MODEL": "pro", "ANTHROPIC_DEFAULT_FABLE_MODEL": "pro"}
    )
    assert same.models == {"large": "pro"}
    apart = provider_spec.from_legacy_env(
        {"ANTHROPIC_DEFAULT_OPUS_MODEL": "pro", "ANTHROPIC_DEFAULT_FABLE_MODEL": "max"}
    )
    assert apart.models == {"large": "pro", "xlarge": "max"}
    assert translators.claude(apart).env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "max"


def test_profile_layer_moving_large_moves_fable_unless_xlarge_is_pinned(home):
    _provider("ds", DEEPSEEK)
    p = _profile_on("work", "ds")
    store.set_profile_field(p.name, "models", {"large": "bigger"})
    env = providers.claude_env(p)
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "bigger[1m]"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "bigger[1m]"
    store.set_profile_field(p.name, "models", {"large": "bigger", "xlarge": "biggest"})
    env = providers.claude_env(p)
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "bigger[1m]"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "biggest[1m]"
    # The provider pins xlarge: a profile moving only large leaves Fable alone.
    store.set_profile_field(p.name, "models", {"large": "bigger"})
    doc_models = dict(DEEPSEEK["models"], xlarge="provider-max")
    _provider("ds", dict(DEEPSEEK, models=doc_models))
    env = providers.claude_env(p)
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "bigger[1m]"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "provider-max[1m]"
