"""Prune finds profile dirs not declared in the store."""

from __future__ import annotations

from claude_launcher import config, profile, prune


def test_no_orphans_when_all_declared(home):
    profile.create("work")
    assert prune.orphans() == []


def test_detects_orphan_dir(home):
    profile.create("keep")
    # A directory with no store entry (created outside the launcher).
    (config.profiles_dir() / "orphan").mkdir(parents=True)
    names = {p.name for p in prune.orphans()}
    assert names == {"orphan"}


def test_removed_profile_is_not_recreated_as_orphan(home):
    profile.create("temp")
    profile.remove("temp")  # drops dir + store entry
    assert prune.orphans() == []
