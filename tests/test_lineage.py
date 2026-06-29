"""Parent inheritance now reads/writes the store; token resolution unchanged."""

from __future__ import annotations

import pytest

from claude_launcher import credentials, lineage, profile, settings, store


def test_parent_set_get_clear_via_store(home):
    profile.create("base")
    child = profile.create("child")
    lineage.set_parent(child, "base")
    assert lineage.get_parent(child) == "base"
    assert store.profile_entry("child")["parent"] == "base"
    lineage.clear_parent(child)
    assert lineage.get_parent(child) is None


def test_set_parent_requires_existing_parent(home):
    child = profile.create("child")
    with pytest.raises(profile.ProfileError):
        lineage.set_parent(child, "missing")


def test_self_parent_rejected(home):
    p = profile.create("p")
    with pytest.raises(lineage.LineageError):
        lineage.set_parent(p, "p")


def test_cycle_rejected(home):
    a = profile.create("a")
    b = profile.create("b")
    lineage.set_parent(b, "a")
    with pytest.raises(lineage.LineageError):
        lineage.set_parent(a, "b")


def test_chain_and_effective_env_child_wins(home):
    profile.create("root")
    mid = profile.create("mid")
    leaf = profile.create("leaf")
    lineage.set_parent(mid, "root")
    lineage.set_parent(leaf, "mid")
    settings.set_env(profile.require("root"), {"A": "root", "B": "root"})
    settings.set_env(mid, {"B": "mid"})
    settings.set_env(leaf, {"C": "leaf"})
    assert [p.name for p in lineage.chain(leaf)] == ["root", "mid", "leaf"]
    assert lineage.effective_env(leaf) == {"A": "root", "B": "mid", "C": "leaf"}


def test_descendants(home):
    profile.create("root")
    a = profile.create("a")
    b = profile.create("b")
    lineage.set_parent(a, "root")
    lineage.set_parent(b, "a")
    names = {p.name for p in lineage.descendants(profile.require("root"))}
    assert names == {"a", "b"}


def test_token_inherited_from_parent(home):
    base = profile.create("base")
    child = profile.create("child")
    lineage.set_parent(child, "base")
    credentials.save_token(base, "sk-ant-oat01-PARENT")
    # Child has no token of its own but inherits the parent's.
    assert lineage.lookup_token(child) == "sk-ant-oat01-PARENT"
    assert lineage.login_state(child) == "inherited"
