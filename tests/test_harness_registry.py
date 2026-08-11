"""The declared harness set: packaged defaults, config overrides, availability."""

from __future__ import annotations

import sys

import pytest

from claude_launcher import config, harnesses, store
from claude_launcher.harnesses import HarnessConfigError


def test_packaged_set_declares_claude_codex_and_pi(home):
    """A fresh install knows more than one harness — that is what lets the web
    UI offer a picker instead of taking the name as free text."""
    reg = harnesses.registry()
    assert {"claude", "codex", "pi"} <= set(reg)
    assert reg["codex"].command == ["codex"]
    assert reg["pi"].command == ["pi"]
    # claude leads the pickers; it is the default and the only profile-managed one
    assert harnesses.names()[0] == "claude"


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


def test_packaged_document_is_proven_by_the_same_parser():
    """The default is YAML read through the parser every user entry goes
    through, so it cannot drift into a shape the parser would reject."""
    parsed = harnesses.parse(harnesses.DEFAULT_YAML)
    assert set(parsed) == {"claude", "codex", "pi"}
