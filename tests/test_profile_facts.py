"""The daemon's in-memory profile facts (claunch-89eom).

The Restart-settings picker and the session detail poll used to re-read
``~/.claude-launcher/.claunch.yaml`` and re-validate every lender on each request. These tests
pin the replacement: one validation per refresh interval, the clock turning it
over, and a config write invalidating it.
"""

from __future__ import annotations

import json

import pytest

from claude_launcher import borrowing, credentials, profile, store
from claude_launcher.daemon import profile_facts, snapshots


@pytest.fixture()
def clock(monkeypatch):
    """A controllable monotonic clock for the snapshot module."""
    now = [1000.0]
    monkeypatch.setattr(snapshots, "_now", lambda: now[0])
    return now


def _count_validate(monkeypatch):
    """Count borrow validations made through the module the facts use."""
    calls = []
    real = borrowing.validate

    def counting(runtime_profile, lender_value, **kwargs):
        calls.append((runtime_profile.name, lender_value))
        return real(runtime_profile, lender_value, **kwargs)

    monkeypatch.setattr(profile_facts.borrowing, "validate", counting)
    return calls


def test_one_borrow_row_is_validated_once_and_then_read_from_memory(
    home, monkeypatch
):
    """Two reads of one selector cost one validation per lender (claunch-89eom).

    This is the whole point of the change: the panel opens per session and the
    detail rail polls every two seconds, and neither should re-run the policy
    evaluation and credential probe per lender.
    """
    profile.create("work")
    profile.create("ds4")
    profile.create("kimi")
    calls = _count_validate(monkeypatch)

    facts = profile_facts.ProfileFacts(interval=0)

    first = facts.borrow_options("work")
    after_first = len(calls)
    second = facts.borrow_options("work")

    assert after_first == 3  # one verdict per lender
    assert len(calls) == after_first  # the second read validated nothing
    assert second == first
    assert {item["name"] for item in first["options"]} == {"work", "ds4", "kimi"}


def test_the_row_is_rebuilt_when_the_interval_turns_over(home, monkeypatch, clock):
    """A token that appears is visible after one clock turn, without a restart.

    ``borrowing.validate`` documents itself as live validation: the detail
    rail must show a replaced or expired lender token. The snapshot keeps that
    promise bounded by its interval instead of by a request.
    """
    profile.create("work")
    lender = profile.create("ds4")
    facts = profile_facts.ProfileFacts(interval=5.0)

    row = facts.borrow_options("work")
    before = {item["name"]: item for item in row["options"]}["ds4"]
    assert before["status"] == "missing-token"
    assert before["selectable"] is False

    credentials.save_token(lender, "secret-never-serialize-me")
    assert facts.borrow_options("work")["options"] == row["options"]  # still stale

    clock[0] += 6.0
    facts.tick()
    after = {item["name"]: item for item in facts.borrow_options("work")["options"]}
    assert after["ds4"]["status"] == "ready"
    assert after["ds4"]["selectable"] is True
    assert "secret-never-serialize-me" not in json.dumps(after)


def test_invalidate_makes_the_next_read_see_the_config_as_it_is(home):
    """A write through the daemon must not wait for the clock (claunch-89eom)."""
    profile.create("work")
    profile.create("ds4")
    facts = profile_facts.ProfileFacts(interval=3600.0)
    assert {item["name"] for item in facts.borrow_options("work")["options"]} == {
        "work", "ds4",
    }

    profile.create("kimi")
    facts.invalidate()

    assert {item["name"] for item in facts.borrow_options("work")["options"]} == {
        "work", "ds4", "kimi",
    }


def test_manual_refresh_bypasses_the_interval(home):
    """``refresh=1`` on the endpoint is this path: rebuild now."""
    profile.create("work")
    profile.create("ds4")
    facts = profile_facts.ProfileFacts(interval=3600.0)
    facts.borrow_options("work")

    profile.create("kimi")
    refreshed = facts.borrow_options("work", refresh=True)

    assert "kimi" in {item["name"] for item in refreshed["options"]}


def test_the_session_detail_verdict_is_live(home, monkeypatch):
    """The rail's verdict is computed for that pair, not read from a row.

    Deleting a lender's credential has to turn the detail rail red on its next
    two-second poll (tests/test_reborrow.py pins the endpoint side of this),
    so this path stays live. What it no longer does is re-parse the config:
    the document comes from the facts (claunch-89eom).
    """
    profile.create("work")
    lender = profile.create("ds4")
    credentials.save_token(lender, "secret-never-serialize-me")
    facts = profile_facts.ProfileFacts(interval=3600.0)

    assert facts.borrow_validation("work", "ds4")["status"] == "ready"

    loads = []
    real_load = store.load
    monkeypatch.setattr(store, "load", lambda: loads.append(1) or real_load())

    (lender.config_dir / credentials.TOKEN_FILENAME).unlink()
    verdict = facts.borrow_validation("work", "ds4")

    assert verdict["status"] == "missing-token"
    assert loads == []  # the facts already hold the document


def test_a_lender_that_is_gone_is_still_answered_for_its_pair(home):
    """A deleted lender still gets a verdict about that exact pair.

    The row lists the profiles that exist; a session that borrowed from one
    since removed is not in it, so the question is asked directly instead of
    answered from a matrix that no longer mentions it.
    """
    profile.create("work")
    lender = profile.create("ds4")
    credentials.save_token(lender, "secret-never-serialize-me")
    facts = profile_facts.ProfileFacts(interval=3600.0)
    facts.borrow_options("work")

    profile.remove("ds4")
    facts.invalidate()

    verdict = facts.borrow_validation("work", "ds4")
    assert verdict is not None
    assert verdict["status"] == "missing-profile"


def test_the_endpoint_answers_from_the_snapshot(home, monkeypatch):
    """``/api/borrow-options`` twice costs one validation per lender.

    The panel opens per session, so this is the repeated read the issue names;
    the second request must be a dictionary lookup. An unknown profile is still
    refused by the request itself.
    """
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    profile.create("work")
    profile.create("ds4")
    store.set_daemon_field("profile_facts_s", 0)  # no thread: drive reads only
    calls = _count_validate(monkeypatch)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = TestClient(TestServer(build_app(
            mgr, "sekrit", started_at=time.monotonic(),
        )))
        await client.start_server()
        try:
            bearer = {"Authorization": "Bearer sekrit"}
            first = await (await client.get(
                "/api/borrow-options?profile=work", headers=bearer
            )).json()
            validated = len(calls)
            second = await (await client.get(
                "/api/borrow-options?profile=work", headers=bearer
            )).json()
            assert second == first
            assert len(calls) == validated  # no re-validation

            resp = await client.get(
                "/api/borrow-options?profile=no-such-profile", headers=bearer
            )
            assert resp.status == 400
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_validate_takes_the_loaded_document_and_stops_reparsing(home, monkeypatch):
    """``doc=`` is what makes one row build parse the config once."""
    profile.create("work")
    lender = profile.create("ds4")
    credentials.save_token(lender, "secret-never-serialize-me")
    doc = store.load()

    def refuse():
        raise AssertionError("validate re-read the config document")

    monkeypatch.setattr(store, "load", refuse)
    report = borrowing.validate(profile.require("work"), "ds4", doc=doc)

    assert report.status == "ready"


def test_a_failed_rebuild_keeps_the_last_value_in_place(home):
    """Every reader keeps being served the previous value while a build fails."""
    profile.create("work")
    facts = profile_facts.ProfileFacts(interval=0)
    assert facts.borrow_options("work")["profile"] == "work"

    def refuse():
        raise RuntimeError("config unreadable")

    snapshot = snapshots.PeriodicSnapshot(refuse, interval=0)
    with pytest.raises(RuntimeError):
        snapshot.refresh()
    # The failure lands on the error field; no half-built value is published.
    assert snapshot.peek() is None
    assert snapshot.error == "config unreadable"


def test_the_thread_interval_is_a_config_key(home):
    assert store.DAEMON_DEFAULTS["profile_facts_s"] > 0
    assert store.daemon_config()["profile_facts_s"] == store.DAEMON_DEFAULTS[
        "profile_facts_s"
    ]
