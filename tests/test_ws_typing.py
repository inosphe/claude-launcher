"""The web terminal's ``typing`` control frame marks the keyboard busy.

A keystroke frame (BINARY) already marks it, but an IME composing Hangul or a
phone keyboard mid-word sends nothing until the syllable/word commits — so the
web client sends a ``{"type": "typing"}`` mark on keydown/composition events
and the daemon treats it exactly like a keystroke for the delivery hold.
"""

from __future__ import annotations

import asyncio
import json

from claude_launcher.daemon import ws as ws_mod


class _Session:
    def __init__(self):
        self.marks = 0
        self.writes = []

    def note_human_input(self, *, at_terminal=False, data=None, composing=False):
        self.marks += 1
        self.at_terminal = at_terminal
        self.data = data
        self.composing = composing

    async def write_bytes(self, data):
        self.writes.append(data)


class _WS:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(s)


def _control(session, msg):
    ws = _WS()
    asyncio.run(
        ws_mod._handle_control(ws, session, json.dumps(msg), ws_mod.ViewerState())
    )
    return ws


def test_typing_mark_restarts_the_guard_and_writes_nothing():
    s = _Session()
    ws = _control(s, {"type": "typing"})
    assert s.marks == 1
    assert s.at_terminal is True  # a person at a terminal, not a send-keys
    assert s.writes == []       # a mark, never a keystroke
    assert ws.sent == []        # and nothing to answer


def test_a_bare_typing_mark_does_not_claim_an_unsent_draft():
    """A mark says "somebody is at this keyboard" and nothing more. The web
    client sends ``draft`` only for events that put text into the composer,
    because a draft opened by a held modifier is one no later keystroke can
    close — and it would hold this session's mail for DRAFT_GUARD."""
    s = _Session()
    _control(s, {"type": "typing"})
    assert s.composing is False


def test_a_composing_mark_opens_the_draft_the_wire_cannot_show():
    """The case the mark exists for: an IME holding a Hangul syllable sends
    no bytes at all, so only the client can say a line is being written."""
    s = _Session()
    _control(s, {"type": "typing", "draft": True})
    assert (s.at_terminal, s.composing) == (True, True)
    assert s.writes == []


def test_other_controls_do_not_mark_the_keyboard():
    s = _Session()
    _control(s, {"type": "ping"})
    assert s.marks == 0


def test_codex_osc_colour_answers_are_not_keyboard_input():
    """xterm.js emits OSC 10/11 answers through the same event as keys.

    Codex consumes the ESC characters as Escape keys and otherwise leaves the
    response text in its composer, so the terminal bridge must discard only
    these complete automatic answers.
    """
    foreground = b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"
    background = b"\x1b]11;rgb:1414/1616/1a1a\x1b\\"

    assert ws_mod._is_codex_osc_color_response("codex", foreground)
    assert ws_mod._is_codex_osc_color_response("codex", foreground + background)
    assert not ws_mod._is_codex_osc_color_response("claude", foreground)
    assert not ws_mod._is_codex_osc_color_response("codex", b"\x1b[<35;10;5M")
    assert not ws_mod._is_codex_osc_color_response("codex", b"]10;rgb:ffff/ffff/ffff\\")
