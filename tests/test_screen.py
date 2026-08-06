"""pyte-backed screen state: capture, cursor, DECCKM tracking, repaint."""

from __future__ import annotations

from claude_launcher.daemon.screen import ScreenState


def test_plain_text_render():
    s = ScreenState(20, 5)
    s.feed(b"hello\r\nworld")
    lines = s.render_screen()
    assert lines[0] == "hello"
    assert lines[1] == "world"
    assert len(lines) == 5


def test_ansi_clear_and_redraw():
    s = ScreenState(20, 5)
    s.feed(b"old content")
    s.feed(b"\x1b[2J\x1b[H")  # clear + home
    s.feed(b"fresh")
    assert s.render_screen()[0] == "fresh"
    assert "old" not in "".join(s.render_screen())


def test_cursor_position():
    s = ScreenState(20, 5)
    s.feed(b"ab")
    assert s.cursor() == (2, 0)
    s.feed(b"\x1b[3;4H")  # row 3, col 4 (1-based)
    assert s.cursor() == (3, 2)


def test_history_scrollback():
    s = ScreenState(10, 3)
    s.feed(b"1\r\n2\r\n3\r\n4\r\n5")
    history = s.render_history()
    assert "1" in history
    assert s.render_screen()[-1] == "5"


def test_decckm_tracking():
    s = ScreenState(10, 3)
    assert s.app_cursor_keys is False
    s.feed(b"\x1b[?1h")
    assert s.app_cursor_keys is True
    s.feed(b"\x1b[?1l")
    assert s.app_cursor_keys is False


def test_decckm_split_across_chunks():
    s = ScreenState(10, 3)
    s.feed(b"\x1b[?")
    s.feed(b"1h")
    assert s.app_cursor_keys is True


def test_resize_changes_grid():
    s = ScreenState(20, 5)
    s.resize(40, 10)
    assert s.cols == 40
    assert s.rows == 10
    assert len(s.render_screen()) == 10


def test_line_hashes_change_with_content():
    s = ScreenState(20, 5)
    before = s.line_hashes()
    s.feed(b"x")
    after = s.line_hashes()
    assert before != after
    assert len(after) == 5


def test_repaint_sequence_contains_content():
    s = ScreenState(20, 5)
    s.feed(b"hi there")
    seq = s.repaint_sequence()
    assert seq.startswith(b"\x1b[2J\x1b[H")
    assert b"hi there" in seq
