"""Remote-shadow sessions (daemon/shadow.py).

A. wire pieces (pure)
   A1 frames round-trip through FrameReader across arbitrary chunk splits
   A2 ResponseStream reads a chunked response head and body incrementally,
      and passes a plain body through
   A3 the viewer re-cuts a host's card to the whitelist (extra keys and
      wrong types dropped)
   A4 cflow_position keeps a position and drops idle runs and free text
   A5 daemon.shadow_input: on unless set false
   A6 HostSocket: writes frame, never yields a message, ends on close
   A7 the live relay bridge queues chunks, ends on EOF and on overflow

B. mesh scope (two daemons, in-process peer transport)
   B1 each daemon's shadow targets are the OTHER daemon's members only
   B2 the host admits a shadow call only with the link token and only for a
      member of that mesh
   B3 a viewer cannot route to a session that is in no shared mesh

C. through HTTP (two apps, raw request/response bytes like the relay's)
   C1 /api/shadows lists the remote member with its card; local ones absent
   C2 the terminal streams init + repaint, and bytes a viewer sends over
      the shadow socket never reach the session
   C3 the session line types into the remote session, journaled with the
      viewer as origin; a key name is typed as text
   C4 the host refuses the session line when daemon.shadow_input is off,
      and refuses a bad token with 403
   C5 the peer route table has exactly the three shadow routes
   C6 a viewer that leaves releases the host's attachment within seconds,
      also when the session writes nothing after it left
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp import WSMsgType

from claude_launcher import store
from claude_launcher.daemon import peer_client, session_input, shadow
from claude_launcher.daemon import relay_uplink
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.mesh import MeshError, MeshManager

from test_mesh_ops import _linked_pair, _manager, _register_py_harness


# --------------------------------------------------------------------------- #
# A. wire pieces
# --------------------------------------------------------------------------- #
def test_frames_round_trip_across_splits():
    blob = (
        shadow.pack(shadow.FRAME_TEXT, b'{"type":"init"}')
        + shadow.pack(shadow.FRAME_BYTES, b"\x1b[2Jhello")
        + shadow.pack(shadow.FRAME_KEEPALIVE)
    )
    for step in (1, 2, 3, 7, len(blob)):
        reader = shadow.FrameReader()
        got = []
        for i in range(0, len(blob), step):
            got.extend(reader.feed(blob[i:i + step]))
        assert got == [
            (shadow.FRAME_TEXT, b'{"type":"init"}'),
            (shadow.FRAME_BYTES, b"\x1b[2Jhello"),
            (shadow.FRAME_KEEPALIVE, b""),
        ]


def test_frame_reader_refuses_a_huge_frame():
    reader = shadow.FrameReader()
    with pytest.raises(peer_client.PeerHttpError, match="too large"):
        reader.feed(bytes([1]) + (shadow.MAX_FRAME + 1).to_bytes(4, "big"))


def test_response_stream_chunked_incremental():
    body = b"hello world, streamed"
    raw = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
        b"Content-Type: x\r\n\r\n"
        + b"5\r\nhello\r\n" + b"10\r\n world, streamed\r\n" + b"0\r\n\r\n"
    )
    for step in (1, 4, 9, len(raw)):
        rs = peer_client.ResponseStream()
        out = b""
        for i in range(0, len(raw), step):
            out += rs.feed(raw[i:i + step])
        assert rs.status == 200 and rs.head_done
        assert rs.headers["content-type"] == "x"
        assert out == body
        assert rs.finished


def test_response_stream_plain_body_passes_through():
    rs = peer_client.ResponseStream()
    assert rs.feed(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 13\r\n") == b""
    assert not rs.head_done
    assert rs.feed(b'\r\n{"error":"x"}') == b'{"error":"x"}'
    assert rs.status == 403


def test_viewer_recuts_the_card():
    row = {
        "session": "s1", "handle": "s1", "role": "worker", "roles": ["worker", 3],
        "harness": "claude", "status": "idle", "exited": 0, "note": "hi",
        "cwd": "/secret", "args": ["--x"], "pid": 42,
        "briefing": {"one_line": "does x", "state": "busy", "evil": "y", "goal": 5},
        "cflow": {"workflow": "w", "step_id": "work", "visit": 2,
                  "journal": ["..."], "instructions": "long", "reason": True},
    }
    card = shadow._sanitize_card(row)
    assert card == {
        "session": "s1", "handle": "s1", "role": "worker", "harness": "claude",
        "status": "idle", "note": "hi", "roles": ["worker"], "exited": False,
        "briefing": {"one_line": "does x", "state": "busy"},
        "cflow": {"workflow": "w", "step_id": "work", "visit": 2},
    }
    assert shadow._sanitize_card(None) is None
    assert shadow._sanitize_card("x") is None


def test_cflow_position():
    assert shadow.cflow_position(None) is None
    assert shadow.cflow_position({"status": "idle"}) is None
    assert shadow.cflow_position({"status": "error", "error": "x"}) is None
    got = shadow.cflow_position({
        "status": "waiting_approval", "workflow": "improv-worker", "run": "run-1",
        "step_id": "review", "title": "Review", "visit": 1, "reason": "gate",
        "instructions": "never shipped", "journal": [], "cwd": "/x",
    })
    assert got == {
        "status": "waiting_approval", "workflow": "improv-worker", "run": "run-1",
        "step_id": "review", "title": "Review", "visit": 1, "reason": "gate",
    }


def test_input_enabled():
    assert shadow.input_enabled(None) is True
    assert shadow.input_enabled({}) is True
    assert shadow.input_enabled({"shadow_input": True}) is True
    assert shadow.input_enabled({"shadow_input": False}) is False
    assert shadow.input_enabled({"shadow_input": "off"}) is False
    assert store.DAEMON_DEFAULTS["shadow_input"] is True


def test_host_socket_writes_frames_and_takes_no_input():
    class Resp:
        def __init__(self):
            self.out = b""
            self.fail = False

        async def write(self, data):
            if self.fail:
                raise ConnectionResetError("gone")
            self.out += data

    async def run():
        resp = Resp()
        sock = shadow.HostSocket(resp)
        await sock.send_str('{"type":"init"}')
        await sock.send_bytes(b"abc")
        await sock.keepalive()
        frames = shadow.FrameReader().feed(resp.out)
        assert frames == [
            (shadow.FRAME_TEXT, b'{"type":"init"}'),
            (shadow.FRAME_BYTES, b"abc"),
            (shadow.FRAME_KEEPALIVE, b""),
        ]
        # the receive side blocks until the socket ends, and then yields
        # nothing at all -- there is no message to take
        got = []

        async def drain():
            async for msg in sock:
                got.append(msg)

        task = asyncio.ensure_future(drain())
        await asyncio.sleep(0.05)
        assert not task.done()
        await sock.close()
        await asyncio.wait_for(task, 1)
        assert got == [] and sock.closed and sock.close_code == 1000
        # a failed write ends it as a dropped viewer
        resp2 = Resp()
        resp2.fail = True
        sock2 = shadow.HostSocket(resp2)
        await sock2.send_bytes(b"x")
        assert sock2.closed and sock2.close_code == 1006
        assert isinstance(sock2.exception(), ConnectionResetError)

    asyncio.run(run())


def test_live_bridge_queue_eof_and_overflow(monkeypatch):
    class Up:
        def __init__(self):
            self.ended = []

        async def _end_peer_stream(self, sid):
            self.ended.append(sid)

    async def run():
        ps = relay_uplink._PeerStream(7, live=True)
        up = Up()
        bridge = relay_uplink.PeerBridge(up, ps)
        ps.feed(b"a")
        ps.feed(b"bc")
        assert await bridge.read() == b"a"
        assert await bridge.read() == b"bc"
        ps.finish()
        assert await bridge.read() is None
        await bridge.close()
        await bridge.close()
        assert up.ended == [7]
        # a reader that falls too far behind loses the stream, never the heap
        monkeypatch.setattr(relay_uplink, "LIVE_BUFFER_MAX", 4)
        ps2 = relay_uplink._PeerStream(8, live=True)
        bridge2 = relay_uplink.PeerBridge(up, ps2)
        ps2.feed(b"abc")
        ps2.feed(b"de")
        assert bridge2.overflowed
        assert await bridge2.read() == b"abc"
        assert await bridge2.read() is None
        # the default mode still collects the whole response
        ps3 = relay_uplink._PeerStream(9)
        ps3.feed(b"x")
        ps3.finish()
        assert ps3.chunks == [b"x"] and ps3.done.is_set()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# B. mesh scope
# --------------------------------------------------------------------------- #
def test_targets_are_the_other_daemons_members(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        a = mm_a.shadow_targets()
        b = mm_b.shadow_targets()
        assert [(t["machine"], t["session"]) for t in a] == [("pcB", "sb")]
        assert [(t["machine"], t["session"]) for t in b] == [("pcA", "sa")]
        assert a[0]["meshes"][0]["handle"] == "bob"
        assert a[0]["meshes"][0]["linked"] is True
        # the authority's own member on the mirror resolves to the authority
        mesh_b = mm_b.get("m@pcA")
        assert mm_b.host_machine(mesh_b, mesh_b.members["alice"]) == "pcA"
        assert mm_b.host_machine(mesh_b, mesh_b.members["bob"]) == ""
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_host_admits_only_linked_members(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        good = mm_a.get("m").links["pcB"]["token_in"]
        with pytest.raises(MeshError, match="bad mesh peer token"):
            mm_a.peer_shadow_member("m", "pcB", "nope", "sa")
        with pytest.raises(MeshError, match="bad mesh peer token"):
            mm_a.peer_shadow_members("m", "pcB", "")
        # sx runs on pcA but is in no mesh: not visible through m
        with pytest.raises(MeshError, match="not a member"):
            mm_a.peer_shadow_member("m", "pcB", good, "sx")
        # sb is a member of m, but it is pcB's own, not pcA's
        with pytest.raises(MeshError, match="not a member"):
            mm_a.peer_shadow_member("m", "pcB", good, "sb")
        assert mm_a.peer_shadow_member("m", "pcB", good, "sa").handle == "alice"
        assert [m.handle for m in mm_a.peer_shadow_members("m", "pcB", good)] == ["alice"]
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_viewer_routes_only_to_mesh_members(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        mesh, member = mm_b.shadow_route("pcA", "sa")
        assert mesh.name == "m@pcA" and member.handle == "alice"
        with pytest.raises(MeshError, match="not a member of any mesh"):
            mm_b.shadow_route("pcA", "sx")
        with pytest.raises(MeshError, match="not a member of any mesh"):
            mm_b.shadow_route("pcZ", "sa")
        n = len(calls)
        with pytest.raises(MeshError):
            await mm_b.shadow_call("pcA", "sx", "/peer/shadow/keys", {})
        assert len(calls) == n  # refused before anything went out
        # without a link to the host there is nobody to ask
        del mesh.links["pcA"]
        with pytest.raises(MeshError, match="no link"):
            mm_b.shadow_route("pcA", "sa")
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C. through HTTP
# --------------------------------------------------------------------------- #
class _RawBridge:
    """A bridge over plain TCP: what the relay hands a PEER_OPEN caller."""

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    async def read(self):
        chunk = await self._reader.read(65536)
        return chunk or None

    async def close(self):
        self._writer.close()


def _raw_wiring(ports: dict):
    """peer_transport/peer_streamer that send the relay's raw bytes over TCP
    to each daemon's loopback server, exactly what the uplink pipes."""

    async def call(machine, path, body):
        reader, writer = await asyncio.open_connection("127.0.0.1", ports[machine])
        writer.write(peer_client.build_request(path, body, host=machine))
        await writer.drain()
        raw = await reader.read()
        writer.close()
        status, payload = peer_client.parse_response(raw)
        if status >= 400:
            raise MeshError(
                f"peer {machine!r} rejected {path}: {payload.get('error') or status}"
            )
        return payload

    async def stream(machine, path, body):
        reader, writer = await asyncio.open_connection("127.0.0.1", ports[machine])
        writer.write(peer_client.build_request(path, body, host=machine))
        await writer.drain()
        return _RawBridge(reader, writer)

    return call, stream


async def _two_apps(mgr, mm_a, mm_b):
    from aiohttp.test_utils import TestClient, TestServer

    app_a = build_app(mgr, "tokA", started_at=time.monotonic(), mesh=mm_a)
    app_b = build_app(mgr, "tokB", started_at=time.monotonic(), mesh=mm_b)
    client_a = TestClient(TestServer(app_a))
    client_b = TestClient(TestServer(app_b))
    await client_a.start_server()
    await client_b.start_server()
    call, stream = _raw_wiring({"pcA": client_a.port, "pcB": client_b.port})
    for mm in (mm_a, mm_b):
        mm.peer_transport = call
        mm.peer_streamer = stream
    return client_a, client_b


async def _wait_for(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def test_shadow_list_stream_and_session_line(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        mgr.set_note("sa", "watching the build")
        client_a, client_b = await _two_apps(mgr, mm_a, mm_b)
        bearer = {"Authorization": "Bearer tokB"}
        try:
            sa = mgr.get("sa")
            assert await _wait_for(lambda: "READY" in "\n".join(sa.capture()))

            # C1: pcB sees pcA's alice, with the card pcA composed
            resp = await client_b.get("/api/shadows", headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            rows = doc["shadows"]
            assert [(r["machine"], r["session"]) for r in rows] == [("pcA", "sa")]
            card = rows[0]["card"]
            assert rows[0]["error"] is None
            assert card["session"] == "sa" and card["handle"] == "alice"
            assert card["note"] == "watching the build"
            assert card["exited"] is False
            assert "cwd" not in card and "args" not in card
            assert rows[0]["meshes"][0]["mesh"] == "m@pcA"
            # the list is behind the daemon's own auth like everything else
            assert (await client_b.get("/api/shadows")).status == 401

            # C2: the terminal, output only
            ws = await client_b.ws_connect(
                "/api/shadows/pcA/sa/ws", headers=bearer
            )
            first = await asyncio.wait_for(ws.receive(), 10)
            assert first.type == WSMsgType.TEXT
            init = json.loads(first.data)
            assert init["type"] == "init" and init["rows"] == 80
            repaint = await asyncio.wait_for(ws.receive(), 10)
            assert repaint.type == WSMsgType.BINARY
            assert b"READY" in repaint.data
            # keystrokes and controls sent to a shadow go nowhere
            await ws.send_bytes(b"evil\r")
            await ws.send_str(json.dumps({"type": "resize", "cols": 20, "rows": 5}))
            await asyncio.sleep(0.5)
            assert "echo:evil" not in "\n".join(sa.capture())
            assert sa.sdef.rows == 80

            # C3: the session line reaches it, and its echo comes back
            resp = await client_b.post(
                "/api/shadows/pcA/sa/keys",
                json={"text": "hello", "input_id": "in-1"},
                headers=bearer,
            )
            assert resp.status == 200, await resp.text()
            seen = b""
            deadline = time.monotonic() + 10
            while b"echo:hello" not in seen and time.monotonic() < deadline:
                msg = await asyncio.wait_for(ws.receive(), 10)
                if msg.type == WSMsgType.BINARY:
                    seen += msg.data
            assert b"echo:hello" in seen
            entries = session_input.read("sa")
            sent = [e for e in entries if e.get("request_id") == "in-1"]
            assert sent and all(e.get("origin") == "peer:pcB" for e in sent)
            assert sent[-1]["status"] == "sent"
            # a replay of the same line is a duplicate, not a second line
            resp = await client_b.post(
                "/api/shadows/pcA/sa/keys",
                json={"text": "hello", "input_id": "in-1"},
                headers=bearer,
            )
            assert (await resp.json()).get("duplicate") is True
            # a key name is typed as the word, not pressed
            resp = await client_b.post(
                "/api/shadows/pcA/sa/keys",
                json={"text": "Escape", "input_id": "in-2"},
                headers=bearer,
            )
            assert resp.status == 200
            assert await _wait_for(
                lambda: "echo:Escape" in "\n".join(sa.capture())
            )
            # C6: the viewer leaving releases the host's attachment
            assert sa.viewers() >= 1
            await ws.close()
            assert await _wait_for(lambda: sa.viewers() == 0, timeout=5.0)

            # a session in no shared mesh is not reachable at all
            resp = await client_b.post(
                "/api/shadows/pcA/sx/keys",
                json={"text": "x", "input_id": "in-3"},
                headers=bearer,
            )
            assert resp.status == 400
            assert "not a member" in (await resp.json())["error"]
            ws2 = await client_b.ws_connect("/api/shadows/pcA/sx/ws", headers=bearer)
            msg = await asyncio.wait_for(ws2.receive(), 10)
            assert json.loads(msg.data)["type"] == "shadow_error"
            await ws2.close()
        finally:
            await client_a.close()
            await client_b.close()
            await mm_a.shutdown()
            await mm_b.shutdown()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_host_refuses_input_when_off_and_bad_tokens(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        client_a, client_b = await _two_apps(mgr, mm_a, mm_b)
        bearer = {"Authorization": "Bearer tokB"}
        try:
            store.update(lambda doc: doc.update({"daemon": {"shadow_input": False}}))
            resp = await client_b.post(
                "/api/shadows/pcA/sa/keys",
                json={"text": "hello", "input_id": "off-1"},
                headers=bearer,
            )
            assert resp.status == 400
            assert "shadow_input" in (await resp.json())["error"]
            assert session_input.latest("sa", "off-1") is None
            # straight at the host: a wrong token is 403 on every shadow route
            for path in ("/peer/shadow/cards", "/peer/shadow/stream",
                         "/peer/shadow/keys"):
                resp = await client_a.post(path, json={
                    "mesh": "m", "machine": "pcB", "token": "nope",
                    "session": "sa", "text": "x", "input_id": "t",
                })
                assert resp.status == 403, path
        finally:
            await client_a.close()
            await client_b.close()
            await mm_a.shutdown()
            await mm_b.shutdown()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_peer_shadow_routes_are_exactly_three(home):
    async def make():
        mgr = _manager()
        return build_app(mgr, "t", started_at=time.monotonic(), mesh=MeshManager(mgr))

    app = asyncio.run(make())
    paths = sorted(
        {r.resource.canonical for r in app.router.routes()
         if r.resource is not None and r.resource.canonical.startswith("/peer/shadow")}
    )
    assert paths == ["/peer/shadow/cards", "/peer/shadow/keys", "/peer/shadow/stream"]
    api = sorted(
        (r.method, r.resource.canonical) for r in app.router.routes()
        if r.resource is not None and r.resource.canonical.startswith("/api/shadows")
    )
    assert api == [
        ("GET", "/api/shadows"),
        ("GET", "/api/shadows/{machine}/{session}/ws"),
        ("HEAD", "/api/shadows"),
        ("HEAD", "/api/shadows/{machine}/{session}/ws"),
        ("POST", "/api/shadows/{machine}/{session}/keys"),
    ]


def test_quiet_session_releases_a_viewer_that_left(home, tmp_path):
    """Nothing is written to a quiet session's stream, so a write failing is
    not how the host learns the viewer left -- the connection watch is."""
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        client_a, client_b = await _two_apps(mgr, mm_a, mm_b)
        bearer = {"Authorization": "Bearer tokB"}
        try:
            sa = mgr.get("sa")
            assert await _wait_for(lambda: "READY" in "\n".join(sa.capture()))
            ws = await client_b.ws_connect("/api/shadows/pcA/sa/ws", headers=bearer)
            await asyncio.wait_for(ws.receive(), 10)  # init
            await asyncio.wait_for(ws.receive(), 10)  # repaint
            await asyncio.sleep(1.5)  # let the session settle: no more output
            assert sa.viewers() == 1
            await ws.close()
            assert await _wait_for(lambda: sa.viewers() == 0, timeout=4.0)
        finally:
            await client_a.close()
            await client_b.close()
            await mm_a.shutdown()
            await mm_b.shutdown()
            await mgr.shutdown_all()

    asyncio.run(run())
