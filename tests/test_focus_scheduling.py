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
