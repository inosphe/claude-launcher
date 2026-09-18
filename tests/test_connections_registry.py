"""The connection registry, and what ``/api/connections`` makes of it.

The reading exists to answer one question the daemon log could not: how many
sockets are open right now, and from where. A close line is written after the
fact and a refused upgrade writes nothing at all, so a viewer that cannot get
a socket up left the log looking quiet
(claunch-restart-disconnect-banner-12p2).

These tests stand on the registry rather than on a live daemon: the counts,
the per-peer grouping and the bounded closed half are the properties a reader
of the endpoint depends on.
"""

from __future__ import annotations

from claude_launcher.daemon import connections


class _Transport:
    def __init__(self, peer):
        self._peer = peer

    def get_extra_info(self, key, default=None):
        return self._peer if key == "peername" else default


class _Request:
    """Enough of an aiohttp request for the registry: peer, headers, query."""

    def __init__(self, peer=("127.0.0.1", 51000), query=None, agent="Firefox/128"):
        self.transport = _Transport(peer)
        self.headers = {"User-Agent": agent, "Origin": "http://127.0.0.1:8378"}
        self.query = query or {}


def test_an_open_socket_is_counted_with_its_peer():
    registry = connections.Registry()
    registry.opened("terminal", "s584", _Request(peer=("127.0.0.1", 51000)))
    registry.opened("terminal", "s586", _Request(peer=("127.0.0.1", 51001)))
    registry.opened("terminal", "s584", _Request(peer=("10.0.0.5", 4000)))

    snapshot = registry.snapshot()
    assert snapshot["open_count"] == 3
    assert snapshot["by_peer"] == {"127.0.0.1": 2, "10.0.0.5": 1}
    assert snapshot["by_session"] == {"s584": 2, "s586": 1}
    row = snapshot["open"][0]
    assert row["session"] == "s584"
    assert row["peer_port"] == 51000
    assert row["user_agent"] == "Firefox/128"
    assert isinstance(row["age_s"], float)


def test_a_closed_socket_leaves_the_open_count_and_keeps_its_reason():
    registry = connections.Registry()
    record = registry.opened("terminal", "s584", _Request())
    registry.closed(record, 1006, TimeoutError("No PONG received after 30.0 seconds"))

    snapshot = registry.snapshot()
    assert snapshot["open_count"] == 0
    assert snapshot["by_peer"] == {}
    closed = snapshot["closed"][0]
    assert closed["session"] == "s584"
    assert closed["code"] == 1006
    assert "No PONG" in closed["error"]
    assert closed["age_s"] >= 0


def test_the_closed_half_stays_bounded():
    registry = connections.Registry()
    for i in range(connections.CLOSED_KEEP + 25):
        registry.closed(registry.opened("terminal", f"s{i}", _Request()), 1000, None)

    snapshot = registry.snapshot()
    assert len(snapshot["closed"]) == connections.CLOSED_KEEP
    # Newest first, so a reader sees the most recent closure without paging.
    assert snapshot["closed"][0]["session"] == f"s{connections.CLOSED_KEEP + 24}"


def test_a_transport_without_a_peer_name_still_records():
    # The in-memory transports tests and Unix sockets use have no peer name.
    # A missing address must not fail an upgrade.
    registry = connections.Registry()
    registry.opened("cli", "(cli shell)", _Request(peer=None))
    row = registry.snapshot()["open"][0]
    assert row["peer_ip"] == "?"
    assert row["peer_port"] is None


def test_an_unknown_http_count_is_reported_as_unknown():
    # Tests build the app with no runner, so aiohttp's own count is absent.
    # None and 0 mean different things and the reading keeps them apart.
    registry = connections.Registry()
    assert registry.snapshot()["http_connections"] is None
    assert registry.snapshot(4)["http_connections"] == 4


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #
def test_the_endpoint_needs_a_credential_and_then_reports(home, tmp_path):
    """``/api/connections`` sits behind the same auth as the rest of /api/.

    It names sessions and remote addresses, so an open reading of it would
    hand out more than ``/api/health`` deliberately does.
    """
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            assert (await client.get("/api/connections")).status == 401

            resp = await client.get(
                "/api/connections", headers={"Authorization": "Bearer sekrit"}
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["open_count"] == 0
            assert body["open"] == []
            assert body["by_peer"] == {}
            # No runner in a test app, so aiohttp's own count is not known.
            assert body["http_connections"] is None
            assert body["closed_kept"] == connections.CLOSED_KEEP
        finally:
            await client.close()

    asyncio.run(run())


def test_live_sockets_appear_in_the_reading_and_leave_it_on_close(home, tmp_path):
    """The property the whole surface exists for: while a viewer holds a
    socket the count includes it, and the moment it closes the reading says
    so with the code it closed on."""
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from test_daemon_e2e import _register_py_harness, _wait_screen

    _register_py_harness()
    auth = {"Authorization": "Bearer sekrit"}

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            session = mgr.create(SessionDef(name="cx1", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            first = await client.ws_connect("/api/sessions/cx1/ws", headers=auth)
            second = await client.ws_connect("/api/sessions/cx1/ws?scrollback=1", headers=auth)
            await asyncio.sleep(0.2)

            body = await (await client.get("/api/connections", headers=auth)).json()
            assert body["open_count"] == 2, body
            assert body["by_session"] == {"cx1": 2}
            assert all(row["kind"] == "terminal" for row in body["open"])
            # The query a viewer asked for rides along, because two sockets on
            # one session differ by exactly that.
            assert {"scrollback": "1"} in [row["query"] for row in body["open"]]

            await first.close()
            await second.close()
            await asyncio.sleep(0.2)

            body = await (await client.get("/api/connections", headers=auth)).json()
            assert body["open_count"] == 0, body
            assert [row["session"] for row in body["closed"][:2]] == ["cx1", "cx1"]
            assert body["closed"][0]["code"] is not None
        finally:
            await client.close()
            for name in list(mgr._sessions):
                mgr.kill(name)

    asyncio.run(run())
