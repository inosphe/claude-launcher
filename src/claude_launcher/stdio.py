"""UTF-8 at the process's standard streams, whatever the console codepage is.

On a Korean Windows machine the locale streams are cp949, and that corrupts
the interchange on both edges:

* ``sys.stdin.read()`` decodes the pipe as cp949 — ``printf '한글' |
  claunch mesh send M S -`` arrives as mojibake and the daemon answers the
  payload with a 500 (claunch-mesh-send-stdin-nonascii-500-qek3).
* ``print()`` into a pipe encodes cp949 — a Git Bash pty, a harness capture
  or a file redirect decodes UTF-8 and shows mojibake, while a child that
  writes UTF-8 itself (``br``, ``git``) reads fine right next to it.

The bytes on both wires are UTF-8 here, so the streams should be too. A real
console is exempt on both sides — Python reaches it with
ReadConsoleW/WriteConsoleW and a codepage never enters the transfer — so the
helpers below only re-point pipes and files, the transports that carry bytes.
"""

from __future__ import annotations

import sys


def harden_console() -> None:
    """Keep stdio from corrupting text, in both directions it can go wrong.

    ``errors="replace"`` alone (the old shape of this) stopped the crash but
    kept the codepage: piped output left as cp949 and every UTF-8 reader saw
    mojibake. Pipes are an interchange format, so they get UTF-8 pinned
    outright; a console keeps its encoding because its writes never go
    through a codec at all (WriteConsoleW takes text, not bytes).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _stdin_buffer():
    stdin = sys.stdin
    if stdin is None:
        return None
    return getattr(stdin, "buffer", None)


def read_stdin() -> str:
    """All of stdin as text, decoded UTF-8.

    ``sys.stdin.read()`` decodes with the locale — cp949 on this machine —
    so any UTF-8 producer (a shell pipe, a heredoc, another tool) arrives
    as mojibake. Reading the buffer skips the codec entirely; a stream
    without one (a test double, a caller that swapped stdin) falls back to
    whatever it already is. ``errors="replace"`` because a mangled message
    is still a message — better delivered damaged than turned into a 500.
    """
    buf = _stdin_buffer()
    if buf is None:
        return "" if sys.stdin is None else sys.stdin.read()
    return buf.read().decode("utf-8", errors="replace")


def read_stdin_line() -> str:
    """One line of stdin, decoded UTF-8 — the same rule as read_stdin."""
    buf = _stdin_buffer()
    if buf is None:
        return "" if sys.stdin is None else sys.stdin.readline()
    return buf.readline().decode("utf-8", errors="replace")
