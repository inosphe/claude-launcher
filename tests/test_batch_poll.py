"""Several reads on one connection, and who decides which ones.

A browser holds a small number of connections to one server (Firefox's
default is six) and the dashboard polled nine paths at once, which took all
six. A WebSocket needs a connection of its own, so its handshake was queued
behind the polls and never sent: the terminal did not come up and the daemon
never saw a request to refuse. Measured as ``firefox 6`` in
``/api/connections`` while exactly one socket was open
(claunch-restart-disconnect-banner-12p2).

The endpoint under test carries the reads the caller names. The point of
these is that it stays a matter of transport: the same routes answer, the
caller keeps its own cadence for each read, and one bad path does not cost a
reader the others.
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}


async def _serve(tmp_path):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_a_batch_answers_every_path_it_was_given(home, tmp_path):
    """Four reads, one request, and each answer under the path that asked."""

    async def run():
        client = await _serve(tmp_path)
        try:
            paths = ["/api/sessions", "/api/mesh?view=rail", "/api/workspaces",
                     "/api/cflow"]
            resp = await client.post(
                "/api/batch", json={"paths": paths}, headers=BEARER
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["errors"] == {}
            assert sorted(body["answers"]) == sorted(paths)

            # The same route answering, so a batched read and a direct one
            # cannot drift: compare one of them against its own endpoint.
            direct = await (await client.get("/api/sessions", headers=BEARER)).json()
            assert body["answers"]["/api/sessions"] == direct
        finally:
            await client.close()

    asyncio.run(run())


def test_the_caller_chooses_the_paths_so_cadence_stays_its_own(home, tmp_path):
    """Nothing here decides how often anything is read. A caller that wants
    one read this tick sends one, which is how a part with a slower period
    stays slow without a second connection."""

    async def run():
        client = await _serve(tmp_path)
        try:
            one = await (
                await client.post(
                    "/api/batch", json={"paths": ["/api/workspaces"]}, headers=BEARER
                )
            ).json()
            assert list(one["answers"]) == ["/api/workspaces"]

            # A query string belongs to the path that carries it, so two reads
            # of one route with different parameters are two entries.
            two = await (
                await client.post(
                    "/api/batch",
                    json={"paths": ["/api/mesh?view=rail", "/api/mesh"]},
                    headers=BEARER,
                )
            ).json()
            assert sorted(two["answers"]) == ["/api/mesh", "/api/mesh?view=rail"]
        finally:
            await client.close()

    asyncio.run(run())


def test_one_bad_path_is_reported_and_the_others_still_answer(home, tmp_path):
    """A batch is a convenience of transport. A path that cannot be served
    must not cost the reader the ones that can."""

    async def run():
        client = await _serve(tmp_path)
        try:
            body = await (
                await client.post(
                    "/api/batch",
                    json={"paths": [
                        "/api/sessions",
                        "/api/not-a-route",
                        "https://example.com/api/sessions",
                        "/api/batch",
                    ]},
                    headers=BEARER,
                )
            ).json()
            assert list(body["answers"]) == ["/api/sessions"]
            assert "/api/not-a-route" in body["errors"]
            assert "only this daemon's /api/ paths" in str(
                body["errors"].get("https://example.com/api/sessions")
            )
            assert "a batch cannot carry a batch" in body["errors"]["/api/batch"]
        finally:
            await client.close()

    asyncio.run(run())


def test_a_batch_is_bounded_and_needs_a_list(home, tmp_path):
    """One request must not be made to walk the whole API."""

    async def run():
        client = await _serve(tmp_path)
        try:
            for bad in ({}, {"paths": []}, {"paths": "/api/sessions"}):
                assert (
                    await client.post("/api/batch", json=bad, headers=BEARER)
                ).status == 400
            too_many = {"paths": ["/api/sessions"] * (api_mod.BATCH_MAX + 1)}
            resp = await client.post("/api/batch", json=too_many, headers=BEARER)
            assert resp.status == 400
            assert str(api_mod.BATCH_MAX) in (await resp.json())["error"]
        finally:
            await client.close()

    asyncio.run(run())


def test_a_batch_needs_a_credential(home, tmp_path):
    """It reaches every read the caller could make directly, so it is behind
    the same door as all of them."""

    async def run():
        client = await _serve(tmp_path)
        try:
            resp = await client.post("/api/batch", json={"paths": ["/api/sessions"]})
            assert resp.status == 401
        finally:
            await client.close()

    asyncio.run(run())


def test_a_batch_costs_one_connection(home, tmp_path):
    """The property the change exists for, read off the daemon's own count:
    four reads that would have taken four connections take one."""

    async def run():
        client = await _serve(tmp_path)
        try:
            await client.post(
                "/api/batch",
                json={"paths": ["/api/sessions", "/api/mesh", "/api/workspaces",
                                "/api/cflow"]},
                headers=BEARER,
            )
            reading = await (await client.get("/api/connections", headers=BEARER)).json()
            batched = [r for r in reading["requests"] if r["path"] == "/api/batch"]
            assert len(batched) == 1
            # The reads it carried are not requests of their own: they never
            # touched a connection, which is the whole point.
            assert not [r for r in reading["requests"] if r["path"] == "/api/mesh"]
        finally:
            await client.close()

    asyncio.run(run())


def test_a_refused_read_keeps_its_route_status_and_message(home, tmp_path, monkeypatch):
    """A route that answers with ``json_error`` must reach the page as that
    status and that message. The batch used to raise a bare HTTPException,
    whose status is -1, so a briefing whose LLM call failed showed "-1 502"
    and a missing session showed as a failure instead of "no record"
    (claunch-authx)."""

    async def failing_briefing(request):
        return api_mod.json_error(502, "llm endpoint answered 502: bad gateway")

    monkeypatch.setattr(api_mod, "h_session_briefing", failing_briefing)

    async def run():
        client = await _serve(tmp_path)
        try:
            body = await (
                await client.post(
                    "/api/batch",
                    json={"paths": [
                        "/api/sessions/s1/briefing",
                        "/api/sessions/nope/status-checks",
                        "/api/not-a-route",
                    ]},
                    headers=BEARER,
                )
            ).json()
            assert body["errors"]["/api/sessions/s1/briefing"] == (
                "llm endpoint answered 502: bad gateway"
            )
            assert body["statuses"]["/api/sessions/s1/briefing"] == 502
            assert body["errors"]["/api/sessions/nope/status-checks"] == (
                "no session named 'nope'"
            )
            assert body["statuses"]["/api/sessions/nope/status-checks"] == 404
            assert body["statuses"]["/api/not-a-route"] == 404
            assert all("-1" not in why for why in body["errors"].values())
        finally:
            await client.close()

    asyncio.run(run())
