"""A viewer that is slow to answer must keep its socket (claunch-u6lz).

Reported symptom: with two dashboard pages open in one browser, a session's
terminal never comes up -- the link retries and fails, and closing either
page makes the next retry succeed.

The socket is closed by the heartbeat. The daemon pings an attached socket
every ``HEARTBEAT`` seconds and aiohttp closes it when no pong comes back
within half of that. The pong is answered by the browser, so a browser busy
enough misses the window; two pages share one renderer, which is how a
person meets it. The daemon log of 2026-09-18 held five
``TimeoutError('No PONG received after 15.0 seconds')`` closes.

Two things are pinned here. The window, which was 15 seconds and is now 30.
And the receive loop, which is the only place a pong is read: the frames a
fresh socket opens with used to be written before that loop started, so
while they drained no pong could be read at all -- measured on the live
daemon against a client that stopped reading, closed after 45 seconds with
that same error.
"""

from __future__ import annotations

import asyncio
import time


from claude_launcher.daemon import ws as ws_mod

BEARER = "sekrit"
CHUNK = b"x" * 4096


class _Screen:
    cols = 80
    rows = 24
    alt_screen = False
    mouse_tracking = False
    history_len = 0

    def repaint_sequence(self, offset: int) -> bytes:
        return b"<repaint>"

    def history_sequence(self) -> bytes:
        # Small next to what the client drains per second: the opening frames
        # are not what this test makes slow, the live output is.
        return b"h" * 4096


class _Def:
    name = "slow"
    harness = "claude"
    cols = 80
    rows = 24


class _Session:
    """Just the surface a terminal socket touches, plus a printing program."""

    def __init__(self) -> None:
        self.sdef = _Def()
        self.screen = _Screen()
        self.pid = 4242
        self.exited = False
        self.exit_code = None
        self.written = []
        self._queues = []

    # --- the viewer registry -------------------------------------------- #
    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._queues.append(q)
        return q

    def unsubscribe(self, q) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def publish(self, item) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    # --- the rest -------------------------------------------------------- #
    def status(self) -> str:
        return "busy"

    def note_visit(self) -> None:
        pass

    def note_human_input(self, **kw) -> None:
        pass

    def set_viewer_focused(self, token, focused) -> None:
        pass

    async def screen_synced(self) -> None:
        return None

    async def write_bytes(self, data: bytes) -> None:
        self.written.append(data)

    def resize(self, cols: int, rows: int) -> None:
        pass


def test_the_opening_frames_are_written_from_the_sender_task():
    """The prologue runs on the pump, so the receive loop is free while it
    drains. Before the fix these frames were written inline, ahead of the
    loop, and a client that was not taking them could not have its pong
    read -- the socket died before it was ever established."""

    async def run():
        session = _Session()
        state = ws_mod.ViewerState()
        queue: asyncio.Queue = asyncio.Queue()
        order = []
        release = asyncio.Event()

        class _WS:
            async def send_bytes(self, b):
                order.append("byte")

            async def send_str(self, s):
                order.append("text")

        async def prologue():
            order.append("prologue-start")
            await release.wait()
            order.append("prologue-end")

        pump = asyncio.ensure_future(
            ws_mod._pump_to_client(_WS(), queue, session, state, prologue=prologue)
        )
        try:
            queue.put_nowait(("data", CHUNK))
            await asyncio.sleep(0.05)
            assert order == ["prologue-start"], order
            assert not queue.empty(), "output must wait behind the opening frames"

            release.set()
            await asyncio.sleep(0.05)
            assert order[:2] == ["prologue-start", "prologue-end"], order
        finally:
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_the_pong_window_is_wide_enough_for_a_loaded_browser():
    """What the window has to clear, as a number.

    aiohttp allows ``HEARTBEAT``/2 for the pong, and the page that answers
    it is a browser tab that may be rendering. At 15 seconds the daemon was
    closing sockets of pages that were merely busy; this pins the window it
    was raised to, so a later tuning of the ping interval cannot quietly put
    it back.
    """
    assert ws_mod.HEARTBEAT / 2 >= 30, (
        "the pong window is back inside what a loaded browser takes"
    )
