"""Harness activity survives quiet thinking and is never inferred from prose."""

import asyncio
from pathlib import Path
import shutil
import subprocess

import pytest

from claude_launcher.daemon import activity
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.session import Session, STATUS_BUSY, STATUS_IDLE, STATUS_EXITED
from claude_launcher.daemon.screen import ScreenState


SIGNALS = [
    ("claude", "\x1b]0;◐ task\x07", "\x1b]0;✳ task\x07"),
    ("codex", "\x1b]2;⠹ task\x1b\\", "\x1b]2;✓ task\x1b\\"),
    ("pi", "\x1b]777;claunch;activity;busy\x07", "\x1b]777;claunch;activity;idle\x07"),
]


@pytest.mark.parametrize("harness,busy,idle", SIGNALS)
def test_activity_split_at_every_byte_and_stops(harness, busy, idle):
    wire = busy.encode()
    for split in range(len(wire) + 1):
        detector = activity.Detector(harness)
        detector.feed(wire[:split])
        detector.feed(wire[split:])
        assert detector.busy(15), split
        detector.feed(idle.encode())
        assert not detector.busy(15)


@pytest.mark.parametrize("harness,busy,idle", SIGNALS)
def test_freshness_requires_new_signal_not_unrelated_output(monkeypatch, harness, busy, idle):
    now = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: now[0])
    detector = activity.Detector(harness)
    detector.feed(busy.encode())
    now[0] += 16
    detector.feed(b"unrelated tool output")
    assert not detector.busy(15)
    detector.feed(busy.encode())
    assert detector.busy(15)
    detector.feed(idle.encode() + busy.encode() + idle.encode())
    assert not detector.busy(15)


@pytest.mark.parametrize("harness,busy,idle", SIGNALS)
def test_plain_text_and_other_harness_signals_do_not_count(harness, busy, idle):
    detector = activity.Detector(harness)
    detector.feed("◐ task ⠹ task Working... esc to interrupt 777;claunch;activity;busy".encode())
    assert not detector.busy(15)
    other = activity.Detector("python")
    other.feed(busy.encode())
    assert not other.busy(15)


def test_incomplete_osc_is_bounded_and_recovers():
    detector = activity.Detector("codex")
    detector.feed(b"\x1b]0;" + b"x" * 10000)
    assert len(detector._pending) <= 2048
    detector.feed(SIGNALS[1][1].encode())
    assert detector.busy(15)


@pytest.mark.parametrize("harness,busy,idle", SIGNALS)
@pytest.mark.parametrize("focused", [False, True])
def test_live_output_overrides_quiet_screen_and_agrees_with_idle_since(
    home, tmp_path, monkeypatch, harness, busy, idle, focused
):
    async def run():
        now = [100.0]
        monkeypatch.setattr(activity.time, "monotonic", lambda: now[0])
        s = Session(SessionDef(name="activity", harness=harness, cwd=str(tmp_path)), idle_threshold=2, scrollback=20)
        monkeypatch.setattr(s, "is_focused", lambda: focused)
        try:
            s._on_output(busy.encode())
            s.tracker.sample((1, 2, 3), now[0])
            now[0] += 5
            assert s._heuristic_status(2) == STATUS_IDLE
            assert s.status() == STATUS_BUSY
            assert s.idle_since() is None
            assert s.info()["status"] == STATUS_BUSY
            now[0] += 16
            assert s.status() == STATUS_IDLE  # a frozen producer expires
            assert s.idle_since() is not None
            s._on_output(busy.encode())
            now[0] += 5
            assert s.status() == STATUS_BUSY
            s._on_output(idle.encode())
            now[0] += 5
            assert s.status() == STATUS_IDLE
            assert s.idle_since() is not None
            s._on_output(busy.encode())
            s.exit_code = 0
            s.exited = True
            assert s.status() == STATUS_EXITED
            assert s.idle_since() is None
        finally:
            s._feeder.close()
            s._log.close()
    asyncio.run(run())


@pytest.mark.parametrize("harness,busy,idle", SIGNALS)
def test_replayed_history_cannot_restore_activity(home, tmp_path, harness, busy, idle):
    async def run():
        s = Session(SessionDef(name="replay", harness=harness, cwd=str(tmp_path)), idle_threshold=2, scrollback=20)
        try:
            s._log.write(busy.encode())
            s._log.flush()
            s.seed_screen_from_log()
            assert not s._activity.busy(15)
        finally:
            s._feeder.close()
            s._log.close()
    asyncio.run(run())


def test_pi_metadata_is_invisible_to_terminal():
    screen = ScreenState(80, 20, history=0)
    screen.feed(SIGNALS[2][1].encode() + b"visible" + SIGNALS[2][2].encode())
    assert screen.render_screen()[0] == "visible"


def test_pi_activity_extension_lifecycle():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("pi_activity.test.mjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
