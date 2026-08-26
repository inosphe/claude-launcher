"""Execution selectors overlay a harness without creating another profile."""

from __future__ import annotations

import pytest

from claude_launcher import harnesses, lineage, profile, runner


def test_selector_shares_the_base_storage_and_exposes_the_full_name(home):
    base = profile.create("ds4")
    selected = profile.require_selector("ds4:pi")

    assert selected.name == "ds4"
    assert selected.selector == "ds4:pi"
    assert selected.config_dir == base.config_dir
    assert lineage.effective_harness(selected) == "pi"
    assert harnesses.get("pi").profile_home(selected.config_dir) == base.config_dir / "pi"


def test_explicit_selector_beats_the_yaml_default_without_mutating_it(home):
    base = profile.create("ds4")
    lineage.set_harness(base, "kimi")

    assert lineage.effective_harness(profile.require_selector("ds4")) == "kimi"
    assert lineage.effective_harness(profile.require_selector("ds4:claude")) == "claude"
    assert lineage.effective_harness(profile.require_selector("ds4:pi")) == "pi"
    assert lineage.effective_harness(base) == "kimi"


@pytest.mark.parametrize("value", ["ds4:", ":pi", "ds4:pi:extra", "ds4:bad name"])
def test_invalid_selector_is_refused_before_path_resolution(home, value):
    profile.create("ds4")
    with pytest.raises(profile.ProfileError, match="invalid"):
        profile.resolve_selector(value)


def test_selector_drives_runner_without_set_harness(home):
    profile.create("ds4")
    selected = profile.require_selector("ds4:pi")
    assert runner.profile_harness(selected).name == "pi"
