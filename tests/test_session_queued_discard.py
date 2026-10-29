"""``DELETE /api/sessions/{name}/queued/{id}``: drop one held message.

The queued strip lists what the mesh has accepted for a session but not yet
typed into its terminal. Its per-row delete (claunch-2onys) takes one of
those messages out for good: it must never reach the terminal afterwards --
not through "deliver now", not when an exited session is respawned, not
after the daemon reloads its mesh state -- while the mesh log and the
message's other recipients are left alone.
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


def _capture(session) -> list:
    """Record what delivery would type into ``session`` instead of pasting.

    What is under test is which messages the mesh hands to the terminal, not
    the paste mechanics, so the paste is replaced by a list of blocks."""
    blocks: list = []

    async def deliver(block, force=False):
        blocks.append(block)
        return True

    session.deliver = deliver
    return blocks


def _url(name, m):
    return (
        f"/api/sessions/{name}/queued/{m['id']}"
        f"?mesh={m['mesh']}&handle={m['handle']}"
    )


def test_deleted_message_is_not_delivered_by_deliver_now(home, tmp_path):
    """Two queued, one deleted: the reply lists only the survivor, and the
    flush types in the survivor alone. The deleted one is no debt either --
    an ``ask`` nobody showed the member must not land in its owed ledger
    once the cursor has moved past it."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            blocks = _capture(mgr.get("s1"))

            await mm.send("team", "operator", "worker", "drop me",
                          external=True, type="ask")
            await mm.send("team", "operator", "worker", "keep me",
                          external=True, type="fyi")
            queued = mm.queued_for_session("s1")
            assert [m["body"] for m in queued] == ["drop me", "keep me"]

            resp = await client.delete(_url("s1", queued[0]), headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["discarded"] == [
                {"mesh": "team", "handle": "worker", "id": queued[0]["id"]}
            ]
            assert [m["body"] for m in body["queued"]["messages"]] == ["keep me"]

            resp = await client.post(
                "/api/sessions/s1/queued/flush", headers=BEARER
            )
            assert (await resp.json())["flushed"] == 1
            assert len(blocks) == 1
            assert "keep me" in blocks[0]
            assert "drop me" not in blocks[0]
            assert mm.queued_for_session("s1") == []

            mesh = mm.get("team")
            assert [m["body"] for m in mesh.owed("worker")] == []
            # the log is history and keeps it; only the delivery is gone
            assert any(m.get("body") == "drop me" for m in mesh.messages)

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_deleted_message_stays_out_after_respawn_and_reload(home, tmp_path):
    """The case in the strip that prompted this: a session that has exited
    holds its backlog "until it is respawned". Deleting a row there must hold
    across the respawn, and across a daemon that reads its mesh state back
    from disk in between."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            s1 = mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            await s1.wait_for("idle", timeout=10.0, threshold=0.5)
            await mm.send("team", "operator", "worker", "stale request",
                          external=True, type="ask")
            await mm.send("team", "operator", "worker", "still wanted",
                          external=True, type="fyi")
            await s1.shutdown()

            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            body = await resp.json()
            assert body["reason"] == "exited"
            stale = body["messages"][0]
            resp = await client.delete(_url("s1", stale), headers=BEARER)
            assert resp.status == 200

            # A daemon restart reads the cursors file back.
            reloaded = MeshManager(mgr, root=tmp_path / "mesh")
            reloaded.load_all()
            assert [m["body"] for m in reloaded.queued_for_session("s1")] == [
                "still wanted"
            ]

            revived = mgr.respawn("s1")
            blocks = _capture(revived)
            session_mod_status = session_mod.STATUS_IDLE
            revived.status = lambda threshold=None: session_mod_status
            revived.keyboard_busy = lambda: False
            mesh = reloaded.get("team")
            await reloaded._deliver_to(mesh, mesh.members["worker"], force=True)
            assert len(blocks) == 1
            assert "still wanted" in blocks[0]
            assert "stale request" not in blocks[0]
            assert reloaded.queued_for_session("s1") == []

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_delete_touches_only_the_rows_session(home, tmp_path):
    """A broadcast queued for two sessions: deleting it from one backlog
    leaves the other recipient's copy queued."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="w1")
            await mm.join("team", "s2", handle="w2")
            await mm.send("team", "operator", "*", "to everyone",
                          external=True, type="fyi")
            [row] = mm.queued_for_session("s1")

            resp = await client.delete(_url("s1", row), headers=BEARER)
            assert resp.status == 200
            assert mm.queued_for_session("s1") == []
            assert [m["body"] for m in mm.queued_for_session("s2")] == [
                "to everyone"
            ]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_delete_refuses_an_id_that_is_not_queued_here(home, tmp_path):
    """An id that is not in this session's backlog -- never sent, queued for
    another session, or already typed in -- is refused, not remembered: a
    suppression nothing could clear would hide a later message for good."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="w1")
            await mm.join("team", "s2", handle="w2")
            _capture(mgr.get("s1"))

            resp = await client.delete(
                "/api/sessions/s1/queued/msg-nope", headers=BEARER
            )
            assert resp.status == 400

            await mm.send("team", "operator", "w2", "for s2",
                          external=True, type="fyi")
            [other] = mm.queued_for_session("s2")
            resp = await client.delete(
                f"/api/sessions/s1/queued/{other['id']}", headers=BEARER
            )
            assert resp.status == 400
            assert len(mm.queued_for_session("s2")) == 1

            await mm.send("team", "operator", "w1", "already in",
                          external=True, type="fyi")
            [mine] = mm.queued_for_session("s1")
            # The row names the copy it shows; a delete that names another
            # handle or mesh is refused instead of widening to any copy.
            for query in ("mesh=team&handle=w2", "mesh=other&handle=w1"):
                resp = await client.delete(
                    f"/api/sessions/s1/queued/{mine['id']}?{query}",
                    headers=BEARER,
                )
                assert resp.status == 400
            assert [m["id"] for m in mm.queued_for_session("s1")] == [mine["id"]]
            await client.post("/api/sessions/s1/queued/flush", headers=BEARER)
            resp = await client.delete(_url("s1", mine), headers=BEARER)
            assert resp.status == 400
            assert mm.get("team").discarded == {}

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_history_never_reads_a_deleted_message_as_delivered(home, tmp_path):
    """The history annotation re-derives delivery from the cursor. A deleted
    message is behind the cursor once a later one is typed in, and the
    member's own later send moves its owed floor past it too -- from then on
    only ``discarded`` says it was never shown, so that record has to
    outlive both."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="worker")
            await mm.join("team", "s2", handle="peer")
            blocks = _capture(mgr.get("s1"))
            _capture(mgr.get("s2"))

            await mm.send("team", "operator", "worker", "drop me",
                          external=True, type="ask")
            [dropped] = mm.queued_for_session("s1")
            resp = await client.delete(_url("s1", dropped), headers=BEARER)
            assert resp.status == 200

            await mm.send("team", "worker", "peer", "after the delete")
            await mm.send("team", "operator", "worker", "typed in",
                          external=True, type="fyi")
            resp = await client.post(
                "/api/sessions/s1/queued/flush", headers=BEARER
            )
            assert (await resp.json())["flushed"] == 1
            assert "drop me" not in "".join(blocks)

            mesh = mm.get("team")
            [k] = [i for i, m in enumerate(mesh.messages)
                   if m.get("id") == dropped["id"]]
            assert k < mesh.cursors["worker"]
            info = mesh.delivery_of(mesh.messages[k], k)
            assert info["recipients"] == ["worker"]
            assert info["delivered"] == []

            reloaded = MeshManager(mgr, root=tmp_path / "mesh")
            reloaded.load_all()
            again = reloaded.get("team")
            assert again.delivery_of(again.messages[k], k)["delivered"] == []

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
