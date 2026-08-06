"""child_env auth assembly — especially provider-overridden backends, where
the stored ``set-token`` secret must override any plaintext
``ANTHROPIC_AUTH_TOKEN`` in the config file."""

from __future__ import annotations

import pytest

from claude_launcher import credentials, lineage, profile, providers, runner, settings, store

BASE = "https://api.example.com/inference"


def _provider_glm(doc: dict) -> None:
    doc["providers"] = {
        "glm": {
            "env": {
                "ANTHROPIC_BASE_URL": BASE,
                "ANTHROPIC_AUTH_TOKEN": "plaintext-key",
                "CLAUDE_CODE_OAUTH_TOKEN": "",
            }
        }
    }


def test_default_provider_still_injects_oauth(home):
    p = profile.create("work")
    credentials.save_token(p, "sk-ant-oat01-abc")
    env = runner.child_env(p, with_token=True)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_provider_selection_prefers_stored_token(home):
    p = profile.create("work")
    store.update(_provider_glm)
    store.set_profile_field("work", "provider", "glm")
    credentials.save_token(p, "backend-secret")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_BASE_URL"] == BASE
    assert env["ANTHROPIC_AUTH_TOKEN"] == "backend-secret"  # overrides plaintext
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""  # yaml's explicit pin kept


def test_provider_without_stored_token_keeps_yaml_value(home):
    profile.create("work")
    store.update(_provider_glm)
    store.set_profile_field("work", "provider", "glm")
    env = runner.child_env(profile.require("work"), with_token=True)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "plaintext-key"  # backwards compatible


def test_provider_without_base_url_still_uses_stored_token(home):
    # The trigger is the provider *selection*, not any particular env key.
    p = profile.create("work")
    store.update(
        lambda doc: doc.update(
            {"providers": {"alt": {"env": {"ANTHROPIC_MODEL": "some-model"}}}}
        )
    )
    store.set_profile_field("work", "provider", "alt")
    credentials.save_token(p, "backend-secret")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "backend-secret"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_profile_env_base_url_alone_does_not_trigger(home):
    # Only a provider override switches auth handling; a base URL in the
    # profile's env (with the default provider) keeps normal OAuth injection.
    p = profile.create("work")
    settings.set_env(p, {"ANTHROPIC_BASE_URL": BASE})
    credentials.save_token(p, "sk-ant-oat01-abc")
    env = runner.child_env(p, with_token=True)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_provider_override_param(home):
    # `run --provider NAME` reaches child_env as provider_override and wins
    # over the config file's (absent) selection.
    p = profile.create("work")
    store.update(_provider_glm)
    credentials.save_token(p, "backend-secret")
    env = runner.child_env(p, with_token=True, provider_override="glm")
    assert env["ANTHROPIC_BASE_URL"] == BASE
    assert env["ANTHROPIC_AUTH_TOKEN"] == "backend-secret"


def test_provider_override_beats_profile_selection(home):
    p = profile.create("work")
    store.update(_provider_glm)
    store.set_profile_field("work", "provider", "glm")
    credentials.save_token(p, "sk-ant-oat01-abc")
    env = runner.child_env(p, with_token=True, provider_override="default")
    assert "ANTHROPIC_BASE_URL" not in env or env["ANTHROPIC_BASE_URL"] != BASE
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"


def test_unknown_provider_override_raises(home):
    p = profile.create("work")
    with pytest.raises(providers.ProviderError):
        runner.child_env(p, with_token=True, provider_override="nope")


def test_shell_oauth_leftover_dropped_on_provider(home, monkeypatch):
    p = profile.create("work")
    store.update(
        lambda doc: doc.update(
            {"providers": {"alt": {"env": {"ANTHROPIC_BASE_URL": BASE}}}}
        )
    )
    store.set_profile_field("work", "provider", "alt")
    credentials.save_token(p, "backend-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-shell-token")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "backend-secret"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # not pinned by yaml -> dropped


def test_child_inherits_provider_and_parent_stored_token(home):
    parent = profile.create("base")
    credentials.save_token(parent, "backend-secret")
    store.update(_provider_glm)
    store.set_profile_field("base", "provider", "glm")
    child = profile.create("kid")
    lineage.set_parent(child, "base")
    env = runner.child_env(child, with_token=True)
    assert env["ANTHROPIC_BASE_URL"] == BASE
    assert env["ANTHROPIC_AUTH_TOKEN"] == "backend-secret"


def test_borrow_uses_lenders_stored_token_and_backend(home):
    runner_p = profile.create("work")
    lender = profile.create("glmprof")
    store.update(_provider_glm)
    store.set_profile_field("glmprof", "provider", "glm")
    credentials.save_token(lender, "lender-secret")
    env = runner.child_env(runner_p, with_token=True, borrow=lender)
    assert env["ANTHROPIC_BASE_URL"] == BASE
    assert env["ANTHROPIC_AUTH_TOKEN"] == "lender-secret"


def test_resolve_with_source(home):
    parent = profile.create("base")
    child = profile.create("kid")
    lineage.set_parent(child, "base")
    store.update(_provider_glm)

    assert providers.resolve_with_source(child) == ("default", "built-in default")
    providers.set_active("glm")
    assert providers.resolve_with_source(child) == ("glm", "global default")
    store.set_profile_field("base", "provider", "glm")
    assert providers.resolve_with_source(child) == (
        "glm",
        "inherited from profile 'base'",
    )
    store.set_profile_field("kid", "provider", "glm")
    assert providers.resolve_with_source(child) == ("glm", "set on profile 'kid'")
