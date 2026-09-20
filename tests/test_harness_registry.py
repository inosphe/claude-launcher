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
    assert reg["pi"].provider_adapter == "pi"
    assert reg["pi"].borrowable is True
    assert reg["pi"].borrow_mode == "token"
    assert reg["claude"].token_env == "ANTHROPIC_AUTH_TOKEN"
    assert reg["claude"].borrow_mode == "provider-token"
    assert reg["claude"].empty_env == ["ANTHROPIC_API_KEY"]
    assert reg["claude"].models == ["haiku", "sonnet", "opus", "fable"]
    assert reg["claude"].btw is not None
    assert reg["claude"].btw.to_dict() == {
        "command": "/btw",
        "aliases": [],
        "minimum_version": "2.1.73",
        "requires_started_conversation": False,
        "available_while_busy": True,
        "context": "current-conversation",
        "history": "ephemeral",
        "tool_access": "none",
        "response_mode": "single-response",
    }
    assert "OPENAI_API_KEY" in reg["codex"].clear_env
    assert reg["codex"].borrowable is False
    assert reg["codex"].models == ["luna", "terra", "sol", "astra"]
    assert reg["codex"].to_dict()["models"] == ["luna", "terra", "sol", "astra"]
    assert reg["codex"].btw is not None
    assert reg["codex"].btw.to_dict() == {
        "command": "/btw",
        "aliases": ["/side"],
        "minimum_version": "0.133.0",
        "requires_started_conversation": True,
        "available_while_busy": True,
        "context": "reference-parent",
        "history": "ephemeral",
        "tool_access": "restricted",
        "response_mode": "conversation",
    }
    assert all(reg[name].btw is None for name in ("pi", "kimi", "agent", "devin"))
    # Claude leads displays; it is the default and the only builtin one.
    assert harnesses.names()[0] == "claude"


def test_devin_declaration_pins_its_measured_launch_contract(home):
    """Devin is declared from measurements, not from its CLI's shape guessed.

    Each assertion here is a fact established against the installed CLI
    (``devin 3000.10.31``) on this machine, so an edit that drops one goes
    red here rather than silently mislaunching a session later.
    """
    devin = harnesses.registry()["devin"]
    assert devin.command == ["devin"]
    assert devin.auth == "oauth"
    # OAuth harnesses keep credentials in their own home and never receive
    # the launcher token; the parser rejects a token_env here.
    assert devin.token_env == ""
    assert devin.borrowable is False
    assert devin.login_args == ["auth", "login"]
    # `devin ... -p <prompt>` runs once and exits; with the trust waiver
    # below this composes to the argv that was run against a live account.
    assert devin.heartbeat_args == ["-p"]
    assert devin.args == ["--respect-workspace-trust", "false"]
    # `-c/--continue` reopens the most recent conversation in the working
    # directory; `-r/--resume` with no id opens an interactive picker.
    assert devin.restore_args == ["--continue"]
    assert devin.skip_permissions_args == ["--permission-mode", "dangerous"]
    # No environment variable relocates devin's home (measured: XDG_CONFIG_HOME
    # and APPDATA are both ignored on win32, and --config moves only
    # config.json). Declaring a home_env nothing honours would be a setting
    # that silently does nothing -- install.py targets the real home instead.
    assert devin.home_env == ""
    # Devin's prompt is only a positional after `--`, so an appended argv
    # prompt would be read as a PATH. The opening is typed in instead.
    assert devin.opening_transport == "pty"
    assert devin.input_readiness == "bracketed-paste"


@pytest.mark.parametrize("aliases", [[], None, {"astra": ""}, {"astra": 6}, {1: "model"}])
def test_model_aliases_reject_invalid_mappings(home, aliases):
    with pytest.raises(HarnessConfigError, match="model_aliases must map"):
        harnesses.parse({"harnesses": {"x": {"command": "x", "model_aliases": aliases}}})


def test_model_aliases_are_serialized(home):
    entry = harnesses.registry()["codex"]
    assert entry.to_dict()["model_aliases"]["astra"] == "gpt-6-astra"


def test_provider_adapter_is_validated_with_its_auth_contract(home):
    with pytest.raises(HarnessConfigError, match="provider_adapter must be pi"):
        harnesses.parse(
            {"harnesses": {"x": {"command": "x", "provider_adapter": "other"}}}
        )
    with pytest.raises(HarnessConfigError, match="requires api-key auth"):
        harnesses.parse(
            {"harnesses": {"x": {"command": "x", "provider_adapter": "pi"}}}
        )


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


@pytest.mark.parametrize(
    ("btw", "message"),
    [
        (True, "btw must be a mapping"),
        ({"command": "btw"}, "btw command must be a slash command"),
        (
            {
                "command": "/btw",
                "aliases": ["/btw"],
                "minimum_version": "1.0.0",
                "requires_started_conversation": False,
                "available_while_busy": True,
                "context": "current-conversation",
                "history": "ephemeral",
                "tool_access": "none",
                "response_mode": "single-response",
            },
            "aliases must be unique",
        ),
        (
            {
                "command": "/btw",
                "aliases": [],
                "minimum_version": "latest",
                "requires_started_conversation": False,
                "available_while_busy": True,
                "context": "current-conversation",
                "history": "ephemeral",
                "tool_access": "none",
                "response_mode": "single-response",
            },
            "minimum_version must be a version string",
        ),
        (
            {
                "command": "/btw",
                "aliases": [],
                "minimum_version": "1.0.0",
                "requires_started_conversation": "false",
                "available_while_busy": True,
                "context": "current-conversation",
                "history": "ephemeral",
                "tool_access": "none",
                "response_mode": "single-response",
            },
            "requires_started_conversation must be true or false",
        ),
        (
            {
                "command": "/btw",
                "aliases": [],
                "minimum_version": "1.0.0",
                "requires_started_conversation": False,
                "available_while_busy": True,
                "context": "unknown",
                "history": "ephemeral",
                "tool_access": "none",
                "response_mode": "single-response",
            },
            "btw context must be one of",
        ),
    ],
)
def test_btw_capability_rejects_an_incomplete_or_invalid_contract(btw, message):
    with pytest.raises(HarnessConfigError, match=message):
        harnesses.parse({"harnesses": {"x": {"btw": btw}}})


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
    assert set(parsed) == {"claude", "codex", "pi", "kimi", "agent", "devin"}
    packaged = resources.files("claude_launcher").joinpath(
        harnesses.DEFAULT_RESOURCE
    )
    assert packaged.is_file()
    assert packaged.read_text(encoding="utf-8") == harnesses.DEFAULT_YAML
