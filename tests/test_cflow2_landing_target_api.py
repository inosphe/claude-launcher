"""``GET /api/cflow2/landing-target`` -- the probe's server half.

The gate itself (``claunch gate landing-target``) holds no routing logic: it
asks this endpoint, which runs ``landing_route.Router`` -- the same code the
push clock runs -- so "somebody can take this request" and "the push found
somebody" cannot be two different answers.

What is pinned here is the wiring that keeps them the same answer:

* the endpoint reads ``cwds`` the way the trigger path reads it
  (``list(clock._cwds) or clock.cwds()``). A probe measuring a different set
  of directories could say "there is a target" where the push then finds
  none, and the whole point of asking first is to be right about that;
* the direction comes from the run's ``inputs.relation``, not an argument --
  the value ``integration-request``'s triggers branch on;
* a relation that is neither of the two pushes is "could not tell", never
  "nobody".
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher.daemon import landing_route
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _client(app):
    from aiohttp.test_utils import TestClient, TestServer

    return TestClient(TestServer(app))


def _patch_holders(monkeypatch, holder_owner="p1"):
    """A ``landing_holders`` that gives ``holder_owner`` a v2 landing run."""
    from claude_launcher.daemon import cflow2_triggers

    def fake(manager, cwds):
        def holders(session, repo):
            if session == holder_owner:
                return [landing_route.Holder("/repo", "main", True, "v2")]
            return []
        return holders

    monkeypatch.setattr(cflow2_triggers, "landing_holders", fake)


@pytest.mark.parametrize("relation", ["stacked", "independent"])
def test_the_endpoint_answers_with_the_routers_own_verdict(home, monkeypatch, relation):
    """No clock and no mesh in this app, so the walk finds the parent (stacked)
    or the leader fallback (independent) -- and either way the answer is a
    dict the probe can turn into an exit code."""
    _patch_holders(monkeypatch)

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = _client(app)
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/cflow2/landing-target?cwd=/repo&scope=w1&relation={relation}",
                headers=AUTH,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert set(doc) >= {"owner", "holder", "why", "passed", "refused",
                                "undecidable", "relation"}
            assert doc["relation"] == relation
            # no mesh, no parent: nobody, and definitely so
            assert doc["holder"] is None
            assert doc["undecidable"] is False, doc["why"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_an_unknown_relation_is_cannot_tell(home, monkeypatch):
    """A run whose relation is missing or misspelled gets no verdict: the
    endpoint does not guess which push it would have been."""
    _patch_holders(monkeypatch)

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = _client(app)
        await client.start_server()
        try:
            resp = await client.get(
                "/api/cflow2/landing-target?cwd=/repo&scope=w1&relation=", headers=AUTH)
            doc = await resp.json()
            assert doc["holder"] is None
            assert doc["undecidable"] is True, doc
            assert "relation" in doc["why"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_relation_is_read_from_the_run_when_not_given(home, monkeypatch, tmp_path):
    """The agent must not have to pass the direction: it is a start input the
    push already branches on. An unreadable run (no run here) is the empty
    relation, which the endpoint reports as cannot-tell rather than
    assuming one."""
    _patch_holders(monkeypatch)

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = _client(app)
        await client.start_server()
        try:
            resp = await client.get(
                f"/api/cflow2/landing-target?cwd={tmp_path}&scope=w1", headers=AUTH)
            doc = await resp.json()
            assert doc["undecidable"] is True
            # ...and the same run WITH a relation answers definitely
            resp = await client.get(
                f"/api/cflow2/landing-target?cwd={tmp_path}&scope=w1&relation=stacked",
                headers=AUTH)
            doc = await resp.json()
            assert doc["undecidable"] is False
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_probe_and_the_trigger_read_the_same_directories(home, monkeypatch):
    """The handler's own reason for existing (claunch-od1mx.1): the probe
    must measure what the push will measure.

    ``Cflow2Clock.run_triggers`` passes ``list(self._cwds) or self.cwds()``.
    This pins that the endpoint reads the clock the same way -- the last
    scan's directories when there is one, and a fresh walk only when there is
    not. A handler that always re-walked, or always used the cached list,
    would answer about a different set in exactly the window where sessions
    are being created or retired.
    """
    _patch_holders(monkeypatch)
    walked = []
    cached = ["/cached-a", "/cached-b"]

    class _Clock:
        _cwds = list(cached)

        def trigger_cwds(self):
            # the method the trigger path itself calls (cflow2_clock)
            return list(self._cwds) if self._cwds else self._walk()

        def _walk(self):
            walked.append(1)
            return ["/fresh"]

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        app["cflow2_clock"] = _Clock()
        client = _client(app)
        await client.start_server()
        seen = {}
        from claude_launcher.daemon import api

        real = api._landing_target

        def spy(manager, mesh, cwds, cwd, scope, relation):
            seen["cwds"] = list(cwds)
            return real(manager, mesh, cwds, cwd, scope, relation)

        monkeypatch.setattr(api, "_landing_target", spy)
        try:
            await client.get(
                "/api/cflow2/landing-target?cwd=/repo&scope=w1&relation=stacked",
                headers=AUTH)
            assert seen["cwds"] == cached, seen
            assert not walked, "a fresh walk ran while the clock had a scan's list"
            # ...and with no scan yet, it walks
            _Clock._cwds = []
            await client.get(
                "/api/cflow2/landing-target?cwd=/repo&scope=w1&relation=stacked",
                headers=AUTH)
            assert seen["cwds"] == ["/fresh"], seen
            assert walked
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
