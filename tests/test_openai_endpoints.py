"""All profile-based Chat Completions consumers share one URL contract."""

import logging

import pytest

from claude_launcher import harnesses, pi_provider, profile, provider_spec, routing, runner, store, translators
from claude_launcher.daemon import api, briefing, observer


@pytest.mark.parametrize(
    "endpoint,base",
    [
        ("https://api.fireworks.ai/inference", "https://api.fireworks.ai/inference/v1"),
        ("https://api.fireworks.ai/inference/v1", "https://api.fireworks.ai/inference/v1"),
        ("https://api.fireworks.ai/inference/chat/completions", "https://api.fireworks.ai/inference/v1"),
        ("https://api.fireworks.ai/inference/v1/chat/completions/", "https://api.fireworks.ai/inference/v1"),
        ("https://api.deepseek.com", "https://api.deepseek.com"),
        ("https://example.test/api/v2/", "https://example.test/api/v2"),
        ("https://example.test/api/v2/chat/completions", "https://example.test/api/v2"),
        ("https://example.test/inference", "https://example.test/inference"),
        ("https://api.fireworks.ai/custom", "https://api.fireworks.ai/custom"),
        ("https://api.fireworks.ai.example/inference", "https://api.fireworks.ai.example/inference"),
        ("http://api.fireworks.ai/inference", "http://api.fireworks.ai/inference"),
    ],
)
def test_pi_briefing_and_observer_use_the_same_url(home, endpoint, base):
    prof = profile.create("shared")
    store.save({
        "providers": {"backend": {
            "api_key": "test-key",
            "endpoints": {"openai": endpoint, "anthropic": "https://example.test/anthropic"},
            "models": {"default": "test-model"},
        }},
        "profiles": {prof.name: {"provider": "backend"}},
        "llm": {"profile": prof.name},
        "observer": {"profile": prof.name, "model": "test-model"},
    })
    projected = pi_provider.resolve(prof, harnesses.get("pi"))
    assert projected.base_url == base
    assert briefing.llm_config()["endpoint"] == base + "/chat/completions"
    assert observer.configuration()["endpoint"] == base + "/chat/completions"
    spec = provider_spec.from_entry(store.load()["providers"]["backend"], "backend")
    assert translators.claude(spec).env["ANTHROPIC_BASE_URL"] == "https://example.test/anthropic"


@pytest.mark.parametrize("suffix", ["?deployment=secret", "#secret", "?deployment=secret#secret"])
@pytest.mark.parametrize("metering", [False, True])
def test_query_and_fragment_are_rejected_before_direct_or_shim_requests(
    home, monkeypatch, caplog, suffix, metering
):
    prof = profile.create("invalid")
    endpoint = "https://api.fireworks.ai/inference/chat/completions" + suffix
    store.save({
        "providers": {"backend": {
            "api_key": "test-key",
            "endpoints": {"openai": endpoint},
            "models": {"default": "test-model"},
        }},
        "profiles": {prof.name: {"provider": "backend"}},
        "llm": {"profile": prof.name},
        "observer": {"profile": prof.name, "model": "test-model"},
    })
    monkeypatch.setenv("CLAUNCH_METERING", "1" if metering else "0")

    def unexpected_transport(*args, **kwargs):
        pytest.fail("invalid endpoint reached the SDK/shim/HTTP transport")

    monkeypatch.setattr(routing, "front", unexpected_transport)
    monkeypatch.setattr(briefing, "call_llm", unexpected_transport)
    monkeypatch.setattr(observer, "complete", unexpected_transport)
    message = "without query parameters or a fragment"
    with pytest.raises(runner.RunnerError, match=message):
        runner.harness_child_env(prof, harnesses.get("pi"), base_env={})
    with pytest.raises(briefing.BriefingError, match=message):
        briefing.profile_backend(prof.name)
    with pytest.raises(ValueError, match=message):
        observer.configuration()
    cfg = briefing.llm_config()
    assert not briefing.llm_configured(cfg)
    settings = api._llm_settings_payload()
    assert not settings["configured"] and message in settings["error"]
    assert "secret" not in settings["error"]
    assert "secret" not in caplog.text


def test_empty_endpoint_stays_unconfigured():
    urls = provider_spec.openai_urls("  ")
    assert urls.base_url == urls.chat_completions_url == ""


def test_fireworks_compatibility_warning_is_bounded(caplog):
    provider_spec.openai_urls.cache_clear()
    endpoint = "https://api.fireworks.ai/inference"
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert provider_spec.openai_urls(endpoint).base_url == "https://api.fireworks.ai/inference/v1"
    assert len(caplog.records) == 1
    assert "endpoints.openai" in caplog.text
    assert "/inference/v1" in caplog.text
    assert "secret" not in caplog.text
