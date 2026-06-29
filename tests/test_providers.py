"""Providers resolve own -> ancestor -> global -> default, all from the store."""

from __future__ import annotations

import pytest

from claude_launcher import lineage, profile, providers, store


def _define_provider(name, env):
    doc = store.load()
    doc.setdefault("providers", {})[name] = {"env": env}
    store.save(doc)


def test_registry_includes_default(home):
    assert providers.registry() == {"default": {}}


def test_provider_env_known_and_unknown(home):
    _define_provider("glm", {"ANTHROPIC_BASE_URL": "https://x"})
    assert providers.provider_env("glm") == {"ANTHROPIC_BASE_URL": "https://x"}
    assert providers.provider_env("default") == {}
    with pytest.raises(providers.ProviderError):
        providers.provider_env("nope")


def test_resolve_prefers_own(home):
    _define_provider("glm", {"A": "1"})
    p = profile.create("work")
    providers.set_profile_selection(p, "glm")
    assert providers.resolve_name(p) == "glm"


def test_resolve_inherits_from_ancestor(home):
    _define_provider("glm", {"A": "1"})
    base = profile.create("base")
    child = profile.create("child")
    lineage.set_parent(child, "base")
    providers.set_profile_selection(base, "glm")
    assert providers.resolve_name(child) == "glm"


def test_resolve_falls_back_to_global_then_default(home):
    _define_provider("glm", {"A": "1"})
    p = profile.create("work")
    assert providers.resolve_name(p) == "default"
    providers.set_active("glm")
    assert providers.resolve_name(p) == "glm"


def test_default_selection_pins_over_global(home):
    _define_provider("glm", {"A": "1"})
    p = profile.create("work")
    providers.set_active("glm")
    # Pinning to 'default' overrides the global provider...
    providers.set_profile_selection(p, "default")
    assert store.profile_entry("work")["provider"] == "default"
    assert providers.resolve_name(p) == "default"
    # ...while clearing the override falls back to the global one.
    providers.clear_profile_selection(p)
    assert "provider" not in store.profile_entry("work")
    assert providers.resolve_name(p) == "glm"


def test_set_active_unknown_rejected(home):
    with pytest.raises(providers.ProviderError):
        providers.set_active("ghost")


def test_clear_active_resets_global(home):
    _define_provider("glm", {"A": "1"})
    providers.set_active("glm")
    providers.clear_active()
    p = profile.create("work")
    assert providers.resolve_name(p) == "default"
