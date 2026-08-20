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
