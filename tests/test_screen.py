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
    # Defaults to the main buffer, asserted explicitly so a viewer never has
    # to guess which buffer a plain program is drawing in.
    assert seq.startswith(b"\x1b[?1049l\x1b[2J\x1b[H")
    assert b"hi there" in seq


def test_alt_screen_tracking():
    s = ScreenState(20, 5)
    assert s.alt_screen is False
    s.feed(b"\x1b[?1049h")
    assert s.alt_screen is True
    s.feed(b"\x1b[?1049l")
    assert s.alt_screen is False


def test_alt_screen_split_across_chunks():
    s = ScreenState(20, 5)
    s.feed(b"\x1b[?")
    s.feed(b"1049h")
    assert s.alt_screen is True


def test_repaint_sequence_leads_with_the_buffer_the_program_is_in():
    # A full-screen TUI re-asserts the alternate screen on every full redraw;
    # the seed must put the viewer in that same buffer, or its wheel has an
    # empty scrollback to shuffle. And the leave sequence stays idempotent
    # for a viewer already on the main buffer.
    s = ScreenState(20, 5)
    s.feed(b"\x1b[?1049h")
    s.feed(b"tui grid")
    seq = s.repaint_sequence()
    assert seq.startswith(b"\x1b[?1049h\x1b[2J\x1b[H")
    assert b"tui grid" in seq
    s.feed(b"\x1b[?1049l")
    assert s.repaint_sequence().startswith(b"\x1b[?1049l\x1b[2J\x1b[H")


def test_repaint_sequence_restores_colors():
    """The repaint seed must carry colors/attributes — TUIs only redraw what
    changes, so a plain-text seed leaves attached viewers monochrome."""
    s = ScreenState(40, 5)
    s.feed(b"\x1b[1;31mred\x1b[0m plain \x1b[42mbg\x1b[0m")
    seq = s.repaint_sequence()
    assert b"\x1b[0;1;31mred" in seq       # bold red run
    assert b"\x1b[0m plain " in seq        # default run reverts attributes
    assert b"\x1b[0;42mbg" in seq          # green background run


def test_repaint_sequence_truecolor_and_bright():
    s = ScreenState(40, 5)
    s.feed(b"\x1b[38;5;196mX\x1b[0m \x1b[91mY\x1b[0m")
    seq = s.repaint_sequence()
    assert b"\x1b[0;38;2;255;0;0mX" in seq  # 256-color palette -> truecolor
    assert b"\x1b[0;91mY" in seq            # bright red
