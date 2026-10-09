"""Session hold (claunch-5l35i): free an idle session's program, keep the session.

A hold ends the program the way a pause does, but it is not an ending of the
session: the exit hooks (board sweep, side-run abort, orphan report) do not
run, the record is filed as *hold* — inside both rail presets — and the
session comes back with its conversation when it is used again: a viewer
attaches, a mesh message is due, a cflow notice is typed, or the wake/respawn
route is pressed by hand.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import hold as hold_mod
from claude_launcher.daemon import mesh as mesh_mod
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.session import DeadSession, Session, STATUS_IDLE
from test_daemon_e2e import _manager, _register_py_harness, _wait_screen

HELD_AT = "2026-10-09T12:00:00+00:00"


def add_dead(mgr, name: str, *, held_at=None, paused_at=None) -> DeadSession:
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd="C:/work"),
        exit_code=0,
        held_at=held_at,
        paused_at=paused_at,
    )
    mgr._sessions[name] = record
    return record


def test_hold_is_its_own_partition():
    def rec(**kw):
        base = dict(exited=True, archived_at=None, paused_at=None, held_at=None)
        base.update(kw)
        return SimpleNamespace(**base)

    assert session_mod.session_category(rec(held_at=HELD_AT)) == "hold"
    # Archive outranks hold, and a held record is never read as paused.
    assert session_mod.session_category(rec(held_at=HELD_AT, archived_at=HELD_AT)) == "archived"
    assert session_mod.session_category(rec(held_at=HELD_AT, paused_at=HELD_AT)) == "hold"
    assert session_mod.session_category(rec(exited=False, held_at=None)) == "running"


def test_held_marker_survives_a_restart_and_owes_no_sweep(home):
    mgr = _manager()
    add_dead(mgr, "asleep", held_at=HELD_AT)
    add_dead(mgr, "dead")
    mgr.persist()

    restarted = _manager()
    assert restarted.restore_all() == []
    asleep = restarted.get("asleep")
    assert asleep.exited and asleep.held_at == HELD_AT
    assert restarted.get("dead").held_at is None
    # A hold is not an ending, so the boot does not sweep the board for it.
    assert [s.sdef.name for s in restarted.take_retired_for_sweep()] == ["dead"]


def test_resumable_only_for_harnesses_that_reopen_their_conversation(monkeypatch):
    from claude_launcher.daemon import harness as harness_mod
    from claude_launcher.daemon.manager import SessionManager

    def sess(harness, cid):
        return SimpleNamespace(sdef=SimpleNamespace(harness=harness, conversation_id=cid))

    monkeypatch.setattr(harness_mod, "restores_blank", lambda sdef: False)
    assert SessionManager.resumable(sess("claude", "c1"))
    assert SessionManager.resumable(sess("codex", "r1"))
    assert not SessionManager.resumable(sess("claude", None))
    assert not SessionManager.resumable(sess("py", "x"))
    monkeypatch.setattr(harness_mod, "restores_blank", lambda sdef: True)
    assert not SessionManager.resumable(sess("claude", "c1"))


class _Quiet:
    """A live session as :func:`hold.eligible` reads it."""

    def __init__(self, **kw):
        self.sdef = SimpleNamespace(name=kw.pop("name", "s"), keep_alive=kw.pop("keep_alive", False))
        self.exited = False
        self.held_at = None
        self.kill_requested = False
        self._status = kw.pop("status", STATUS_IDLE)
        self._viewers = kw.pop("viewers", 0)
        self._pending = kw.pop("pending", [])
        self._quiet = kw.pop("quiet", 7200.0)
        assert not kw

    def status(self):
        return self._status

    def viewers(self):
        return self._viewers

    def pending_deliveries(self):
        return self._pending

    def quiet_for(self, now=None):
        return self._quiet


def test_idle_eligibility_and_sweep():
    held = []
    mgr = SimpleNamespace(
        resumable=lambda s: s.sdef.name != "blank",
        hold=lambda name, **kw: held.append((name, kw.get("reason"))),
    )
    fleet = [
        _Quiet(name="ok"),
        _Quiet(name="busy", status="busy"),
        _Quiet(name="watched", viewers=1),
        _Quiet(name="queued", pending=[{"preview": "x"}]),
        _Quiet(name="pinned", keep_alive=True),
        _Quiet(name="blank"),
        _Quiet(name="recent", quiet=60.0),
    ]
    mgr.list = lambda: fleet
    assert hold_mod.sweep(mgr, 3600.0) == ["ok"]
    assert held == [("ok", "idle")]
    # 0 is off.
    assert hold_mod.sweep(mgr, 0.0) == []
    assert hold_mod.threshold_seconds({"hold_idle_minutes": 60}) == 3600.0
    assert hold_mod.threshold_seconds({"hold_idle_minutes": 0}) == 0.0
    assert hold_mod.threshold_seconds({}) == 0.0


def test_hold_api_skips_the_exit_hooks_and_wakes_on_use(home, tmp_path, monkeypatch):
    """End to end on the py harness: hold by hand, read the partitions, then
    wake through each of the paths -- the wake route, a viewer attaching, a
    scheduled wake carrying a notice -- and turn the last hold into a kill."""
    _register_py_harness()
    delivered = []
    monkeypatch.setattr(Session, "queue_delivery",
                        lambda self, text: delivered.append((self.sdef.name, text)) or True)

    async def run():
        mgr = _manager()
        exits = []
        mgr.exit_hooks.append(lambda s: exits.append(s.sdef.name))
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer sekrit"}

        async def names(state: str) -> list:
            response = await client.get(
                f"/api/sessions?view=rail&state={state}", headers=headers
            )
            assert response.status == 200
            return sorted(row["name"] for row in (await response.json())["sessions"])

        async def hold(name: str) -> dict:
            response = await client.post(f"/api/sessions/{name}/hold", headers=headers)
            assert response.status == 200, await response.text()
            body = await response.json()
            await mgr.get(name).wait_for("exited", timeout=10.0, threshold=0.5)
            return body

        try:
            for name in ("a0", "a1", "a2", "a3"):
                s = mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
                await _wait_screen(s, "READY")

            # The py harness cannot reopen a conversation: refused.
            response = await client.post("/api/sessions/a0/hold", headers=headers)
            assert response.status == 400
            assert not mgr.get("a0").exited
            mgr.resumable = lambda s: True

            body = await hold("a0")
            assert body["held_at"] and "already_exited" not in body
            assert [e["kind"] for e in mgr.events.rows(mgr.get("a0"))] == ["create", "hold", "hold"]
            assert exits == [], "a hold ran the exit hooks"
            for name in ("a1", "a2", "a3"):
                await hold(name)

            assert await names("hold") == ["a0", "a1", "a2", "a3"]
            assert await names("killed") == []
            assert await names("current") == ["a0", "a1", "a2", "a3"]
            # Bulk archive and bulk respawn leave held records alone.
            body = await (await client.post("/api/sessions/archive", headers=headers)).json()
            assert body == {"archived": [], "failed": []}
            body = await (await client.post("/api/sessions/respawn", headers=headers)).json()
            assert body == {"respawned": [], "failed": []}

            # --- wake by hand ------------------------------------------------
            response = await client.post("/api/sessions/a0/wake", headers=headers)
            assert response.status == 200
            body = await response.json()
            assert body["woken"] is True and body["status"] != "exited"
            await _wait_screen(mgr.get("a0"), "READY")
            assert mgr.get("a0").held_at is None
            assert [e["kind"] for e in mgr.events.rows(mgr.get("a0"))][-1] == "wake"
            body = await (await client.post("/api/sessions/a0/wake", headers=headers)).json()
            assert body["woken"] is False

            # --- wake by attaching a viewer ------------------------------------
            ws = await client.ws_connect("/api/sessions/a1/ws", headers=headers)
            try:
                init = json.loads((await ws.receive()).data)
                assert init["type"] == "init" and init["exited"] is False
            finally:
                await ws.close()
            assert not mgr.get("a1").exited

            # --- wake scheduled by a notice --------------------------------------
            assert mgr.wake_soon("a2", by="cflow2", deliver="notice") is True
            for _ in range(100):
                if delivered:
                    break
                await asyncio.sleep(0.1)
            assert delivered == [("a2", "notice")]
            assert not mgr.get("a2").exited
            assert mgr.wake_soon("a2") is False  # awake: nothing to schedule

            # --- kill turns the hold into a real ending ---------------------------
            response = await client.post("/api/sessions/a3/kill", headers=headers)
            body = await response.json()
            assert body["unheld"] is True and body["already_exited"] is True
            assert body["held_at"] is None
            assert exits == ["a3"], "the deferred exit hooks did not run once"
            assert await names("killed") == ["a3"]
            assert await names("hold") == []
        finally:
            for s in list(mgr.list()):
                await s.shutdown()
            await client.close()

    asyncio.run(run())


def test_mesh_wakes_a_held_recipient_instead_of_reporting_it_stranded(home, tmp_path):
    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        mesh = mm.create("sleepy")
        for handle in ("carol", "bob"):
            mesh.members[handle] = mesh_mod.Member(handle, handle)
        add_dead(mgr, "bob", held_at=HELD_AT)
        woken = []
        mgr.wake_soon = lambda name, **kw: woken.append((name, kw.get("by"))) or True
        mm._send_core(mesh, "carol", "bob", "are you there", type="ask")
        await mm._deliver_to(mesh, mesh.members["bob"])
        assert woken == [("bob", "mesh")]
        assert [m for m in mesh.messages if m["from"] == "policy"] == []
        assert len(mesh.pending("bob")) == 1  # delivered after the wake
        info = mm.mesh_info(mesh)
        assert info["member_counts"]["hold"] == 1 and info["member_counts"]["current"] >= 1

        # A killed recipient is still reported stranded, as before.
        add_dead(mgr, "bob")
        await mm._deliver_to(mesh, mesh.members["bob"], force=True)
        assert [m["to"] for m in mesh.messages if m["from"] == "policy"] == [["carol"]]

    asyncio.run(run())


def test_unhold_runs_the_exit_hooks_on_archive_and_remove(home):
    mgr = _manager()
    exits = []
    mgr.exit_hooks.append(lambda s: exits.append(s.sdef.name))
    add_dead(mgr, "h0", held_at=HELD_AT)
    add_dead(mgr, "h1", held_at=HELD_AT)
    add_dead(mgr, "k0")
    mgr.archive("h0")
    assert mgr.get("h0").held_at is None and mgr.get("h0").archived_at
    mgr.remove("h1")
    mgr.archive("k0")
    assert exits == ["h0", "h1"]
    assert mgr.unhold("k0") is False


def test_hold_of_a_missing_session_raises(home):
    mgr = _manager()
    try:
        mgr.hold("nobody")
    except ManagerError:
        pass
    else:
        raise AssertionError("hold of a missing session did not raise")


def test_shipped_page_ids_are_unique():
    """The idle-hold button once reused the delivery chip's id (#term-hold):
    $("term-hold") found the chip, so the hold click and the exited toggle
    landed on the chip and the button was never wired."""
    import collections
    import re
    from pathlib import Path

    import claude_launcher

    page = Path(claude_launcher.__file__).parent / "web" / "static" / "index.html"
    ids = collections.Counter(re.findall(r'\bid="([^"]+)"', page.read_text(encoding="utf-8")))
    assert [i for i, n in ids.items() if n > 1] == []
    assert ids["term-hold"] == 1 and ids["term-idle-hold"] == 1
