"""Sampling a screen costs the lines that changed, not the whole grid.

Every live session samples its grid every ``SAMPLE_INTERVAL`` (0.4s) to
decide whether it is idle, and the sample is one hash per row.
``line_hashes`` memoised against a revision counter, so a grid nothing had
touched cost nothing -- but a grid one line had touched was rebuilt in full:
``render_screen`` reads ``lines * columns`` cells through pyte's ``Char``
namedtuple, and the hash is taken over the joined rows.

That is the shape a busy session has. Measured on the live daemon (s586,
2026-09-21, 42 running sessions): a py-spy sample put 28.5% of the event
loop thread's time under ``_sample_loop``, 28.3% of it under
``line_hashes`` and 9.1% inside pyte's ``Char.__getattribute__``. The
terminal sockets are on that loop.

pyte already records which rows moved, in ``Screen.dirty``, and marks every
row when the whole grid moves (a scroll, a reset, a resize, a page turn).
So these pin two rules: a sample re-hashes only the rows pyte marked, and
the tuple it returns is the same tuple hashing every row would give.
"""

from __future__ import annotations

import pytest

from claude_launcher.daemon.screen import ScreenState


def _naive(s: ScreenState) -> tuple:
    """What the hashes have to equal: every row, hashed from the grid."""
    return tuple(hash(line) for line in s.render_screen())


def _counted(s: ScreenState, monkeypatch) -> list:
    """Records one entry per row the implementation actually hashes."""
    rows = []
    real = ScreenState._hash_line

    def watched(self, y):
        rows.append(y)
        return real(self, y)

    monkeypatch.setattr(ScreenState, "_hash_line", watched)
    return rows


def test_a_one_line_change_rehashes_one_line(monkeypatch):
    s = ScreenState(80, 50)
    s.feed(b"first\r\n")
    s.line_hashes()
    rows = _counted(s, monkeypatch)
    s.feed(b"second")
    s.line_hashes()
    assert rows == [1], f"rehashed rows {rows}"


def test_the_first_sample_hashes_the_whole_grid(monkeypatch):
    """Nothing is cached yet, so there is nothing to skip."""
    s = ScreenState(20, 6)
    rows = _counted(s, monkeypatch)
    s.feed(b"x")
    s.line_hashes()
    assert sorted(rows) == list(range(6))


def test_the_answer_equals_hashing_every_line():
    """The property, over the operations that move a grid in different
    ways: a draw, an erase, a scroll past the bottom, an absolute cursor
    move, the alternate screen, and a CJK line whose cells carry stubs."""
    s = ScreenState(20, 5)
    steps = [
        b"hello\r\n",
        b"\x1b[2J\x1b[H",
        b"a\r\nb\r\nc\r\nd\r\ne\r\nf\r\ng\r\n",   # scrolls
        b"\x1b[2;3Hxy",
        b"\x1b[?1049h",                            # alternate screen
        b"\xed\x95\x9c\xea\xb8\x80 wide\r\n",      # 한글
        b"\x1b[K",
        b"\x1b[?1049l",                            # back to the primary
    ]
    for data in steps:
        s.feed(data)
        assert s.line_hashes() == _naive(s), f"after {data!r}"


def test_a_resize_rehashes_everything(monkeypatch):
    """Every row is a different row afterwards, and the cache is a
    different length."""
    s = ScreenState(20, 5)
    s.feed(b"hello")
    s.line_hashes()
    rows = _counted(s, monkeypatch)
    s.resize(30, 8)
    out = s.line_hashes()
    assert len(out) == 8
    assert sorted(rows) == list(range(8))
    assert out == _naive(s)


def test_a_scroll_rehashes_every_row(monkeypatch):
    """A scroll moves the content of every row up by one, so no row's hash
    may be carried over from before it."""
    s = ScreenState(20, 4)
    s.feed(b"a\r\nb\r\nc\r\nd")
    s.line_hashes()
    rows = _counted(s, monkeypatch)
    s.feed(b"\r\ne")          # pushes the grid up
    assert s.line_hashes() == _naive(s)
    assert sorted(set(rows)) == list(range(4))


def test_an_unchanged_grid_hashes_nothing(monkeypatch):
    """The memo that was already there is kept: no rows at all."""
    s = ScreenState(20, 5)
    s.feed(b"hello")
    s.line_hashes()
    rows = _counted(s, monkeypatch)
    assert s.line_hashes() == s.line_hashes()
    assert rows == []


def test_the_scrollback_limit_change_rehashes_everything(monkeypatch):
    """``set_history_limit`` rebuilds the history deques and bumps the
    revision. pyte marks no rows for it, so the cache is dropped here
    rather than trusted."""
    s = ScreenState(20, 5, history=100)
    s.feed(b"a\r\nb\r\nc")
    s.line_hashes()
    rows = _counted(s, monkeypatch)
    s.set_history_limit(50)
    assert s.line_hashes() == _naive(s)
    assert sorted(rows) == list(range(5))


def test_rolling_the_grid_into_history_moves_every_hash():
    """``scroll_grid_into_history`` feeds a newline for every row."""
    s = ScreenState(20, 5)
    s.feed(b"a\r\nb\r\nc")
    before = s.line_hashes()
    s.scroll_grid_into_history()
    after = s.line_hashes()
    assert after == _naive(s)
    assert after != before


@pytest.mark.parametrize("rows_n", [1, 3, 24])
def test_every_grid_height_round_trips(rows_n):
    s = ScreenState(12, rows_n)
    s.feed(b"x" * 11)
    assert s.line_hashes() == _naive(s)
    assert len(s.line_hashes()) == rows_n
