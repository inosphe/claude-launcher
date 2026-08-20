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
    await session.wait_for("idle", timeout=timeout, threshold=0.5)


def test_backlog_is_listed_and_the_keyboard_hold_is_named(home, tmp_path):
    """One undelivered message: the endpoint lists it (the recipient's own
    body, who sent it, through which mesh), and the reason tracks the same
    signals the delivery gate reads — quiet keyboard first, then a keystroke
    flips it to ``keyboard`` without touching the backlog itself."""
    _register_py_harness()

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
            await _wait_idle(mgr.get("s1"))

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
