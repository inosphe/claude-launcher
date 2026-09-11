"""Notices: a line for the person at a session, drawn over the terminal.

Three layers, each tested on its own surface: the composer (``daemon/notice``
— fitting, the sequence tracker, the draw/clear bytes), the per-viewer pump
in ``ws.py`` (control frame for every viewer, bytes only for one that asked,
the expiry that restores row 1 from the grid), and the doors (``Session.notify``
through the API, and a viewer's own ``notice`` control frame).
"""

from __future__ import annotations

import asyncio
import json
import time

from claude_launcher.daemon import notice as notice_mod
from claude_launcher.daemon import ws as ws_mod
from claude_launcher.daemon.notice import Notice, Overlay, SequenceTracker, fit
from claude_launcher.daemon.screen import ScreenFeeder, ScreenState

ESC = chr(27)
CSI = ESC + "["


# --------------------------------------------------------------------------- #
# the composer
# --------------------------------------------------------------------------- #
def test_fit_counts_cells_not_characters():
    # Hangul is two cells wide: four syllables fill eight columns exactly.
    assert fit("웹데몬알림", 8) == "웹데몬알"
    assert fit("ab", 5) == "ab   "
    assert fit("abcdef", 3) == "abc"
    assert fit("", 2) == "  "


def test_notice_make_clamps_and_folds():
    n = Notice.make("  two\nlines  here ", ttl=999, level="loud")
    assert n.text == "two lines here"
    assert n.ttl == notice_mod.MAX_TTL
    assert n.level == "info"
    assert Notice.make("x", ttl=0).ttl == 0.5
    assert Notice.make("x").ttl == notice_mod.DEFAULT_TTL
    assert Notice.make("x", level="warn").frame()["level"] == "warn"


def test_tracker_holds_across_a_split_csi():
    t = SequenceTracker()
    assert t.feed(b"plain text")
    assert not t.feed((CSI + "38;5;").encode())  # final byte still to come
    assert not t.feed(b"12")
    assert t.feed(b"mafter")


def test_tracker_knows_string_sequences_and_two_byte_escapes():
    t = SequenceTracker()
    assert not t.feed((ESC + "]0;title").encode())  # OSC, unterminated
    assert not t.feed(ESC.encode())  # ST begun
    assert t.feed(b"\\")  # ST complete
    assert not t.feed((ESC + "]0;t").encode())
    assert t.feed(b"\x07")  # BEL terminates too
    assert not t.feed(ESC.encode())
    assert t.feed(b"7")  # DECSC
    assert not t.feed((ESC + "(").encode())  # charset: intermediate, final pending
    assert t.feed(b"B")
    assert not t.feed((ESC + "P1$r").encode())  # DCS runs to ST
    assert t.feed((ESC + "\\").encode())


def test_tracker_sees_a_split_utf8_character():
    t = SequenceTracker()
    han = "한".encode("utf-8")
    assert not t.feed(han[:1])
    assert not t.feed(han[:2])
    assert t.feed(han)
    assert t.feed(han[2:])  # the tail alone completes what came before
    assert not t.feed(b"ok" + "🙂".encode("utf-8")[:3])


def test_overlay_draws_after_clean_chunks_and_defers_across_a_split():
    ov = Overlay()
    n = Notice.make("hello", level="info")
    first = ov.show(n, 10)
    assert first.startswith((ESC + "7" + CSI + "1;1H").encode())
    assert b"hello     " in first
    assert first.endswith((ESC + "8").encode())
    # A chunk that ends mid-sequence gets no draw; the next clean one does.
    assert ov.after_output((CSI + "3").encode(), 10) == b""
    assert ov.pending
    assert ov.after_output(b"1m", 10) == ov.draw(10)
    assert not ov.pending
    # Cleared: the caller's row restore, wrapped in the same save/restore.
    restore = (CSI + "1;1H" + CSI + "2Kold row").encode()
    assert ov.clear(restore) == (ESC + "7").encode() + restore + (ESC + "8").encode()
    assert ov.notice is None
    assert ov.after_output(b"x", 10) == b""


def test_overlay_will_not_clear_mid_sequence():
    ov = Overlay()
    ov.show(Notice.make("n"), 4)
    ov.after_output((CSI + "1").encode(), 4)
    assert ov.clear(b"row") == b""
    assert ov.notice is None


# --------------------------------------------------------------------------- #
# the pump
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self):
        self.text = []
        self.binary = []
        self.closed = False

    async def send_str(self, s):
        self.text.append(json.loads(s))

    async def send_bytes(self, b):
        self.binary.append(b)


class _Session:
    def __init__(self, screen, feeder):
        self.screen = screen
        self._feeder = feeder

    async def screen_synced(self):
        await self._feeder.drained()


def _pump_once(items, *, overlay):
    """Run the pump over ``items`` (queued before it starts) and stop it."""

    async def run():
        screen = ScreenState(12, 3, history=50)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        feeder.submit(b"row one\r\nrow two")
        await feeder.drained()
        ws = _WS()
        state = ws_mod.ViewerState(overlay_bytes=overlay)
        queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)
        task = asyncio.ensure_future(ws_mod._pump_to_client(ws, queue, session, state))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ws, state, session

    return asyncio.run(run())


def test_pump_sends_the_frame_to_every_viewer_and_bytes_only_on_request():
    n = Notice.make("look here", ttl=5)
    ws, state, _ = _pump_once([("notice", n), ("data", b"abc")], overlay=False)
    assert ws.text == [n.frame()]
    assert ws.binary == [b"abc"], "a plain viewer's bytes are the program's alone"
    assert state.overlay.notice is n
    state.expiry.cancel()

    ws, state, _ = _pump_once([("notice", n), ("data", b"abc")], overlay=True)
    assert ws.text == [n.frame()]
    assert len(ws.binary) == 2
    assert ws.binary[0] == state.overlay.draw(12)
    # The chunk and its redraw travel as one frame, chunk first.
    assert ws.binary[1].startswith(b"abc")
    assert ws.binary[1][3:] == state.overlay.draw(12)
    state.expiry.cancel()


def test_pump_restores_row_one_from_the_grid_when_the_notice_expires():
    n = Notice.make("gone soon", ttl=0.5)

    async def run():
        screen = ScreenState(12, 3, history=50)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        feeder.submit(b"row one\r\nrow two")
        await feeder.drained()
        ws = _WS()
        state = ws_mod.ViewerState(overlay_bytes=True)
        await ws_mod._show_notice(ws, session, state, n)
        assert state.overlay.notice is n
        await asyncio.sleep(0.8)
        return ws, state, screen

    ws, state, screen = asyncio.run(run())
    assert state.overlay.notice is None
    restore = screen.row_sequence(0)
    assert b"row one" in restore
    assert ws.binary[-1] == (ESC + "7").encode() + restore + (ESC + "8").encode()


def test_a_newer_notice_replaces_the_older_ones_timer():
    first = Notice.make("first", ttl=0.3)
    second = Notice.make("second", ttl=5)

    async def run():
        screen = ScreenState(12, 3, history=50)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        ws = _WS()
        state = ws_mod.ViewerState(overlay_bytes=True)
        await ws_mod._show_notice(ws, session, state, first)
        await ws_mod._show_notice(ws, session, state, second)
        await asyncio.sleep(0.6)
        up = state.overlay.notice
        state.expiry.cancel()
        return up, ws

    up, ws = asyncio.run(run())
    assert up is second, "the first notice's expiry must not take the second down"
    assert [m["text"] for m in ws.text] == ["first", "second"]


def test_viewer_can_put_up_a_notice_for_itself():
    async def run():
        screen = ScreenState(12, 3, history=50)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        ws = _WS()
        state = ws_mod.ViewerState(overlay_bytes=True)
        await ws_mod._handle_control(
            ws, session, json.dumps({"type": "notice", "text": "cp 949", "ttl": 3}), state
        )
        await ws_mod._handle_control(
            ws, session, json.dumps({"type": "notice", "text": "   "}), state
        )
        up = state.overlay.notice
        state.expiry.cancel()
        return ws, up

    ws, up = asyncio.run(run())
    assert up is not None and up.text == "cp 949"
    assert len(ws.text) == 1, "a blank notice is refused, not drawn"
    assert b"cp 949" in ws.binary[0]


def test_repaint_redraws_the_notice_over_it():
    n = Notice.make("still here", ttl=5)

    async def run():
        screen = ScreenState(12, 3, history=50)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        ws = _WS()
        state = ws_mod.ViewerState(overlay_bytes=True)
        await ws_mod._show_notice(ws, session, state, n)
        await ws_mod._handle_control(ws, session, json.dumps({"type": "repaint"}), state)
        state.expiry.cancel()
        return ws, state

    ws, state = asyncio.run(run())
    repaint = ws.binary[-1]
    assert repaint.startswith(CSI.encode()[:1])  # a repaint sequence...
    assert repaint.endswith(state.overlay.draw(12))  # ...with the notice re-laid


def test_row_sequence_restores_one_row_with_attributes():
    screen = ScreenState(10, 2)
    screen.feed((CSI + "31mred" + CSI + "0m rest\r\nsecond").encode())
    row0 = screen.row_sequence(0)
    assert row0.startswith((CSI + "1;1H" + CSI + "2K").encode())
    assert b"31m" in row0 and b"red" in row0 and b"rest" in row0
    assert b"second" not in row0
    assert screen.row_sequence(1).startswith((CSI + "2;1H").encode())


# --------------------------------------------------------------------------- #
# the doors: the API and Session.notify, against a real session
# --------------------------------------------------------------------------- #
def test_api_notice_reaches_attached_viewers_and_counts_them(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from test_daemon_e2e import _register_py_harness, _wait_screen

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit"}
        try:
            session = mgr.create(SessionDef(name="nt1", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            # Nobody looking: accepted, shown to no one.
            r = await client.post(
                "/api/sessions/nt1/notice", json={"text": "hello"}, headers=auth
            )
            assert r.status == 200 and (await r.json())["viewers"] == 0
            r = await client.post("/api/sessions/nt1/notice", json={"text": " "}, headers=auth)
            assert r.status == 400
            r = await client.post(
                "/api/sessions/nt1/notice", json={"text": "x", "level": "shout"}, headers=auth
            )
            assert r.status == 400

            # One viewer with the overlay, one without.
            cli = await client.ws_connect("/api/sessions/nt1/ws?overlay=1", headers=auth)
            web = await client.ws_connect("/api/sessions/nt1/ws", headers=auth)
            for sock in (cli, web):
                init = await sock.receive_json()
                assert init["type"] == "init"
                await sock.receive_bytes()  # the repaint
            r = await client.post(
                "/api/sessions/nt1/notice",
                json={"text": "밥 먹고 하자", "ttl": 2, "level": "warn"},
                headers=auth,
            )
            assert (await r.json())["viewers"] == 2

            async def frames(sock):
                got = {"text": [], "binary": b""}
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    msg = await asyncio.wait_for(sock.receive(), timeout=1)
                    if msg.type.name == "TEXT":
                        got["text"].append(json.loads(msg.data))
                        if any(m.get("type") == "notice" for m in got["text"]):
                            break
                    elif msg.type.name == "BINARY":
                        got["binary"] += msg.data
                return got

            got_cli, got_web = await frames(cli), await frames(web)
            for got in (got_cli, got_web):
                frame = [m for m in got["text"] if m.get("type") == "notice"][0]
                assert frame["text"] == "밥 먹고 하자"
                assert frame["level"] == "warn" and frame["ttl"] == 2
            # The overlay viewer got the bar in bytes; the other did not.
            drawn = b""
            deadline = time.monotonic() + 3
            while "밥 먹고 하자".encode() not in drawn and time.monotonic() < deadline:
                msg = await asyncio.wait_for(cli.receive(), timeout=1)
                if msg.type.name == "BINARY":
                    drawn += msg.data
            assert "밥 먹고 하자".encode() in drawn
            assert "밥".encode() not in got_web["binary"]
            await cli.close()
            await web.close()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
