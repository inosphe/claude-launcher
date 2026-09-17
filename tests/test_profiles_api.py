"""The profiles page's two server facts: what a profile's mode is, and setting it.

The read is on ``GET /api/profiles`` (one row per profile carries
``permission_mode``), the write is ``POST /api/profiles/permission-mode``. Both
go through the store's *declaration* rather than a per-profile field — that is
the shape the shared layer already converges, and a page that invented its own
per-profile state would disagree with ``claunch shared`` about the same key.

No test here lets a real ``claude`` run: with nothing declared, convergence
touches only ``settings.json``, so ``apply_all`` never reaches a subprocess.
"""

from __future__ import annotations

import asyncio
import json

from claude_launcher import plugins, profile, settings, store
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
