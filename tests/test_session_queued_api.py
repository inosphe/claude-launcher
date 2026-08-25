"""``GET /api/sessions/{name}/queued``: the delivery backlog, with its reason.

A mesh message is not typed into a terminal the moment it is sent — the
worker holds it while the session is mid-turn, and while a keyboard is
active on it. The web terminal's banner is drawn from this endpoint, and
what it must get right is the diagnosis: the backlog itself (re-derived the
same way delivery derives it), and WHY it is still a backlog — most
importantly ``keyboard``, the hold the operator causes themselves by keeping
focus in the terminal they are waiting on.
"""

from __future__ import annotations

import asyncio
import sys
import time

from claude_launcher import store
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _wait_idle(session, timeout=10.0):
    """Wait until the child is actually a terminal worth typing into.

    This is a *readiness* wait, not a premise: a test that really pastes
    something into the session needs the harness up and the screen settled
    first. It is not a way to make ``status()`` say idle at some later
    assertion — by then a late sample can have moved the baseline again
    (see :func:`_pin_status`). Wait with this; assert with that.
    """
    await session.wait_for("idle", timeout=timeout, threshold=0.5)


def _pin_status(session, status):
    """State the screen's status as a premise instead of racing the sampler.

    ``reason`` is derived from the status and the keyboard
    (``_session_queued`` in daemon/api.py) and *that* derivation is what
    these tests are about — not the sampler's ability to call a screen quiet.
    Waiting for the real thing reads honest and is not: the sampler does
    reach idle, and then a paint that lands late moves the baseline back.
    (``IdleTracker.sample`` stamps ``_last_meaningful`` with now on any
    meaningful change; on a loaded machine the 0.4s sample loop is starved,
    so a child's last repaint can be *seen* after the wait already returned.)
    The very next ``status()`` then says busy, the endpoint correctly reports
    ``busy``, and the assertion below fails while nothing is actually wrong.
    Raising the idle threshold cannot help — the baseline was reset, not
    merely young — so the fix is to stop making the premise a race.
    """
    session.status = lambda threshold=None: status


def test_backlog_is_listed_and_the_keyboard_hold_is_named(home, tmp_path, monkeypatch):
    """One undelivered message: the endpoint lists it (the recipient's own
    body, who sent it, through which mesh), and the reason tracks the same
    signals the delivery gate reads — quiet keyboard first, then a keystroke
    flips it to ``keyboard`` without touching the backlog itself."""
    _register_py_harness()
    # The keystroke below must still count as "just typed" when the assertion
    # reads it. What is under test is that a keystroke causes the hold, not
    # when the guard lapses, so the window is widened out of the way rather
    # than left to how fast the two requests happen to run.
    monkeypatch.setattr(session_mod, "TYPING_GUARD", 3600.0)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        # The manager is constructed directly (no daemon), so no delivery
        # worker ever runs: whatever is sent STAYS pending, which is the
        # state under test.
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            _pin_status(mgr.get("s1"), session_mod.STATUS_IDLE)

            await mm.send("team", "operator", "worker", "hello there",
                          external=True, type="ask")

            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert [m["body"] for m in body["messages"]] == ["hello there"]
            m = body["messages"][0]
            assert (m["mesh"], m["handle"], m["from"], m["type"]) == (
                "team", "worker", "operator", "ask"
            )
            assert m["held_for"] >= 0
            # idle screen, quiet keyboard: nothing holds it but the next tick
            assert body["reason"] == "settling"
            assert body["keyboard_busy"] is False

            # A keystroke into the terminal (the web viewer's passthrough)
            # is the hold the banner exists to name.
            await client.post(
                "/api/sessions/s1/keys",
                json={"keys": ["x"], "literal": True}, headers=BEARER,
            )
            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            body = await resp.json()
            assert body["keyboard_busy"] is True
            assert body["reason"] == "keyboard"
            assert len(body["messages"]) == 1  # the hold is not a loss

            # The same payload rides inside /meta for the detail panel.
            resp = await client.get("/api/sessions/s1/meta", headers=BEARER)
            meta = await resp.json()
            assert [m["id"] for m in meta["queued"]["messages"]] == [m["id"] for m in body["messages"]]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_empty_backlog_and_no_mesh_answer_the_same_quiet_shape(home, tmp_path):
    """No memberships, or memberships with nothing pending: ``messages`` is
    empty and ``reason`` is null — the banner's signal to not exist."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))

            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["messages"] == []
            assert body["reason"] is None

            # unknown session: the manager's refusal, not a crash
            resp = await client.get("/api/sessions/nope/queued", headers=BEARER)
            assert resp.status == 400

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_blunter_hold_wins_when_two_of_them_apply(home, tmp_path, monkeypatch):
    """``reason`` is one word for a backlog that several things can be
    holding, so the order matters and it is the delivery gate's order: a
    session that has exited cannot be typed into at all, a session mid-turn
    is held before anyone thinks to ask about the keyboard, and only once the
    screen is quiet is the keyboard left to explain the wait. The sharper
    reason must never hide the blunter one — a banner saying "your typing"
    about a session that is busy (or gone) sends the operator to the wrong
    fix. The raw signals ride along either way, so a client can still see
    that both applied."""
    _register_py_harness()
    monkeypatch.setattr(session_mod, "TYPING_GUARD", 3600.0)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            session = mgr.get("s1")
            await mm.send("team", "operator", "worker", "hello there",
                          external=True, type="ask")

            # A keystroke lands: on its own this is the ``keyboard`` hold.
            await client.post(
                "/api/sessions/s1/keys",
                json={"keys": ["x"], "literal": True}, headers=BEARER,
            )

            _pin_status(session, session_mod.STATUS_BUSY)
            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert (body["reason"], body["keyboard_busy"]) == ("busy", True)

            # Still starting counts as busy too — not yet a terminal to type into.
            _pin_status(session, session_mod.STATUS_STARTING)
            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert body["reason"] == "busy"

            # Quiet screen: now the keyboard is the only thing left holding it.
            _pin_status(session, session_mod.STATUS_IDLE)
            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert body["reason"] == "keyboard"

            # And an exit outranks everything, however quiet the screen reads.
            session.exited = True
            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert body["reason"] == "exited"
            assert len(body["messages"]) == 1  # no hold is a loss

            session.exited = False
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# POST .../queued/flush — the operator overruling the wait
#
# The banner could only ever describe the hold. These cover the button that
# ends it: what it delivers, what it deliberately still waits for, and what it
# says when it delivers nothing.
# --------------------------------------------------------------------------- #
def test_flush_types_in_a_backlog_that_nothing_else_would_have_typed(
    home, tmp_path
):
    """No delivery worker runs in these tests, so the backlog is permanent
    until something forces it — which makes this the cleanest proof that the
    flush is what put the messages in, and that it advanced the cursor rather
    than merely claiming to."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            await _wait_idle(mgr.get("s1"))

            await mm.send("team", "operator", "worker", "first",
                          external=True, type="fyi")
            await mm.send("team", "operator", "worker", "second",
                          external=True, type="fyi")
            assert len(mm.queued_for_session("s1")) == 2

            resp = await client.post(
                "/api/sessions/s1/queued/flush", headers=BEARER
            )
            assert resp.status == 200
            body = await resp.json()
            # both messages ride in one delivery block, so the count is the
            # messages that left the backlog, not the number of pastes
            assert body["flushed"] == 2
            assert body["handles"] == ["worker@team"]
            # the re-read backlog rides along so the caller need not re-poll
            assert body["queued"]["messages"] == []
            assert body["queued"]["reason"] is None
            assert mm.queued_for_session("s1") == []

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_flush_overrules_the_idle_gate_but_still_waits_for_the_paste(
    home, tmp_path, monkeypatch
):
    """The whole point, and its limit.

    A live keyboard holds delivery: an ordinary worker pass declines to type.
    The flush types anyway — that is the operator's call to make. What it does
    NOT do is skip ``Session.deliver``'s own wait for the keyboard to fall
    quiet; that wait is about the paste landing intact, not about politeness.
    Its bound is shortened here so the test does not sit out the real one.
    """
    from claude_launcher.daemon import session as session_mod

    _register_py_harness()
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 0.5)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            session = mgr.get("s1")
            await _wait_idle(session)
            await mm.send("team", "operator", "worker", "urgent",
                          external=True, type="ask")

            # a keystroke is the hold the banner names
            session.note_human_input()
            assert session.keyboard_busy() is True
            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            assert (await resp.json())["reason"] == "keyboard"

            # an ordinary worker pass leaves it exactly where it was
            mesh = mm.get("team")
            await mm._deliver_to(mesh, mesh.members["worker"])
            assert len(mm.queued_for_session("s1")) == 1

            # the operator's flush types it in regardless
            resp = await client.post(
                "/api/sessions/s1/queued/flush", headers=BEARER
            )
            body = await resp.json()
            assert body["flushed"] == 1
            assert mm.queued_for_session("s1") == []
            # ...and it went through deliver(), which is what kept the paste
            # safe: the keyboard wait was entered, not skipped
            assert session.keyboard_busy() is True

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_flush_delivers_nothing_to_an_exited_session_and_keeps_the_backlog(
    home, tmp_path
):
    """``flushed: 0`` is an answer, not a failure. There is no terminal to
    type into, so the messages stay queued for a respawn — losing them to
    make a button feel responsive would be the worst possible trade."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            await _wait_idle(mgr.get("s1"))
            await mm.send("team", "operator", "worker", "for later",
                          external=True, type="fyi")

            await mgr.get("s1").shutdown()

            resp = await client.post(
                "/api/sessions/s1/queued/flush", headers=BEARER
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["flushed"] == 0
            assert body["handles"] == []
            assert [m["body"] for m in body["queued"]["messages"]] == ["for later"]
            assert body["queued"]["reason"] == "exited"

            # nothing queued at all is the same quiet success
            mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/s2/queued/flush", headers=BEARER
            )
            assert (await resp.json())["flushed"] == 0

            # unknown session: refused like every other session route
            resp = await client.post(
                "/api/sessions/nope/queued/flush", headers=BEARER
            )
            assert resp.status == 400

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_human_hold_stops_delivery_and_resuming_lets_it_through(home, tmp_path):
    """The hold a person sets, at the one gate that decides whether a message
    is typed in.

    Every other hold in this system is the daemon *inferring* from status and
    keystroke timing that now is a bad moment. This is the case that timing
    cannot see: somebody reading their scrollback, hands off the keys, screen
    quiet — every automatic signal says "deliverable" and the person would
    rather it were not. So the premise here is deliberately the *most*
    deliverable state there is (idle screen, quiet keyboard, a message
    already waiting): nothing but the hold itself can explain a message that
    does not move, and nothing but its removal can explain one that then
    does.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        # Constructed without a daemon, so no delivery worker runs and the
        # gate is only ever entered by this test — one pass, one assertion.
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="worker")
        session = mgr.get("s1")
        await _wait_idle(session)
        _pin_status(session, session_mod.STATUS_IDLE)
        mesh = mm.get("team")
        member = mesh.members["worker"]

        await mm.send("team", "operator", "worker", "hold me",
                      external=True, type="fyi")
        assert len(mesh.pending("worker")) == 1

        session.set_delivery_hold(True)
        await mm._deliver_to(mesh, member)
        # Held, and held WITHOUT loss: the cursor has not moved, so this is a
        # message still waiting rather than one quietly dropped.
        assert [m["body"] for m in mesh.pending("worker")] == ["hold me"]

        session.set_delivery_hold(False)
        await mm._deliver_to(mesh, member)
        assert mesh.pending("worker") == []

        await mgr.shutdown_all()

    asyncio.run(run())


def test_hold_is_named_before_a_message_has_arrived_and_toggles_from_the_route(
    home, tmp_path
):
    """``state`` answers the question the header chip asks and ``reason``
    cannot: what would happen to a message arriving *now*.

    ``reason`` explains an existing backlog, so it is null while there is
    none — correct for the banner, useless for a chip that has to be readable
    when nothing is queued yet. An empty backlog under a hold and an empty
    backlog under nothing at all are the same empty list and opposite
    situations, and only ``state`` tells them apart. The route toggles when
    asked for no particular value, which is what lets the chip be one button
    instead of a read followed by a racing write.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await _wait_idle(session)
            _pin_status(session, session_mod.STATUS_IDLE)

            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert (body["hold"], body["state"], body["reason"]) == (
                False, "settling", None
            )

            resp = await client.post(
                "/api/sessions/s1/queued/hold", json={"hold": True},
                headers=BEARER,
            )
            assert resp.status == 200
            body = await resp.json()
            # The answer carries the re-read backlog, so one round trip is
            # enough to render the truth after the change.
            assert body["hold"] is True
            assert body["queued"]["state"] == "hold"
            assert body["queued"]["reason"] is None   # nothing queued yet
            assert session.delivery_held() is True

            # No value = toggle: the chip is a button, not a form.
            body = await (await client.post(
                "/api/sessions/s1/queued/hold", json={}, headers=BEARER)).json()
            assert body["hold"] is False
            assert body["queued"]["state"] == "settling"

            resp = await client.post(
                "/api/sessions/nope/queued/hold", json={"hold": True},
                headers=BEARER,
            )
            assert resp.status == 400

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_hold_outranks_the_timing_holds_but_not_a_dead_session(
    home, tmp_path, monkeypatch
):
    """Where a human's hold sits in the one-word ladder.

    It is reported ahead of ``busy`` and ``keyboard`` because the gate reads
    it first — a chip saying "held by your typing" about a session somebody
    deliberately pinned shut sends them to the wrong fix (wait a moment)
    instead of the right one (press resume). It stays behind ``exited``,
    which is not a hold anyone can lift. The raw signals ride along either
    way, so a client can still see that both applied.
    """
    _register_py_harness()
    monkeypatch.setattr(session_mod, "TYPING_GUARD", 3600.0)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            session = mgr.get("s1")
            await _wait_idle(session)
            await mm.send("team", "operator", "worker", "hello there",
                          external=True, type="ask")
            session.set_delivery_hold(True)

            # Busy AND held: the blunter automatic hold must not hide the
            # deliberate one, because only one of the two has a button.
            _pin_status(session, session_mod.STATUS_BUSY)
            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert body["state"] == "hold"
            assert body["reason"] == "hold"
            assert body["status"] == session_mod.STATUS_BUSY   # both visible

            # Keyboard AND held: same order, same reason.
            _pin_status(session, session_mod.STATUS_IDLE)
            await client.post(
                "/api/sessions/s1/keys",
                json={"keys": ["x"], "literal": True}, headers=BEARER,
            )
            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert body["keyboard_busy"] is True
            assert body["reason"] == "hold"

            # Exited outranks it: nobody can lift that one.
            await session.shutdown()
            body = await (await client.get(
                "/api/sessions/s1/queued", headers=BEARER)).json()
            assert body["reason"] == "exited"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_flush_overrules_a_hold_without_lifting_it(home, tmp_path):
    """"Deliver now" goes through a held session, and the hold survives it.

    Two different instructions: "let this one in" and "stop holding". Folding
    the second into the first would mean every use of the button silently
    un-pinned the session, so the next arrival lands in the terminal somebody
    is still reading — the exact interruption the hold was set to prevent.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            session = mgr.get("s1")
            await _wait_idle(session)
            _pin_status(session, session_mod.STATUS_IDLE)
            await mm.send("team", "operator", "worker", "let me in",
                          external=True, type="fyi")
            session.set_delivery_hold(True)

            body = await (await client.post(
                "/api/sessions/s1/queued/flush", headers=BEARER)).json()
            assert body["flushed"] == 1
            assert body["handles"] == ["worker@team"]
            assert body["queued"]["messages"] == []
            # Still pinned: the button delivered one message, it did not
            # revoke the standing instruction.
            assert body["queued"]["hold"] is True
            assert body["queued"]["state"] == "hold"
            assert session.delivery_held() is True

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
