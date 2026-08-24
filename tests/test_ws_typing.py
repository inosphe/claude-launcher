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

    def note_human_input(self, *, at_terminal=False):
        self.marks += 1
        self.at_terminal = at_terminal

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


def test_other_controls_do_not_mark_the_keyboard():
    s = _Session()
    _control(s, {"type": "ping"})
    assert s.marks == 0
