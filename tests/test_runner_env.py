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


@pytest.fixture(autouse=True)
def _scrub_ambient_auth(monkeypatch):
    """Make the "not in env" assertions immune to the harness's own token.

    A managed claude session runs with ``ANTHROPIC_AUTH_TOKEN`` set for the
    harness, and :func:`runner.child_env` copies ``os.environ`` — so running
    this module inside such a session leaks the ambient token into the very
    dict the default-provider tests assert it is absent from, and they fail
    wherever the gate happens to run. The profile/provider env under test is
    built fresh by each test; the ambient token is noise, so this module
    scrubs it. Scoped here (a fixture defined in this test file), not in the
    global conftest, because other tests may deliberately examine ambient env.
    """
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


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


def test_profile_env_base_url_alone_does_not_trigger(home, monkeypatch):
    # Only a provider override switches auth handling; a base URL in the
    # profile's env (with the default provider) keeps normal OAuth injection.
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
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


def test_null_token_clears_oauth_everywhere(home, monkeypatch):
    # `run --null`: no injection from the store, and even a token pinned by
    # the profile's own env or left in the shell is cleared.
    p = profile.create("work")
    credentials.save_token(p, "sk-ant-oat01-X")
    settings.set_env(p, {"CLAUDE_CODE_OAUTH_TOKEN": "pinned"})
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-shell-token")
    env = runner.child_env(p, with_token=True, null_token=True)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


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


def test_profile_env_overrides_provider_env(home):
    # The requested precedence: a profile's own ``env`` beats the provider's
    # for a shared key — provider env is a low-priority backend default, and
    # child_env applies it before the profile env. Keys only the provider sets
    # still reach the child.
    p = profile.create("work")
    settings.set_env(p, {"ANTHROPIC_MODEL": "neural-chat"})
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "backend": {
                        "env": {
                            "ANTHROPIC_MODEL": "glm-5p2",
                            "ANTHROPIC_BASE_URL": "https://backend.example/inference",
                        }
                    }
                }
            }
        )
    )
    store.set_profile_field("work", "provider", "backend")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_MODEL"] == "neural-chat"  # profile wins
    assert env["ANTHROPIC_BASE_URL"] == "https://backend.example/inference"


def test_env_precedence_shell_under_provider_under_profile(home, monkeypatch):
    # shell < provider < profile: the provider covers the shell, and the
    # profile covers the provider for the same key.
    p = profile.create("work")
    monkeypatch.setenv("ANTHROPIC_MODEL", "shell-stale")
    settings.set_env(p, {"ANTHROPIC_MODEL": "profile-model"})
    store.update(
        lambda doc: doc.update(
            {"providers": {"backend": {"env": {"ANTHROPIC_MODEL": "provider-model"}}}}
        )
    )
    store.set_profile_field("work", "provider", "backend")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_MODEL"] == "profile-model"


def test_profile_without_key_uses_provider_value(home):
    # No profile pin for a key -> the provider's default shows through.
    p = profile.create("work")
    store.update(
        lambda doc: doc.update(
            {"providers": {"backend": {"env": {"ANTHROPIC_MODEL": "provider-model"}}}}
        )
    )
    store.set_profile_field("work", "provider", "backend")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_MODEL"] == "provider-model"


def test_borrow_keeps_running_profile_env_over_lender_provider(home):
    # Borrow swaps the auth backend but not the running profile's env: the
    # runner's own keys still beat the lender's provider defaults.
    runner_p = profile.create("work")
    settings.set_env(runner_p, {"ANTHROPIC_MODEL": "runner-model"})
    lender = profile.create("lender")
    store.update(
        lambda doc: doc.update(
            {"providers": {"backend": {"env": {"ANTHROPIC_MODEL": "lender-model"}}}}
        )
    )
    store.set_profile_field("lender", "provider", "backend")
    env = runner.child_env(runner_p, with_token=True, borrow=lender)
    assert env["ANTHROPIC_MODEL"] == "runner-model"


def test_borrow_lends_profile_env_above_provider_below_runner(home):
    # Borrow carries the lender's provider *and* its env: the lender's env is a
    # fill layer above the provider's defaults, so a key the runner never sets
    # shows through the way the lender's own backend would use it natively —
    # while the runner's env still wins for any key it does set, and a key
    # neither profile sets falls through to the provider.
    runner_p = profile.create("work")
    settings.set_env(runner_p, {"ANTHROPIC_MODEL": "runner-model"})
    lender = profile.create("glmprof")
    settings.set_env(
        lender,
        {
            "ANTHROPIC_MODEL": "lender-model",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "lender-opus",
        },
    )
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "backend": {
                        "env": {
                            "ANTHROPIC_MODEL": "provider-model",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL": "provider-opus",
                            "ANTHROPIC_DEFAULT_SONNET_MODEL": "provider-sonnet",
                        }
                    }
                }
            }
        )
    )
    store.set_profile_field("glmprof", "provider", "backend")
    env = runner.child_env(runner_p, with_token=True, borrow=lender)
    assert env["ANTHROPIC_MODEL"] == "runner-model"  # runner's own key still wins
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "lender-opus"  # lender fills the gap
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "provider-sonnet"  # provider falls through
