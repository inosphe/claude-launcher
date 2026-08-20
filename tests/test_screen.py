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
