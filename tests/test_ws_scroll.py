"""The virtual-scroll control, and the one regime that still uses it.

The wheel has three owners now (see ``ws.py``): a program that took the mouse
gets its ticks forwarded, the main buffer scrolls the browser's own scrollback,
and only the alternate screen with the mouse left alone is served by a
``scroll`` control — answered with a clamped offset and a repaint windowed over
the daemon's pyte history. The render behind that history is deferred
(:class:`ScreenFeeder`), which is why the handler syncs before it reads
``history_len`` — reading it first clamps the viewer against lines that have
not been rendered yet, and during a burst that number can still be 0.

The second half of this module is the handover itself: a viewer holding a
frozen history window when the program takes the mouse has to be dropped back
to live, because from then on no wheel of theirs will lower the offset.
"""

from __future__ import annotations

import asyncio
import json

from claude_launcher.daemon import ws as ws_mod
from claude_launcher.daemon.screen import ScreenFeeder, ScreenState


class _WS:
    def __init__(self):
        self.text = []
        self.binary = []

    async def send_str(self, s):
        self.text.append(json.loads(s))

    async def send_bytes(self, b):
        self.binary.append(b)


class _Session:
    """Just the surface the scroll control touches."""

    def __init__(self, screen, feeder):
        self.screen = screen
        self._feeder = feeder

    async def screen_synced(self):
        await self._feeder.drained()


def _burst(lines: int) -> bytes:
    nl = chr(13) + chr(10)
    return "".join("line %d%s" % (i, nl) for i in range(lines)).encode()


def test_scroll_clamps_against_the_rendered_grid_not_the_pending_one():
    """A wheel during a burst must reach the history the burst is producing.

    The feeder renders in slices off the event loop, so at the moment the
    control arrives the grid can be arbitrarily far behind the byte stream —
    including completely empty. Clamping to that reads as a dead wheel.
    """

    async def run():
        screen = ScreenState(20, 5, history=500)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)

        feeder.submit(_burst(300))
        assert screen.history_len == 0, "premise: nothing rendered yet"

        ws = _WS()
        state = ws_mod.ViewerState()
        await ws_mod._handle_control(
            ws, session, json.dumps({"type": "scroll", "lines": 50}), state
        )

        assert state.offset == 50
        assert ws.text[-1] == {"type": "scrolled", "offset": 50}
        assert ws.binary, "the clamped answer comes with its repaint"

    asyncio.run(run())


def test_scroll_still_clamps_to_the_history_that_exists():
    """Past the end of history the offset pins to history_len, not beyond."""

    async def run():
        screen = ScreenState(20, 5, history=500)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        feeder.submit(_burst(20))

        ws = _WS()
        state = ws_mod.ViewerState()
        await ws_mod._handle_control(
            ws, session, json.dumps({"type": "scroll", "lines": 9999}), state
        )

        assert state.offset == screen.history_len
        assert 0 < state.offset < 9999

    asyncio.run(run())


def test_scrolling_back_toward_live_floors_at_zero():
    async def run():
        screen = ScreenState(20, 5, history=500)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        feeder.submit(_burst(60))

        ws = _WS()
        state = ws_mod.ViewerState()
        send = lambda n: ws_mod._handle_control(
            ws, session, json.dumps({"type": "scroll", "lines": n}), state
        )
        await send(10)
        assert state.offset == 10
        await send(-999999)          # the client's snap-to-live
        assert state.offset == 0

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# who owns the wheel
# --------------------------------------------------------------------------- #
async def _drain(ws, session, state, frame):
    """Run one queued frame through the pump and stop it again."""
    queue = asyncio.Queue()
    queue.put_nowait(frame)
    pump = asyncio.ensure_future(ws_mod._pump_to_client(ws, queue, session, state))
    while not queue.empty():
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass


def test_a_program_taking_the_mouse_unfreezes_a_scrolled_back_viewer():
    """From that moment the wheel is the program's, so nothing would scroll
    this viewer out of the frozen window it is holding.

    Left frozen the terminal simply stops advancing: live bytes are
    suppressed while ``offset > 0``, and the wheel that used to lower the
    offset now goes to the program instead. Dropping to live is the only way
    back, so the daemon takes it on the viewer's behalf.
    """

    async def run():
        screen = ScreenState(20, 5, history=500)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)
        feeder.submit(_burst(60))

        ws = _WS()
        state = ws_mod.ViewerState()
        await ws_mod._handle_control(
            ws, session, json.dumps({"type": "scroll", "lines": 10}), state
        )
        assert state.offset == 10

        await _drain(ws, session, state, ("mouse", True))

        assert state.offset == 0, "the viewer was returned to live"
        assert {"type": "scrolled", "offset": 0} in ws.text
        assert {"type": "mouse", "tracking": True} in ws.text

    asyncio.run(run())


def test_giving_the_mouse_back_is_announced_without_disturbing_the_viewer():
    """A program releasing the mouse hands the wheel back to the terminal; a
    viewer already sitting live has nothing to be moved off."""

    async def run():
        screen = ScreenState(20, 5, history=500)
        feeder = ScreenFeeder(screen, slice_size=64)
        session = _Session(screen, feeder)

        ws = _WS()
        state = ws_mod.ViewerState()
        await _drain(ws, session, state, ("mouse", False))

        assert ws.text == [{"type": "mouse", "tracking": False}]
        assert not ws.binary, "no repaint: the viewer never left live"

    asyncio.run(run())
