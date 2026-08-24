"""The status dot must read busy mid-turn, where only the spinner row moves.

The idle heuristic is built to ignore the spinner/counter rows that animate
during a claude turn, so a mid-turn session reads "idle" and the dot goes
green. The claude footer carries "esc to interrupt" iff a turn is in flight
— a positive marker on exactly those otherwise-ignored rows — so the session
overrides heuristic-idle to busy when the marker is present, and falls back
to the heuristic when it is absent (never false-busy) or the harness is not
claude (never applied elsewhere). Delivery readiness keeps using the
heuristic alone, so a starting session that has not painted the footer still
becomes readable.
"""

from __future__ import annotations

import asyncio
import time

from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.harness import CLAUDE_HARNESS, SessionDef
from claude_launcher.daemon.session import STATUS_BUSY, STATUS_IDLE, Session


def _idle_session(harness, tmp_path, monkeypatch):
    """A claude session whose heuristic reads idle: output seen, then still.

    A tiny idle threshold so a brief real-time sleep crosses it without
    slowing the suite; the marker logic is threshold-independent.
    """
    monkeypatch.setattr(session_mod, "INPUT_SETTLE", 0.05)
    s = Session(
        SessionDef(name="sx", harness=harness, cwd=str(tmp_path)),
        idle_threshold=0.05,
        scrollback=200,
    )
    s._saw_output = True
    s.tracker.sample((1, 2, 3), time.monotonic())
    time.sleep(0.2)  # exceed idle_threshold -> heuristic idle
    return s


def test_marker_overrides_heuristic_idle_to_busy(home, tmp_path, monkeypatch):
    async def run():
        s = _idle_session(CLAUDE_HARNESS, tmp_path, monkeypatch)
        assert s._heuristic_status(s.idle_threshold) == STATUS_IDLE  # precondition
        # Mid-turn: only the spinner row moves (heuristic idle), footer carries
        # the in-turn marker -> the dot must read busy.
        s.screen.render_screen = lambda: [
            "Whisking... (9m 54s / 16.2k tokens)",
            "  auto mode on  ·  esc to interrupt  ·  1 agent",
        ]
        assert s.status() == STATUS_BUSY
        # Marker absent (awaiting input) -> falls back to the heuristic, idle.
        s.screen.render_screen = lambda: [
            "  auto mode on  ·  install gh for PR status  ·  1 agent",
        ]
        assert s.status() == STATUS_IDLE
        s._log.close()

    asyncio.run(run())


def test_marker_not_applied_to_non_claude_harness(home, tmp_path, monkeypatch):
    async def run():
        s = _idle_session("py", tmp_path, monkeypatch)
        s.screen.render_screen = lambda: ["esc to interrupt"]  # marker present
        assert s.status() == STATUS_IDLE  # a non-claude harness ignores it
        s._log.close()

    asyncio.run(run())


def test_await_readable_ignores_marker(home, tmp_path, monkeypatch):
    # Mid-turn (marker present) but heuristic-idle: status() reads busy, yet
    # _await_readable must still declare the session readable. The marker
    # fixes the *displayed* status, not the startup-readiness gate, so a
    # starting session that has not painted the footer is not stalled.
    async def run():
        s = _idle_session(CLAUDE_HARNESS, tmp_path, monkeypatch)
        s.screen.render_screen = lambda: ["esc to interrupt"]
        s.screen.bracketed_paste = True
        assert s.status() == STATUS_BUSY
        await asyncio.wait_for(s._await_readable(), timeout=5)
        assert s._input_ready is True
        s._log.close()

    asyncio.run(run())
