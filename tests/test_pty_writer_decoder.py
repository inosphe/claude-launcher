"""A half-written character belongs to the writer that started it.

``6588a9df`` gave the Windows backend an incremental decoder so a syllable
split across two writes survives. That fixed one writer's split frames. A
session has several writers -- each viewer, the delivery queue, send-keys --
and they shared the one decoder, so a fragment left by one was finished off
by the next writer's bytes.

The lock in ``Session.write_bytes`` does not close this: it keeps two
writers out of the decoder at the same moment, and the fragment outlives the
call that left it. The gap between a viewer's two frames is exactly where an
automated delivery lands, and this machine's sessions carry deliveries
constantly.

Measured on the two syllables of 한글 split over two frames with one
delivery between them (s586, 2026-09-22), four of the five split points lost
a syllable::

    cut 1 -> U+FFFD [mesh] hello U+FFFD U+FFFD 글    1 of 2
    cut 2 -> U+FFFD [mesh] hello U+FFFD 글           1 of 2
    cut 3 -> 한 [mesh] hello 글                      2 of 2
    cut 4 -> 한 U+FFFD [mesh] hello U+FFFD U+FFFD    1 of 2
    cut 5 -> 한 U+FFFD [mesh] hello U+FFFD           1 of 2

Only the cut that fell on a character boundary came through. So each writer
gets its own decoder, and these pin that: a fragment waits for the writer
that left it, another writer's bytes are decoded on their own, and a writer
that goes away takes its fragment with it.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from claude_launcher.daemon import pty_backend

HANGUL = "한글"
HANGUL_BYTES = HANGUL.encode("utf-8")
DELIVERY = "[mesh] hello\r".encode("utf-8")


class _Recorder:
    """pywinpty's PtyProcess, reduced to what ``write`` reaches."""

    def __init__(self):
        self.written = []

    def write(self, text):
        self.written.append(text)

    @property
    def text(self):
        return "".join(self.written)


def _backend():
    win = object.__new__(pty_backend._WinPty)
    win._pty = _Recorder()
    win._job = None
    win._open_decoder()
    return win


VIEWER, DELIVERER = "viewer", "deliverer"


@pytest.mark.parametrize("cut", [1, 2, 3, 4, 5])
def test_a_delivery_between_two_frames_costs_the_syllable_nothing(cut):
    """The case that was reported. Every split point, not only the awkward
    ones: the cut that lands on a character boundary already worked and has
    to keep working."""
    win = _backend()
    win.write(HANGUL_BYTES[:cut], VIEWER)
    win.write(DELIVERY, DELIVERER)
    win.write(HANGUL_BYTES[cut:], VIEWER)
    out = win._pty.text
    assert "\ufffd" not in out, f"cut {cut} produced replacement characters: {out!r}"
    assert out.replace("[mesh] hello\r", "") == HANGUL


def test_the_delivery_itself_is_not_corrupted():
    """The other half of the same failure: the delivery was prefixed with a
    replacement character, because its first byte completed nothing."""
    win = _backend()
    win.write(HANGUL_BYTES[:2], VIEWER)
    win.write(DELIVERY, DELIVERER)
    assert win._pty.text == "[mesh] hello\r"


def test_two_viewers_typing_at_once_keep_their_own_characters():
    """Two people on one terminal, both mid-syllable. Neither finishes the
    other's character."""
    win = _backend()
    a, b = "sock-a", "sock-b"
    win.write("한".encode("utf-8")[:2], a)
    win.write("글".encode("utf-8")[:1], b)
    win.write("한".encode("utf-8")[2:], a)
    win.write("글".encode("utf-8")[1:], b)
    assert win._pty.text == "한글"


def test_one_writer_split_over_many_frames_is_unchanged():
    """The rule 6588a9df established still holds, per writer: a syllable fed
    one byte at a time arrives as that syllable."""
    win = _backend()
    for byte in HANGUL_BYTES:
        win.write(bytes([byte]), VIEWER)
    assert win._pty.text == HANGUL


def test_the_default_writer_is_a_writer_like_any_other():
    """The paths a session owns (delivery, send-keys, paste) pass no key, so
    they share one decoder among themselves -- correct, the same lock
    serialises them -- and do not share a viewer's."""
    win = _backend()
    win.write(HANGUL_BYTES[:4], None)      # the session writes it, split
    win.write(b"x", VIEWER)                # a viewer types between the halves
    win.write(HANGUL_BYTES[4:], None)
    assert win._pty.text == "한x글"


def test_a_writer_that_goes_away_takes_its_fragment_with_it():
    """Held, the fragment would be handed to whoever next writes under the
    same key -- a socket object reused, or simply a new viewer."""
    win = _backend()
    win.write(HANGUL_BYTES[:2], VIEWER)
    win.forget_writer(VIEWER)
    win.write(b"ok\r", VIEWER)
    assert win._pty.text == "ok\r"


def test_forgetting_an_unknown_writer_is_not_an_error():
    win = _backend()
    win.forget_writer("nobody")


def test_the_decoder_table_does_not_grow_without_bound():
    """A caller that invented a key per write would otherwise hold one
    decoder per write. Past the cap the oldest is dropped, which costs that
    writer a partial character and nothing else."""
    win = _backend()
    for i in range(pty_backend._WinPty.MAX_WRITERS + 20):
        win.write(b"x", f"w{i}")
    assert len(win._in) <= pty_backend._WinPty.MAX_WRITERS
    assert win._pty.text == "x" * (pty_backend._WinPty.MAX_WRITERS + 20)


def test_a_writer_still_typing_keeps_its_fragment_across_other_traffic():
    """The cap is on writers, not on writes: a viewer mid-syllable holds its
    fragment while the other writers of one session -- the delivery queue,
    send-keys, another viewer -- write as much as they like."""
    win = _backend()
    win.write(HANGUL_BYTES[:1], VIEWER)                 # first byte only
    for i in range(500):
        win.write(b".", DELIVERER)
    win.write(HANGUL_BYTES[1:], VIEWER)                 # the rest, much later
    assert win._pty.text.replace(".", "") == HANGUL


def test_the_oldest_writer_is_dropped_once_the_cap_is_reached():
    """The cap is a backstop against a caller that invents a key per write
    rather than per writer. What it costs the dropped writer is the partial
    character it had not finished; it is not reached by a session's own
    writers, which number in single digits."""
    win = _backend()
    win.write(HANGUL_BYTES[:1], VIEWER)
    for i in range(pty_backend._WinPty.MAX_WRITERS):
        win.write(b".", "passer-%d" % i)
    assert VIEWER not in win._in
    assert len(win._in) <= pty_backend._WinPty.MAX_WRITERS


def test_the_unix_backend_ignores_the_writer():
    """Bytes go to the master as they are; there is nothing to hold."""
    import io

    class _Master:
        def __init__(self):
            self.seen = b""

    if not hasattr(pty_backend, "_UnixPty"):
        pytest.skip("no unix backend in this build")
    unix = object.__new__(pty_backend._UnixPty)
    written = []
    unix._master = 7
    import os as _os

    real = _os.write
    _os.write = lambda fd, data: written.append((fd, data)) or len(data)
    try:
        unix.write(HANGUL_BYTES[:2], "a")
        unix.write(HANGUL_BYTES[2:], "b")
    finally:
        _os.write = real
    assert b"".join(d for _, d in written) == HANGUL_BYTES
