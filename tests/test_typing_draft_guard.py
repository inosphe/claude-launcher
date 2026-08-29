"""A delivery must not land inside a line a human is still writing.

The keyboard guard used to be a stopwatch: "did anyone press a key in the
last few seconds". Two things fall through a stopwatch, and both of them
were reported from the web terminal as a message spliced into a half-typed
prompt.

* **The thinking pause.** Writing a paragraph is not a continuous stream of
  keystrokes. People stop to read, to look something up, to choose a word —
  regularly for longer than ``TYPING_GUARD``. The composer is still full;
  the stopwatch says the keyboard is free.
* **The long prompt.** Every wait here is bounded, and the bound used to end
  in *type it anyway*: ``TYPING_HOLD_TIMEOUT`` seconds of composing and the
  paste went in regardless. A long prompt therefore did not risk a splice,
  it guaranteed one — which is why short messages "worked fine" and real
  ones did not.

So the guard tracks the composer's *state* as well as the keyboard's
timing: the keys themselves say whether an unsent line exists (a draft is
opened by typing and closed by the human's own Enter, ``C-c`` or ``C-u`` —
never by a timer), and while one is open no automated write may land. The
message is not lost and not stored anywhere new: ``deliver`` returns False
with nothing typed, so the sender keeps it exactly as it was and the next
attempt goes in behind the human's Enter.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.screen import ScreenState
from claude_launcher.daemon.session import KeyboardHeld

BEARER = {"Authorization": "Bearer sekrit"}


def _fake_session(*, bracketed: bool = True):
    """A stand-in Session wired to the real guard, deliver and passthrough."""
    writes: list = []

    class FakeSession:
        exited = False
        sdef = SessionDef(name="s")
        screen = ScreenState(80, 24)
        idle_threshold = 0.0
        _last_human_input = 0.0
        _last_terminal_input = 0.0
        _draft_open = False
        paste = session_mod.Session.paste
        deliver = session_mod.Session.deliver
        send_keys = session_mod.Session.send_keys
        _await_readable = session_mod.Session._await_readable
        await_keyboard_quiet = session_mod.Session.await_keyboard_quiet
        note_human_input = session_mod.Session.note_human_input
        keyboard_busy = session_mod.Session.keyboard_busy
        draft_open = session_mod.Session.draft_open

        def status(self, threshold=None):
            return session_mod.STATUS_IDLE

        async def write_bytes(self, data: bytes) -> None:
            writes.append(data)

    s = FakeSession()
    s._started_mono = time.monotonic()
    s._input_ready = True
    if bracketed:
        s.screen.feed(b"\x1b[?2004h")
    return s, writes


# --------------------------------------------------------------------------- #
# what the keys themselves say about the composer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "data, expected",
    [
        (b"hello", True),                 # plain typing
        ("한글".encode(), True),           # a committed Hangul syllable
        (b" ", True),                     # a space is a character too
        (b"\r", False),                   # Enter submits: the composer empties
        (b"\n", False),
        (b"\x03", False),                 # C-c discards the line
        (b"\x15", False),                 # C-u kills it
        (b"\x1b\r", True),                # Alt/Shift-Enter: a newline IN the draft
        (b"\\\r", True),                  # ...and its backslash spelling
        (b"\x1b[A", None),                # an arrow key is not typing
        (b"\x1bOB", None),                # ...in either encoding
        (b"\x1b", None),                  # a lone Escape decides nothing
        (b"\x7f", None),                  # nor does a backspace
        (b"\x1b[200~hi\x1b[201~", True),  # a paste fills the composer
        (b"ab\rcd", True),                # sent, then started again
        (b"ab\r", False),                 # ...and the other way round
    ],
)
def test_draft_state_is_read_from_the_keystrokes(data, expected):
    """``None`` matters as much as the other two: a key that says nothing
    about the composer must LEAVE a draft open. An arrow key pressed inside
    a half-written prompt is still a half-written prompt."""
    assert session_mod.draft_state_from_bytes(data) is expected


def test_a_thinking_pause_does_not_open_the_terminal_to_a_delivery(monkeypatch):
    """The first of the two holes, exactly as reported: type, pause longer
    than the stopwatch, and the line is still sitting there unsent."""
    monkeypatch.setattr(session_mod, "TYPING_GUARD", 0.05)
    s, _ = _fake_session()
    s.note_human_input(at_terminal=True, data=b"webui ")
    time.sleep(0.1)  # longer than the guard: a pause to think
    assert s.draft_open() is True
    assert s.keyboard_busy() is True
    # ...and their Enter is what ends it, not the clock
    s.note_human_input(at_terminal=True, data=b"\r")
    assert s.draft_open() is False
    assert s.keyboard_busy(terminal_only=True) is True  # still recent, though


def test_an_abandoned_draft_stops_holding_the_session_eventually(monkeypatch):
    """The cap, and the only thing it is for. A draft closes on a keypress,
    so one typed character and a walk to lunch would otherwise hold this
    session's mail forever — and with nobody at the keyboard there is
    nothing left to protect."""
    monkeypatch.setattr(session_mod, "DRAFT_GUARD", 0.1)
    s, _ = _fake_session()
    s.note_human_input(at_terminal=True, data=b"x")
    assert s.draft_open() is True
    time.sleep(0.15)
    assert s.draft_open() is False


def test_send_keys_does_not_open_a_draft_of_its_own():
    """``send-keys`` types a line and its Enter together. If its own text
    opened a draft, the next automated line would wait behind a composer
    nobody is sitting at — and a script driving a session would deadlock
    against itself."""
    s, _ = _fake_session()
    asyncio.run(s.send_keys(["hello"]))
    assert s.draft_open() is False


# --------------------------------------------------------------------------- #
# the rule: no automated write into a half-written line
# --------------------------------------------------------------------------- #
def test_deliver_refuses_rather_than_splicing_into_an_unsent_line(monkeypatch):
    """The second hole. The hold is bounded — it must be, a background sender
    cannot be parked forever — but running out of patience is not a licence
    to type into somebody's sentence. Nothing is written, nothing is stored,
    and False hands the message back to the sender it came from."""
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 0.2)
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"a long prompt in progress")

    assert asyncio.run(s.deliver("mesh: hello")) is False
    assert writes == [], "pasted into a line the human had not sent"


def test_the_delivery_lands_the_moment_the_human_sends_their_own_line(
    monkeypatch,
):
    """And the wait really is theirs to end: no timer, no window, just their
    Enter. This is what makes refusing safe — the delay is 'until they send
    the prompt they are typing', which is where the message wanted to go
    anyway."""
    monkeypatch.setattr(session_mod, "TYPING_GUARD", 0.0)
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "delivery_stamp", lambda: "[T]")
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"my own prompt")

    async def run():
        sending = asyncio.ensure_future(s.deliver("mesh: hello"))
        await asyncio.sleep(0.3)
        assert writes == []
        s.note_human_input(at_terminal=True, data=b"\r")  # they hit Enter
        assert await asyncio.wait_for(sending, timeout=5) is True

    asyncio.run(run())
    assert writes == [b"\x1b[200~[T]\rmesh: hello\x1b[201~", b"\r"]


# --------------------------------------------------------------------------- #
# ...and the one exception to it: a person who says "deliver now"
# --------------------------------------------------------------------------- #
def test_a_forced_delivery_submits_the_unsent_line_instead_of_refusing(
    monkeypatch,
):
    """The refusal above is right for a background sender and wrong for the
    only sender that is a human pressing a button: it answers "still waiting"
    to somebody who is watching, which is the same as answering nothing.

    So ``force`` delivers — and still does not splice. The unsent line is
    SUBMITTED first, as its own bare CR, and the paste lands on the composer
    that clears behind it. Both texts survive whole, in the order they were
    written, which is the part of the refusal worth keeping.
    """
    monkeypatch.setattr(session_mod, "FORCE_TYPING_GRACE", 0.05)
    monkeypatch.setattr(session_mod, "FORCE_DRAFT_SETTLE", 0.0)
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "delivery_stamp", lambda: "[T]")
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"a long prompt in progress")

    assert s.draft_open() is True
    assert asyncio.run(s.deliver("mesh: hello", force=True)) is True
    # Their Enter first — the line goes to the agent as they wrote it — then
    # the delivery as a separate message. Never one write containing both.
    assert writes == [
        b"\r",
        b"\x1b[200~[T]\rmesh: hello\x1b[201~",
        b"\r",
    ]


def test_a_forced_delivery_does_not_park_for_the_background_sender_s_timeout(
    monkeypatch,
):
    """The wait shortens as well as ending differently. ``force`` is reached
    through an HTTP request somebody is holding open, and the ordinary
    ``TYPING_HOLD_TIMEOUT`` would spend half a minute of it before doing what
    it was always going to do."""
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 30.0)
    monkeypatch.setattr(session_mod, "FORCE_TYPING_GRACE", 0.05)
    monkeypatch.setattr(session_mod, "FORCE_DRAFT_SETTLE", 0.0)
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "delivery_stamp", lambda: "[T]")
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"still typing")

    started = time.monotonic()
    assert asyncio.run(s.deliver("mesh: hello", force=True)) is True
    assert time.monotonic() - started < 5.0, (
        "a forced delivery waited out the background sender's hold"
    )
    assert writes[0] == b"\r"


def test_a_forced_delivery_with_a_quiet_keyboard_writes_nothing_extra(
    monkeypatch,
):
    """No draft, no stray CR. The submitted line exists to clear a composer
    that has something in it; sending one into an empty composer would put a
    blank message in somebody's transcript every time the button is used on
    an idle session."""
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "delivery_stamp", lambda: "[T]")
    s, writes = _fake_session()

    assert s.draft_open() is False
    assert asyncio.run(s.deliver("mesh: hello", force=True)) is True
    assert writes == [b"\x1b[200~[T]\rmesh: hello\x1b[201~", b"\r"]


def test_keys_that_leave_no_draft_still_get_the_old_bounded_hold(monkeypatch):
    """The refusal is scoped to a half-written line, not to a busy keyboard.
    Thirty seconds of arrows and modifiers leaves the composer empty, there
    is nothing for the paste to land in, and the bound goes on meaning what
    it always meant: delayed, then delivered."""
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 0.2)
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    monkeypatch.setattr(session_mod, "delivery_stamp", lambda: "[T]")
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"\x1b[A")  # an arrow, not a line

    assert asyncio.run(s.deliver("mesh: hello")) is True
    assert writes == [b"\x1b[200~[T]\rmesh: hello\x1b[201~", b"\r"]


def test_send_keys_text_is_refused_rather_than_spliced(monkeypatch):
    """The path the report actually named. There is no queue behind this
    door — the sender is a person or an agent running ``claunch send-keys``
    — so the honest answer is an error it can retry, not a line typed
    through somebody's prompt."""
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 0.2)
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"half a prompt")

    with pytest.raises(KeyboardHeld):
        asyncio.run(s.send_keys(["do X", "Enter"]))
    assert writes == []


def test_forced_send_keys_submits_open_draft_and_sends_prompt(monkeypatch):
    """The operator's explicit web send preserves an open prompt and is
    bounded by the short forced grace instead of the background 30s hold."""
    monkeypatch.setattr(session_mod, "FORCE_TYPING_GRACE", 0.01)
    monkeypatch.setattr(session_mod, "FORCE_DRAFT_SETTLE", 0.0)
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"half a prompt")

    asyncio.run(s.send_keys(["do X", "Enter"], force=True))

    assert writes == [b"\r", b"do X", b"\r"]
    assert s.draft_open() is False


@pytest.mark.parametrize("args", [["Enter"], ["C-c"], ["Escape"], ["Up"]])
def test_bare_keys_still_go_through_an_open_draft(monkeypatch, args):
    """Unchanged, and deliberately: an interrupt is wanted the instant it was
    sent, and it carries no text to splice. ``C-c`` reaching a session whose
    operator is mid-sentence is the whole reason someone reaches for it."""
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 30.0)
    s, writes = _fake_session()
    s.note_human_input(at_terminal=True, data=b"half a prompt")

    asyncio.run(asyncio.wait_for(s.send_keys(args), timeout=1))
    assert len(writes) == 1


# --------------------------------------------------------------------------- #
# and the same answer across the process boundary
# --------------------------------------------------------------------------- #
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def test_the_keys_endpoint_answers_409_while_a_draft_is_open(home, tmp_path,
                                                             monkeypatch):
    """``claunch send-keys`` is the same refusal one process further out: a
    retryable status and a sentence saying nothing was sent, rather than a
    200 for keys that went into someone's prompt."""
    from aiohttp.test_utils import TestClient, TestServer

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 0.2)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await session.wait_for("idle", timeout=10.0, threshold=0.5)
            session.note_human_input(at_terminal=True, data=b"a prompt")

            resp = await client.post(
                "/api/sessions/s1/keys", json={"keys": ["do X", "Enter"]},
                headers=BEARER,
            )
            assert resp.status == 409
            assert "typing" in (await resp.json())["error"]

            # a paste is text by another name, and gets the same answer
            resp = await client.post(
                "/api/sessions/s1/keys", json={"paste": "do X", "enter": True},
                headers=BEARER,
            )
            assert resp.status == 409

            # their Enter ends it, and the very next attempt is a 200
            session.note_human_input(at_terminal=True, data=b"\r")
            resp = await client.post(
                "/api/sessions/s1/keys", json={"keys": ["do X", "Enter"]},
                headers=BEARER,
            )
            assert resp.status == 200, await resp.text()

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_web_terminals_keystrokes_open_and_close_the_draft(home, tmp_path):
    """The socket the report came through. A keystroke frame is not just a
    timestamp — the bytes ride along, because they are the only thing that
    knows whether what the person typed is still sitting in the composer."""
    from aiohttp.test_utils import TestClient, TestServer

    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200,
                             restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await session.wait_for("idle", timeout=10.0, threshold=0.5)
            ws = await client.ws_connect("/api/sessions/s1/ws", headers=BEARER)
            await ws.receive(timeout=10)  # init
            await ws.receive(timeout=10)  # repaint

            await ws.send_bytes("반쯤 쓴 프롬프트".encode())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not session.draft_open():
                await asyncio.sleep(0.05)
            assert session.draft_open() is True

            await ws.send_bytes(b"\r")   # the human sends their own line
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and session.draft_open():
                await asyncio.sleep(0.05)
            assert session.draft_open() is False

            await ws.close()
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
