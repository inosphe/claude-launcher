"""daemon/wallclock: monotonic stamps as wall times that hold still (claunch-4thlk.1)."""

from __future__ import annotations

from claude_launcher.daemon import wallclock


def _clocks(monkeypatch, wall, mono):
    monkeypatch.setattr(wallclock, "_offset", None)
    monkeypatch.setattr(wallclock.time, "time", lambda: wall[0])
    monkeypatch.setattr(wallclock.time, "monotonic", lambda: mono[0])


def test_a_stamp_reads_the_same_wall_time_through_clock_jitter(monkeypatch):
    wall, mono = [1_760_000_000.0], [5000.0]
    _clocks(monkeypatch, wall, mono)
    first = wallclock.wall(5600.0)
    assert first == 1_760_000_600.0
    # The two clocks advance by slightly different amounts between reads (the
    # resolution of each, ~16 ms on Windows). The published time must not.
    wall[0] += 5.016
    mono[0] += 5.0
    assert wallclock.wall(5600.0) == first


def test_a_real_wall_clock_step_is_followed(monkeypatch):
    wall, mono = [1_760_000_000.0], [5000.0]
    _clocks(monkeypatch, wall, mono)
    before = wallclock.wall(5600.0)
    wall[0] += 3600.0          # the system clock was set an hour ahead
    assert wallclock.wall(5600.0) == before + 3600.0


def test_none_is_none():
    assert wallclock.wall(None) is None
