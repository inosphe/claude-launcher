"""Prune finds profile dirs not declared in the store."""

from __future__ import annotations

import json

from claude_launcher import bootstrap, config, profile, prune


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


def test_migrated_token_only_profile_not_orphaned(home):
    # A pre-existing profile with only a token (no env/parent) must survive prune.
    d = config.profiles_dir() / "tok"
    d.mkdir(parents=True)
    (d / ".launcher-token").write_text("sk-ant-oat01-X\n", encoding="utf-8")
    bootstrap.run()
    assert prune.orphans() == []
