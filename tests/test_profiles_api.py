"""The profiles page's server facts: a profile's mode, its rule lists, and setting it.

The read is on ``GET /api/profiles`` (one row per profile carries
``permission_mode`` and ``permission_rules``), the write is
``POST /api/profiles/permission-mode``. Both go through the store's
shared/profile declarations and the common apply planner. ``permission_rules``
is read-only here and everywhere — planting the gate guard is ``claunch
install``'s job, and the expectation it is read against is imported from there
rather than copied.

No test here lets a real ``claude`` run: with nothing declared, convergence
touches only ``settings.json``, so ``apply_all`` never reaches a subprocess.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher import install, plugins, profile, settings, store
from claude_launcher.daemon import api

MODE = "permissions.defaultMode"


class _Request:
    """Enough of an ``aiohttp`` request for the two handlers under test."""

    def __init__(self, payload=None):
        self._payload = payload

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def post(payload):
    return asyncio.run(api.h_profiles_permission_mode(_Request(payload)))


def listing():
    return json.loads(asyncio.run(api.h_profiles(_Request())).text)


def modes_by_profile(body):
    return {
        row["profile"]: row["permission_mode"]
        for row in body["profile_details"]
        if row.get("explicit") is False
    }


# --------------------------------------------------------------------------- #
# the read: one row per profile, on the profile's own harness
# --------------------------------------------------------------------------- #
def test_a_profile_with_no_value_reports_it_as_unconverged(home):
    profile.create("work")
    row = modes_by_profile(listing())["work"]
    assert row["key"] == MODE
    # No value in the file: this is the state that asks before every tool call.
    assert row["value"] is None
    # ...and what claunch would write, which is the packaged default.
    assert row["target"] == "auto"
    assert row["declared"] is None
    assert row["source"] == "claunch-default"
    assert row["converged"] is False
    assert "auto" in row["modes"]


def test_a_converged_profile_reports_the_value_and_its_source(home):
    p = profile.create("work")
    post({"mode": "acceptEdits"})
    row = modes_by_profile(listing())["work"]
    assert row["value"] == "acceptEdits"
    assert row["target"] == "acceptEdits"
    assert row["declared"] == "acceptEdits"
    # Declared, not defaulted: the page has to be able to say who decided.
    assert row["source"] == "declared"
    assert row["converged"] is True
    assert settings.load(p)["permissions"]["defaultMode"] == "acceptEdits"


def test_a_profile_that_disagrees_is_not_reported_as_settled(home):
    """The state ``claunch apply`` closes, and the reason value alone is not enough."""
    p = profile.create("work")
    post({"mode": "plan"})
    # Something wrote a different value into the profile afterwards; the
    # declaration has not been converged onto it again yet.
    data = settings.load(p)
    settings.dotted_set(data, MODE, "acceptEdits")
    settings.save(p, data)
    row = modes_by_profile(listing())["work"]
    assert row["value"] == "acceptEdits"
    assert row["target"] == "plan"
    assert row["converged"] is False


def test_a_row_for_another_harness_reports_no_mode(home):
    """It is a Claude Code settings key; a codex profile never reads it."""
    profile.create("work")
    profile.create("cx", )
    store.update(lambda doc: doc.setdefault("profiles", {}).setdefault("cx", {}).update({"harness": "codex"}))
    body = listing()
    assert modes_by_profile(body)["cx"] is None
    assert modes_by_profile(body)["work"] is not None


# --------------------------------------------------------------------------- #
# the write
# --------------------------------------------------------------------------- #
def test_setting_the_mode_declares_and_converges(home):
    p = profile.create("work")
    body = json.loads(post({"mode": "plan"}).text)
    assert body["declared"] == "plan"
    assert body["target"] == "plan"
    assert body["converged"] == ["work"]
    assert body["failed"] == []
    assert settings.load(p)["permissions"]["defaultMode"] == "plan"


def test_setting_the_mode_leaves_the_gate_guard_alone(home):
    """Same sibling-preservation the CLI path has, through the HTTP door."""
    p = profile.create("work")
    guard = ["Bash(claunch cflow approve)", "PowerShell(claunch cflow approve)"]
    settings.merge_permission_deny(p.config_dir / settings.SETTINGS_FILENAME, guard)
    post({"mode": "auto"})
    perms = settings.load(p)["permissions"]
    assert perms["deny"] == guard
    assert perms["defaultMode"] == "auto"


def test_setting_the_same_mode_twice_is_not_an_edit(home):
    profile.create("work")
    first = json.loads(post({"mode": "auto"}).text)
    assert first["converged"] == ["work"]
    second = json.loads(post({"mode": "auto"}).text)
    # Nothing to write: the declaration already said this and the profile
    # already holds it.
    assert second["converged"] == []
    assert second["unchanged"] == ["work"]


def test_an_unknown_mode_is_refused_before_anything_is_written(home):
    p = profile.create("work")
    response = post({"mode": "accept-edits"})
    assert response.status == 400
    assert "accept-edits" in json.loads(response.text)["error"]
    # Refused means refused: no declaration, and no profile written.
    assert store.shared_settings() == {}
    assert "permissions" not in settings.load(p)


def test_undeclaring_returns_every_profile_to_the_default(home):
    p = profile.create("work")
    post({"mode": "bypassPermissions"})
    assert settings.load(p)["permissions"]["defaultMode"] == "bypassPermissions"
    body = json.loads(post({"mode": None}).text)
    # Null means "nobody declares one" -- the packaged default is what is in
    # force, and it is named, not left blank.
    assert body["declared"] is None
    assert body["target"] == "auto"
    assert store.shared_settings() == {}
    assert settings.load(p)["permissions"]["defaultMode"] == "auto"


def test_an_empty_string_undeclares_too(home):
    profile.create("work")
    post({"mode": "plan"})
    body = json.loads(post({"mode": "  "}).text)
    assert body["declared"] is None
    assert store.shared_settings() == {}


def test_a_profile_whose_permissions_is_not_an_object_is_reported_not_repaired(home):
    p = profile.create("work")
    settings.save(p, {"permissions": "the user's own shape"})
    body = json.loads(post({"mode": "auto"}).text)
    assert body["failed"] and body["failed"][0]["profile"] == "work"
    assert settings.load(p) == {"permissions": "the user's own shape"}


def test_a_broken_lineage_is_skipped_rather_than_written(home):
    """A profile whose chain cannot be resolved is not one to write into.

    The failure that raises is an unknown *harness* name -- a missing parent
    link is skipped by the chain walk and the profile keeps the historical
    default. Same condition ``h_profiles`` turns into an error row, so a
    profile the listing cannot describe is a profile this must not write.
    """
    good = profile.create("good")
    orphan = profile.create("orphan")
    store.update(
        lambda doc: doc.setdefault("profiles", {}).setdefault("orphan", {}).update(
            {"harness": "nosuchharness"}
        )
    )
    body = json.loads(post({"mode": "auto"}).text)
    assert body["converged"] == ["good"]
    assert settings.load(good)["permissions"]["defaultMode"] == "auto"
    assert "permissions" not in settings.load(orphan)


def test_convergence_uses_the_shared_layer_not_a_second_writer(home):
    """The endpoint's whole job is ``set_shared_setting`` + ``apply_all``.

    Asserted rather than assumed because a second writer is how the same key
    gets two sources of truth: the CLI's ``claunch shared`` would then report
    one declaration while the page wrote another.
    """
    profile.create("work")
    post({"mode": "plan"})
    assert plugins.store.shared_settings() == {MODE: "plan"}
    assert store.effective_shared_settings()[MODE] == "plan"


def test_profile_apply_changes_only_the_selected_profile(home):
    work = profile.create("work")
    other = profile.create("other")
    post({"mode": "plan"})
    guard = ["Bash(claunch cflow approve)"]
    settings.merge_permission_deny(work.config_dir / settings.SETTINGS_FILENAME, guard)
    before = settings.load(other)
    body = json.loads(post({"profile": "work", "mode": "acceptEdits"}).text)
    assert body["profile"] == "work"
    assert body["converged"] == ["work"]
    assert store.shared_settings()[MODE] == "plan"
    assert settings.load(other) == before
    assert settings.load(work)["permissions"] == {"defaultMode": "acceptEdits", "deny": guard}
    row = modes_by_profile(listing())["work"]
    assert row["source"] == "profile"
    assert row["override"] == row["target"] == row["value"] == "acceptEdits"
    assert row["shared_target"] == row["shared_declared"] == "plan"
    assert row["converged"] is True


def test_profile_override_survives_shared_apply_and_cli_planner(home):
    work = profile.create("work")
    other = profile.create("other")
    post({"profile": "work", "mode": "plan"})
    post({"mode": "acceptEdits"})
    plugins.apply_all([work, other])
    assert settings.load(work)["permissions"]["defaultMode"] == "plan"
    assert settings.load(other)["permissions"]["defaultMode"] == "acceptEdits"
    fresh = profile.create("fresh")
    plugins.apply_to(fresh)
    assert settings.load(fresh)["permissions"]["defaultMode"] == "acceptEdits"
    # Removing the shared declaration also preserves the profile override.
    post({"mode": None})
    assert settings.load(work)["permissions"]["defaultMode"] == "plan"
    assert settings.load(other)["permissions"]["defaultMode"] == "auto"


@pytest.mark.parametrize("mode", [None, "", "  "])
def test_removing_profile_override_rejoins_shared_default(home, mode):
    work = profile.create("work")
    post({"mode": "acceptEdits"})
    post({"profile": "work", "mode": "plan"})
    store.set_profile_setting("work", "outputStyle", "concise")
    body = json.loads(post({"profile": "work", "mode": mode}).text)
    assert body["declared"] is None
    assert body["target"] == "acceptEdits"
    assert store.profile_settings("work") == {"outputStyle": "concise"}
    assert settings.load(work)["permissions"]["defaultMode"] == "acceptEdits"
    assert modes_by_profile(listing())["work"]["override"] is None
    post({"mode": "default"})
    assert settings.load(work)["permissions"]["defaultMode"] == "default"


@pytest.mark.parametrize("payload", [
    {"profile": "missing", "mode": "auto"},
    {"profile": "../work", "mode": "auto"},
    {"profile": "..", "mode": "auto"},
    {"profile": ".", "mode": "auto"},
    {"profile": "work:claude", "mode": "auto"},
    {"profile": None, "mode": "auto"},
    {"profile": "", "mode": "auto"},
    {"profile": "work", "mode": "nope"},
    {"profile": "work"},
    {"profile": "cx", "mode": "auto"},
])
def test_invalid_profile_edits_do_not_write(home, payload):
    work = profile.create("work")
    profile.create("cx")
    store.set_profile_field("cx", "harness", "codex")
    before = store.load()
    assert post(payload).status == 400
    assert store.load() == before
    assert settings.load(work) == {}


def test_profile_apply_reports_write_failure_and_can_retry(home):
    work = profile.create("work")
    settings.save(work, {"permissions": "invalid"})
    body = json.loads(post({"profile": "work", "mode": "plan"}).text)
    assert body["failed"][0]["profile"] == "work"
    assert modes_by_profile(listing())["work"]["converged"] is False
    settings.save(work, {})
    plugins.apply_to(work)
    assert modes_by_profile(listing())["work"]["converged"] is True


# --------------------------------------------------------------------------- #
# the rules: the other half of the same settings file
# --------------------------------------------------------------------------- #
def rules_by_profile(body):
    return {
        row["profile"]: row["permission_rules"]
        for row in body["profile_details"]
        if row.get("explicit") is False
    }


def plant(p, *, allow=True, deny=True):
    """What ``claunch install``'s gate guard does to one profile's file."""
    path = p.config_dir / settings.SETTINGS_FILENAME
    if deny:
        settings.merge_permission_deny(path, install.GATE_DENY_RULES)
    if allow:
        settings.merge_permission_allow(path, install.GATE_ALLOW_RULES)


def test_a_profile_with_no_rules_reports_both_halves_missing(home):
    profile.create("work")
    row = rules_by_profile(listing())["work"]
    assert row["allow"]["key"] == "permissions.allow"
    assert row["deny"]["key"] == "permissions.deny"
    # The expectation is read off the installer, not re-spelled in the daemon:
    # a second copy is a second thing to forget when a gate verb gains a shell.
    assert row["allow"]["expected"] == list(install.GATE_ALLOW_RULES)
    assert row["deny"]["expected"] == list(install.GATE_DENY_RULES)
    # five gate verbs (approve, select, goto, abort, set) x two shells x two spellings
    assert len(row["deny"]["expected"]) == 20
    assert row["allow"]["value"] == []
    assert row["allow"]["missing"] == ["mcp__claunch"]
    assert row["deny"]["missing"] == list(install.GATE_DENY_RULES)
    assert row["allow"]["converged"] is False
    assert row["deny"]["converged"] is False
    assert row["converged"] is False


def test_a_planted_guard_reports_both_halves_whole(home):
    plant(profile.create("work"))
    row = rules_by_profile(listing())["work"]
    assert row["allow"]["missing"] == []
    assert row["deny"]["missing"] == []
    assert row["allow"]["converged"] is True and row["deny"]["converged"] is True
    assert row["converged"] is True
    # The value is the file's list, not the guard's slice of it -- the card and
    # the table both read the answer off ``missing``.
    assert row["allow"]["value"] == list(install.GATE_ALLOW_RULES)


def test_a_rule_the_person_added_is_not_a_gap(home):
    """Both lists are unions; a profile holding more than the guard is settled."""
    p = profile.create("work")
    path = p.config_dir / settings.SETTINGS_FILENAME
    settings.merge_permission_allow(path, ("Bash(git status)",))
    settings.merge_permission_deny(path, ("Bash(rm -rf:*)",))
    plant(p)
    row = rules_by_profile(listing())["work"]
    assert row["converged"] is True
    assert "Bash(git status)" in row["allow"]["value"]
    assert "Bash(rm -rf:*)" in row["deny"]["value"]


def test_a_half_planted_guard_names_the_rules_that_are_absent(home):
    """Why ``missing`` is a list and not a boolean: the deny half can be partway.

    A guard that reports "has some" is the failure this shape exists to catch —
    sixteen rules with three planted is a profile an agent can still be locked
    out of, and only the names say which.
    """
    p = profile.create("work")
    path = p.config_dir / settings.SETTINGS_FILENAME
    settings.merge_permission_deny(path, install.GATE_DENY_RULES[:3])
    settings.merge_permission_allow(path, install.GATE_ALLOW_RULES)
    row = rules_by_profile(listing())["work"]
    assert row["allow"]["converged"] is True
    assert row["deny"]["converged"] is False
    assert row["converged"] is False
    assert row["deny"]["value"] == list(install.GATE_DENY_RULES[:3])
    assert row["deny"]["missing"] == list(install.GATE_DENY_RULES[3:])


def test_a_rule_list_of_another_shape_is_reported_not_repaired(home):
    """A list claunch does not recognise reads as empty; the file is left alone."""
    p = profile.create("work")
    settings.save(p, {"permissions": {"allow": "managed elsewhere"}})
    row = rules_by_profile(listing())["work"]
    assert row["allow"]["value"] == []
    assert row["allow"]["converged"] is False
    assert settings.load(p)["permissions"]["allow"] == "managed elsewhere"


def test_a_row_for_another_harness_reports_no_rules(home):
    """A rule list another harness never reads has no answer here."""
    profile.create("work")
    profile.create("cx")
    store.set_profile_field("cx", "harness", "codex")
    rows = rules_by_profile(listing())
    assert rows["cx"] is None
    assert rows["work"] is not None


def test_the_shared_declaration_is_reported_beside_the_files(home):
    """Two routes reach a profile and the answer says which one is in play.

    A declaration arrives through ``claunch apply`` (which assigns the list);
    the guard arrives through ``claunch install`` (which merges). Here one is
    declared and nothing has been written yet, which is the state between
    ``claunch shared`` and the apply that follows it.
    """
    profile.create("work")
    store.update(lambda doc: doc.setdefault("shared", {}).setdefault(
        "settings", {}).update({"permissions.allow": ["mcp__claunch"]}))
    row = rules_by_profile(listing())["work"]
    assert row["allow"]["declared"] == ["mcp__claunch"]
    assert row["deny"]["declared"] is None
    # Declared is not planted: the file is still empty.
    assert row["allow"]["value"] == []
    assert row["allow"]["converged"] is False


def test_two_profiles_are_told_apart(home):
    """The boundary the page exists for: one profile guarded, one not."""
    guarded = profile.create("guarded")
    profile.create("bare")
    plant(guarded)
    rows = rules_by_profile(listing())
    assert rows["guarded"]["converged"] is True
    assert rows["bare"]["converged"] is False
    assert rows["bare"]["allow"]["missing"] == ["mcp__claunch"]
    assert len(rows["bare"]["deny"]["missing"]) == 20
