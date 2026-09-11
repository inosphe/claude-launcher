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


def test_bottom_line_is_only_the_footer_row():
    # A transcript row (top) carries an English phrase that must never be
    # mistaken for the footer signal; the footer (bottom row) is clean.
    s = ScreenState(60, 5)
    s.feed(b"Whisking... the esc to interrupt phrase in content\r\n")
    s.feed(b"another line of content\r\n")
    s.feed(b"\x1b[5;1H  auto mode on  |  install gh for PR status  |  1 agent")
    assert "esc to interrupt" in s.render_screen()[0]  # content really has it
    assert "esc to interrupt" not in s.bottom_line()  # footer clean
    assert s.bottom_line().endswith("1 agent")


def test_bottom_line_matches_render_screen_tail():
    s = ScreenState(60, 5)
    s.feed(b"Whisking... (9m 54s / 16.2k tokens)\r\n")
    s.feed(b"\x1b[5;1H  auto mode on  |  esc to interrupt  |  1 agent")
    assert "esc to interrupt" in s.bottom_line()
    assert s.bottom_line() == s.render_screen()[-1]


def test_bottom_line_survives_a_leading_stub_cell():
    # A DCH (delete-char) shift can push a wide glyph's stub cell to column 0,
    # where its data is "" — reading its width would explode. The footer read
    # must not, and must return the visible text (the wide glyphs intact).
    s = ScreenState(20, 3)
    s.feed("\x1b[3;1H가나다".encode())
    s.feed(b"\x1b[3;1H\x1b[1P")  # DCH: delete the leading cell -> stub at col 0
    assert s.bottom_line() == "나다"


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


# --------------------------------------------------------------------------- #
# virtual scroll: repaint_sequence(offset) browsing the daemon's history
# --------------------------------------------------------------------------- #
# xterm cannot scroll the alternate screen a TUI like claude draws in, and the
# repaint seeds only the grid — so the wheel's scrollback is served by the
# daemon: an offset repaint windows over `history + grid`. offset=0 must be
# byte-identical to the long-standing behaviour, because every existing caller
# keeps calling repaint_sequence().

def test_repaint_default_offset_zero_is_current_behavior():
    s = ScreenState(20, 5)
    s.feed(b"hi there")
    assert s.repaint_sequence() == s.repaint_sequence(0)


def test_history_len_counts_scrolled_off_lines():
    s = ScreenState(10, 3)
    assert s.history_len == 0
    s.feed(b"1\r\n2")
    assert s.history_len == 0                      # still fits the grid
    s.feed(b"\r\n3\r\n4\r\n5")
    assert s.history_len == 2                      # 1 and 2 scrolled off


def test_repaint_offset_windows_over_history_and_grid():
    s = ScreenState(10, 3)
    s.feed(b"1\r\n2\r\n3\r\n4\r\n5")
    assert s.history_len == 2
    seq = s.repaint_sequence(1)
    # one line back: the window is [history[1], grid[0], grid[1]]
    plain = seq.replace(b"\x1b[0m", b"")          # per-row default-SGR prefix
    assert b"2\r\n3\r\n4" in plain
    assert seq.count(b"\r\n") == 2                 # exactly rows-1 separators


def test_repaint_offset_clamps_to_history_len():
    s = ScreenState(10, 3)
    s.feed(b"1\r\n2\r\n3\r\n4\r\n5")
    deep = s.repaint_sequence(100)
    assert deep == s.repaint_sequence(s.history_len)
    plain = deep.replace(b"\x1b[0m", b"")
    assert b"1\r\n2\r\n3" in plain                 # everything left on screen


def test_repaint_offset_without_history_is_the_live_grid():
    s = ScreenState(10, 3)
    s.feed(b"live")
    assert s.history_len == 0
    assert s.repaint_sequence(5) == s.repaint_sequence()


def test_repaint_offset_hides_the_cursor_live_restores_it():
    s = ScreenState(10, 3)
    s.feed(b"ab\r\ncd\r\nef\r\ngh\r\nij")
    assert s.history_len == 2
    live = s.repaint_sequence()
    assert b"ij" in live and live.endswith(b"\x1b[3;3H")  # cursor at the tail
    assert b"\x1b[?25l" not in live
    back = s.repaint_sequence(1)
    # a scrolled-away cursor position is meaningless; hide it
    assert back.endswith(b"\x1b[0m\x1b[?25l")
    assert b"\x1b[?25h" not in back


def test_repaint_offset_preserves_attributes_on_history_lines():
    s = ScreenState(40, 2)
    s.feed(b"\x1b[31mred\x1b[0m\r\nblue\r\none")
    assert s.history_len == 1
    seq = s.repaint_sequence(1)
    assert b"\x1b[0;31mred" in seq                 # colour rode the scroll


def test_repaint_offset_leads_with_the_buffer_the_program_is_in():
    s = ScreenState(10, 3)
    s.feed(b"\x1b[?1049h")
    s.feed(b"1\r\n2\r\n3\r\n4")
    assert s.alt_screen is True and s.history_len == 1
    seq = s.repaint_sequence(1)
    assert seq.startswith(b"\x1b[?1049h\x1b[2J\x1b[H")
    assert b"1\r\n2\r\n3" in seq.replace(b"\x1b[0m", b"")


# --------------------------------------------------------------------------- #
# ScreenFeeder: the same rendering, without owning the event loop
# --------------------------------------------------------------------------- #
# A flooding session used to stall the whole daemon: pyte renders at roughly
# 530 KiB/s here, the render ran inline in the PTY callback, and asyncio drains
# every ready callback before it polls for I/O -- so the loop burned a core and
# answered nothing (measured during the outage: HTTP dead ~40 min, accept never
# reached, and the singleton lock held throughout, so no replacement daemon
# could start either). These pin the fix: the grid ends up identical however
# the bytes are sliced, and other loop work keeps running while a large feed is
# in flight. Written with asyncio.run rather than async test functions -- this
# suite has no pytest-asyncio, and a test that silently never ran would be
# worse than no test at all.

import asyncio

from claude_launcher.daemon.screen import ScreenFeeder


def _burst(repeats: int = 40) -> bytes:
    """A TUI-ish repaint: colour changes, full rows, CR/LF -- the shape of
    output that makes pyte slow (a namedtuple rebuilt per cell)."""
    row = "".join(
        "\x1b[3%dm%s\x1b[0m\r\n" % (i % 8, "x" * 79) for i in range(30)
    )
    return (row * repeats).encode()


def test_sliced_rendering_lands_on_the_same_screen():
    """Slicing is an implementation detail and must not be visible: pyte is a
    state machine, so a slice boundary inside an escape sequence would corrupt
    the grid if the feeder split anything but the byte stream."""
    payload = _burst(3) + b"\x1b[2J\x1b[H" + b"final line"
    whole = ScreenState(120, 30)
    whole.feed(payload)

    async def run():
        sliced = ScreenState(120, 30)
        feeder = ScreenFeeder(sliced, slice_size=7)   # tiny, to land mid-sequence
        for i in range(0, len(payload), 13):          # and mid-chunk too
            feeder.submit(payload[i : i + 13])
        await feeder.drained()
        feeder.close()
        return sliced

    sliced = asyncio.run(run())
    assert sliced.render_screen() == whole.render_screen()
    assert sliced.cursor() == whole.cursor()


def test_a_large_feed_leaves_the_loop_free_to_run_other_work():
    """The regression the outage is named after. While ~100 KiB is rendering,
    an unrelated coroutine must keep getting turns -- it stands in for the
    accept() and the mesh delivery that went unserved for forty minutes."""

    async def run():
        feeder = ScreenFeeder(ScreenState(120, 30))
        ticks = 0

        async def other_work():
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        ticker = asyncio.ensure_future(other_work())
        feeder.submit(_burst())            # ~105 KiB, ~200 ms of pyte
        await feeder.drained()
        ticker.cancel()
        feeder.close()
        return ticks

    # 105 KiB at 4 KiB a slice is ~26 yields; anything in that neighbourhood
    # proves interleaving, where the old inline feed would have given zero.
    assert asyncio.run(run()) >= 20


def test_the_feed_yields_before_it_has_rendered_everything():
    """Interleaving is worth nothing if it only happens after the last byte:
    the loop has to get its turn *during* the render, not after it."""

    async def run():
        feeder = ScreenFeeder(ScreenState(120, 30), slice_size=1024)
        feeder.submit(_burst(20))
        await asyncio.sleep(0)             # one turn for the pump to start
        midway = feeder.pending_bytes
        await feeder.drained()
        left = feeder.pending_bytes
        feeder.close()
        return midway, left

    midway, left = asyncio.run(run())
    assert midway > 0                      # we ran while work was still queued
    assert left == 0


def test_modes_are_tracked_at_submit_not_at_render():
    """DECCKM decides how send-keys encodes an arrow, so it cannot wait in the
    queue: a keystroke arriving between submit and render must already see the
    new mode."""

    async def run():
        feeder = ScreenFeeder(ScreenState(20, 5))
        before = feeder.screen.app_cursor_keys
        feeder.submit(b"\x1b[?1h")
        at_submit = feeder.screen.app_cursor_keys   # before the pump has run
        await feeder.drained()
        after = feeder.screen.app_cursor_keys
        feeder.close()
        return before, at_submit, after

    assert asyncio.run(run()) == (False, True, True)


def test_pending_output_still_renders_when_the_session_ends():
    """A session's last words are what somebody reads afterwards, so teardown
    renders the queue out instead of dropping it with the pump."""

    async def run():
        feeder = ScreenFeeder(ScreenState(20, 5))
        feeder.submit(b"last words")
        feeder.drain_now()                          # what _finish() does
        line = feeder.screen.render_screen()[0]
        feeder.close()
        return line

    assert asyncio.run(run()) == "last words"


# --------------------------------------------------------------------------- #
# sparse rows: a row is a column-keyed mapping, not a list
#
# A TUI that jumps the cursor to a right-aligned element leaves the cells in
# between untouched, so the row holds far fewer written cells than its
# rightmost column. Sizing the repaint by that count drops everything to the
# right of it — the live PTY bytes carry the region fine, so it reads as "part
# of the screen is missing until something redraws it".
# --------------------------------------------------------------------------- #
def _sparse_row_screen(cols=80):
    s = ScreenState(cols, 5)
    s.feed(b"\x1b[2J\x1b[H")
    s.feed(b"LEFT")                  # columns 0-3
    s.feed(b"\x1b[1;60HRIGHTEDGE")   # a jump to column 59, nothing in between
    return s


def test_repaint_keeps_content_right_of_the_written_cell_count():
    s = _sparse_row_screen()
    row = s._screen.buffer[0]
    # The premise: far fewer written cells than the rightmost column.
    assert len(row) < max(row) < s._screen.columns
    seq = s.repaint_sequence(0)
    assert b"LEFT" in seq
    assert b"RIGHTEDGE" in seq


def test_repaint_of_a_sparse_row_does_not_grow_the_row_mapping():
    """Reading an unwritten cell must not store one, or the grid bloats."""
    s = _sparse_row_screen()
    before = len(s._screen.buffer[0])
    s.repaint_sequence(0)
    assert len(s._screen.buffer[0]) == before


def test_repaint_still_trims_the_blank_tail():
    s = _sparse_row_screen()
    row = s.repaint_sequence(0).split(b"\r\n")[0]
    # Nothing is emitted past the last glyph, so the row ends at RIGHTEDGE.
    assert row.rstrip(b"\x1b[0m").endswith(b"RIGHTEDGE")
    assert not row.endswith(b" ")


def test_render_history_keeps_content_right_of_the_written_cell_count():
    s = ScreenState(80, 2)
    s.feed(b"LEFT\x1b[1;60HRIGHTEDGE\r\n")
    s.feed(b"a\r\nb\r\nc")                 # push the sparse row into history
    joined = "".join(s.render_history())
    assert "LEFT" in joined
    assert "RIGHTEDGE" in joined


def test_repaint_of_a_sparse_history_row_survives_the_scroll():
    s = ScreenState(80, 2)
    s.feed(b"LEFT\x1b[1;60HRIGHTEDGE\r\n")
    s.feed(b"a\r\nb\r\nc")
    assert s.history_len >= 1
    seq = s.repaint_sequence(s.history_len)
    assert b"RIGHTEDGE" in seq


# --------------------------------------------------------------------------- #
# who owns the wheel
# --------------------------------------------------------------------------- #
def test_mouse_tracking_is_tracked_like_the_other_private_modes():
    """A program that asks for the mouse is asking for the wheel.

    pyte models none of 1000/1002/1003, so the byte sweep is the only place
    this can be learned — and it decides whether the web viewer forwards a
    wheel tick or spends it on a scroll control of its own.
    """
    s = ScreenState(20, 5)
    assert s.mouse_tracking is False
    assert s.wheel_is_the_programs is False

    s.feed(b"\x1b[?1000h")
    assert s.mouse_tracking is True
    assert s.wheel_is_the_programs is True

    s.feed(b"\x1b[?1000l")
    assert s.mouse_tracking is False


def test_the_tracking_levels_are_independent():
    """Set and cleared separately, so the flag is the OR of what is still on.

    claude asserts all three and clears them one sequence at a time; reading
    only the last mode seen would hand the wheel back while the program is
    still listening on another level.
    """
    s = ScreenState(20, 5)
    s.feed(b"\x1b[?1000h\x1b[?1002h\x1b[?1003h")
    assert s.mouse_tracking is True
    s.feed(b"\x1b[?1000l\x1b[?1002l")
    assert s.mouse_tracking is True, "1003 is still on"
    s.feed(b"\x1b[?1003l")
    assert s.mouse_tracking is False


def test_mouse_encoding_is_tracked():
    s = ScreenState(20, 5)
    assert s.mouse_encoding == ""
    s.feed(b"\x1b[?1006h")
    assert s.mouse_encoding == "sgr"
    s.feed(b"\x1b[?1006l")
    assert s.mouse_encoding == ""


def test_one_sequence_can_carry_several_modes():
    """``CSI ? 1000 ; 1002 ; 1006 h`` is one match with three parameters."""
    s = ScreenState(20, 5)
    s.feed(b"\x1b[?1000;1002;1006h")
    assert s.mouse_tracking is True
    assert s.mouse_encoding == "sgr"


def test_repaint_re_asserts_the_mouse_modes():
    """A viewer that attached late has only the repaint to learn them from.

    Without this the browser's terminal swallows the wheel instead of
    reporting it, and the session reads as unscrollable — which is exactly
    how it read before.
    """
    s = ScreenState(20, 5)
    s.feed(b"\x1b[?1049h\x1b[?1000h\x1b[?1002h\x1b[?1006h")
    s.feed(b"hello")
    rep = s.repaint_sequence(0)
    assert b"\x1b[?1000h" in rep
    assert b"\x1b[?1002h" in rep
    assert b"\x1b[?1006h" in rep
    assert b"\x1b[?1049h" in rep


def test_repaint_stays_quiet_when_the_program_left_the_mouse_alone():
    s = ScreenState(20, 5)
    s.feed(b"hello")
    rep = s.repaint_sequence(0)
    assert b"\x1b[?1000h" not in rep
    assert b"\x1b[?1006h" not in rep


def test_forget_modes_drops_the_mouse_too():
    """A replayed log's modes belong to a program that is gone."""
    s = ScreenState(20, 5)
    s.feed(b"\x1b[?1000h\x1b[?1006h")
    s.forget_modes()
    assert s.mouse_tracking is False
    assert s.mouse_encoding == ""
    assert b"\x1b[?1000h" not in s.repaint_sequence(0)


# --------------------------------------------------------------------------- #
# the scrollback seed
# --------------------------------------------------------------------------- #
def test_history_sequence_carries_the_scrolled_off_lines():
    """The seed that fills the browser's own scrollback at attach."""
    s = ScreenState(10, 3)
    s.feed(b"1\r\n2\r\n3\r\n4\r\n5")
    seed = s.history_sequence()
    assert seed.startswith(b"\x1b[?1049l"), "must land in the main buffer"
    text = seed.decode()
    assert "1" in text and "2" in text
    # The rows are newline-separated so a terminal scrolls them off the way
    # they originally went; that is what puts them in ITS scrollback.
    assert "\r\n" in text


def test_history_sequence_is_empty_without_history():
    """The honest answer for a TUI that repaints instead of scrolling.

    Measured on this project's own sessions: 424 KiB of claude output leaves
    one or two lines behind. Seeding nothing is right — the wheel there
    belongs to the program.
    """
    s = ScreenState(20, 5)
    s.feed(b"\x1b[?1049h")
    s.feed(b"just a grid, never scrolled")
    assert s.history_sequence() == b""


def test_history_sequence_honours_the_limit():
    s = ScreenState(10, 3)
    s.feed(b"".join(b"%d\r\n" % i for i in range(200)))
    seed = s.history_sequence(limit=5).decode()
    rows = [r for r in seed.split("\r\n") if r.strip()]
    # The preamble shares the first row, so allow it; what matters is that a
    # limit of 5 does not ship 197 lines.
    assert len(rows) <= 6, rows


# --------------------------------------------------------------------------- #
# the grid read that used to raise
# --------------------------------------------------------------------------- #
def test_render_screen_survives_a_wide_character():
    """pyte's ``display`` calls ``wcwidth(char[0])`` on a wide glyph's stub.

    The stub's ``data`` is empty, so that indexing raises ``IndexError`` —
    on any grid holding CJK text. ``session.capture()`` reads through here,
    and so does idle detection.
    """
    s = ScreenState(20, 3)
    s.feed("한글 wide 글자".encode())
    lines = s.render_screen()
    assert "한글" in lines[0]
    assert len(s.line_hashes()) == 3


# --------------------------------------------------------------------------- #
# CSI sequences pyte mis-reads
# --------------------------------------------------------------------------- #
def test_xtmodkeys_does_not_underline_the_grid():
    """``CSI > 4 ; 2 m`` is keyboard negotiation, not an SGR.

    pyte's parser drops the ``>`` and reads the rest as an ordinary CSI, so
    XTMODKEYS modifyOtherKeys=2 — which claude asserts on every start — used
    to arrive at ``select_graphic_rendition(4, 2)`` and set underline on the
    cursor's attributes. Every cell drawn afterwards carried it, and
    ``repaint_sequence`` rebuilt each one with an SGR 4: the browser's xterm
    then underlined the whole screen, while the terminal reading the raw
    bytes showed nothing of the sort.
    """
    s = ScreenState(20, 3)
    s.feed(b"\x1b[>4;2m")
    s.feed(b"hello")
    assert s.render_screen()[0] == "hello"
    assert not s._screen.buffer[0][0].underscore
    assert b";4m" not in s.repaint_sequence()


def test_xtmodkeys_is_dropped_however_the_chunks_fall():
    """The PTY splits where the read ended, and ScreenFeeder splits again.

    Half a sequence is worse than none: the marker byte says to drop it, and
    the bytes after the cut would land on the grid as text.
    """
    seq = b"\x1b[>4;2m"
    for cut in range(len(seq) + 1):
        s = ScreenState(20, 3)
        s.feed_render(seq[:cut])
        s.feed_render(seq[cut:])
        s.feed_render(b"hi")
        assert s.render_screen()[0] == "hi", cut
        assert not s._screen.buffer[0][0].underscore, cut


def test_kitty_keyboard_pop_leaves_no_text_on_the_grid():
    """``CSI < u`` — the pop claude pairs with ``CSI > 5 u``.

    ``<`` is in neither pyte's ``>`` branch nor its ``?`` one, so it fell
    through to the parameter default and ended the sequence early, leaving
    the final byte to be drawn as a letter.
    """
    s = ScreenState(20, 3)
    s.feed(b"\x1b[<u")
    s.feed(b"X")
    assert s.render_screen()[0] == "X"

    s = ScreenState(20, 3)
    s.feed(b"\x1b[>5u")
    s.feed(b"X")
    assert s.render_screen()[0] == "X"


def test_the_filter_leaves_ordinary_sequences_alone():
    """Guard against over-filtering: only ``<``, ``=`` and ``>`` go."""
    s = ScreenState(20, 3)
    s.feed(b"\x1b[1;4munder\x1b[0m plain")
    assert s.render_screen()[0] == "under plain"
    assert s._screen.buffer[0][0].underscore
    assert not s._screen.buffer[0][6].underscore

    s = ScreenState(20, 3)
    s.feed(b"\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?1h")
    assert s.alt_screen and s.mouse_tracking
    assert s.mouse_encoding == "sgr"
    assert s.app_cursor_keys

    s = ScreenState(20, 3)
    s.feed(b"\x1b]8;id=1;file:///tmp/x\x07link\x1b]8;;\x07 after")
    assert s.render_screen()[0] == "link after"


def test_a_flooding_session_sheds_its_oldest_unrendered_bytes():
    """The queue is bounded. A session writing faster than it renders (a
    background session is paced to ~80 KiB/s, the pi harness was measured at
    60-110 KiB/s) grew the daemon by 1.4 GB in two minutes on 2026-09-11.
    Past the cap the oldest bytes go, the newest stay, and the owner hears
    about it once per episode, not once per chunk."""
    overflows = []

    async def run():
        feeder = ScreenFeeder(
            ScreenState(120, 30), slice_size=64, max_pending=1000,
            on_overflow=overflows.append,
        )
        # Nothing renders until the loop gets a turn, so this is pure queueing.
        for i in range(30):
            feeder.submit(bytes([65 + i]) * 100)      # 'A'*100, 'B'*100, ...
        capped = feeder.pending_bytes
        newest_kept = feeder._pending[-1][:1]
        oldest_kept = feeder._pending[0][:1]
        await feeder.drained()
        feeder.close()
        return capped, oldest_kept, newest_kept, feeder.dropped_bytes

    capped, oldest_kept, newest_kept, dropped = asyncio.run(run())
    assert capped == 1000
    assert newest_kept == bytes([65 + 29])            # the last chunk survived
    assert oldest_kept != b"A"                        # the first did not
    assert dropped == 2000
    assert len(overflows) == 1                        # one episode, one notice
    assert overflows[0] > 0


def test_drain_now_renders_only_the_tail_of_a_long_queue():
    """Session exit renders the queue synchronously on the loop. With a
    queue of hundreds of MB that was minutes of stall (2026-09-11); the last
    words are at the end, so only the end is rendered."""
    screen = ScreenState(120, 30)
    feeder = ScreenFeeder(screen, max_pending=10_000_000)
    old = b"OLD LINE\r\n" * 5000                      # 50 KB that must not matter
    feeder._pending.append(old)                       # bypass the pump: no loop here
    feeder._pending_size += len(old)
    feeder._pending.append(b"\x1b[2J\x1b[Hlast words")
    feeder._pending_size += len(b"\x1b[2J\x1b[Hlast words")
    feeder.drain_now(tail=1024)
    assert feeder.pending_bytes == 0
    assert feeder.dropped_bytes > 0
    assert "last words" in screen.render_screen()[0]


def test_shedding_cuts_on_an_escape_boundary():
    """A cut inside ``ESC[48;2;r;g;bm`` hands pyte ``2;r;g;bB`` -- cursor_down
    with five arguments, which raises. The survivor must start at an ESC."""
    feeder = ScreenFeeder(ScreenState(120, 30), max_pending=10_000_000)
    seq = b"\x1b[48;2;10;20;30mX" * 100          # 1700 bytes of sequences
    feeder._pending.append(seq)
    feeder._pending_size += len(seq)
    dropped = feeder._shed(1000)
    head = feeder._pending[0]
    assert head.startswith(b"\x1b[")
    assert feeder.pending_bytes == len(head) <= 1000
    assert dropped == len(seq) - len(head)


def test_a_render_error_costs_one_slice_not_the_pump():
    """90 pump deaths on 2026-09-11: pyte raised on a sequence and the task
    carrying the exception took the session's screen with it."""
    calls = []

    class Flaky(ScreenState):
        def feed_render(self, data):
            calls.append(data)
            if len(calls) == 2:
                raise TypeError("cursor_down() takes from 1 to 2 positional arguments")
            super().feed_render(data)

    async def run():
        screen = Flaky(120, 30)
        feeder = ScreenFeeder(screen, slice_size=8)
        feeder.submit(b"a" * 8 + b"b" * 8 + b"\x1b[2J\x1b[Hlast")
        await feeder.drained()
        feeder.close()
        return screen, feeder

    screen, feeder = asyncio.run(run())
    assert feeder.render_errors == 1
    assert feeder.pending_bytes == 0
    assert "last" in screen.render_screen()[0]      # rendering went on after the error


def test_background_feeders_share_one_render_budget():
    """Thirty unattended sessions each paced to 80 KiB/s still add up to more
    than pyte can do. The budget is the daemon-wide ceiling: with it at
    20 KiB/s, four background feeders fed 40 KiB each take ~8 s together,
    where per-session pacing alone would have let them finish in one."""
    from claude_launcher.daemon.screen import RenderBudget
    import time as _t

    async def run():
        budget = RenderBudget(20 * 1024)
        feeders = [
            ScreenFeeder(ScreenState(120, 30), foreground=lambda: False,
                         background_delay=0.0, budget=budget)
            for _ in range(4)
        ]
        t0 = _t.monotonic()
        for f in feeders:
            f.submit(b"x" * (40 * 1024))
        await asyncio.gather(*(f.drained() for f in feeders))
        for f in feeders:
            f.close()
        return _t.monotonic() - t0

    elapsed = asyncio.run(run())
    # 160 KiB at 20 KiB/s, minus the 20 KiB burst: about 7 s. Generous bounds.
    assert 4.0 < elapsed < 12.0


def test_a_foreground_feeder_ignores_the_budget():
    from claude_launcher.daemon.screen import RenderBudget
    import time as _t

    async def run():
        budget = RenderBudget(1024)                          # 1 KiB/s: tiny
        feeder = ScreenFeeder(ScreenState(120, 30), foreground=lambda: True, budget=budget)
        t0 = _t.monotonic()
        feeder.submit(b"x" * (64 * 1024))
        await feeder.drained()
        feeder.close()
        return _t.monotonic() - t0

    assert asyncio.run(run()) < 2.0


def test_an_attached_only_feeder_parks_its_tail_until_a_viewer_arrives():
    """``background_render: false``: nothing is rendered unattended, only the
    last ``background_max_pending`` bytes are kept, and ``wake()`` (a viewer
    focusing) renders that tail."""
    focused = {"on": False}

    async def run():
        screen = ScreenState(120, 30)
        feeder = ScreenFeeder(screen, foreground=lambda: focused["on"],
                              background_render=False, background_max_pending=2048)
        feeder.submit(b"old\r\n" * 1000)                     # 5000 bytes, mostly shed
        feeder.submit(b"\x1b[2J\x1b[Hlast words")
        await asyncio.sleep(0.05)
        parked = feeder.pending_bytes
        untouched = screen.render_screen()[0]
        focused["on"] = True
        feeder.wake()
        await feeder.drained()
        feeder.close()
        return parked, untouched, screen.render_screen()[0]

    parked, untouched, after = asyncio.run(run())
    assert 0 < parked <= 2048
    assert untouched == ""                                   # nothing rendered unattended
    assert "last words" in after


def test_history_limit_can_shrink_and_grow():
    s = ScreenState(80, 5, history=100)
    for i in range(50):
        s.feed_render(b"line %d\r\n" % i)
    assert s.history_len > 20
    s.set_history_limit(20)
    assert s.history_limit == 20 and s.history_len == 20
    assert s.render_history()[-1].startswith("line")          # newest rows kept
    s.set_history_limit(100)
    for i in range(50, 100):
        s.feed_render(b"line %d\r\n" % i)
    assert 20 < s.history_len <= 100
