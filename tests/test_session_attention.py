"""Who has been near a session: the three readings the rail's card draws.

Three separate facts, and the tests keep them separate on purpose — the bug
this file exists to catch is not "the number is wrong", it is one reading
quietly standing in for another:

* **visited** — a person had this terminal OPEN. Only the viewer socket says
  so, and it says so on both of its edges.
* **typed** — a person typed here AT A KEYBOARD. ``send-keys`` and mesh
  deliveries also write into a session, and counting them would turn "when
  did I last say something to this agent" into "when was this session last
  written to", which is a different question with a much more recent answer.
* **moved** — the SCREEN changed, and not because something was animating.
  ``last_output_at`` is the tempting stand-in and is useless for it: claude
  animates a spinner and an elapsed-time counter while it waits for you, so
  raw output never goes quiet and that stamp reads "just now" on a session
  that has done nothing for an hour. The test below asserts that divergence
  directly rather than trusting the two to differ.

The visit and the typing survive a relaunch, because the session they belong
to does: a restore, a respawn or a redefine is the same session continuing,
and a rail that forgot who had been near it every time the daemon came back
would be blank at the one moment the question gets asked. ``moved`` does not
survive, and that is not an oversight — it is read off this incarnation's
screen history, which a restarted session does not have.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import db, paths
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession

# A child that prints on demand and otherwise says nothing, so "the screen
# moved" is something the test causes rather than something it waits out.
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if line == 'quit':\n"
    "        break\n"
    "    print('echo:' + line)\n"
)


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _wait_screen(session, needle: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in "\n".join(session.capture()):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared on screen")


# --------------------------------------------------------------------------- #
# a fresh session has been near nobody, and says so as absence, not as zero
# --------------------------------------------------------------------------- #
def test_new_session_reports_no_visit_and_no_typing(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="fresh", harness="py", cwd=str(tmp_path)))
            info = s.info()
            # None, not "" and not the creation time: nobody has been here, and
            # the row draws that as a dash rather than as a very old visit.
            assert info["last_visited_at"] is None
            assert info["last_input_at"] is None
            assert info["viewers"] == 0
            # Every key is present even when empty — a reader that draws the
            # line unconditionally must not have to guess at a missing key.
            assert "last_activity_at" in info
            # The rail grades a busy dot by it (dotGrade in app.js); an int
            # from the first poll, so the grade never has to guess.
            assert isinstance(info["moved_rows"], int)
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# typed: a human at a terminal, and nothing else
# --------------------------------------------------------------------------- #
def test_typing_at_a_terminal_stamps_the_input_time(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="typed", harness="py", cwd=str(tmp_path)))
            assert s.info()["last_input_at"] is None
            # What the WebSocket bridge does for every keystroke frame.
            s.note_human_input(at_terminal=True, data=b"h")
            assert s.info()["last_input_at"] is not None
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_send_keys_is_not_me_typing(home, tmp_path):
    """The distinction the whole reading rests on.

    ``send-keys`` is another agent driving this session — a cflow nudge, a
    mesh delivery, a script. It moves the *delivery* clock (that is what
    ``_last_human_input`` is for, and the gate that reads it) and must leave
    the card's "you last typed here" alone, or every automated poke would
    read back as the operator having just been in.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="driven", harness="py", cwd=str(tmp_path)))
            s.note_human_input(at_terminal=False, data=b"hello\r")
            assert s.info()["last_input_at"] is None
            # ...while the delivery gate did see it.
            assert s._last_human_input > 0.0
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# visited: both edges of the socket, and "right now" while it is open
# --------------------------------------------------------------------------- #
def test_visit_is_stamped_on_open_and_again_on_close(home, tmp_path):
    """A tab open all afternoon was visited when it closed, not when it opened.

    Both edges, because either alone lies in one direction: only-on-open ages
    a session somebody is reading right now, and only-on-close says a session
    being watched has never been visited at all.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="watched", harness="py", cwd=str(tmp_path)))
            q = s.subscribe()
            s.note_visit()
            opened = s.info()
            assert opened["last_visited_at"] is not None
            # ...and while it is open the row is told so directly, because no
            # stamp taken in the past can say "still here".
            assert opened["viewers"] == 1

            await asyncio.sleep(1.1)  # cross a whole second: the stamp is to seconds
            s.unsubscribe(q)
            s.note_visit()
            closed = s.info()
            assert closed["viewers"] == 0
            assert closed["last_visited_at"] > opened["last_visited_at"]
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_viewers_counts_the_terminal_sockets(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="crowd", harness="py", cwd=str(tmp_path)))
            a, b = s.subscribe(), s.subscribe()
            assert s.info()["viewers"] == 2
            s.unsubscribe(a)
            assert s.info()["viewers"] == 1
            s.unsubscribe(b)
            assert s.info()["viewers"] == 0
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# moved: the tracker's answer, not the byte stream's
# --------------------------------------------------------------------------- #
def test_activity_time_comes_from_the_screen_not_the_byte_stream(home, tmp_path):
    """The reading that could not be taken from ``last_output_at``.

    Proved structurally rather than by waiting out a spinner: the session is
    fed output whose *rendered rows do not change*, the byte stamp moves
    because bytes arrived, and the screen stamp must not — that is the entire
    difference between "something is being printed" and "something is
    happening".
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="moving", harness="py", cwd=str(tmp_path)))
            await _wait_screen(s, "READY")
            # Let the sampler take at least one look at a settled screen.
            await asyncio.sleep(1.0)
            first = s.info()["last_activity_at"]
            assert first is not None  # the screen has painted at least once

            before_bytes = s.info()["last_output_at"]
            # Bytes that render to nothing: a cursor parked where it already
            # is. Output happened; the screen did not.
            s._on_output(b"\x1b[1;1H")
            await asyncio.sleep(1.0)
            after = s.info()
            assert after["last_output_at"] >= before_bytes
            assert after["last_activity_at"] == first

            # ...and real content does move it.
            await s.send_keys(["hello", "Enter"])
            await _wait_screen(s, "echo:hello")
            await asyncio.sleep(1.0)
            assert s.info()["last_activity_at"] > first
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the two human stamps outlive the process; the screen one honestly does not
# --------------------------------------------------------------------------- #
def test_stamps_are_persisted_and_restored(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(
                SessionDef(name="remembered", harness="py", cwd=str(tmp_path))
            )
            s.note_visit()
            s.note_human_input(at_terminal=True, data=b"x")
            visited, typed = s.last_visited_at, s.last_input_at
            mgr.persist()

            entry = db.open_default().load_all()[0]
            assert entry["last_visited_at"] == visited
            assert entry["last_input_at"] == typed
        finally:
            await mgr.shutdown_all()

        # The record a restart retires: the stamps come back with it, because
        # "when did I last look at the one that died" is most of what these
        # records are opened for.
        dead = DeadSession(
            SessionDef(name="remembered", harness="py", cwd=str(tmp_path)),
            last_visited_at=visited,
            last_input_at=typed,
        )
        info = dead.info()
        assert info["last_visited_at"] == visited
        assert info["last_input_at"] == typed
        # ...and the screen reading does not, because there is no screen to
        # have read it from. Blank, never a stale value dressed as current.
        assert info["last_activity_at"] is None
        assert info["viewers"] == 0

    asyncio.run(run())


def test_relaunch_carries_the_stamps_into_the_new_process(home, tmp_path):
    """A respawn is the same session continuing — including who has been in it.

    The failure this guards is silent: the new ``Session`` object simply
    starts at ``None``, the rail draws a dash, and nothing anywhere says that
    a fact was dropped rather than never recorded.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="phoenix", harness="py", cwd=str(tmp_path)))
            s.note_visit()
            s.note_human_input(at_terminal=True, data=b"x")
            visited, typed = s.last_visited_at, s.last_input_at

            await s.shutdown()
            back = mgr.respawn("phoenix")
            assert back.last_visited_at == visited
            assert back.last_input_at == typed
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
