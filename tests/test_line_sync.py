"""The session line's shared mode (``daemon/line_sync.py``).

Three surfaces: the store (mode switch, last-writer-wins text, the
conditional clear, the refusals), the terminal socket (a viewer's
``line_sync`` write fans out to every viewer, a refused write answers only
the writer, the opening frames carry the state of a session that has one),
and the HTTP door (read, mode switch, text write, 409 while local).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import line_sync
from claude_launcher.daemon import ws as ws_mod


@pytest.fixture(autouse=True)
def _fresh_store():
    line_sync.reset()
    yield
    line_sync.reset()


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def test_a_session_never_set_is_local_and_empty():
    state = line_sync.get("s1")
    assert (state.mode, state.text, state.rev) == ("local", "", 0)
    assert state.frame()["type"] == "line_sync"


def test_text_is_refused_while_local():
    with pytest.raises(line_sync.NotRemote):
        line_sync.set_text("s1", "hello")
    line_sync.set_mode("s1", "remote")
    line_sync.set_mode("s1", "local")
    with pytest.raises(line_sync.NotRemote):
        line_sync.set_text("s1", "hello")


def test_mode_switch_bumps_rev_once_and_keeps_the_text():
    state, changed = line_sync.set_mode("s1", "remote", client="a")
    assert changed and state.rev == 1 and state.client == "a"
    _, changed = line_sync.set_mode("s1", "remote")
    assert not changed and line_sync.get("s1").rev == 1
    line_sync.set_text("s1", "draft", client="b")
    line_sync.set_mode("s1", "local")
    line_sync.set_mode("s1", "remote")
    assert line_sync.get("s1").text == "draft"
    with pytest.raises(line_sync.LineSyncError):
        line_sync.set_mode("s1", "both")


def test_last_writer_wins_and_identical_writes_are_not_changes():
    line_sync.set_mode("s1", "remote")
    state, changed = line_sync.set_text("s1", "ab", client="a")
    assert changed and state.rev == 2
    state, changed = line_sync.set_text("s1", "xy", client="b")
    assert changed and state.text == "xy" and state.client == "b" and state.rev == 3
    _, changed = line_sync.set_text("s1", "xy", client="a")
    assert not changed and line_sync.get("s1").client == "b"


def test_conditional_clear_spares_a_line_started_meanwhile():
    line_sync.set_mode("s1", "remote")
    line_sync.set_text("s1", "first line", client="a")
    # b starts the next line while a's send of "first line" is in flight
    line_sync.set_text("s1", "second", client="b")
    state, changed = line_sync.set_text("s1", "", client="a", if_text="first line")
    assert not changed and state.text == "second"
    state, changed = line_sync.set_text("s1", "", client="b", if_text="second")
    assert changed and state.text == ""


def test_sessions_are_kept_apart_and_oversized_text_is_refused():
    line_sync.set_mode("s1", "remote")
    line_sync.set_text("s1", "one")
    assert line_sync.get("s2").mode == "local"
    with pytest.raises(line_sync.LineSyncError):
        line_sync.set_text("s1", "x" * (line_sync.MAX_TEXT + 1))
    with pytest.raises(line_sync.LineSyncError):
        line_sync.set_text("s1", 5)
    line_sync.forget("s1")
    assert line_sync.get("s1").rev == 0


# --------------------------------------------------------------------------- #
# the terminal socket
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self):
        self.text = []
        self.closed = False

    async def send_str(self, s):
        self.text.append(json.loads(s))

    async def send_bytes(self, b):
        pass


class _Session:
    def __init__(self, name):
        self.sdef = SimpleNamespace(name=name)
        self.announced = []

    def announce_line_sync(self, frame):
        self.announced.append(frame)
        return 2


def _control(session, msg):
    ws = _WS()

    async def run():
        await ws_mod._handle_control(ws, session, json.dumps(msg), ws_mod.ViewerState())

    asyncio.run(run())
    return ws


def test_a_socket_write_is_announced_to_every_viewer():
    line_sync.set_mode("s1", "remote")
    session = _Session("s1")
    ws = _control(session, {"type": "line_sync", "text": "같이 쓰는 줄", "client": "p1"})
    assert ws.text == [], "the writer hears it with everyone else, not twice"
    assert len(session.announced) == 1
    frame = session.announced[0]
    assert frame["text"] == "같이 쓰는 줄" and frame["client"] == "p1"
    assert frame["mode"] == "remote" and frame["rev"] == 2


def test_a_refused_socket_write_answers_the_writer_alone():
    session = _Session("s1")
    ws = _control(session, {"type": "line_sync", "text": "x", "client": "p1"})
    assert session.announced == []
    assert ws.text == [line_sync.get("s1").frame()]
    assert ws.text[0]["mode"] == "local"
    # A conditional clear that lost the race: the writer is told the line
    # as it stands, nobody else hears anything.
    line_sync.set_mode("s1", "remote")
    line_sync.set_text("s1", "newer", client="p2")
    ws = _control(session, {"type": "line_sync", "text": "", "if_text": "older", "client": "p1"})
    assert session.announced == []
    assert ws.text[0]["text"] == "newer" and ws.text[0]["client"] == "p2"


def test_the_pump_forwards_the_frame_verbatim():
    frame = {"type": "line_sync", "mode": "remote", "text": "t", "rev": 3, "at": "", "client": "c"}

    async def run():
        ws = _WS()
        queue = asyncio.Queue()
        queue.put_nowait(("line_sync", frame))
        task = asyncio.ensure_future(
            ws_mod._pump_to_client(ws, queue, SimpleNamespace(), ws_mod.ViewerState())
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ws

    assert asyncio.run(run()).text == [frame]


# --------------------------------------------------------------------------- #
# end to end: the HTTP door and two viewers on one session
# --------------------------------------------------------------------------- #
def test_two_viewers_share_the_line_and_a_new_one_opens_on_it(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from test_daemon_e2e import _register_py_harness, _wait_screen

    _register_py_harness()

    async def next_sync(sock):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            msg = await asyncio.wait_for(sock.receive(), timeout=2)
            if msg.type.name == "TEXT":
                data = json.loads(msg.data)
                if data.get("type") == "line_sync":
                    return data
        raise AssertionError("no line_sync frame")

    async def opening(sock):
        """The text frames a fresh socket opens with, up to its repaint."""
        got = []
        while True:
            msg = await asyncio.wait_for(sock.receive(), timeout=3)
            if msg.type.name == "BINARY":
                return got
            got.append(json.loads(msg.data))

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit"}
        base = "/api/sessions/ls1/line-sync"
        try:
            session = mgr.create(SessionDef(name="ls1", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            r = await client.get(base, headers=auth)
            assert r.status == 200 and (await r.json())["mode"] == "local"
            # An untouched session opens with no line_sync frame at all.
            a = await client.ws_connect("/api/sessions/ls1/ws", headers=auth)
            first = await opening(a)
            assert [m["type"] for m in first] == ["init"]

            r = await client.post(base, json={"text": "x"}, headers=auth)
            assert r.status == 409
            r = await client.post(base, json={}, headers=auth)
            assert r.status == 400
            r = await client.post(base, json={"mode": "sideways"}, headers=auth)
            assert r.status == 400

            r = await client.post(base, json={"mode": "remote", "client": "pa"}, headers=auth)
            doc = await r.json()
            assert r.status == 200 and doc["mode"] == "remote" and doc["changed"]
            assert doc["viewers"] == 1
            assert (await next_sync(a))["mode"] == "remote"

            b = await client.ws_connect("/api/sessions/ls1/ws", headers=auth)
            opened = await opening(b)
            assert [m["type"] for m in opened] == ["init", "line_sync"]
            assert opened[1]["mode"] == "remote"

            # a types; both a (its echo) and b see it.
            await a.send_str(json.dumps({"type": "line_sync", "text": "공유 초안", "client": "pa"}))
            for sock in (a, b):
                frame = await next_sync(sock)
                assert frame["text"] == "공유 초안" and frame["client"] == "pa"

            # The line outlives every socket: a page closed and opened again
            # finds it, over HTTP and on the new socket's opening frames.
            await a.close()
            await b.close()
            r = await client.get(base, headers=auth)
            assert (await r.json())["text"] == "공유 초안"
            c = await client.ws_connect("/api/sessions/ls1/ws", headers=auth)
            opened = await opening(c)
            assert opened[1]["text"] == "공유 초안"

            # The HTTP door writes too, and announces to the socket.
            r = await client.post(
                base, json={"text": "", "if_text": "공유 초안", "client": "pc"}, headers=auth
            )
            assert (await r.json())["text"] == ""
            assert (await next_sync(c))["text"] == ""

            # Back to local: everyone is told, and text writes are refused.
            r = await client.post(base, json={"mode": "local"}, headers=auth)
            assert (await r.json())["mode"] == "local"
            assert (await next_sync(c))["mode"] == "local"
            r = await client.post(base, json={"text": "late"}, headers=auth)
            assert r.status == 409
            await c.close()

            # An unknown session is refused the way every session route
            # refuses one.
            r = await client.get("/api/sessions/nope/line-sync", headers=auth)
            same = await client.get("/api/sessions/nope", headers=auth)
            assert r.status == same.status and r.status >= 400
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
