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


def _fake_session(*, bracketed: bool):
    writes: list = []

    class FakeSession:
        exited = False
        sdef = SessionDef(name="s")
        screen = ScreenState(80, 24)
        paste = session_mod.Session.paste
        deliver = session_mod.Session.deliver
        send_keys = session_mod.Session.send_keys

        async def write_bytes(self, data: bytes) -> None:
            writes.append(data)

    s = FakeSession()
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
