"""Idle classification: animated (spinner) rows must not count as activity."""

from __future__ import annotations

from claude_launcher.daemon.idle import IdleTracker


def make(rows):
    return tuple(rows)


def test_no_samples_means_unknown():
    t = IdleTracker()
    assert t.idle_for(10.0) is None


def test_static_screen_goes_idle():
    t = IdleTracker()
    screen = make([1, 2, 3])
    for i in range(10):
        t.sample(screen, float(i))
    assert t.idle_for(9.0) == 9.0


def test_content_change_resets_idle():
    t = IdleTracker()
    t.sample(make([1, 2, 3]), 0.0)
    t.sample(make([1, 2, 3]), 1.0)
    t.sample(make([9, 2, 3]), 2.0)  # real new content on row 0
    assert t.idle_for(2.5) == 0.5


def test_spinner_row_is_ignored_once_classified():
    t = IdleTracker(window=5, flap_threshold=3)
    # Row 0 changes every sample (spinner); rows 1-2 static.
    now = 0.0
    for i in range(20):
        t.sample(make([100 + i, 2, 3]), now)
        now += 0.4
    # After classification settles, only the flapping row changed — the last
    # meaningful change should be early, not at the final sample.
    assert t.idle_for(now) is not None
    assert t.idle_for(now) > 5.0


def test_static_row_change_still_detected_alongside_spinner():
    t = IdleTracker(window=5, flap_threshold=3)
    now = 0.0
    for i in range(20):
        t.sample(make([100 + i, 2, 3]), now)
        now += 0.4
    # New real content on row 1 while the spinner keeps spinning.
    t.sample(make([999, 42, 3]), now)
    assert t.idle_for(now) == 0.0


def test_resize_counts_as_activity():
    t = IdleTracker()
    t.sample(make([1, 2, 3]), 0.0)
    t.sample(make([1, 2, 3]), 5.0)
    t.sample(make([1, 2]), 6.0)  # row count changed
    assert t.idle_for(6.0) == 0.0
