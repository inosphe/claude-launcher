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
    # The in-turn marker is now trusted only while the spinner row is alive;
    # seed that row as "just moved" so a test that wants a LIVE turn gets one.
    s.tracker._last_change_at[s.screen.rows - 2] = time.monotonic()
    time.sleep(0.2)  # exceed idle_threshold -> heuristic idle
    return s


def test_marker_overrides_heuristic_idle_to_busy(home, tmp_path, monkeypatch):
    async def run():
        s = _idle_session(CLAUDE_HARNESS, tmp_path, monkeypatch)
        assert s._heuristic_status(s.idle_threshold) == STATUS_IDLE  # precondition
        # Mid-turn: only the spinner row moves (heuristic idle), the footer
        # carries the in-turn marker -> the dot must read busy.
        monkeypatch.setattr(s.screen, 'bottom_line', lambda: "  auto mode on  ·  esc to interrupt  ·  1 agent")
        assert s.status() == STATUS_BUSY
        # Marker absent from the footer (awaiting input) -> heuristic idle.
        monkeypatch.setattr(s.screen, 'bottom_line', lambda: "  auto mode on  ·  install gh for PR status  ·  1 agent")
        assert s.status() == STATUS_IDLE
        s._log.close()

    asyncio.run(run())


def test_marker_in_content_does_not_make_busy(home, tmp_path, monkeypatch):
    # The marker is an ordinary English phrase that legitimately shows up in
    # transcript content (a commit subject, a quoted docstring). Only the
    # FOOTER row counts: a whole-grid scan pinned a genuinely idle session
    # busy forever the moment such a row rendered.
    async def run():
        s = _idle_session(CLAUDE_HARNESS, tmp_path, monkeypatch)
        # A transcript row renders the phrase; the footer (bottom row) does not.
        monkeypatch.setattr(s.screen, "render_screen", lambda: [
            "Whisking... c8ab950: claude footer의 esc to interrupt 마커로 busy 판정 ...",
            "  auto mode on  ·  install gh for PR status  ·  1 agent",
        ])
        assert s.screen.render_screen()[-1].count("esc to interrupt") == 0
        monkeypatch.setattr(s.screen, 'bottom_line', lambda: s.screen.render_screen()[-1])
        assert s.status() == STATUS_IDLE  # footer clean -> not a turn -> idle
        s._log.close()

    asyncio.run(run())


def test_marker_not_applied_to_non_claude_harness(home, tmp_path, monkeypatch):
    async def run():
        s = _idle_session("py", tmp_path, monkeypatch)
        monkeypatch.setattr(s.screen, "bottom_line", lambda: "esc to interrupt")  # marker present
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
        monkeypatch.setattr(s.screen, 'bottom_line', lambda: "  auto mode on  ·  esc to interrupt  ·  1 agent")
        s.screen.bracketed_paste = True
        assert s.status() == STATUS_BUSY
        await asyncio.wait_for(s._await_readable(), timeout=5)
        assert s._input_ready is True
        s._log.close()

    asyncio.run(run())


# ---------------------------------------------------------------------- #
# frozen-marker: a dead claude leaves "esc to interrupt" painted on its
# last frame; that fossil must NOT keep the session reading busy.
# ---------------------------------------------------------------------- #

def _seed_idle_with_marker(s, monkeypatch, *, spinner_last_change):
    """An idle claude session whose footer shows the marker, with the spinner
    row's last-change time set as given (None = never moved).
    """
    s._saw_output = True
    s.tracker.sample((1,) * s.screen.rows, time.monotonic())
    time.sleep(0.2)  # heuristic -> idle
    footer = "  auto mode on  ·  esc to interrupt  ·  2 agents"
    monkeypatch.setattr(s.screen, "bottom_line", lambda: footer)
    # Freeze/seed the spinner row's last-change directly.
    spinner_row = s.screen.rows - 2
    if spinner_last_change is None:
        s.tracker._last_change_at.pop(spinner_row, None)
    else:
        s.tracker._last_change_at[spinner_row] = spinner_last_change
    return s


def test_frozen_marker_falls_back_to_idle(home, tmp_path, monkeypatch):
    # Marker on footer, but the spinner row has not moved for a long time —
    # a dead/wedged TUI's fossil. Must NOT read busy.
    async def run():
        monkeypatch.setattr(session_mod, "INPUT_SETTLE", 0.05)
        s = Session(
            SessionDef(name="sx", harness=CLAUDE_HARNESS, cwd=str(tmp_path)),
            idle_threshold=0.05, scrollback=200,
        )
        stale = time.monotonic() - (session_mod.TURN_MARKER_FRESH_FOR + 5.0)
        _seed_idle_with_marker(s, monkeypatch, spinner_last_change=stale)
        assert s._heuristic_status(s.idle_threshold) == STATUS_IDLE
        assert s.status() == STATUS_IDLE  # fossil ignored
        s._log.close()

    asyncio.run(run())


def test_live_marker_reads_busy(home, tmp_path, monkeypatch):
    # Same footer, but the spinner row moved just now — a genuine turn.
    async def run():
        monkeypatch.setattr(session_mod, "INPUT_SETTLE", 0.05)
        s = Session(
            SessionDef(name="sx", harness=CLAUDE_HARNESS, cwd=str(tmp_path)),
            idle_threshold=0.05, scrollback=200,
        )
        _seed_idle_with_marker(s, monkeypatch, spinner_last_change=time.monotonic())
        assert s._heuristic_status(s.idle_threshold) == STATUS_IDLE
        assert s.status() == STATUS_BUSY  # live turn
        s._log.close()

    asyncio.run(run())


def test_marker_with_no_spinner_history_falls_back(home, tmp_path, monkeypatch):
    # Footer has the marker but the spinner row was never observed to move
    # (e.g. right after spawn with a fossil frame). No evidence of life ->
    # not busy.
    async def run():
        monkeypatch.setattr(session_mod, "INPUT_SETTLE", 0.05)
        s = Session(
            SessionDef(name="sx", harness=CLAUDE_HARNESS, cwd=str(tmp_path)),
            idle_threshold=0.05, scrollback=200,
        )
        _seed_idle_with_marker(s, monkeypatch, spinner_last_change=None)
        assert s._heuristic_status(s.idle_threshold) == STATUS_IDLE
        assert s.status() == STATUS_IDLE
        s._log.close()

    asyncio.run(run())
