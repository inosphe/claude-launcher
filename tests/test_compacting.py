"""Compaction-notice detection: ``daemon/compacting.py``.

The detector scans the raw pty byte stream a session receives and turns the
harness's compaction paint (notice line + progress bar, painted as one frame)
into a ``compacting`` flag. The tests cover the observed paint shapes (the
``✽``/``·`` prefix variants, second/minutes elapsed counters, chunk-split
paints, ANSI interleaved with cells), the window the flag survives, and the
one false-positive the screen cannot avoid: conversation content that merely
quotes the notice but carries no progress bar.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher import lineage, profile as profile_mod
from claude_launcher.daemon import compacting
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession

#: A live paint as the daemon saw it on a running session (s127's log):
#: notice line + progress bar, with the TUI's cursor moves and colour codes
#: interleaved the way a real frame arrives.
LIVE = (
    "\x1b[6;1H\x1b[0m\x1b[38;2;255;255;255m ✽ Compacting conversation… "
    "(1m 23s · ↓156.2k tokens)\x1b[0m\r\n"
    "\x1b[7;1H\x1b[38;2;240;164;32m▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 53%\x1b[0m\r\n"
).encode("utf-8")

#: The other observed prefix variant (middle dot), with a bare-seconds
#: counter and spaces in the token count.
LIVE_DOT = (
    "·Compacting conversation… (15s · ↓ 156.2k tokens)\r\n"
    "▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 0%\r\n"
).encode("utf-8")

#: Conversation text quoting the notice — the user's own pasted task line —
#: with no progress bar after it. Must NOT ignite.
QUOTED_ONLY = (
    "✽ Compacting conversation… (3m 10s · ↓ 8.4k tokens)\r\n\r\n"
    "이런 출력 패턴을 감지해서 'compacting' 라벨을 세션 카드에 표시해줘\r\n"
).encode("utf-8")


def _feed(det: compacting.Detector, *chunks: bytes) -> None:
    for chunk in chunks:
        det.feed(chunk)


def test_claude_declares_a_notice_and_other_harnesses_do_not():
    assert compacting.HARNESS_NOTICES["claude"]
    assert compacting.Detector("codex")._patterns == ()
    assert compacting.Detector("no-such-harness")._patterns == ()


def test_a_live_paint_sets_the_flag():
    det = compacting.Detector("claude")
    _feed(det, LIVE)
    assert det.compacting


def test_the_middle_dot_variant_is_the_same_notice():
    det = compacting.Detector("claude")
    _feed(det, LIVE_DOT)
    assert det.compacting


def test_conversation_text_that_only_quotes_the_notice_is_not_compaction():
    det = compacting.Detector("claude")
    _feed(det, QUOTED_ONLY)
    assert not det.compacting


def test_a_paint_split_across_chunks_is_still_one_notice():
    det = compacting.Detector("claude")
    cut = LIVE.index(" Compacting conversation".encode("utf-8"))
    _feed(det, LIVE[:cut], LIVE[cut:])
    assert det.compacting


def test_the_progress_bar_landing_in_a_later_chunk_still_counts():
    det = compacting.Detector("claude")
    cut = LIVE.index(b"tokens)\x1b[0m\r\n") + len(b"tokens)\x1b[0m\r\n")
    assert cut < len(LIVE)
    _feed(det, LIVE[:cut], LIVE[cut:])
    assert det.compacting


def test_control_sequences_between_cells_do_not_hide_the_notice():
    det = compacting.Detector("claude")
    noisy = (
        b"\x1b]0;claude\x07"                       # OSC title, BEL-terminated
        b"\x1b[?25l"                               # hide cursor
        + "\x1b[38;2;255;255;255m✽ Compacting conversation…\x1b[0m\r\n".encode()
        + b"\x1b[2K" + "▰▰▰▱▱▱ 42%\r\n".encode()
    )
    _feed(det, noisy)
    assert det.compacting


def test_the_flag_expires_after_the_notices_own_window(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(compacting.time, "monotonic", lambda: now)
    det = compacting.Detector("claude")
    _feed(det, LIVE_DOT)  # "15s" -> window 15 + 60 = 75s
    now += 74.0
    assert det.compacting
    now += 2.0
    assert not det.compacting


def test_minutes_in_the_parenthetical_size_the_window(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(compacting.time, "monotonic", lambda: now)
    det = compacting.Detector("claude")
    _feed(det, LIVE)  # "1m 23s" -> 83 + 60 = 143s
    now += 142.0
    assert det.compacting
    now += 2.0
    assert not det.compacting


def test_a_notice_without_a_duration_uses_the_default_window(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(compacting.time, "monotonic", lambda: now)
    det = compacting.Detector("claude")
    _feed(det, b"Compacting conversation ...\n\x1b[31m" + "▰▰▰ 1%".encode())
    assert det.compacting
    now += compacting.DEFAULT_WINDOW_S - 0.1
    assert det.compacting
    now += 1.0
    assert not det.compacting


def test_every_repaint_restarts_the_window(monkeypatch):
    now = 1_000_000.0
    monotonic = []
    monkeypatch.setattr(
        compacting.time, "monotonic", lambda: (monotonic.append(now) or now)
    )
    det = compacting.Detector("claude")
    _feed(det, LIVE_DOT)  # window 75s
    now += 100.0          # the compaction is still painting, far past 75s
    _feed(det, LIVE_DOT)
    assert det.compacting
    now += 74.0
    assert det.compacting
    now += 2.0
    assert not det.compacting  # and only the LAST paint's window runs


def test_an_unsupported_harness_ignores_everything():
    det = compacting.Detector("codex")
    _feed(det, LIVE, LIVE_DOT, QUOTED_ONLY)
    assert not det.compacting


def _register_claude_profile() -> None:
    """A claude session needs a profile to exist; give it a throwaway one."""
    if not profile_mod.resolve("p").exists():
        lineage.set_harness(profile_mod.create("p"), "claude")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def test_info_carries_the_compacting_flag(home, tmp_path):
    _register_claude_profile()
    """The display path's daemon leg: the key the rail actually reads.

    The Detector unit tests cover the scanning; this pins the wiring around
    it. ``Session.info()`` reports the flag (false, never missing, on a fresh
    session; true after a live compaction paint comes through the real feed
    path), and a dead session carries no key — nothing is running to have
    compacted. Deleting the ``info()`` line or the ``_on_output`` feed call
    turns this red.
    """

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="comp", harness="claude",
                                      profile="p", cwd=str(tmp_path)))
            assert s.info()["compacting"] is False
            s._on_output(LIVE)
            assert s.info()["compacting"] is True
            dead = DeadSession(SessionDef(name="gone", harness="claude",
                                          profile="p", cwd=str(tmp_path)))
            assert "compacting" not in dead.info()
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
