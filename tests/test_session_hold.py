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
import contextlib
import json
from types import SimpleNamespace

import pytest

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import hold as hold_mod
from claude_launcher.daemon import mesh as mesh_mod
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.session import DeadSession, Session, STATUS_IDLE
from test_daemon_e2e import _manager, _on_screen, _register_py_harness, _wait_screen

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


def test_rail_preserves_hold_marker_before_and_after_restart(home):
    """The rail classifies exited rows from their markers, not server filters."""
    async def run():
        mgr = _manager()
        add_dead(mgr, "asleep", held_at=HELD_AT)
        add_dead(mgr, "paused", paused_at=HELD_AT)
        add_dead(mgr, "dead")
        mgr.persist()

        for restored in (False, True):
            if restored:
                mgr = _manager()
                assert mgr.restore_all() == []
            client = TestClient(TestServer(build_app(mgr, "sekrit", started_at=0.0)))
            await client.start_server()
            try:
                for state in ("current", "hold"):
                    response = await client.get(
                        f"/api/sessions?view=rail&state={state}",
                        headers={"Authorization": "Bearer sekrit"},
                    )
                    assert response.status == 200
                    rows = {row["name"]: row for row in (await response.json())["sessions"]}
                    assert rows["asleep"]["status"] == "exited"
                    assert rows["asleep"]["held_at"] == HELD_AT
                    assert sum(bool(row["held_at"]) for row in rows.values()) == 1
                    if state == "current":
                        assert rows["paused"]["held_at"] is None
                        assert rows["paused"]["paused_at"] == HELD_AT
                        assert rows["dead"]["held_at"] is None
                        assert rows["dead"]["paused_at"] is None
                    else:
                        assert set(rows) == {"asleep"}
            finally:
                await client.close()

    asyncio.run(run())


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


def test_a_held_recipient_is_not_reported_dead_to_senders(home, tmp_path):
    """claunch-8tvq7: a sender was told a held recipient "has exited —
    nothing is reading that terminal … stop sending there", and the roster,
    the join briefing and the cflow responder pool all read it as gone. It
    is asleep: the delivery pass wakes it. A killed one still reads dead."""
    from claude_launcher.cflow import responders

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        mesh = mm.create("sleepy")
        for handle in ("carol", "bob", "dan"):
            mesh.members[handle] = mesh_mod.Member(handle, handle)
        add_dead(mgr, "carol")
        add_dead(mgr, "bob", held_at=HELD_AT)
        add_dead(mgr, "dan")

        sent = mm._send_core(mesh, "carol", ["bob", "dan"], "are you there", type="ask")
        assert [e["handle"] for e in sent["undeliverable"]] == ["dan"]
        assert "bob" not in (sent.get("notice") or "")
        assert mm.stranded_recipients(mesh, ["bob"]) == []

        rows = {r["handle"]: r for r in mm.mesh_info(mesh, state="all")["members"]}
        assert rows["bob"]["reachability"] == "held"
        assert rows["dan"]["reachability"] == "exited"
        assert responders.Responder("bob", "bob", "worker",
                                    reachability="held").answerable
        assert not responders.Responder("dan", "dan", "worker",
                                        reachability="exited").answerable

        # Mid-wake the record is briefly out of the registry: still asleep,
        # never "missing".
        del mgr._sessions["bob"]
        mgr._waking.add("bob")
        try:
            assert mm.stranded_recipients(mesh, ["bob"]) == []
            assert mm._reachability(mesh, mesh.members["bob"]) == "held"
        finally:
            mgr._waking.discard("bob")

    asyncio.run(run())


def test_join_briefing_lists_a_held_peer(home, tmp_path):
    mgr = _manager()
    mm = MeshManager(mgr, root=tmp_path / "meshes")
    mesh = mm.create("sleepy")
    for handle in ("alice", "bob", "dan"):
        mesh.members[handle] = mesh_mod.Member(handle, handle)
    add_dead(mgr, "alice", held_at=HELD_AT)  # the reader; its own state is moot
    add_dead(mgr, "bob", held_at=HELD_AT)
    add_dead(mgr, "dan")
    block = mm.briefing_block(mesh, mesh.members["alice"])
    members = next(l for l in block.splitlines() if l.startswith("members:"))
    assert "bob" in members and "dan" not in members


def test_a_held_holder_keeps_an_exclusive_role_and_a_killed_one_frees_it(home, tmp_path):
    """A held leader comes back holding the role (claunch-8tvq7): a second
    leader joining meanwhile would leave two once it wakes. A killed holder
    still frees the role for a successor, as before."""
    mgr = _manager()
    mm = MeshManager(mgr, root=tmp_path / "meshes")
    mesh = mm.create("sleepy")
    mesh.members["lead1"] = mesh_mod.Member("lead1", "lead1", role="leader")
    add_dead(mgr, "lead1", held_at=HELD_AT)
    holder = mm.exclusive_holder(mesh, "lead2", "leader")
    assert holder is not None and holder.handle == "lead1"

    add_dead(mgr, "lead1")  # killed: the record carries no hold marker
    assert mm.exclusive_holder(mesh, "lead2", "leader") is None


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


class _TTY:
    def isatty(self):
        return True


class _AttachClient:
    """What :func:`attach.attach` asks the daemon, answered from a script."""

    base_url = "http://daemon"
    token = "t"

    def __init__(self, info, wake):
        self.info = info
        self.wake = wake
        self.posts = []

    def get(self, path):
        return dict(self.info)

    def post(self, path):
        self.posts.append(path)
        if isinstance(self.wake, Exception):
            raise self.wake
        return dict(self.wake)


def _attach_terminal(monkeypatch):
    """Stand in for the terminal and the websocket so attach runs headless."""
    from claude_launcher import attach as attach_mod

    monkeypatch.setattr(attach_mod.sys, "stdin", _TTY())
    # stdout is still written to (the detach line); only claim it is a tty.
    monkeypatch.setattr(attach_mod.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(attach_mod.herdr, "rename_pane", lambda label: False)
    monkeypatch.setattr(attach_mod, "_write_text", lambda text: None)

    class _Raw:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(attach_mod, "_RawTerminal", _Raw)
    attached = []

    async def fake_attach(base_url, token, name, notice=None):
        attached.append(name)
        return {"reason": "detach"}

    monkeypatch.setattr(attach_mod, "_attach_async", fake_attach)
    return attach_mod, attached


def test_cli_attach_wakes_a_held_session_first(monkeypatch, capsys):
    attach_mod, attached = _attach_terminal(monkeypatch)
    client = _AttachClient(
        {"status": "exited", "held_at": HELD_AT, "cwd": "C:/work"},
        {"status": "starting", "held_at": None, "woken": True, "cwd": "C:/work"},
    )
    assert attach_mod.attach(client, "asleep") == 0
    assert client.posts == ["/api/sessions/asleep/wake"]
    assert attached == ["asleep"]
    assert "on hold" in capsys.readouterr().err


def test_cli_attach_points_at_respawn_when_the_wake_fails(monkeypatch, capsys):
    attach_mod, attached = _attach_terminal(monkeypatch)
    client = _AttachClient(
        {"status": "exited", "held_at": HELD_AT},
        RuntimeError("HTTP 400: cannot respawn"),
    )
    assert attach_mod.attach(client, "asleep") == 1
    err = capsys.readouterr().err
    assert "could not wake 'asleep'" in err and "claunch respawn asleep" in err
    assert attached == []

    # The route answered but the session is still down: the same manual path.
    client = _AttachClient(
        {"status": "exited", "held_at": HELD_AT},
        {"status": "exited", "held_at": HELD_AT, "exit_code": 1},
    )
    assert attach_mod.attach(client, "asleep") == 1
    assert "claunch respawn asleep" in capsys.readouterr().err
    assert attached == []

    # A killed (not held) session is never woken by attaching.
    client = _AttachClient({"status": "exited", "exit_code": 0}, {})
    assert attach_mod.attach(client, "dead") == 1
    assert client.posts == []


def test_a_mesh_message_wakes_a_held_recipient_and_lands_after_the_respawn(
    home, tmp_path
):
    """End to end on the py harness: no stub between the mesh pass, the wake
    and the respawned program reading the message."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mgr.resumable = lambda s: True
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        mesh = mm.create("sleepy")
        try:
            bob = mgr.create(SessionDef(name="bob", harness="py", cwd=str(tmp_path)))
            await _wait_screen(bob, "READY")
            for handle in ("carol", "bob"):
                mesh.members[handle] = mesh_mod.Member(handle, handle)
            mgr.hold("bob", reason="idle")
            await mgr.get("bob").wait_for("exited", timeout=10.0, threshold=0.5)
            assert mgr.get("bob").held_at

            mm._send_core(mesh, "carol", "bob", "wakeup-ping", type="ask")
            await mm._deliver_to(mesh, mesh.members["bob"])
            assert [m for m in mesh.messages if m["from"] == "policy"] == []

            for _ in range(150):
                if not mesh.pending("bob"):
                    break
                await mm._deliver_to(mesh, mesh.members["bob"], force=True)
                await asyncio.sleep(0.1)
            assert mesh.pending("bob") == []
            awake = mgr.get("bob")
            assert not awake.exited and awake.held_at is None
            await _wait_screen(awake, "wakeup-ping")
            assert [e["kind"] for e in mgr.events.rows(awake)][-1] == "wake"
        finally:
            for s in list(mgr.list()):
                await s.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("route", ["input", "keys", "paste", "deliver", "defer", "checks", "peer",
                                   "cflow-checks", "v2-checks", "cflow-nudge", "observer", "opening"])
@pytest.mark.parametrize("retired", [False, True])
def test_messages_restore_hold_and_reach_the_program(home, tmp_path, route, retired):
    from claude_launcher.daemon import session_input, shadow, status_checks

    _register_py_harness()

    async def run():
        mgr = _manager()
        mgr.resumable = lambda s: True
        client = TestClient(TestServer(build_app(mgr, "sekrit", started_at=0.0)))
        await client.start_server()
        headers = {"Authorization": "Bearer sekrit"}
        try:
            original = mgr.create(SessionDef(name="sleepy", harness="py", cwd=str(tmp_path)))
            await _wait_screen(original, "READY")
            mgr.hold("sleepy")
            await original.wait_for("exited", timeout=10, threshold=0.5)
            if retired:
                mgr._retire(original.sdef, original.info())
            text = "hold-message-received"
            path = "/api/sessions/sleepy/"
            if route in ("cflow-checks", "v2-checks", "cflow-nudge"):
                from claude_launcher.daemon import cflow_clock, cflow2_triggers
                from claude_launcher.daemon.api import _nudge_sessions
                if route == "cflow-nudge":
                    assert await _nudge_sessions(mgr, str(tmp_path), "sleepy", text) == ["sleepy"]
                else:
                    status_checks.set_entries([{"name": "Tests", "question": "Tests passed?"}])
                    if route == "cflow-checks":
                        result = await cflow_clock.TriggerClock(mgr)._perform(str(tmp_path), "sleepy", {"do": "checks"})
                    else:
                        result = await cflow2_triggers.perform(mgr, str(tmp_path), "sleepy", {"do": "checks"}, driver="sleepy")
                    assert result[0], result
                    text = status_checks.REFRESH_PROMPT.splitlines()[0]
            elif route == "observer":
                from claude_launcher.daemon.observer_reports import Reports
                reports = Reports(tmp_path, mgr)
                event = reports.publish("sleepy", {"text": "Continue?", "question": True})
                result = await reports.answer("sleepy", event["id"], {"text": text})
                assert result["delivery"] == "sent"
                text = "[Observer user answer]"
            elif route == "opening":
                from claude_launcher.daemon.onboard import _deliver_until_it_lands
                await _deliver_until_it_lands(mgr.get("sleepy"), text)
            elif route == "peer":
                result = await shadow.type_line(mgr.get("sleepy"), text, "peer-1", origin="peer:test")
                assert result["ok"]
            else:
                if route == "checks":
                    status_checks.set_entries([{"name": "Tests", "question": "Tests passed?"}])
                    endpoint, body = "status-checks/refresh", {}
                    text = status_checks.REFRESH_PROMPT.splitlines()[0]
                elif route in ("deliver", "defer"):
                    endpoint, body = "deliver", {"text": text, "defer": route == "defer"}
                elif route == "paste":
                    endpoint, body = "keys", {"paste": text, "enter": True}
                else:
                    endpoint, body = "keys", {"keys": [text, "Enter"], "force": True}
                    if route == "input":
                        body["input_id"] = "input-1"
                response = await client.post(path + endpoint, json=body, headers=headers)
                assert response.status in (200, 202), await response.text()
                result = await response.json()
                if route in ("deliver", "checks"):
                    assert result["delivered"] is True
                if route == "defer":
                    assert result["queued"] is True
            if mgr._wake_tasks:
                await asyncio.wait_for(asyncio.gather(*list(mgr._wake_tasks)), 15)
            if mgr._input_flush_tasks:
                await asyncio.wait_for(asyncio.gather(*list(mgr._input_flush_tasks)), 15)
            current = mgr.get("sleepy")
            assert current is not original and not current.exited
            assert current.held_at is None
            await _wait_screen(current, "echo:" + text)
            if route == "input":
                assert session_input.latest("sleepy", "input-1")["status"] == "sent"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_held_deliveries_share_one_restore_and_keep_queue_order(home, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    async def run():
        mgr = _manager()
        held = mgr._retire(SessionDef(name="sleepy", harness="py"), {"held_at": HELD_AT})
        started, release = asyncio.Event(), asyncio.Event()
        received = []
        live = SimpleNamespace(exited=False, queue_delivery=lambda text: received.append(text) or True)

        async def respawn(name):
            started.set()
            await release.wait()
            mgr._sessions[name] = live
            return live

        spawn = AsyncMock(side_effect=respawn)
        monkeypatch.setattr(mgr, "respawn_async", spawn)
        assert held.queue_delivery("first")
        await started.wait()
        assert held.queue_delivery("second")
        release.set()
        await asyncio.gather(*list(mgr._wake_tasks))
        assert received == ["first", "second"]
        assert spawn.await_count == 1
        # A sender can still hold a reference to the old incarnation.
        assert held.queue_delivery("third")
        assert received == ["first", "second", "third"]

    asyncio.run(run())


@pytest.mark.parametrize("sender", ["old", "staged", "wake"])
def test_deferred_messages_keep_order_during_actual_restore_staging(
    home, tmp_path, monkeypatch, sender
):
    import sys
    from claude_launcher import store
    from test_daemon_e2e import CHILD

    _register_py_harness()

    def configure(doc):
        doc["harnesses"]["py"]["input_readiness"] = "bracketed-paste"
        doc["harnesses"]["py"]["command"] = [
            sys.executable, "-u", "-c",
            "import sys; sys.stdout.write('\\x1b[?2004h'); sys.stdout.flush()\n" + CHILD,
        ]

    store.update(configure)

    async def run():
        mgr = _manager()
        mgr.resumable = lambda s: True
        entered, release = asyncio.Event(), asyncio.Event()
        received = []
        original_launch = mgr.launch_async
        original_deliver = Session._deliver

        async def launch(session, **kwargs):
            entered.set()
            await release.wait()
            return await original_launch(session, **kwargs)

        async def deliver(session, text, **kwargs):
            result = await original_deliver(session, text, **kwargs)
            if result:
                received.append(text)
            return result

        def state():
            # What a failing wait reports. Read-only: nothing here changes the run.
            rows = []
            for label, s in (("original", original), ("current", mgr.get("sleepy"))):
                rows.append(
                    f"{label}: exited={getattr(s, 'exited', None)} "
                    f"exit_code={getattr(s, 'exit_code', None)} "
                    f"input_ready={getattr(s, '_input_ready', None)} "
                    f"deferred={len(getattr(s, '_deferred_deliveries', ()))}"
                )
            rows.append(f"received={received} wake_tasks={len(mgr._wake_tasks)}")
            return "; ".join(rows)

        async def waited(label, awaitable, timeout):
            try:
                return await asyncio.wait_for(awaitable, timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                raise AssertionError(
                    f"{label}: {type(exc).__name__} within {timeout}s; {state()}"
                ) from exc

        monkeypatch.setattr(mgr, "launch_async", launch)
        monkeypatch.setattr(Session, "_deliver", deliver)
        try:
            original = mgr.create(SessionDef(name="sleepy", harness="py", cwd=str(tmp_path)))
            await _wait_screen(original, "READY")
            mgr.hold("sleepy")
            await original.wait_for("exited", timeout=10, threshold=0.5)
            assert original.queue_delivery("first")
            await waited("restore entered", entered.wait(), 15)
            current = mgr.get("sleepy")
            assert current is not original and not current.exited, state()
            if sender == "wake":
                assert mgr.wake_soon("sleepy", deliver="second")
            else:
                assert (original if sender == "old" else current).queue_delivery("second")
            release.set()
            await waited("wake tasks", asyncio.gather(*list(mgr._wake_tasks)), 15)
            await waited(
                "deferred deliveries", asyncio.gather(*list(current._deferred_deliveries)), 15
            )
            assert received == ["first", "second"], state()
        finally:
            release.set()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_failed_restore_leaves_operator_input_queued_for_retry(home, monkeypatch):
    from unittest.mock import AsyncMock
    from claude_launcher.daemon import session_input

    async def run():
        mgr = _manager()
        held = mgr._retire(SessionDef(name="sleepy", harness="py"), {"held_at": HELD_AT})
        spawn = AsyncMock(side_effect=ManagerError("restore failed"))
        monkeypatch.setattr(mgr, "respawn_async", spawn)
        client = TestClient(TestServer(build_app(mgr, "sekrit", started_at=0.0)))
        await client.start_server()
        try:
            body = {"keys": ["keep this", "Enter"], "input_id": "retry-1"}
            for duplicate in (False, True):
                response = await client.post("/api/sessions/sleepy/keys", json=body,
                                             headers={"Authorization": "Bearer sekrit"})
                assert response.status == (200 if duplicate else 202)
                await asyncio.gather(*list(mgr._wake_tasks))
            assert spawn.await_count == 2
            assert mgr.get("sleepy") is held
            assert [r["text"] for r in session_input.pending("sleepy")] == ["keep this"]
            assert not await held.deliver("a failed delivery is not acknowledged")
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("markers", [{}, {"paused_at": HELD_AT}, {"archived_at": HELD_AT}])
def test_messages_do_not_restore_other_exited_states(home, monkeypatch, markers):
    from unittest.mock import AsyncMock

    async def run():
        mgr = _manager()
        dead = mgr._retire(SessionDef(name="dead", harness="py"), markers)
        spawn = AsyncMock()
        monkeypatch.setattr(mgr, "respawn_async", spawn)
        assert not await dead.deliver("message")
        assert not dead.queue_delivery("message")
        spawn.assert_not_called()

    asyncio.run(run())


def test_message_waits_for_hold_process_exit_before_restore(home, monkeypatch):
    from unittest.mock import AsyncMock

    async def run():
        mgr = _manager()
        terminating = SimpleNamespace(exited=False, held_at=HELD_AT)
        live = SimpleNamespace(exited=False, held_at=None)
        mgr._sessions["sleepy"] = terminating
        calls = []

        async def wait_for(*args, **kwargs):
            calls.append("exit")
            terminating.exited = True

        async def respawn(name):
            assert terminating.exited
            calls.append("restore")
            mgr._sessions[name] = live
            return live

        terminating.wait_for = wait_for
        monkeypatch.setattr(mgr, "respawn_async", AsyncMock(side_effect=respawn))
        assert await mgr.wake_async("sleepy", by="message") is live
        assert calls == ["exit", "restore"]

    asyncio.run(run())


def test_input_during_restore_flush_keeps_order_without_suppressing_old_retries(
    home, tmp_path, monkeypatch
):
    from claude_launcher.daemon import session_input

    _register_py_harness()

    async def run():
        mgr = _manager()
        client = TestClient(TestServer(build_app(mgr, "sekrit", started_at=0.0)))
        await client.start_server()
        entered, release = asyncio.Event(), asyncio.Event()
        original_type = session_input.type_line
        typed = []

        async def type_line(session, text):
            typed.append(text)
            if text == "first":
                entered.set()
                await release.wait()
            return await original_type(session, text)

        monkeypatch.setattr(session_input, "type_line", type_line)
        headers = {"Authorization": "Bearer sekrit"}
        try:
            session = mgr.create(SessionDef(name="sleepy", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")
            session_input.queue("sleepy", request_id="first", text="first")
            mgr.flush_queued_input(session)
            await asyncio.wait_for(entered.wait(), 15)
            for rid in ("first", "second"):
                response = await client.post("/api/sessions/sleepy/keys", headers=headers,
                    json={"keys": [rid, "Enter"], "input_id": rid, "force": True})
                doc = await response.json()
                assert response.status == (200 if rid == "first" else 202), doc
                assert doc.get("duplicate" if rid == "first" else "queued") is True
            release.set()
            await asyncio.wait_for(asyncio.gather(*list(mgr._input_flush_tasks)), 15)
            assert typed == ["first", "second"]
            await _wait_screen(session, "echo:second")
            # A previous daemon's accepted entry is not an in-flight send.
            session_input.write("sleepy", "input_accepted", request_id="old", text="retry-old", status="accepted")
            response = await client.post("/api/sessions/sleepy/keys", headers=headers,
                json={"keys": ["retry-old", "Enter"], "input_id": "old", "force": True})
            assert response.status == 200
            assert not (await response.json()).get("duplicate")
            await _wait_screen(session, "echo:retry-old")
        finally:
            release.set()
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_needle_cut_by_a_row_wrap_is_still_on_the_screen():
    # claunch-jnwbc: the failing sweep's screen held "echo:[claunch status-check re"
    # at the end of a 120-column row and "fresh]" at the start of the next.
    rows = ["x" * 94 + "echo:[claunch status-check re", "fresh]", "echo:next"]
    text = "\n".join(rows)
    needle = "echo:[claunch status-check refresh]"
    assert needle not in text
    assert _on_screen(needle, text)
    assert not _on_screen("echo:[claunch status-check refresh] gone", text)


@pytest.mark.parametrize("by, by_human", [
    ("mesh", False), ("cflow", False), ("cflow2", False),
    ("status-checks", False), ("deliver", False), ("restore-queue", False),
    ("cflow-checks", False), ("cflow2-checks", False), ("notice", False),
    ("attach", True), ("input", True), ("peer-input", True), ("api", True),
    ("message", True),         # a delivery that does not name itself the daemon's
    ("qa", True), ("thread", True), ("", True),
])
def test_only_a_person_s_wake_marks_the_session_resumed_by_human(
        home, monkeypatch, by, by_human):
    """claunch-u9m6z: respawn marks every relaunch as a person's, and a held
    done session woken by a mesh delivery (s122) then had its kill-on-end
    called off. A wake the daemon starts on its own is nobody asking for
    this incarnation; an attach, typed input or a QA answer is."""
    from unittest.mock import AsyncMock

    async def run():
        mgr = _manager()
        mgr._sessions["sleepy"] = SimpleNamespace(exited=True, held_at=HELD_AT)
        live = SimpleNamespace(exited=False, held_at=None, resumed_by_human=True)

        async def respawn(name):
            mgr._sessions[name] = live
            return live                      # as _respawned leaves it

        monkeypatch.setattr(mgr, "respawn_async", AsyncMock(side_effect=respawn))
        assert await mgr.wake_async("sleepy", by=by) is live
        assert live.resumed_by_human is by_human

    asyncio.run(run())


@pytest.mark.parametrize("word", [None, "notice", "mesh"])
def test_a_held_delivery_wakes_with_the_word_its_caller_gives(home, word):
    """claunch-2ri1b: every Session.deliver and queue_delivery that reaches a
    held session woke it ``by="message"`` -- a person's -- so a reminder or a
    cflow notice that woke a done session called its kill-on-end off. A
    caller that is the daemon's own names itself with delivery_wake; one that
    says nothing stays a person's."""
    from claude_launcher.daemon.session import delivery_wake

    async def run():
        mgr = _manager()
        held = mgr._retire(SessionDef(name="sleepy", harness="py"), {"held_at": HELD_AT})
        woken, queued = [], []
        gone = SimpleNamespace(exited=True)

        async def wake(name, *, by=""):
            woken.append(by)
            return gone

        held.wake_on_message = wake
        held.queue_on_message = lambda name, text, *, by: queued.append(by) or True
        with delivery_wake(word) if word else contextlib.nullcontext():
            await held.deliver("[claunch session: reminder]")
            assert held.queue_delivery("[claunch cflow2: the run moved]")
        assert woken == [word or "message"]
        assert queued == [word or "message"]

    asyncio.run(run())


@pytest.mark.parametrize("joined, by_human", [(None, False), ("message", True), ("mesh", False)])
def test_a_person_s_message_joining_a_notice_s_wake_keeps_the_session(
        home, monkeypatch, joined, by_human):
    """Messages that reach one held session share one wake, started by the
    first: a notice's wake that a person's message joins before the relaunch
    is still that person's incarnation (claunch-2ri1b)."""
    from unittest.mock import AsyncMock

    async def run():
        mgr = _manager()
        mgr._sessions["sleepy"] = SimpleNamespace(exited=True, held_at=HELD_AT)
        live = SimpleNamespace(exited=False, held_at=None, resumed_by_human=True,
                               queue_delivery=lambda text: True)
        started, release = asyncio.Event(), asyncio.Event()

        async def respawn(name):
            started.set()
            await release.wait()
            mgr._sessions[name] = live
            return live                      # as _respawned leaves it

        monkeypatch.setattr(mgr, "respawn_async", AsyncMock(side_effect=respawn))
        assert mgr.queue_message("sleepy", "[claunch session: reminder]", by="notice")
        await started.wait()
        if joined:
            assert mgr.queue_message("sleepy", "typed by a person", by=joined)
        release.set()
        await asyncio.gather(*list(mgr._wake_tasks))
        assert live.resumed_by_human is by_human
        assert "sleepy" not in mgr._wake_message_people

    asyncio.run(run())
