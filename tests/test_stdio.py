"""The standard streams carry UTF-8 here, whatever the locale codepage is.

On a cp949 machine ``sys.stdin.read()`` decodes a UTF-8 pipe as cp949 —
``claunch mesh send MESH S -`` answered a non-ASCII body with a 500
(claunch-mesh-send-stdin-nonascii-500-qek3) — and ``print()`` encodes a
pipe as cp949 for readers that decode UTF-8, which is every consumer in
this stack. These pin the two edges of ``claude_launcher.stdio``.
"""

from __future__ import annotations

import io
import sys

from claude_launcher import stdio


class _Stdin:
    """The piece of ``sys.stdin`` the helpers read: a byte source."""

    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


def test_read_stdin_decodes_utf8_bytes_not_the_locale(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin("한글 stdin".encode("utf-8")))
    assert stdio.read_stdin() == "한글 stdin"


def test_read_stdin_replaces_bytes_that_are_not_utf8(monkeypatch):
    """A cp949 pipe delivers damaged text rather than nothing — or a crash."""
    monkeypatch.setattr(sys, "stdin", _Stdin("한글".encode("cp949")))
    assert stdio.read_stdin() != "한글"


def test_read_stdin_falls_back_to_a_text_stream(monkeypatch):
    """A swapped-in StringIO has no buffer; what it returns is already text."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("한글"))
    assert stdio.read_stdin() == "한글"


def test_read_stdin_line_reads_one_line_utf8(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin("한 줄\nsecond\n".encode("utf-8")))
    assert stdio.read_stdin_line() == "한 줄\n"


class _Stream:
    """Records what ``harden_console`` asks of a stream."""

    def __init__(self, tty: bool):
        self._tty = tty
        self.calls = []

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_harden_console_pins_a_pipe_to_utf8(monkeypatch):
    """A pipe is an interchange format: every reader here decodes UTF-8, so
    the writer must too — this is the line that used to emit cp949."""
    out, err = _Stream(tty=False), _Stream(tty=False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    stdio.harden_console()
    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_harden_console_leaves_a_console_on_its_codepage(monkeypatch):
    """A real console is written with WriteConsoleW — text, not bytes — so
    re-pointing its encoding would only corrupt a legacy-stdio console.
    The crash guard still applies."""
    out = _Stream(tty=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _Stream(tty=True))
    stdio.harden_console()
    assert out.calls == [{"errors": "replace"}]


def test_harden_console_survives_a_stream_that_refuses(monkeypatch):
    """reconfigure can raise (closed stream, odd wrapper); hardening is a
    courtesy, never a reason the command fails to run."""
    class Refusing(_Stream):
        def reconfigure(self, **kwargs):
            raise OSError("no")

    monkeypatch.setattr(sys, "stdout", Refusing(tty=False))
    monkeypatch.setattr(sys, "stderr", Refusing(tty=False))
    stdio.harden_console()  # must not raise
