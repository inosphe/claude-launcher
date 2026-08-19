"""One way to hand an agent a message, enforced.

Injecting a message into a session looks like a one-liner and is not: the
submitting Enter has to arrive in its own PTY write, or a bracketed-paste TUI
(Claude Code) folds it into the pasted text and the message sits in the
composer forever. That has been re-learned once per hand-rolled sender, so the
knowledge lives in exactly one place -- ``Session.deliver`` -- and these tests
fail the build when a new caller starts assembling its own writes instead.

If a test here fails, the fix is almost never to extend the allowlist: call
``session.deliver(text)`` (in-process) or ``POST /api/sessions/{name}/deliver``
(out-of-process).
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest

from claude_launcher.daemon import keys as keys_mod, session as session_mod
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.screen import ScreenState

SRC = Path(__file__).resolve().parents[1] / "src" / "claude_launcher"

#: Low-level ways to put bytes in a PTY. Everything automated must go through
#: ``Session.deliver`` instead; these stay reachable for the raw keyboard
#: passthrough a human drives (web terminal, ``claunch send-keys``).
RAW_WRITERS = {"paste", "send_keys", "write_bytes"}

#: (module, enclosing function) pairs allowed to call them, each with the
#: reason it is not a message sender.
RAW_WRITER_ALLOWLIST = {
    ("daemon/session.py", "deliver"): "the one message sender",
    ("daemon/session.py", "paste"): "implements the paste + delayed-CR rule",
    ("daemon/session.py", "send_keys"): "implements the raw keystroke path",
    ("daemon/api.py", "h_session_keys"): "raw keyboard passthrough over HTTP",
    ("daemon/ws.py", "terminal_ws"): "raw keyboard passthrough over WebSocket",
}

#: Same rule across the process boundary: the keys endpoint is the passthrough,
#: so an out-of-process sender that posts to it is re-implementing delivery.
KEYS_ENDPOINT_ALLOWLIST = {
    ("daemon/api.py", "build_app"): "declares the route",
    ("cli_sessions.py", "_cmd_send_keys"): "the user-facing send-keys command",
}


def _module_files():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), path


def _calls_with_scope(tree: ast.AST):
    """Yield (node, enclosing def name or None) for every Call in the tree."""
    scopes = []

    class Walker(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            scopes.append(node.name)
            self.generic_visit(node)
            scopes.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_Call(self, node):  # noqa: N802
            found.append((node, scopes[-1] if scopes else None))
            self.generic_visit(node)

    found: list = []
    Walker().visit(tree)
    return found


def test_only_deliver_writes_messages_into_a_session():
    offenders = []
    for rel, path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, scope in _calls_with_scope(tree):
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in RAW_WRITERS:
                continue
            if (rel, scope) in RAW_WRITER_ALLOWLIST:
                continue
            offenders.append(f"{rel}:{node.lineno} in {scope or '<module>'}() "
                             f"calls .{func.attr}()")
    assert not offenders, (
        "these call the raw PTY writers directly; use session.deliver(text) so "
        "the paste/Enter chunking stays in one place:\n  " + "\n  ".join(offenders)
    )


def test_no_second_http_path_for_delivering_messages():
    offenders = []
    for rel, path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, scope in _calls_with_scope(tree):
            if (rel, scope) in KEYS_ENDPOINT_ALLOWLIST:
                continue
            for arg in ast.walk(node):
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                if "/keys" in arg.value:
                    offenders.append(
                        f"{rel}:{node.lineno} in {scope or '<module>'}()"
                    )
    assert not offenders, (
        "these post to the raw /keys passthrough; automated senders must use "
        "POST /api/sessions/{name}/deliver:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def _fake_session(*, bracketed: bool, ready: bool = True):
    """A stand-in Session. ``ready`` is the latch every already-running session
    carries; the readiness tests below clear it to watch it being set."""
    writes: list = []

    class FakeSession:
        exited = False
        sdef = SessionDef(name="s")
        screen = ScreenState(80, 24)
        idle_threshold = 0.0
        _last_human_input = 0.0
        paste = session_mod.Session.paste
        deliver = session_mod.Session.deliver
        send_keys = session_mod.Session.send_keys
        _await_readable = session_mod.Session._await_readable
        _await_keyboard_quiet = session_mod.Session._await_keyboard_quiet
        note_human_input = session_mod.Session.note_human_input
        keyboard_busy = session_mod.Session.keyboard_busy

        def status(self, threshold=None):
            return session_mod.STATUS_IDLE

        async def write_bytes(self, data: bytes) -> None:
            writes.append(data)

    s = FakeSession()
    s._started_mono = time.monotonic()  # a session that just came up
    s._input_ready = ready
    if bracketed:
        s.screen.feed(b"\x1b[?2004h")
    return s, writes


def test_deliver_sends_the_enter_as_its_own_write(monkeypatch):
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    s, writes = _fake_session(bracketed=True)
    assert asyncio.run(s.deliver("cflow: go")) is True
    assert writes == [b"\x1b[200~cflow: go\x1b[201~", b"\r"]


def test_deliver_reports_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    s, _ = _fake_session(bracketed=True)

    async def boom(data: bytes) -> None:
        raise session_mod.SessionGone("gone")

    s.write_bytes = boom
    # Callers hold their position on a False; a raised exception would take
    # down whatever background loop was doing the sending.
    assert asyncio.run(s.deliver("cflow: go")) is False


def test_send_keys_splits_a_trailing_enter_for_a_paste_aware_tui(monkeypatch):
    """Even the raw path cannot produce the typed-but-unsent symptom: a human
    running 'claunch send-keys s1 hello Enter' against Claude Code gets the
    same two writes."""
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    s, writes = _fake_session(bracketed=True)
    asyncio.run(s.send_keys(["hello", "Enter"]))
    assert writes == [b"hello", b"\r"]


def test_send_keys_leaves_a_plain_program_alone(monkeypatch):
    """A program that never asked for bracketed paste (a shell, a REPL) reads
    line-buffered input and does not care -- don't add latency for it."""
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    s, writes = _fake_session(bracketed=False)
    asyncio.run(s.send_keys(["hello", "Enter"]))
    assert writes == [b"hello\r"]


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"hello\r", (b"hello", b"\r")),
        (b"hello\n", (b"hello", b"\n")),
        (b"\r", (b"\r", b"")),          # a bare Enter is already its own write
        (b"", (b"", b"")),
        (b"hello", (b"hello", b"")),
    ],
)
def test_split_submit(data, expected):
    assert keys_mod.split_submit(data) == expected


# --------------------------------------------------------------------------- #
# the other half of the rule: two writes are only two *reads* if the program is
# reading. A harness that has gone quiet may not be there yet.
# --------------------------------------------------------------------------- #
def test_deliver_waits_for_a_booting_tui_to_take_the_keyboard(monkeypatch):
    """Quiet is not ready.

    A just-started Claude Code prints its banner and pauses long enough to read
    as idle well before its input exists. Written into that gap, the paste and
    its separately-written CR sit in one buffer and come out of one read --
    which folds the CR into the text and leaves the message typed but never
    sent, the exact symptom the split was introduced to prevent.
    """
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "INPUT_SETTLE", 0.0)
    s, writes = _fake_session(bracketed=False, ready=False)

    async def run():
        sending = asyncio.ensure_future(s.deliver("cflow: go"))
        await asyncio.sleep(0.3)
        assert writes == [], "wrote into a terminal that was not reading"
        s.screen.feed(b"\x1b[?2004h")  # the TUI takes the keyboard
        assert await asyncio.wait_for(sending, timeout=5) is True

    asyncio.run(run())
    assert writes == [b"\x1b[200~cflow: go\x1b[201~", b"\r"]


def test_deliver_waits_out_the_startup_that_follows_the_keyboard(monkeypatch):
    """...and taking the keyboard is not ready either.

    Claude Code enables bracketed paste partway through startup and keeps
    loading for several seconds after; a submit sent in there is dropped even
    though the paste before it lands and renders. So the mode has to be on AND
    the session quiet since, which is what marks the end of it.
    """
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "INPUT_SETTLE", 0.5)
    s, writes = _fake_session(bracketed=True, ready=False)
    busy = {"now": True}
    s.status = lambda threshold=None: (
        session_mod.STATUS_BUSY if busy["now"] else session_mod.STATUS_IDLE
    )

    async def run():
        sending = asyncio.ensure_future(s.deliver("cflow: go"))
        await asyncio.sleep(0.3)
        assert writes == [], "wrote into a harness that was still starting"
        busy["now"] = False
        assert await asyncio.wait_for(sending, timeout=5) is True

    asyncio.run(run())
    assert writes == [b"\x1b[200~cflow: go\x1b[201~", b"\r"]
    assert s._input_ready is True  # latched: the next message pays nothing


def test_deliver_does_not_wait_on_a_harness_that_never_sets_the_mode():
    """A shell or a REPL never enables bracketed paste, so waiting for it would
    be waiting forever. Only the harness known to be an Ink TUI is held back."""
    s, writes = _fake_session(bracketed=False, ready=False)
    s.sdef = SessionDef(name="s", harness="py")
    assert asyncio.run(s.deliver("echo hi")) is True
    assert writes == [b"echo hi", b"\r"]


def test_deliver_gives_up_waiting_once_the_session_is_no_longer_young():
    """The wait is bounded from the moment the session was spawned: a harness
    that never settles delays a message rather than losing it."""
    s, writes = _fake_session(bracketed=False, ready=False)
    s._started_mono = time.monotonic() - session_mod.INPUT_READY_TIMEOUT - 1
    assert asyncio.run(s.deliver("cflow: go")) is True
    assert writes == [b"cflow: go", b"\r"]


# --------------------------------------------------------------------------- #
# ...and the program is not the only party reading the terminal: a HUMAN may be
# mid-keystroke. Typing keeps the screen changing, but the thinking pauses
# inside composing a message outlast the idle threshold — a paste-plus-Enter
# injected into one submits the half-typed line with the delivery folded in.
# --------------------------------------------------------------------------- #
def test_deliver_holds_while_a_human_is_typing(monkeypatch):
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    s, writes = _fake_session(bracketed=True)
    s.note_human_input()  # a keystroke just landed in this terminal

    async def run():
        sending = asyncio.ensure_future(s.deliver("mesh: hello"))
        await asyncio.sleep(0.3)
        assert writes == [], "pasted into a message a human was composing"
        # the keyboard goes quiet: age the mark past the guard window
        s._last_human_input = time.monotonic() - session_mod.TYPING_GUARD
        assert await asyncio.wait_for(sending, timeout=5) is True

    asyncio.run(run())
    assert writes == [b"\x1b[200~mesh: hello\x1b[201~", b"\r"]


def test_deliver_gives_up_the_typing_hold_rather_than_losing_the_message(
    monkeypatch,
):
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 0.2)
    s, writes = _fake_session(bracketed=True)
    s.note_human_input()
    assert asyncio.run(s.deliver("mesh: hello")) is True
    assert writes == [b"\x1b[200~mesh: hello\x1b[201~", b"\r"]


def test_send_keys_counts_as_human_typing(monkeypatch):
    """The raw passthrough IS the human ('claunch send-keys'; attach and web
    keystrokes are marked by the WebSocket handler): every keystroke restarts
    the quiet window a delivery must see before it may type."""
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    s, _ = _fake_session(bracketed=False)
    assert s.keyboard_busy() is False
    asyncio.run(s.send_keys(["h"]))
    assert s.keyboard_busy() is True
