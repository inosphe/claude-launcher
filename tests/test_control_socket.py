"""The page's reads on a connection it already holds.

``/api/batch`` put the dashboard's tick on one request instead of nine, which
was not enough. A browser keeps a connection in its pool after the answer
arrives -- Firefox for 115 seconds by default -- so what counts against its
per-server ceiling is the high-water mark of the page's own concurrency, not
what is in flight. That was five against a ceiling of six: two for the read
budget, one for the liveness probe, one for the terminal socket and one for
the parked one. A second tab reached the ceiling, and there a terminal's
upgrade waited in the browser's connection queue until a pooled connection
expired, with nothing on this side to see, because a queued handshake is
never sent (claunch-riq5).

A socket does not have that shape. These pin what rides it: the same reads
the batch endpoint answers, answered by the same routes; a report of a socket
the client could not open at all, which is the failure no other record in
this daemon can hold; and the rules that keep one bad path from costing a
reader the others.
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import connections as conn_mod
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


async def _read(ws, paths, read_id=1):
    await ws.send_json({"type": "read", "id": read_id, "paths": paths})
    while True:
        frame = await asyncio.wait_for(ws.receive_json(), 5)
        if frame.get("type") == "read_result":
            return frame


def test_the_socket_answers_the_reads_a_batch_would(home, tmp_path):
    """Four reads on one socket, each answer under the path that asked, and
    the same routes answering as a direct read gets."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                hello = await asyncio.wait_for(ws.receive_json(), 5)
                assert hello == {"type": "init"}

                paths = ["/api/sessions", "/api/mesh?view=rail",
                         "/api/workspaces", "/api/cflow"]
                frame = await _read(ws, paths, read_id=7)
                assert frame["id"] == 7, "the caller's id comes back"
                assert frame["errors"] == {}
                assert sorted(frame["answers"]) == sorted(paths)

                direct = await (
                    await client.get("/api/sessions", headers=BEARER)
                ).json()
                assert frame["answers"]["/api/sessions"] == direct
        finally:
            await client.close()

    asyncio.run(run())


def test_several_reads_ride_one_socket_and_keep_their_ids(home, tmp_path):
    """The whole point of the connection: the tick after this one does not
    open anything, and an id says which answer belongs to which ask."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                first = await _read(ws, ["/api/sessions"], read_id=1)
                second = await _read(ws, ["/api/workspaces"], read_id=2)
                assert first["id"] == 1 and second["id"] == 2
                assert "/api/sessions" in first["answers"]
                assert "/api/workspaces" in second["answers"]
        finally:
            await client.close()

    asyncio.run(run())


def test_one_bad_path_does_not_cost_a_reader_the_others(home, tmp_path):
    """A tick asks for eight things. A route that is gone must fail alone."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                frame = await _read(ws, ["/api/sessions", "/api/nope"])
                assert "/api/sessions" in frame["answers"]
                assert "/api/nope" in frame["errors"]
                assert "/api/nope" not in frame["answers"]
        finally:
            await client.close()

    asyncio.run(run())


def test_only_this_daemons_api_paths_are_reachable(home, tmp_path):
    """The socket carries reads of this app's own routes, and nothing is a
    way to reach anything else it serves."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                frame = await _read(ws, ["/static/app.js", "/", "../etc"])
                assert frame["answers"] == {}
                assert len(frame["errors"]) == 3
                for why in frame["errors"].values():
                    assert "/api/" in why
        finally:
            await client.close()

    asyncio.run(run())


def test_a_read_that_is_too_large_is_refused_rather_than_walked(home, tmp_path):
    """The same cap the batch endpoint has, for the same reason: one frame
    must not be a way to make the daemon walk its whole API."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                frame = await _read(ws, ["/api/sessions"] * 25)
                assert frame["answers"] == {}
                assert "at most" in "".join(frame["errors"].values())

                empty = await _read(ws, [], read_id=2)
                assert empty["answers"] == {}
                assert empty["errors"]
        finally:
            await client.close()

    asyncio.run(run())


def test_a_ping_is_answered_and_a_malformed_frame_is_ignored(home, tmp_path):
    """The socket must survive anything a page sends it: a frame that is not
    JSON, or is JSON but not an object, cannot be a way to end the tick."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                await ws.send_str("not json at all")
                await ws.send_str("[1, 2, 3]")
                await ws.send_json({"type": "nonsense"})
                await ws.send_json({"type": "ping"})
                assert await asyncio.wait_for(ws.receive_json(), 5) == {"type": "pong"}

                # And it still reads afterwards.
                frame = await _read(ws, ["/api/sessions"])
                assert "/api/sessions" in frame["answers"]
        finally:
            await client.close()

    asyncio.run(run())


def test_the_socket_is_counted_as_a_connection_the_page_holds(home, tmp_path):
    """It is one of the six, so it must appear where the six are counted.
    A connection this daemon cannot see is how the original failure hid."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                snap = await (
                    await client.get("/api/connections", headers=BEARER)
                ).json()
                kinds = [row["kind"] for row in snap["open"]]
                assert "control" in kinds, kinds
        finally:
            await client.close()

    asyncio.run(run())


def test_a_socket_the_client_could_not_open_is_recorded(home, tmp_path):
    """The failure no other record here can hold.

    Every other record in this registry starts with a request arriving. The
    failure this endpoint was built for has the opposite shape: the upgrade
    never left the browser's connection queue, so there was nothing to open,
    close or refuse, and the daemon's view was indistinguishable from nobody
    having asked. The page says it instead, over the connection it does hold.
    """

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                await ws.send_json({
                    "type": "link_failed",
                    "session": "s578",
                    "code": 1006,
                    "reason": "",
                    "tries": 4,
                    "open_ms": None,
                })
                # Nothing answers a report, so read something else to know
                # the frame before it has been handled.
                await _read(ws, ["/api/sessions"])

                snap = await (
                    await client.get("/api/connections", headers=BEARER)
                ).json()
                assert snap["link_failures_count"] == 1
                row = snap["link_failures"][0]
                assert row["session"] == "s578"
                assert row["code"] == 1006
                assert row["tries"] == 4
                assert row["open_ms"] is None, "it never reached OPEN"
                assert row["peer_port"], "dated against the rest of the log"
                assert row["at"]
        finally:
            await client.close()

    asyncio.run(run())


def test_a_report_cannot_be_made_to_carry_something_large(home, tmp_path):
    """It is a client's account, so it is stored as one -- bounded, and with
    nothing taken on trust. A page that reported a novel would otherwise be
    a way to grow the daemon's memory from the browser."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                await ws.send_json({
                    "type": "link_failed",
                    "session": "s" * 500,
                    "reason": "x" * 5000,
                    "code": "not an int",
                    "tries": {"nested": True},
                })
                await _read(ws, ["/api/sessions"])
                snap = await (
                    await client.get("/api/connections", headers=BEARER)
                ).json()
                row = snap["link_failures"][0]
                assert len(row["session"]) == 64
                assert len(row["reason"]) == 200
                assert row["code"] is None, "a non-integer code is dropped"
                assert row["tries"] is None
        finally:
            await client.close()

    asyncio.run(run())


def test_the_failure_log_is_bounded(home, tmp_path):
    """A page that cannot get a socket retries for as long as a person leaves
    it open. What is useful is the shape of the run, not its length."""

    registry = conn_mod.Registry()

    class _Req:
        headers = {"User-Agent": "Mozilla/5.0 Firefox/155.0"}
        transport = None

        @property
        def remote(self):
            return "127.0.0.1"

    for i in range(conn_mod.LINK_FAILURES_KEEP + 25):
        registry.link_failed(_Req(), {"session": f"s{i}", "code": 1006})
    snap = registry.snapshot()
    assert snap["link_failures_count"] == conn_mod.LINK_FAILURES_KEEP
    assert snap["link_failures"][0]["session"] == f"s{conn_mod.LINK_FAILURES_KEEP + 24}"


def test_an_unauthenticated_upgrade_is_refused_and_recorded(home, tmp_path):
    """The socket is behind the same middleware as the rest of /api/, and a
    refused upgrade leaves the record that the whole registry exists for."""

    async def run():
        client = await _serve(tmp_path)
        try:
            resp = await client.get(
                "/api/control/ws",
                headers={"Upgrade": "websocket", "Connection": "Upgrade",
                         "Sec-WebSocket-Version": "13",
                         "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="},
            )
            assert resp.status == 401
            snap = await (
                await client.get("/api/connections", headers=BEARER)
            ).json()
            refused = [r for r in snap["refused"] if r["path"] == "/api/control/ws"]
            assert refused, snap["refused"]
            assert refused[0]["upgrade"] is True
        finally:
            await client.close()

    asyncio.run(run())


def test_a_refused_read_keeps_its_route_status_and_message(home, tmp_path):
    """The socket reports a route's own error text and status, the same as
    ``/api/batch`` does: the page branches on the status (claunch-authx)."""

    async def run():
        client = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive_json()
                frame = await _read(ws, ["/api/sessions/nope/briefing"])
                why = frame["errors"]["/api/sessions/nope/briefing"]
                assert why == "no session named 'nope'"
                assert frame["statuses"]["/api/sessions/nope/briefing"] == 404
        finally:
            await client.close()

    asyncio.run(run())
