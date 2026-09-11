"""Keystrokes must never wait behind a repaint or scroll (claunch-wpd0).

The terminal socket's receive loop is one coroutine. ``repaint`` and
``scroll`` answer with a snapshot of the rendered grid, so they wait for the
feeder to catch up — and while a Codex session floods, that queue does not
empty. Awaited inline, that wait held every keystroke that followed it:
Escape and Ctrl-C included, since they travel the same socket. The session
line box (``/keys``) kept working, which is how the wedge was located.

The first test is the mechanism itself, reproduced: serve the control inline
and the key never reaches the PTY. The rest are the fix.
"""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp import WSMessage, WSMsgType

from claude_launcher.daemon import ws as ws_mod


class _Screen:
    cols = 80
    history_len = 0
    alt_screen = False
    mouse_tracking = False

    def repaint_sequence(self, offset):
        return b"\x1b[H"


class _Session:
    """A session whose render never catches up: the grid is behind a burst."""

    def __init__(self):
        self.screen = _Screen()
        self.sdef = type("D", (), {"harness": "codex"})()
        self.writes = []
        self.drained = asyncio.Event()  # never set unless a test does it
        self.sync_calls = 0

    async def screen_synced(self):
        self.sync_calls += 1
        await self.drained.wait()

    def note_human_input(self, **kw):
        pass

    async def write_bytes(self, data):
        self.writes.append(data)


class _WS:
    """An async-iterable socket that yields scripted frames, then ends."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.text = []
        self.binary = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send_str(self, s):
        self.text.append(json.loads(s))

    async def send_bytes(self, b):
        self.binary.append(b)


def _text(msg: dict) -> WSMessage:
    return WSMessage(WSMsgType.TEXT, json.dumps(msg), None)


def _key(data: bytes) -> WSMessage:
    return WSMessage(WSMsgType.BINARY, data, None)


class _InlineLane:
    """The pre-fix behaviour: nothing is taken, every control runs inline."""

    def submit(self, raw):
        return False

    def close(self):
        pass


def test_the_wedge_reproduced_inline_control_holds_the_keys_behind_it(monkeypatch):
    """Premise: with the control served inline, Ctrl-C after a repaint never
    reaches the PTY while the render has not caught up."""
    monkeypatch.setattr(ws_mod, "SYNC_TIMEOUT", 60.0)

    async def run():
        session = _Session()
        ws = _WS([_text({"type": "repaint"}), _key(b"\x03")])
        pump = asyncio.ensure_future(
            ws_mod._pump_from_client(ws, session, ws_mod.ViewerState(), _InlineLane())
        )
        await asyncio.sleep(0.2)
        assert session.sync_calls == 1, "the repaint is waiting on the render"
        assert session.writes == [], "and the Ctrl-C behind it was never written"
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_keys_are_written_while_a_repaint_still_waits_on_the_render(monkeypatch):
    monkeypatch.setattr(ws_mod, "SYNC_TIMEOUT", 60.0)

    async def run():
        session = _Session()
        state = ws_mod.ViewerState()
        ws = _WS([_text({"type": "repaint"}), _key(b"\x1b"), _key(b"\x03")])
        lane = ws_mod._SyncLane(ws, session, state)
        t0 = time.monotonic()
        await ws_mod._pump_from_client(ws, session, state, lane)
        assert session.writes == [b"\x1b", b"\x03"]
        assert time.monotonic() - t0 < 1.0
        await asyncio.sleep(0)
        assert lane.busy, "the repaint is still parked on the render, off the loop"
        assert ws.binary == [], "and has not answered yet"
        # The render catches up: the parked repaint is answered.
        session.drained.set()
        await asyncio.sleep(0.05)
        assert not lane.busy
        assert ws.binary == [b"\x1b[H"]
        lane.close()

    asyncio.run(run())


def test_scrolls_waiting_together_are_coalesced_into_one_answer(monkeypatch):
    monkeypatch.setattr(ws_mod, "SYNC_TIMEOUT", 60.0)

    async def run():
        session = _Session()
        session.screen.history_len = 500
        state = ws_mod.ViewerState()
        ws = _WS([])
        lane = ws_mod._SyncLane(ws, session, state)
        # All three arrive while the first is still waiting on the render.
        assert lane.submit(json.dumps({"type": "scroll", "lines": 30}))
        assert lane.submit(json.dumps({"type": "repaint"}))
        assert lane.submit(json.dumps({"type": "scroll", "lines": 20}))
        await asyncio.sleep(0.05)
        assert session.sync_calls == 1
        session.drained.set()
        await asyncio.sleep(0.05)
        # One scroll of the summed delta; the repaint was folded into it.
        assert [m for m in ws.text if m["type"] == "scrolled"] == [
            {"type": "scrolled", "offset": 50}
        ]
        assert len(ws.binary) == 1
        assert not lane.busy
        lane.close()

    asyncio.run(run())


def test_other_controls_stay_inline():
    lane = ws_mod._SyncLane(None, None, ws_mod.ViewerState())
    assert not lane.submit(json.dumps({"type": "typing"}))
    assert not lane.submit(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
    assert not lane.submit("not json")
    assert not lane.submit(json.dumps(["repaint"]))
    assert not lane.busy


def test_a_sync_that_never_completes_is_bounded(monkeypatch):
    monkeypatch.setattr(ws_mod, "SYNC_TIMEOUT", 0.05)

    async def run():
        session = _Session()
        t0 = time.monotonic()
        await ws_mod._synced(session, timeout=ws_mod.SYNC_TIMEOUT)
        assert time.monotonic() - t0 < 1.0

    asyncio.run(run())
