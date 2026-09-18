"""The connection an asset was served on, returned when the asset is.

A browser holds a connection open after the answer arrives -- Firefox for
115 seconds by default -- and counts it against a per-server ceiling of six.
The dashboard asks for twelve assets on a load, so the load by itself fills
that pool, and the terminal's WebSocket upgrade that follows waits in the
browser's own connection queue, never sent, until an idle connection
expires. Nothing arrives here to record, which is why three rounds of this
failure had only a person's account to go on (claunch-6j09).

Closing each asset's connection with its response returns the slot at
delivery instead of two minutes later. These pin that, and pin where it
stops: the API keeps its connections, because the reads that use them are
budgeted elsewhere and a socket per read is the thing being avoided.

The header is the signal, not the contract -- a test that only reads
``Connection: close`` would pass against a server that sent it and then held
the socket anyway. So these speak HTTP over a raw socket and watch what the
server does with it.
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

#: Long enough that a server which intends to keep the connection has
#: finished answering, short enough that a whole suite is not paying for it.
LINGER = 1.0


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _serve(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _raw_get(port: int, path: str, *, auth: bool = False) -> tuple[bytes, bool]:
    """One HTTP/1.1 GET on a socket of our own, kept open afterwards.

    Returns the response head and whether the server closed the connection,
    which is the question these tests are actually asking.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"GET {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: keep-alive"]
    if auth:
        lines.append("Authorization: Bearer sekrit")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()

    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    # Drain the body, then see whether the peer goes away on its own. EOF
    # here is the server closing; a timeout is the server holding.
    try:
        await asyncio.wait_for(reader.read(), LINGER)
        closed = reader.at_eof()
    except asyncio.TimeoutError:
        closed = False
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, OSError):
        pass
    return head, closed


def test_an_asset_does_not_leave_its_connection_in_the_pool(home, tmp_path):
    """The load is what fills the browser's pool, so the load is where the
    connections have to come back."""

    async def run():
        client = await _serve(build_app(_manager(), "sekrit", started_at=time.monotonic()))
        try:
            port = client.server.port
            for path in ("/", "/static/app.js", "/static/style.css"):
                head, closed = await _raw_get(port, path)
                assert b" 200 " in head, (path, head[:80])
                assert b"connection: close" in head.lower(), (path, head[:200])
                assert closed, f"{path} left its connection open"
        finally:
            await client.close()

    asyncio.run(run())


def test_the_api_keeps_its_connections(home, tmp_path):
    """Where this stops. The page's reads ride one socket precisely so they
    do not spend a connection each; closing their connections would undo
    that and make every read pay a handshake."""

    async def run():
        client = await _serve(build_app(_manager(), "sekrit", started_at=time.monotonic()))
        try:
            port = client.server.port
            head, closed = await _raw_get(port, "/api/health", auth=True)
            assert b" 200 " in head, head[:80]
            assert b"connection: close" not in head.lower(), head[:200]
            assert not closed, "an API response gave up its connection"
        finally:
            await client.close()

    asyncio.run(run())


def test_releasing_the_connection_did_not_cost_the_revalidation(home, tmp_path):
    """The same middleware carries both, and the older one is what keeps an
    upgraded daemon from serving the UI it just replaced."""

    async def run():
        client = await _serve(build_app(_manager(), "sekrit", started_at=time.monotonic()))
        try:
            for path in ("/", "/static/app.js", "/static/style.css"):
                resp = await client.get(path)
                assert resp.status == 200, path
                assert "no-cache" in resp.headers.get("Cache-Control", ""), path
                if path != "/":
                    assert resp.headers.get("ETag") or resp.headers.get(
                        "Last-Modified"
                    ), path
                await resp.read()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_page_load_ends_holding_nothing(home, tmp_path):
    """The whole point, at the size the failure has.

    Twelve assets against a ceiling of six is what the dashboard asks for,
    and the terminal's upgrade comes after all of them. This walks that
    load and checks the far side: no connection from it is still held when
    the last asset has arrived.
    """

    async def run():
        client = await _serve(build_app(_manager(), "sekrit", started_at=time.monotonic()))
        try:
            port = client.server.port
            load = [
                "/",
                "/static/vendor/xterm.css",
                "/static/style.css",
                "/static/observer.css",
                "/static/vendor/xterm.js",
                "/static/vendor/addon-fit.js",
                "/static/observer.js",
                "/static/diagram-viewport.js",
                "/static/app.js",
            ]
            results = await asyncio.gather(*(_raw_get(port, p) for p in load))
            held = [p for p, (_, closed) in zip(load, results) if not closed]
            assert not held, f"still holding after the load: {held}"
        finally:
            await client.close()

    asyncio.run(run())
