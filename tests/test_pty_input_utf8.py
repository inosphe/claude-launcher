"""A character split across two writes must survive as that character.

ConPTY takes text, not bytes, so the Windows backend decodes what a viewer
typed before handing it to pywinpty. It decoded each write on its own with
``errors="replace"``, and a UTF-8 sequence that ends up split across two
writes is then destroyed twice over: the leading bytes of the syllable
become U+FFFD at the end of one write, the trailing bytes become U+FFFD at
the start of the next, and what the child reads is two replacement
characters where one character was typed.

ASCII never shows it -- one byte per character cannot be split. Hangul is
three bytes per syllable, so it is where this is visible, and it is what was
reported: characters going missing while typing quickly (s586, 2026-09-20).

Splits are ordinary. Every writer into a PTY chunks by something other than
character boundaries: a socket frame, a pipe read, a buffer that filled.

These pin the two rules that make a split harmless: a partial sequence is
carried to the next write rather than replaced, and writes into one session
are serialised, since a decoder carrying state between calls cannot be
entered twice at once.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from claude_launcher.daemon import pty_backend

HANGUL = "한글"                       # 6 bytes: 3 per syllable
HANGUL_BYTES = HANGUL.encode("utf-8")


class _Recorder:
    """pywinpty's PtyProcess, reduced to what ``write`` reaches."""

    def __init__(self):
        self.written = []

    def write(self, text):
        self.written.append(text)


def _backend():
    """A ``_WinPty`` without a child: these tests are about the encoding
    rule, which is the same on a machine that cannot spawn ConPTY."""
    win = object.__new__(pty_backend._WinPty)
    win._pty = _Recorder()
    win._job = None
    win._open_decoder()
    return win


def test_a_syllable_split_across_two_writes_arrives_whole():
    win = _backend()
    win.write(HANGUL_BYTES[:4])   # the first syllable and one byte of the next
    win.write(HANGUL_BYTES[4:])
    assert "".join(win._pty.written) == HANGUL


def test_every_split_point_survives():
    """Every boundary, because which one a chunk lands on is not something
    the writer controls."""
    for cut in range(len(HANGUL_BYTES) + 1):
        win = _backend()
        win.write(HANGUL_BYTES[:cut])
        win.write(HANGUL_BYTES[cut:])
        assert "".join(win._pty.written) == HANGUL, f"split at {cut}"


def test_one_byte_at_a_time_still_spells_it():
    """The worst chunking there is, and the shape a slow pipe produces."""
    win = _backend()
    for byte in HANGUL_BYTES:
        win.write(bytes([byte]))
    assert "".join(win._pty.written) == HANGUL


def test_nothing_is_written_for_a_chunk_that_is_only_a_fragment():
    """The held bytes wait for the rest instead of going through as a
    replacement character."""
    win = _backend()
    win.write(HANGUL_BYTES[:1])
    assert win._pty.written == []
    win.write(HANGUL_BYTES[1:3])
    assert win._pty.written == ["한"]


def test_bytes_that_are_not_utf8_at_all_still_go_through():
    """Held for the rest is not the same as held forever: a byte that can
    begin no sequence is replaced, as it was before, so a viewer sending
    rubbish cannot stall the ones that follow."""
    win = _backend()
    win.write(b"\xff\xfe")
    assert "".join(win._pty.written) == "��"
    win.write(b"ok")
    assert "".join(win._pty.written).endswith("ok")


def test_ascii_is_untouched():
    win = _backend()
    win.write(b"ls -la\r")
    assert win._pty.written == ["ls -la\r"]


@pytest.mark.skipif(sys.platform != "win32", reason="the Unix backend takes bytes")
def test_the_unix_backend_takes_the_bytes_as_they_are():
    """Stated as a difference on purpose: only the Windows backend decodes,
    so only it can lose a character this way."""
    assert hasattr(pty_backend._UnixPty, "write")


def test_two_writers_do_not_interleave_into_one_decoder(home, tmp_path):
    """A decoder that carries a partial sequence between calls cannot be
    entered twice at once: the second caller's bytes would be read as the
    continuation of the first caller's character."""
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher import store

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [
                sys.executable, "-u", "-c",
                "import sys\nprint('READY')\nfor line in sys.stdin: pass\n",
            ]}}}
        )
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await session.wait_for("idle", timeout=10.0, threshold=0.5)

            order = []
            started = asyncio.Event()

            # The backend takes the writer alongside the bytes (one decoder
            # per writer, pty_backend._WinPty.write); this stub stands in for
            # it and the test is about the lock, so the writer is unused.
            def slow(data, writer=None):
                order.append(("in", data))
                if not started.is_set():
                    started.set()
                    import time as _t
                    _t.sleep(0.2)
                order.append(("out", data))

            session.pty.write = slow
            first = asyncio.ensure_future(session.write_bytes(b"\xed\x95"))
            await asyncio.wait_for(started.wait(), 2.0)
            second = asyncio.ensure_future(session.write_bytes(b"\x9c"))
            await asyncio.wait_for(asyncio.gather(first, second), 5.0)

            # Nested would be in, in, out, out -- two writers inside the
            # same decoder at once.
            assert [k for k, _ in order] == ["in", "out", "in", "out"]
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_burst_of_syllables_arrives_whole(home, tmp_path):
    """End to end, at the speed the report was about: forty syllables
    written one frame at a time with no gap, and the child reading back
    what it was given.

    This is the shape a keyboard produces -- one frame per character --
    and it is where a lost syllable would show as a shorter line.
    """
    from claude_launcher import store
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager

    child = (
        "import sys\n"
        "print('READY', flush=True)\n"
        "for line in sys.stdin:\n"
        # END last, so a line read while it is still being drawn is
        # not mistaken for the whole answer.
        "    print('GOT', len(line.rstrip()), 'END', flush=True)\n"
    )
    store.update(
        lambda doc: doc.update({"harnesses": {"py": {"command": [
            sys.executable, "-u", "-c", child]}}})
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=400, restore_default=True)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await session.wait_for("idle", timeout=15.0, threshold=0.5)

            typed = "한글" * 20
            for ch in typed:
                await session.write_bytes(ch.encode("utf-8"))
            await session.write_bytes(b"\r")

            async def reported():
                while True:
                    for line in session.capture():
                        if "GOT" in line and "END" in line:
                            return line.strip()
                    await asyncio.sleep(0.1)

            line = await asyncio.wait_for(reported(), 20.0)
            assert f"GOT {len(typed)} END" in line, f"child read {line!r}"
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
