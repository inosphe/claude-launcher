"""child_env auth assembly — especially provider-overridden backends, where
the stored ``set-token`` secret must override any plaintext
``ANTHROPIC_AUTH_TOKEN`` in the config file."""

from __future__ import annotations

import pytest

from claude_launcher import (
    credentials,
    harnesses,
    lineage,
    profile,
    providers,
    runner,
    settings,
    store,
)

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


def test_claude_always_blanks_anthropic_api_key(home):
    p = profile.create("console-api")
    settings.set_env(p, {"ANTHROPIC_API_KEY": "console-key"})

    env = runner.child_env(p, with_token=True)

    assert env["ANTHROPIC_API_KEY"] == ""
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_provider_selection_prefers_stored_token(home):
    p = profile.create("work")
    store.update(_provider_glm)
    store.set_profile_field("work", "provider", "glm")
    credentials.save_token(p, "backend-secret")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_BASE_URL"] == BASE
    assert env["ANTHROPIC_AUTH_TOKEN"] == "backend-secret"  # overrides plaintext
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""  # yaml's explicit pin kept


def test_provider_projects_the_single_profile_token(home):
    p = profile.create("kimi-claude")
    store.update(_provider_glm)
    store.set_profile_field(p.name, "provider", "glm")
    credentials.save_token(p, "kimi-api-key")

    env = runner.child_env(p, with_token=True)

    assert env["ANTHROPIC_AUTH_TOKEN"] == "kimi-api-key"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert credentials.stored_token(p) == "kimi-api-key"


def test_provider_without_stored_token_keeps_yaml_value(home):
    profile.create("work")
    store.update(_provider_glm)
    store.set_profile_field("work", "provider", "glm")
    env = runner.child_env(profile.require("work"), with_token=True)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "plaintext-key"  # backwards compatible
    assert env["ANTHROPIC_API_KEY"] == ""


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


def test_stale_backend_env_never_leaks_from_base(home, monkeypatch):
    # The daemon is usually started from inside a claude session that itself
    # ran on a borrowed backend, so its env carries that backend's keys. A
    # later session whose profile/provider sets none of them must not inherit
    # the stale backend — the config file is the only source for these keys.
    p = profile.create("work")
    credentials.save_token(p, "sk-ant-oat01-abc")
    stale = {
        "ANTHROPIC_BASE_URL": "https://stale.example/coding/",
        "ANTHROPIC_MODEL": "stale-model",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "stale-model",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "stale-model",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "stale-model",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "stale-model",
        "ANTHROPIC_SMALL_FAST_MODEL": "stale-model",
        "ANTHROPIC_API_KEY": "stale-key",
        "ANTHROPIC_AUTH_TOKEN": "stale-token",
        "CLAUDE_CODE_SUBAGENT_MODEL": "stale-model",
    }
    for key, value in stale.items():
        monkeypatch.setenv(key, value)
    env = runner.child_env(p, with_token=True)
    for key in stale:
        if key == "ANTHROPIC_API_KEY":
            # The packaged Claude harness deliberately keeps this variable
            # present but empty so Claude Code cannot send X-Api-Key auth.
            assert env[key] == ""
        else:
            assert key not in env, f"{key} leaked from the base env"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"


def test_provider_defined_key_does_not_leak_to_other_profiles(home, monkeypatch):
    # A key defined by *any* provider is config-managed: switching to a
    # profile that uses neither that provider nor the key must clear it, even
    # when it lingers in the ambient (daemon-inherited) environment.
    p = profile.create("work")
    credentials.save_token(p, "sk-ant-oat01-abc")
    store.update(
        lambda doc: doc.update(
            {"providers": {"alt": {"env": {"MY_CUSTOM_BACKEND_FLAG": "1"}}}}
        )
    )
    monkeypatch.setenv("MY_CUSTOM_BACKEND_FLAG", "1")
    env = runner.child_env(p, with_token=True)
    assert "MY_CUSTOM_BACKEND_FLAG" not in env


def test_reborrow_to_profile_without_backend_keys_clears_lender_env(home, monkeypatch):
    # The reported bug: reborrow kimi -> nc kept k3. The lender's backend keys
    # (in the daemon's env from a previous spawn context) must not survive
    # into a borrow-less profile that pins none of them.
    runner_p = profile.create("work")
    lender = profile.create("lender")
    credentials.save_token(runner_p, "sk-ant-oat01-abc")
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "backend": {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://lender.example/",
                            "ANTHROPIC_MODEL": "lender-model",
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "lender-model",
                        }
                    }
                }
            }
        )
    )
    store.set_profile_field("lender", "provider", "backend")
    borrowed = runner.child_env(runner_p, with_token=True, borrow=lender)
    assert borrowed["ANTHROPIC_MODEL"] == "lender-model"
    # The stale base simulates the daemon env after hosting borrowed children.
    monkeypatch.setenv("ANTHROPIC_MODEL", "lender-model")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "lender-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://lender.example/")
    unborrowed = runner.child_env(runner_p, with_token=True)
    assert "ANTHROPIC_MODEL" not in unborrowed
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in unborrowed
    assert "ANTHROPIC_BASE_URL" not in unborrowed


def test_borrow_keeps_running_profile_env_over_lender_provider(home):
    # Borrow swaps the auth backend but not the running profile's env: the
    # runner's own keys still beat the lender's provider defaults. Asserted on
    # a NON-backend key, because a backend key is exactly what a borrow across
    # providers takes away (see the model-pin tests below): the model ids are
    # the lender's business once the endpoint is, everything else is not.
    runner_p = profile.create("work")
    settings.set_env(runner_p, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"})
    lender = profile.create("lender")
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "backend": {
                        "env": {
                            "ANTHROPIC_MODEL": "lender-model",
                            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "900000",
                        }
                    }
                }
            }
        )
    )
    store.set_profile_field("lender", "provider", "backend")
    env = runner.child_env(runner_p, with_token=True, borrow=lender)
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"
    assert env["ANTHROPIC_MODEL"] == "lender-model"


def test_borrow_lends_profile_env_above_provider_below_runner(home):
    # Borrow carries the lender's provider *and* its env: the lender's env is a
    # fill layer above the provider's defaults, so a key the runner never sets
    # shows through the way the lender's own backend would use it natively —
    # while the runner's env still wins for any key it does set, and a key
    # neither profile sets falls through to the provider.
    runner_p = profile.create("work")
    settings.set_env(runner_p, {"ANTHROPIC_MODEL": "runner-model"})
    lender = profile.create("glmprof")
    # Same provider on both sides, so the runner's pins still describe the
    # backend in play and keep their final say. Across providers they do not:
    # test_borrow_across_providers_drops_running_profile_model_pins.
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
    store.set_profile_field("work", "provider", "backend")
    store.set_profile_field("glmprof", "provider", "backend")
    env = runner.child_env(runner_p, with_token=True, borrow=lender)
    assert env["ANTHROPIC_MODEL"] == "runner-model"  # runner's own key still wins
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "lender-opus"  # lender fills the gap
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "provider-sonnet"  # provider falls through


def test_borrow_across_providers_drops_running_profile_model_pins(home):
    """The reported bug: `ds4:claude` borrowing a plain-Anthropic profile.

    ds4 pins Fireworks model ids in its own profile env and resolves to the
    Fireworks provider. Borrowing a default-provider lender swings the
    endpoint to api.anthropic.com -- and under the old layering the Fireworks
    model ids came along, so claude asked Anthropic for a model id only
    Fireworks has ("There's an issue with the selected model ... It may not
    exist or you may not have access to it"). The pins describe a backend this
    run is not talking to, so they do not get the final say.
    """
    fireworks_models = {
        "ANTHROPIC_MODEL": "accounts/fireworks/models/deepseek-v4-flash-0731",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "accounts/fireworks/models/deepseek-v4-flash-0731",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "accounts/fireworks/models/deepseek-v4-flash-0731",
    }
    runner_p = profile.create("ds4")
    settings.set_env(
        runner_p, {**fireworks_models, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000"}
    )
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "fireworks": {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://api.fireworks.ai/inference",
                            "ANTHROPIC_AUTH_TOKEN": "fw-key",
                            "CLAUDE_CODE_OAUTH_TOKEN": "",
                            **fireworks_models,
                        }
                    }
                }
            }
        )
    )
    store.set_profile_field("ds4", "provider", "fireworks")
    lender = profile.create("sr")
    credentials.save_token(lender, "sk-ant-oat01-lender")

    env = runner.child_env(runner_p, with_token=True, borrow=lender)

    # Anthropic's endpoint, Anthropic's auth -- and no Fireworks model id left.
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-lender"
    assert "ANTHROPIC_BASE_URL" not in env
    for key in fireworks_models:
        assert key not in env, f"{key} names a backend this run does not call"
    # Only the backend keys go. Everything else the profile pins is still its
    # own business, borrow or not -- config dir included.
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "600000"
    assert env["CLAUDE_CONFIG_DIR"] == str(runner_p.config_dir)


def test_borrow_within_one_provider_keeps_model_pins(home):
    # The carve-out: when the lender resolves to the *same* provider, the
    # runner's pins still describe the backend being talked to, so they keep
    # their final say exactly as before. This is the case that must not
    # regress when the cross-provider one is fixed.
    store.update(
        lambda doc: doc.update(
            {"providers": {"backend": {"env": {"ANTHROPIC_MODEL": "provider-model"}}}}
        )
    )
    runner_p = profile.create("work")
    settings.set_env(runner_p, {"ANTHROPIC_MODEL": "runner-model"})
    store.set_profile_field("work", "provider", "backend")
    lender = profile.create("lender")
    store.set_profile_field("lender", "provider", "backend")

    env = runner.child_env(runner_p, with_token=True, borrow=lender)

    assert env["ANTHROPIC_MODEL"] == "runner-model"


def test_provider_override_drops_running_profile_model_pins(home):
    # The same mismatch without a borrow: `run work --provider other` swings
    # the endpoint while the profile's pins still name its own backend.
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "mine": {"env": {"ANTHROPIC_BASE_URL": "https://mine.example/"}},
                    "other": {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://other.example/",
                            "ANTHROPIC_MODEL": "other-model",
                        }
                    },
                }
            }
        )
    )
    p = profile.create("work")
    settings.set_env(p, {"ANTHROPIC_MODEL": "mine-model"})
    store.set_profile_field("work", "provider", "mine")

    env = runner.child_env(p, with_token=True, provider_override="other")

    assert env["ANTHROPIC_BASE_URL"] == "https://other.example/"
    assert env["ANTHROPIC_MODEL"] == "other-model"


def test_borrow_across_providers_leaves_a_pinless_profile_alone(home):
    # A profile that pins nothing has nothing to lose: the borrowed backend is
    # the only source of model ids either way. Guards against the fix reaching
    # further than the keys the profile actually set.
    store.update(
        lambda doc: doc.update(
            {
                "providers": {
                    "backend": {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://lender.example/",
                            "ANTHROPIC_MODEL": "lender-model",
                        }
                    }
                }
            }
        )
    )
    runner_p = profile.create("work")
    lender = profile.create("lender")
    store.set_profile_field("lender", "provider", "backend")

    env = runner.child_env(runner_p, with_token=True, borrow=lender)

    assert env["ANTHROPIC_MODEL"] == "lender-model"
    assert env["ANTHROPIC_BASE_URL"] == "https://lender.example/"


def test_pi_gets_only_its_projected_profile_token(home, monkeypatch):
    p = profile.create("pi-work")
    lineage.set_harness(p, "pi")
    settings.set_env(
        p,
        {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "90",
            "ANTHROPIC_BASE_URL": "https://claude-only.example",
            "KEEP_ME": "yes",
        },
    )
    credentials.save_token(p, "pi-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai")

    env = runner.harness_child_env(p, harnesses.get("pi"), base_env=dict())

    assert env["ANTHROPIC_API_KEY"] == "pi-secret"
    assert env["KEEP_ME"] == "yes"
    assert "ANTHROPIC_BASE_URL" not in env
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["PI_CODING_AGENT_DIR"] == str(p.config_dir / "pi")


def test_pi_can_borrow_a_base_profiles_token_without_borrowing_its_env(home):
    runtime = profile.create("pi-work")
    lender = profile.create("ds4")
    lineage.set_harness(runtime, "pi")
    credentials.save_token(runtime, "own-secret")
    credentials.save_token(lender, "lender-secret")
    settings.set_env(runtime, {"RUNTIME_SETTING": "kept"})
    settings.set_env(lender, {"LENDER_SETTING": "must-not-cross"})

    env = runner.harness_child_env(
        runtime, harnesses.get("pi"), base_env={}, borrow=lender
    )

    assert env["ANTHROPIC_API_KEY"] == "lender-secret"
    assert env["RUNTIME_SETTING"] == "kept"
    assert "LENDER_SETTING" not in env
    assert env["PI_CODING_AGENT_DIR"] == str(runtime.config_dir / "pi")


@pytest.mark.parametrize(
    "name,key_name,home_name",
    [
        ("codex", "OPENAI_API_KEY", "CODEX_HOME"),
        ("kimi", "KIMI_API_KEY", "KIMI_CODE_HOME"),
        ("agent", "CURSOR_API_KEY", "CURSOR_CONFIG_DIR"),
    ],
)
def test_oauth_harnesses_ignore_api_keys_and_use_namespaced_storage(
    home, name, key_name, home_name
):
    p = profile.create(name)
    lineage.set_harness(p, name)
    credentials.save_token(p, "shared-but-ignored-by-oauth")
    settings.set_env(
        p,
        {
            key_name: "stale-profile-key",
            "ANTHROPIC_MODEL": "claude-only-model",
            "PLAIN_SETTING": "kept",
        },
    )

    env = runner.harness_child_env(p, harnesses.get(name), base_env={key_name: "shell"})

    assert key_name not in env
    assert "ANTHROPIC_MODEL" not in env
    assert env["PLAIN_SETTING"] == "kept"
    assert env[home_name] == str(p.config_dir / name)
