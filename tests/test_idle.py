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


# ---- moved_rows: how much the screen moved in the last minute ------------


def test_moved_rows_counts_real_changes_in_the_window():
    t = IdleTracker()
    t.sample(make([1, 2, 3]), 0.0)          # first sample: not counted
    t.sample(make([9, 2, 3]), 1.0)          # one row
    t.sample(make([9, 8, 7]), 2.0)          # two rows
    assert t.moved_rows(2.0) == 3


def test_moved_rows_forgets_changes_older_than_the_window():
    t = IdleTracker()
    t.sample(make([1, 2, 3]), 0.0)
    t.sample(make([9, 2, 3]), 1.0)
    t.sample(make([9, 2, 3]), 70.0)
    assert t.moved_rows(70.0) == 0


def test_moved_rows_leaves_out_a_spinner():
    t = IdleTracker(window=5, flap_threshold=3)
    now = 0.0
    for i in range(200):                    # 80 s of a lone spinner row
        t.sample(make([100 + i, 2, 3, 4, 5]), now)
        now += 0.4
    # Only the first samples, before the row was classified, count.
    assert t.moved_rows(now) == 0


def test_moved_rows_counts_a_scrolling_screen():
    # A streaming reply rewrites every row on every sample: all of them flap,
    # all are "animated" to the idle classifier, and yet this is the busiest
    # screen there is.
    t = IdleTracker(window=5, flap_threshold=3)
    rows = 10
    now = 0.0
    for i in range(50):
        t.sample(make([i * 100 + r for r in range(rows)]), now)
        now += 0.4
    assert t.moved_rows(now) == rows * 49


def test_moved_rows_ignores_a_resize():
    t = IdleTracker()
    t.sample(make([1, 2, 3]), 0.0)
    t.sample(make([4, 5, 6, 7]), 1.0)       # row count changed
    assert t.moved_rows(1.0) == 0
