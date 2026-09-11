"""Focused-session CPU scheduling and background screen pacing."""

from __future__ import annotations

import asyncio

from claude_launcher.daemon import process_priority
from claude_launcher.daemon.screen import ScreenFeeder, ScreenState
from claude_launcher.daemon.session import Session


class _Feeder:
    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


def test_focus_transitions_change_only_the_session_child_priority(monkeypatch):
    """A parked viewer lowers its child; an active viewer restores it."""
    calls = []
    monkeypatch.setattr(process_priority, "set_background", lambda pid: calls.append(("bg", pid)) or True)
    monkeypatch.setattr(process_priority, "set_foreground", lambda pid: calls.append(("fg", pid)) or True)

    session = object.__new__(Session)
    session.pid = 42
    session._focused_subscribers = set()
    session._focused_session_scheduling = True
    session._cpu_background = None
    session._feeder = _Feeder()

    session._apply_cpu_priority()
    viewer = object()
    session.set_viewer_focused(viewer, True)
    session.set_viewer_focused(viewer, False)

    assert calls == [("bg", 42), ("fg", 42), ("bg", 42)]
    assert session._feeder.wakes == 2


def test_background_feeder_wakes_immediately_when_session_is_focused():
    async def run():
        focused = False
        feeder = ScreenFeeder(
            ScreenState(20, 5),
            slice_size=1,
            foreground=lambda: focused,
            background_delay=1.0,
        )
        feeder.submit(b"abc")
        await asyncio.sleep(0.01)  # first byte rendered; the feeder is paced
        assert feeder.pending_bytes > 0
        focused = True
        feeder.wake()
        await asyncio.wait_for(feeder.drained(), 0.2)
        feeder.close()

    asyncio.run(run())


def test_capture_renders_the_parked_tail_of_an_attached_only_session():
    """pi (background_render: false) parks its output while unattached;
    capture-pane must still show it -- it is the one read that means
    'somebody is looking' without a viewer being attached."""
    from claude_launcher.daemon.screen import ScreenState

    async def run():
        session = object.__new__(Session)
        session._focused_subscribers = set()
        session.background_render = False
        session.screen = ScreenState(40, 5)
        session._feeder = ScreenFeeder(
            session.screen, foreground=lambda: False, background_render=False
        )
        session._feeder.submit(b"parked words\r\n")
        await asyncio.sleep(0.05)
        assert not any("parked" in line for line in session.screen.render_screen())
        assert any("parked words" in line for line in session.capture())
        session._feeder.submit(b"more\r\n")
        await session.screen_synced()
        assert any("more" in line for line in session.screen.render_screen())

    asyncio.run(run())
