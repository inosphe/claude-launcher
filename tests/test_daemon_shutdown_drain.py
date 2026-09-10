"""``SessionManager.shutdown_all`` drains live sessions concurrently.

claunch-a5l9, 2026-09-10 17:31: a ``claunch daemon restart`` shut the daemon
down with 18 live sessions. ``shutdown_all`` awaited each session's
``shutdown`` in turn, and each waits up to its 5s grace window for the child
to go, so the predecessor held the singleton lock for 90s+ -- longer than
the successor's entire retry budget. Every spawn lost the lock race and the
daemon stayed down for 37 minutes until a person restarted it by hand.

This pins the fix: the drain runs all shutdowns together, so its wall time
is one grace window, not the session count times one.
"""

from __future__ import annotations

import asyncio
import time

from claude_launcher.daemon.manager import SessionManager


class _SlowSession:
    """A live session whose shutdown takes a whole grace window."""

    exited = False

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.shut = False

    async def shutdown(self, grace: float = 5.0) -> None:
        await asyncio.sleep(self.delay)
        self.shut = True
        self.exited = True


def test_shutdown_all_drains_sessions_concurrently(home, monkeypatch):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    monkeypatch.setattr(mgr, "persist", lambda: None)
    sessions = [_SlowSession(0.3) for _ in range(12)]
    for i, s in enumerate(sessions):
        mgr._sessions[f"s{i}"] = s

    started = time.monotonic()
    asyncio.run(mgr.shutdown_all())
    elapsed = time.monotonic() - started

    assert all(s.shut for s in sessions)
    # serial would be 12 x 0.3s = 3.6s; concurrent is about one delay
    assert elapsed < 1.5, elapsed
