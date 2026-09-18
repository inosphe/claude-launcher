"""The shared layer: one declaration in the store, converged onto every profile.

No test here lets a real ``claude`` run: every install goes through an injected
runner (or a patched ``plugins._run``), which is also the only way to assert
*what* would have been executed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_launcher import cli, plugins, profile, settings, store


def run(*argv):
    return cli.main(list(argv))


class FakeClaude:
    """Records the argv/env of every ``claude`` call and reports success."""

    def __init__(self, code: int = 0, output: str = "") -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.code = code
        self.output = output

    def __call__(self, argv, env):
        self.calls.append((list(argv), env.get("CLAUDE_CONFIG_DIR", "")))
        return self.code, self.output

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]

    @property
    def config_dirs(self) -> list[str]:
        return [cfg for _, cfg in self.calls]


def write_state(
    p: profile.Profile,
    *,
    marketplaces: dict | None = None,
    installed: list[str] | None = None,
    enabled: list[str] | None = None,
) -> None:
    """Write the three files Claude Code keeps per profile, as an install leaves them."""
    plugins_dir = p.config_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    known = {
        name: {"source": source, "installLocation": str(plugins_dir / name)}
        for name, source in (marketplaces or {}).items()
    }
    (plugins_dir / "known_marketplaces.json").write_text(
        json.dumps(known), encoding="utf-8"
    )
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {i: [] for i in installed or []}}),
        encoding="utf-8",
    )
    if enabled is not None:
        data = settings.load(p)
        data["enabledPlugins"] = {i: True for i in enabled}
        settings.save(p, data)


#: The one settings key claunch converges without anyone declaring it
#: (``store.SHARED_SETTINGS_DEFAULTS``). Every plan therefore holds it until the
#: profile has it, which is why the tests below name it instead of counting
#: actions: a bare count would go stale the moment the packaged default changes
#: and would say nothing about which key moved.
MODE = "permissions.defaultMode"


def satisfy_mode(p: profile.Profile) -> None:
    """Put the packaged default in a profile, so a plan holds only the test's own."""
    data = settings.load(p)
    settings.dotted_set(data, MODE, store.SHARED_SETTINGS_DEFAULTS[MODE])
    settings.save(p, data)


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #
def test_declaration_round_trips_through_the_store(home):
    assert plugins.declare_marketplace("snflkd/fluent-korean") is True
    assert plugins.declare_plugin("fluent-korean@fluent-korean") is True
    plugins.set_shared_setting("outputStyle", "fluent-korean:fluent-korean")
    assert store.shared_marketplaces() == ["snflkd/fluent-korean"]
    assert store.shared_plugins() == ["fluent-korean@fluent-korean"]
    assert store.shared_settings() == {"outputStyle": "fluent-korean:fluent-korean"}


def test_declaring_twice_is_not_an_edit(home):
    plugins.declare_marketplace("snflkd/fluent-korean")
    # Same source, spelled the way Windows would hand it back.
    assert plugins.declare_marketplace("snflkd/fluent-korean/") is False
    assert plugins.declare_plugin("a@b") is True
    assert plugins.declare_plugin("a@b") is False
    assert store.shared_marketplaces() == ["snflkd/fluent-korean"]


def test_undeclare_removes_and_reports_absence(home):
    plugins.declare_plugin("a@b")
    assert plugins.undeclare_plugin("a@b") is True
    assert plugins.undeclare_plugin("a@b") is False
    assert store.shared_plugins() == []


def test_unknown_shared_key_is_refused(home):
    with pytest.raises(store.StoreError):
        store.set_shared_field("plugns", ["typo"])


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def test_plan_lists_only_what_is_missing(home):
    p = profile.create("work")
    plugins.declare_marketplace("owner/repo")
    plugins.declare_plugin("thing@repo")
    plugins.set_shared_setting("outputStyle", "korean")
    write_state(
        p,
        marketplaces={"repo": {"source": "github", "repo": "owner/repo"}},
        installed=["thing@repo"],
        enabled=["thing@repo"],
    )
    settings_data = settings.load(p)
    settings_data["outputStyle"] = "korean"
    settings.save(p, settings_data)
    satisfy_mode(p)
    assert plugins.plan(p) == []


def test_plan_counts_a_disabled_plugin_as_missing(home):
    p = profile.create("work")
    plugins.declare_plugin("thing@repo")
    write_state(p, installed=["thing@repo"], enabled=[])
    assert [a.target for a in plugins.plan(p)] == ["thing@repo", MODE]


def test_plan_counts_an_enabled_but_unfetched_plugin_as_missing(home):
    p = profile.create("work")
    plugins.declare_plugin("thing@repo")
    write_state(p, installed=[], enabled=["thing@repo"])
    assert [a.target for a in plugins.plan(p)] == ["thing@repo", MODE]


def test_plan_matches_a_directory_marketplace_across_path_spelling(home):
    p = profile.create("work")
    plugins.declare_marketplace("D:/works/hq/harness")
    write_state(
        p,
        marketplaces={"hq": {"source": "directory", "path": "D:\\works\\hq\\harness"}},
    )
    satisfy_mode(p)
    assert plugins.plan(p) == []


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
def test_apply_runs_claude_per_profile_with_its_own_config_dir(home):
    a = profile.create("a")
    b = profile.create("b")
    plugins.declare_marketplace("owner/repo")
    plugins.declare_plugin("thing@repo")
    fake = FakeClaude()
    results = plugins.apply_all([a, b], runner=fake)
    assert all(r.ok for r in results)
    # Two plugin actions each, plus the packaged settings key.
    assert [len(r.done) for r in results] == [3, 3]
    assert fake.config_dirs == [
        str(a.config_dir), str(a.config_dir), str(b.config_dir), str(b.config_dir)
    ]
    assert fake.argvs[0][1:] == [
        "plugin", "marketplace", "add", "owner/repo", "--scope", "user"
    ]
    assert fake.argvs[1][1:] == [
        "plugin", "install", "thing@repo", "--scope", "user", "-y"
    ]


def test_apply_writes_declared_settings_keys(home):
    p = profile.create("work")
    plugins.set_shared_setting("outputStyle", "fluent-korean:fluent-korean")
    plugins.set_shared_setting("autoCompactEnabled", False)
    result = plugins.apply_to(p, runner=FakeClaude())
    assert result.ok and len(result.done) == 3
    data = settings.load(p)
    assert data["outputStyle"] == "fluent-korean:fluent-korean"
    assert data["autoCompactEnabled"] is False
    assert data["permissions"]["defaultMode"] == "auto"


def test_apply_keeps_the_profiles_other_settings(home):
    p = profile.create("work")
    settings.save(p, {"model": "opus"})
    plugins.set_shared_setting("outputStyle", "korean")
    plugins.apply_to(p, runner=FakeClaude())
    assert settings.load(p)["model"] == "opus"


def test_dry_run_changes_nothing(home):
    p = profile.create("work")
    plugins.declare_plugin("thing@repo")
    plugins.set_shared_setting("outputStyle", "korean")
    fake = FakeClaude()
    result = plugins.apply_to(p, dry_run=True, runner=fake)
    assert len(result.done) == 3
    assert fake.calls == []
    assert "outputStyle" not in settings.load(p)
    assert "permissions" not in settings.load(p)


def test_a_failed_install_does_not_stop_the_rest(home):
    p = profile.create("work")
    plugins.declare_plugin("thing@repo")
    plugins.set_shared_setting("outputStyle", "korean")
    result = plugins.apply_to(p, runner=FakeClaude(code=1, output="no such plugin"))
    assert not result.ok
    assert [a.target for a, _ in result.failed] == ["thing@repo"]
    # The settings key is independent of the install, so it still landed.
    assert settings.load(p)["outputStyle"] == "korean"


def test_apply_is_idempotent(home):
    p = profile.create("work")
    plugins.declare_plugin("thing@repo")
    fake = FakeClaude()
    plugins.apply_to(p, runner=fake)
    # The fake writes no state, so simulate what a real install leaves behind.
    write_state(p, installed=["thing@repo"], enabled=["thing@repo"])
    second = plugins.apply_to(p, runner=fake)
    assert second.done == [] and second.ok
    assert len(fake.calls) == 1


def test_apply_never_removes_what_a_profile_has_on_its_own(home):
    p = profile.create("work")
    write_state(p, installed=["local@repo"], enabled=["local@repo"])
    plugins.declare_plugin("shared@repo")
    plugins.apply_to(p, runner=FakeClaude())
    assert plugins.installed_plugins(p) == ["local@repo"]


def test_missing_profile_directory_is_reported_not_raised(home):
    p = profile.resolve("ghost")
    plugins.declare_plugin("thing@repo")
    result = plugins.apply_to(p, runner=FakeClaude())
    assert not result.ok
    assert "profile directory missing" in result.failed[0][1]


# --------------------------------------------------------------------------- #
# discovering a marketplace source from an existing profile
# --------------------------------------------------------------------------- #
def test_marketplace_source_is_read_back_off_a_profile_that_knows_it(home):
    a = profile.create("a")
    write_state(
        a, marketplaces={"fluent-korean": {"source": "github", "repo": "snflkd/fluent-korean"}}
    )
    found = plugins.discover_marketplace_source("fluent-korean", [a])
    assert found == "snflkd/fluent-korean"
    assert plugins.discover_marketplace_source("absent", [a]) is None


def test_install_declares_the_marketplace_it_found(home, capsys, monkeypatch):
    a = profile.create("a")
    write_state(
        a, marketplaces={"fk": {"source": "github", "repo": "snflkd/fluent-korean"}}
    )
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    assert run("plugin", "install", "thing@fk") == 0
    assert store.shared_marketplaces() == ["snflkd/fluent-korean"]
    assert "declared marketplace" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #
def test_install_declares_and_applies_to_every_profile(home, capsys, monkeypatch):
    a = profile.create("a")
    b = profile.create("b")
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    assert run("plugin", "install", "thing@repo") == 0
    assert store.shared_plugins() == ["thing@repo"]
    assert fake.config_dirs == [str(a.config_dir), str(b.config_dir)]


def test_no_apply_declares_without_running_anything(home, capsys, monkeypatch):
    profile.create("a")
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    assert run("plugin", "install", "thing@repo", "--no-apply") == 0
    assert store.shared_plugins() == ["thing@repo"]
    assert fake.calls == []
    assert "--no-apply" in capsys.readouterr().out


def test_install_can_target_one_profile(home, capsys, monkeypatch):
    a = profile.create("a")
    profile.create("b")
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    assert run("plugin", "install", "thing@repo", "--profile", "a") == 0
    assert fake.config_dirs == [str(a.config_dir)]


def test_apply_check_exits_nonzero_on_drift(home, capsys, monkeypatch):
    p = profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("plugin", "install", "thing@repo", "--no-apply")
    assert run("apply", "--check") == 1
    assert "drifted" in capsys.readouterr().out
    write_state(p, installed=["thing@repo"], enabled=["thing@repo"])
    satisfy_mode(p)
    assert run("apply", "--check") == 0


def test_apply_reports_a_failure_with_a_nonzero_exit(home, capsys, monkeypatch):
    profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude(code=1, output="boom"))
    run("plugin", "install", "thing@repo", "--no-apply")
    assert run("apply") == 1
    assert "FAILED" in capsys.readouterr().out


def test_shared_command_declares_a_settings_key_and_writes_it(home, capsys, monkeypatch):
    p = profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    assert run("shared", "outputStyle=fluent-korean:fluent-korean") == 0
    assert settings.load(p)["outputStyle"] == "fluent-korean:fluent-korean"


def test_shared_values_keep_their_json_type(home, monkeypatch):
    p = profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("shared", "autoCompactEnabled=false", "someCount=3")
    data = settings.load(p)
    assert data["autoCompactEnabled"] is False
    assert data["someCount"] == 3


def test_shared_unset_stops_managing_but_leaves_the_profile_alone(home, monkeypatch):
    p = profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("shared", "outputStyle=korean")
    run("shared", "--unset", "outputStyle")
    assert store.shared_settings() == {}
    assert settings.load(p)["outputStyle"] == "korean"


def test_uninstall_undeclares_and_removes_from_the_profiles(home, capsys, monkeypatch):
    p = profile.create("a")
    write_state(p, installed=["thing@repo"], enabled=["thing@repo"])
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    plugins.declare_plugin("thing@repo")
    assert run("plugin", "uninstall", "thing@repo") == 0
    assert store.shared_plugins() == []
    assert fake.argvs[0][1:] == [
        "plugin", "uninstall", "thing@repo", "--scope", "user"
    ]


def test_uninstall_skips_a_profile_that_does_not_have_it(home, monkeypatch):
    profile.create("a")
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    plugins.declare_plugin("thing@repo")
    assert run("plugin", "uninstall", "thing@repo") == 0
    assert fake.calls == []


def test_marketplace_remove_leaves_the_profiles_registration(home, capsys, monkeypatch):
    p = profile.create("a")
    write_state(p, marketplaces={"repo": {"source": "github", "repo": "owner/repo"}})
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("plugin", "marketplace", "add", "owner/repo")
    assert run("plugin", "marketplace", "remove", "owner/repo") == 0
    assert store.shared_marketplaces() == []
    assert "repo" in plugins.known_marketplaces(p)


def test_list_shows_the_declaration_and_the_drift(home, capsys, monkeypatch):
    profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("plugin", "install", "thing@repo", "--no-apply")
    assert run("plugin", "list") == 0
    out = capsys.readouterr().out
    assert "thing@repo" in out
    assert "pending on 1 of 1 profiles" in out


def test_list_json_is_machine_readable(home, capsys):
    plugins.declare_plugin("thing@repo")
    assert run("plugin", "list", "--json") == 0
    assert json.loads(capsys.readouterr().out)["plugins"] == ["thing@repo"]


# --------------------------------------------------------------------------- #
# new profiles start converged
# --------------------------------------------------------------------------- #
def test_a_new_profile_gets_the_declaration_at_creation(home, capsys, monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    plugins.declare_plugin("thing@repo")
    plugins.set_shared_setting("outputStyle", "korean")
    assert run("create", "fresh") == 0
    p = profile.require("fresh")
    assert settings.load(p)["outputStyle"] == "korean"
    assert fake.config_dirs == [str(p.config_dir)]


def test_creating_a_profile_with_nothing_declared_runs_no_claude(home, monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(plugins, "_run", fake)
    assert run("create", "fresh") == 0
    assert fake.calls == []


def test_error_line_quotes_the_end_of_the_output_not_the_progress(home):
    output = 'Installing plugin "thing@repo"...\nError: marketplace "repo" not found'
    assert plugins.error_line(output) == 'Error: marketplace "repo" not found'
    assert plugins.error_line("") == "failed"
def test_redeclaring_the_same_settings_value_writes_nothing(home, monkeypatch):
    """A no-op must not rewrite the config file: the rewrite can be refused.

    ``atomic.replace`` cannot replace a file another process holds open, which
    on Windows an editor with the file loaded is enough to cause. A command
    that has nothing to do has to stay off that path entirely.
    """
    plugins.set_shared_setting("outputStyle", "korean")

    def refuse(*args, **kwargs):
        raise AssertionError("the store was written for a no-op")

    monkeypatch.setattr(store, "set_shared_field", refuse)
    assert plugins.set_shared_setting("outputStyle", "korean") is False


def test_declaring_a_different_value_still_writes(home):
    plugins.set_shared_setting("outputStyle", "korean")
    assert plugins.set_shared_setting("outputStyle", "english") is True
    assert store.shared_settings() == {"outputStyle": "english"}


def test_shared_command_reports_an_unchanged_declaration(home, capsys, monkeypatch):
    profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("shared", "outputStyle=korean")
    capsys.readouterr()
    assert run("shared", "outputStyle=korean") == 0
    assert "was already declared" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the packaged default permission mode
# --------------------------------------------------------------------------- #
def test_apply_lands_the_default_permission_mode(home):
    """Nothing declares it, and the profile still converges onto it."""
    p = profile.create("work")
    result = plugins.apply_to(p, runner=FakeClaude())
    assert result.ok
    assert [a.target for a in result.done] == [MODE]
    assert settings.load(p)["permissions"]["defaultMode"] == "auto"


def test_cli_apply_and_shared_preserve_profile_override(home, monkeypatch):
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    p = profile.create("work")
    other = profile.create("other")
    store.set_profile_setting("work", MODE, "plan")
    assert run("shared", "permissions.defaultMode=acceptEdits") == 0
    assert run("apply") == 0
    assert run("apply", "--check") == 0
    assert settings.load(p)["permissions"]["defaultMode"] == "plan"
    assert settings.load(other)["permissions"]["defaultMode"] == "acceptEdits"
    assert run("create", "fresh") == 0
    assert settings.load(profile.require("fresh"))["permissions"]["defaultMode"] == "acceptEdits"


def test_the_mode_write_leaves_the_gate_guard_alone(home):
    """The whole reason the write is dotted: ``permissions.deny`` is a sibling.

    ``install`` puts the cflow gate guard in that same object, so a write that
    replaced ``permissions`` outright would drop the rules that keep an agent's
    shell off the human approve commands -- silently, in the direction nobody
    checks.
    """
    p = profile.create("work")
    guard = ["Bash(claunch cflow approve)", "PowerShell(claunch cflow approve)"]
    settings.merge_permission_deny(
        p.config_dir / settings.SETTINGS_FILENAME, guard
    )
    plugins.apply_to(p, runner=FakeClaude())
    perms = settings.load(p)["permissions"]
    assert perms["deny"] == guard
    assert perms["defaultMode"] == "auto"


def test_a_declared_mode_replaces_the_default(home, monkeypatch):
    p = profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    assert run("shared", "permissions.defaultMode=default") == 0
    assert settings.load(p)["permissions"]["defaultMode"] == "default"


def test_unsetting_the_mode_returns_it_to_the_default(home, monkeypatch):
    p = profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("shared", "permissions.defaultMode=default")
    assert run("shared", "--unset", "permissions.defaultMode") == 0
    assert store.shared_settings() == {}
    # Undeclaring says "the launcher no longer decides this", and for a key
    # claunch ships a default for that means the default -- so the profile is
    # converged back onto it rather than left on the value nobody declared.
    run("apply")
    assert settings.load(p)["permissions"]["defaultMode"] == "auto"


def test_creating_a_profile_lands_the_mode(home, monkeypatch):
    """The creation path, not just ``apply`` -- this is the reported symptom."""
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    assert run("create", "fresh") == 0
    assert settings.load(profile.require("fresh"))["permissions"]["defaultMode"] == "auto"


def test_a_permissions_block_that_is_not_an_object_is_reported_not_rewritten(home):
    p = profile.create("work")
    settings.save(p, {"permissions": "the user's own shape"})
    result = plugins.apply_to(p, runner=FakeClaude())
    assert not result.ok
    failed = [a.target for a, _ in result.failed]
    assert failed == [MODE]
    # Left exactly as it was: one key claunch converges is not worth
    # overwriting whatever the user wrote in its place.
    assert settings.load(p) == {"permissions": "the user's own shape"}


def test_shared_listing_marks_the_packaged_default(home, capsys, monkeypatch):
    profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    run("shared", "outputStyle=korean")
    assert run("shared") == 0
    out = capsys.readouterr().out
    assert 'outputStyle="korean"' in out
    assert 'permissions.defaultMode="auto"  (claunch default)' in out


def test_plugin_list_shows_the_key_it_counts_as_drift(home, capsys, monkeypatch):
    """The listing and the drift line read the same set, so neither can lie alone."""
    profile.create("a")
    monkeypatch.setattr(plugins, "_run", FakeClaude())
    assert run("plugin", "list") == 0
    out = capsys.readouterr().out
    assert 'permissions.defaultMode="auto"  (claunch default)' in out
    assert "pending on 1 of 1 profiles" in out
