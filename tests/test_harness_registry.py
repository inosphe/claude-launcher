"""The declared harness set: packaged defaults, config overrides, availability."""

from __future__ import annotations

import sys
from importlib import resources

import pytest

from claude_launcher import config, harnesses, store
from claude_launcher.harnesses import HarnessConfigError


def test_packaged_set_declares_supported_harnesses(home):
    """A fresh install knows the supported harness and auth declarations."""
    reg = harnesses.registry()
    assert {"claude", "codex", "pi", "kimi", "agent"} <= set(reg)
    assert reg["codex"].command == ["codex"]
    assert reg["codex"].restore_args == ["resume", "--last"]
    assert reg["pi"].command == ["pi"]
    assert reg["kimi"].auth == "oauth"
    # The routing key usage.py:742 dispatches on and cli.py:971 labels.
    # Two tests in test_usage_harnesses already go red when the yaml
    # line is dropped, but both fail inside the routing they exercise,
    # reporting "usage reporting is not available for harness 'kimi'"
    # -- the symptom, several call frames away from the declaration.
    # This one names the field and the value it lost.
    assert reg["kimi"].usage == "kimi-web-server"
    assert reg["agent"].home_env == "CURSOR_CONFIG_DIR"
    assert reg["pi"].auth == "api-key"
    assert reg["pi"].token_env == "ANTHROPIC_API_KEY"
    assert reg["pi"].borrowable is True
    assert reg["pi"].borrow_mode == "token"
    assert reg["claude"].token_env == "ANTHROPIC_AUTH_TOKEN"
    assert reg["claude"].borrow_mode == "provider-token"
    assert reg["claude"].empty_env == ["ANTHROPIC_API_KEY"]
    assert "OPENAI_API_KEY" in reg["codex"].clear_env
    assert reg["codex"].borrowable is False
    # Claude leads displays; it is the default and the only builtin one.
    assert harnesses.names()[0] == "claude"


def test_packaged_runtime_capabilities_are_harness_native(home):
    reg = harnesses.registry()
    assert reg["claude"].skip_permissions_args == [
        "--dangerously-skip-permissions"
    ]
    assert reg["codex"].skip_permissions_args == [
        "--approval-mode", "full-auto"
    ]
    assert reg["codex"].full_access_args == [
        "--sandbox", "danger-full-access"
    ]
    assert reg["codex"].full_access_off_args == ["--sandbox", "workspace-write"]
    assert reg["codex"].mode_conflict_args == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]


def test_claude_is_builtin_and_its_command_is_not_declared_here(home):
    """claude's executable is CLAUDE_LAUNCHER_BIN and its argv comes from the
    profile, so a 'command:' on it would be a setting that does nothing."""
    claude = harnesses.registry()["claude"]
    assert claude.builtin is True
    assert claude.command == []
    assert claude.program() == config.claude_bin()

    store.update(
        lambda doc: doc.update({"harnesses": {"claude": {"command": "elsewhere"}}})
    )
    overridden = harnesses.registry()["claude"]
    assert overridden.builtin is True
    assert overridden.command == []


def test_config_overrides_a_packaged_harness_whole(home):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"codex": {"command": ["codex", "--sandbox"], "env": {"K": "V"}}}}
        )
    )
    codex = harnesses.registry()["codex"]
    assert codex.command == ["codex", "--sandbox"]
    assert codex.env == {"K": "V"}
    assert codex.description == ""  # replaced whole, never half-merged


def test_config_adds_a_harness_and_a_tombstone_drops_one(home):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"mine": {"command": "mine"}, "pi": None}}
        )
    )
    reg = harnesses.registry()
    assert "mine" in reg
    assert "pi" not in reg


def test_command_defaults_to_the_harness_name(home):
    store.update(lambda doc: doc.update({"harnesses": {"solo": {}}}))
    assert harnesses.registry()["solo"].command == ["solo"]


def test_availability_follows_the_program_not_the_declaration(home):
    """Declared and installed are different questions — 'pi' ships declared
    and (usually) not installed, and the picker has to say which."""
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"real": {"command": sys.executable}, "fake": {"command": "no-such-program-xyz"}}}
        )
    )
    reg = harnesses.registry()
    assert reg["real"].available() is True
    assert reg["fake"].available() is False
    assert reg["fake"].to_dict()["available"] is False


def test_a_broken_entry_does_not_take_the_whole_registry_down(home):
    """A hand-edited config that no longer parses must not stop every session
    command from running."""
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"bad": {"command": {"not": "a command"}}, "good": {}}}
        )
    )
    reg = harnesses.registry()
    assert "good" in reg
    assert "bad" not in reg
    assert "claude" in reg  # the packaged set still stands


def test_parse_rejects_a_malformed_document():
    with pytest.raises(HarnessConfigError, match="must be a mapping"):
        harnesses.parse({"harnesses": {"x": "just a string"}})
    with pytest.raises(HarnessConfigError, match="must be a string or a list"):
        harnesses.parse({"harnesses": {"x": {"command": 7}}})
    with pytest.raises(HarnessConfigError, match="has no token_env"):
        harnesses.parse({"harnesses": {"x": {"auth": "api-key"}}})
    with pytest.raises(HarnessConfigError, match="invalid env name"):
        harnesses.parse(
            {"harnesses": {"x": {"clear_env": ["NOT-AN-ENV"]}}}
        )
    with pytest.raises(HarnessConfigError, match="cannot declare token_env"):
        harnesses.parse(
            {"harnesses": {"x": {"auth": "oauth", "token_env": "TOKEN"}}}
        )
    with pytest.raises(HarnessConfigError, match="invalid harness name"):
        harnesses.parse({"harnesses": {"not:selectable": {}}})
    with pytest.raises(HarnessConfigError, match="opening_transport"):
        harnesses.parse(
            {"harnesses": {"x": {"opening_transport": "clipboard"}}}
        )
    with pytest.raises(HarnessConfigError, match="paste_enter_delay"):
        harnesses.parse(
            {"harnesses": {"x": {"paste_enter_delay": -1}}}
        )


def test_brief_api_key_field_is_an_input_only_compatibility_alias(home):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"legacy": {"auth": "api-key", "api_key_env": "TOKEN"}}}
        )
    )
    entry = harnesses.get("legacy")
    assert entry.token_env == "TOKEN"
    assert "api_key_env" not in entry.to_dict()


def test_packaged_document_is_proven_by_the_same_parser():
    """The default is YAML read through the parser every user entry goes
    through, so it cannot drift into a shape the parser would reject."""
    parsed = harnesses.parse(harnesses.DEFAULT_YAML)
    assert set(parsed) == {"claude", "codex", "pi", "kimi", "agent"}
    packaged = resources.files("claude_launcher").joinpath(
        harnesses.DEFAULT_RESOURCE
    )
    assert packaged.is_file()
    assert packaged.read_text(encoding="utf-8") == harnesses.DEFAULT_YAML
