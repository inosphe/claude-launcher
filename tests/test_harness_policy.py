"""Profile/provider ``allowed_harnesses`` policy and its inheritance rules."""

from __future__ import annotations

import pytest

from claude_launcher import harness_policy, lineage, profile, providers, store


def _profile_limit(name: str, allowed) -> None:
    store.set_profile_field(name, "allowed_harnesses", allowed)


def _provider(name: str, allowed=None) -> None:
    doc = store.load()
    spec = {"env": {"ANTHROPIC_BASE_URL": f"https://{name}.invalid"}}
    if allowed is not None:
        spec["allowed_harnesses"] = allowed
    doc.setdefault("providers", {})[name] = spec
    store.save(doc)


def test_missing_fields_allow_every_declared_harness(home):
    p = profile.create("work")
    names = harness_policy.allowed_names(p)
    assert {"claude", "pi", "kimi", "codex", "agent"}.issubset(names)


def test_profile_allow_list_filters_explicit_selectors_and_default(home):
    p = profile.create("work")
    _profile_limit("work", ["claude", "pi"])

    assert harness_policy.evaluate(p, "claude").allowed
    assert harness_policy.evaluate(p, "pi").allowed
    denied = harness_policy.evaluate(p, "kimi")
    assert not denied.allowed
    assert "profile 'work'" in denied.reason
    assert harness_policy.allowed_names(p) == ["claude", "pi"]
    with pytest.raises(lineage.LineageError, match="allows only"):
        lineage.effective_harness(profile.resolve_selector("work:kimi"))


def test_explicit_empty_list_denies_every_harness(home):
    p = profile.create("work")
    _profile_limit("work", [])
    assert harness_policy.allowed_names(p) == []
    with pytest.raises(lineage.LineageError, match=r"\(none\)"):
        lineage.effective_harness(p)


def test_profile_lineage_is_an_intersection_and_child_cannot_widen(home):
    parent = profile.create("account")
    child = profile.create("work")
    lineage.set_parent(child, parent.name)
    _profile_limit(parent.name, ["claude", "pi"])
    _profile_limit(child.name, ["pi", "kimi"])

    assert harness_policy.allowed_names(child) == ["pi"]
    report = harness_policy.evaluate(child, "kimi")
    assert not report.allowed
    assert report.profile_constraints == (
        ("account", ("claude", "pi")),
        ("work", ("pi", "kimi")),
    )


def test_effective_provider_constraint_intersects_profile_constraint(home):
    _provider("kimi-api", ["claude"])
    p = profile.create("work")
    _profile_limit(p.name, ["claude", "pi"])
    providers.set_profile_selection(p, "kimi-api")

    assert harness_policy.allowed_names(p) == ["claude"]
    denied = harness_policy.evaluate(p, "pi")
    assert denied.provider == "kimi-api"
    assert "provider 'kimi-api'" in denied.reason


def test_provider_constraint_follows_inherited_and_global_selection(home):
    _provider("claude-only", ["claude"])
    parent = profile.create("account")
    child = profile.create("work")
    lineage.set_parent(child, parent.name)
    providers.set_profile_selection(parent, "claude-only")
    assert harness_policy.allowed_names(child) == ["claude"]

    providers.clear_profile_selection(parent)
    providers.set_active("claude-only")
    assert harness_policy.allowed_names(child) == ["claude"]


def test_set_harness_refuses_a_denied_pin(home):
    p = profile.create("work")
    _profile_limit(p.name, ["pi"])
    with pytest.raises(lineage.LineageError, match="allows only"):
        lineage.set_harness(p, "claude")
    lineage.set_harness(p, "pi")
    assert lineage.effective_harness(p) == "pi"


def test_clear_harness_does_not_persist_a_denied_default(home):
    p = profile.create("work")
    lineage.set_harness(p, "pi")
    _profile_limit(p.name, ["pi"])

    with pytest.raises(lineage.LineageError, match="allows only"):
        lineage.clear_harness(p)

    assert store.profile_entry(p.name)["harness"] == "pi"


def test_parent_change_cannot_introduce_or_reveal_a_denied_harness(home):
    claude_parent = profile.create("claude-account")
    _profile_limit(claude_parent.name, ["claude"])
    child = profile.create("work")
    lineage.set_harness(child, "pi")

    with pytest.raises(lineage.LineageError, match="allows only"):
        lineage.set_parent(child, claude_parent.name)
    assert lineage.get_parent(child) is None

    pi_parent = profile.create("pi-account")
    lineage.set_harness(pi_parent, "pi")
    inherited = profile.create("inherited")
    lineage.set_parent(inherited, pi_parent.name)
    _profile_limit(inherited.name, ["pi"])
    with pytest.raises(lineage.LineageError, match="allows only"):
        lineage.clear_parent(inherited)
    assert lineage.get_parent(inherited) == pi_parent.name


@pytest.mark.parametrize(
    "entry,match",
    [
        ({"allowed_harnesses": "claude"}, "must be a list"),
        ({"allowed_harnesses": ["bad:name"]}, "invalid harness name"),
    ],
)
def test_malformed_profile_policy_fails_closed(home, entry, match):
    p = profile.create("work")
    doc = store.load()
    doc["profiles"][p.name].update(entry)
    store.save(doc)
    with pytest.raises(harness_policy.HarnessPolicyError, match=match):
        harness_policy.evaluate(p, "claude")


def test_malformed_provider_policy_fails_closed(home):
    p = profile.create("work")
    doc = store.load()
    doc.setdefault("providers", {})["broken"] = {
        "env": {},
        "allowed_harnesses": "claude",
    }
    doc["profiles"][p.name]["provider"] = "broken"
    store.save(doc)

    with pytest.raises(harness_policy.HarnessPolicyError, match="must be a list"):
        harness_policy.evaluate(p, "claude")


def test_unknown_future_names_are_retained_but_not_offered(home):
    p = profile.create("work")
    _profile_limit(p.name, ["future-agent", "pi"])
    assert harness_policy.profile_constraints(p) == (
        ("work", ("future-agent", "pi")),
    )
    assert harness_policy.allowed_names(p) == ["pi"]
