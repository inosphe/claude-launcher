"""One viewer sizes a session at a time (claunch-tre55).

Every viewer used to resize the session to its own window: two people on
one session -- two browsers, or a browser and ``claunch attach`` -- took the
size back from each other on each focus and refit, and both screens redrew
without end. The daemon now keeps a size owner per session
(``Session.claim_size``): only the owner's ``resize`` reaches the PTY, the
others mirror its grid, and the size changes hands only when the owner has
gone or is not being looked at, or when a viewer takes it explicitly -- the
``steal`` control frame (the web header's button) or ``?steal=1`` at the
open (``claunch attach``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import time

from claude_launcher import attach as attach_mod
from claude_launcher import store
from claude_launcher.daemon import ws as ws_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession, Session

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


# --------------------------------------------------------------------------- #
# the rule, on the session
# --------------------------------------------------------------------------- #
def _bare_session():
    """A Session with only what the ownership rule reads."""
    s = Session.__new__(Session)
    s._subscribers = set()
    s._focused_subscribers = set()
    s._size_owner = None
    s.events = []
    s._broadcast = lambda item: s.events.append(item)
    return s


def _viewer(s, *, focused=True):
    v = object()
    s._subscribers.add(v)
    if focused:
        s._focused_subscribers.add(v)
    return v


def test_the_first_viewer_takes_the_size_and_a_focused_owner_keeps_it():
    s = _bare_session()
    a, b = _viewer(s), _viewer(s)
    assert s.claim_size(a) is True
    assert s.claim_size(b) is False
    assert s.size_owner() is a
    assert s.events == [("size_owner", a)]


def test_a_steal_takes_it_from_a_focused_owner():
    s = _bare_session()
    a, b = _viewer(s), _viewer(s)
    s.claim_size(a)
    assert s.claim_size(b, force=True) is True
    assert s.size_owner() is b
    assert s.events[-1] == ("size_owner", b)


def test_an_owner_whose_window_lost_focus_keeps_the_size():
    """Two windows on one desktop blur each other on every click. A claim
    granted on blur handed the size to whichever was clicked last, and back
    again on the next click -- the redraw claunch-tre55 exists to stop.
    Only a steal, or the holder leaving, moves it."""
    s = _bare_session()
    a, b = _viewer(s), _viewer(s)
    s.claim_size(a)
    s._focused_subscribers.discard(a)  # focus:false, as a blurred window sends
    assert s.claim_size(b) is False
    assert s.size_owner() is a
    assert s.claim_size(b, force=True) is True


def test_the_size_is_released_when_its_owner_leaves():
    s = _bare_session()
    a, b = _viewer(s), _viewer(s)
    s.claim_size(a)
    s.release_size(b)  # not the owner: nothing happens
    assert s.size_owner() is a
    s.release_size(a)
    assert s.size_owner() is None
    assert s.events[-1] == ("size_owner", None)
    assert s.claim_size(b) is True


def test_a_finished_session_has_no_size_to_contest():
    dead = DeadSession(SessionDef(name="d", harness="py", cwd="."), exit_code=0)
    assert dead.claim_size(object()) is True
    assert dead.size_owner() is None


# --------------------------------------------------------------------------- #
# the control frames
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))


def _resizable(s):
    s.sdef = SessionDef(name="s", harness="py", cwd=".", cols=80, rows=24)
    s.resizes = []

    def resize(cols, rows):
        s.resizes.append((cols, rows))
        s.sdef = dataclasses.replace(s.sdef, cols=cols, rows=rows)

    s.resize = resize
    return s


def _control(s, viewer, msg):
    ws = _WS()
    asyncio.run(
        ws_mod._handle_control(
            ws, s, json.dumps(msg), ws_mod.ViewerState(focus_token=viewer)
        )
    )
    return ws.sent


def test_a_resize_from_a_viewer_without_the_size_is_not_applied():
    s = _resizable(_bare_session())
    a, b = _viewer(s), _viewer(s)
    s.claim_size(a)
    sent = _control(s, b, {"type": "resize", "cols": 200, "rows": 60})
    assert s.resizes == []
    # Told who holds it, and what the grid is, so it can mirror it.
    assert sent == [
        {"type": "size_owner", "owner": False, "held": True},
        {"type": "resize", "cols": 80, "rows": 24},
    ]


def test_the_owner_resizes_and_a_steal_moves_the_size():
    s = _resizable(_bare_session())
    a, b = _viewer(s), _viewer(s)
    s.claim_size(a)
    assert _control(s, a, {"type": "resize", "cols": 100, "rows": 30}) == []
    assert s.resizes == [(100, 30)]

    assert _control(s, b, {"type": "steal"}) == []
    assert s.size_owner() is b
    _control(s, b, {"type": "resize", "cols": 200, "rows": 60})
    assert s.resizes[-1] == (200, 60)
    _control(s, a, {"type": "resize", "cols": 100, "rows": 30})
    assert s.resizes[-1] == (200, 60)


# --------------------------------------------------------------------------- #
# over the real socket
# --------------------------------------------------------------------------- #
def _client(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic())
    return TestClient(TestServer(app))


async def _next_text(ws, kind, timeout=10.0, **want):
    """The next text frame of ``kind`` whose fields match ``want``, skipping
    output, other controls, and earlier frames of the same kind."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = await ws.receive(timeout=timeout)
        if msg.type.name == "TEXT":
            frame = json.loads(msg.data)
            if frame.get("type") == kind and all(
                frame.get(k) == v for k, v in want.items()
            ):
                return frame
        elif msg.type.name in ("CLOSE", "CLOSED", "CLOSING", "ERROR"):
            break
    raise AssertionError(f"no {kind} frame")


async def _settle(session, cols, rows, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (session.sdef.cols, session.sdef.rows) == (cols, rows):
            return True
        await asyncio.sleep(0.05)
    return False


def test_two_viewers_one_size(home, tmp_path):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        client = _client(mgr)
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await session.wait_for("idle", timeout=10.0, threshold=0.5)

            a = await client.ws_connect("/api/sessions/s1/ws", headers=BEARER)
            assert (await _next_text(a, "init"))["owner"] is True
            b = await client.ws_connect("/api/sessions/s1/ws", headers=BEARER)
            assert (await _next_text(b, "init"))["owner"] is False

            await a.send_str(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
            assert await _settle(session, 100, 30)

            # The late viewer's own window does not reach the PTY...
            await b.send_str(json.dumps({"type": "resize", "cols": 150, "rows": 40}))
            assert (await _next_text(b, "size_owner")) == {
                "type": "size_owner", "owner": False, "held": True,
            }
            assert not await _settle(session, 150, 40, timeout=0.5)

            # ...until it takes the size, which the first viewer is told.
            await b.send_str(json.dumps({"type": "steal"}))
            await b.send_str(json.dumps({"type": "resize", "cols": 150, "rows": 40}))
            assert await _settle(session, 150, 40)
            await _next_text(a, "size_owner", owner=False, held=True)

            # An attach takes it as it opens, from a focused owner.
            c = await client.ws_connect(
                "/api/sessions/s1/ws?overlay=1&steal=1", headers=BEARER
            )
            assert (await _next_text(c, "init"))["owner"] is True
            await _next_text(b, "size_owner", owner=False, held=True)

            # The owner leaving frees the size for whoever is still here.
            await c.close()
            await _next_text(a, "size_owner", owner=False, held=False)
            await a.send_str(json.dumps({"type": "resize", "cols": 90, "rows": 25}))
            assert await _settle(session, 90, 25)

            await a.close()
            await b.close()
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# claunch attach
# --------------------------------------------------------------------------- #
def test_attach_opens_with_a_steal():
    url = attach_mod.ws_url("http://h:1", "s1", overlay=True, steal=True)
    assert url == "ws://h:1/api/sessions/s1/ws?overlay=1&steal=1"
    assert attach_mod.ws_url("http://h:1", "s1") == "ws://h:1/api/sessions/s1/ws"


def test_attach_hears_that_it_lost_the_size_and_when_it_is_free_again():
    lost = {"type": "size_owner", "owner": False, "held": True}
    free = {"type": "size_owner", "owner": False, "held": False}
    mine = {"type": "size_owner", "owner": True, "held": True}
    assert attach_mod.size_owner_update(lost, True) == (False, True)
    assert attach_mod.size_owner_update(lost, False) == (False, False)
    assert attach_mod.size_owner_update(free, False) == (True, False)
    assert attach_mod.size_owner_update(mine, True) == (True, False)
    assert "attach again" in attach_mod.SIZE_LOST_NOTICE
