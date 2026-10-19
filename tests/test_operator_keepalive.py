"""Operator sessions always carry keep-alive (claunch-higqx).

What has to hold:
  - every live or held operator session -- bound to an Operator v2 record, or
    holding the ``operator`` role on a mesh -- gets ``keep_alive`` set, and
    no other session does;
  - an ended (not held) session and one being killed are left alone;
  - a flag cleared on an operator session is set again on the next pass;
  - the idle-hold clock runs the pre-pass before its sweep, and a failing
    pre-pass does not stop the sweep;
  - the Operator v2 start route sets the flag on the session it binds.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from claude_launcher.daemon import hold as hold_mod
from claude_launcher.daemon import operator_keepalive


class FakeSession:
    def __init__(self, name, exited=False, held_at=None, kill_requested=False, keep_alive=False):
        self.sdef = SimpleNamespace(name=name, keep_alive=keep_alive)
        self.exited = exited
        self.held_at = held_at
        self.kill_requested = kill_requested


class FakeManager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.set_calls = []

    def list(self):
        return list(self.sessions.values())

    def set_keep_alive(self, name, on):
        self.set_calls.append((name, on))
        session = self.sessions[name]
        session.sdef = SimpleNamespace(**{**vars(session.sdef), "keep_alive": bool(on)})
        return session


class FakeOperators:
    def __init__(self, *names):
        self.names = set(names)

    def operator_names(self):
        return set(self.names)


class FakeMesh:
    def __init__(self, roles):
        self.roles = roles

    def meshes_for_session(self, name):
        role = self.roles.get(name)
        return [{"mesh": "m", "role": role, "roles": [role]}] if role else []


def _flags(manager):
    return {n: s.sdef.keep_alive for n, s in manager.sessions.items()}


def test_operator_sessions_get_keep_alive_and_others_do_not():
    manager = FakeManager(
        FakeSession("opv2"), FakeSession("meshop"), FakeSession("worker"),
        FakeSession("held-op", exited=True, held_at=1.0),
        FakeSession("ended-op", exited=True),
        FakeSession("dying-op", kill_requested=True),
    )
    changed = operator_keepalive.ensure(
        manager,
        operators=FakeOperators("opv2", "held-op", "ended-op", "dying-op"),
        mesh_mgr=FakeMesh({"meshop": "operator", "worker": "worker"}),
    )
    assert sorted(changed) == ["held-op", "meshop", "opv2"]
    assert _flags(manager) == {
        "opv2": True, "meshop": True, "worker": False, "held-op": True,
        "ended-op": False, "dying-op": False,
    }


def test_already_flagged_sessions_are_not_rewritten_and_a_cleared_flag_comes_back():
    manager = FakeManager(FakeSession("op", keep_alive=True))
    ops = FakeOperators("op")
    assert operator_keepalive.ensure(manager, operators=ops) == []
    assert manager.set_calls == []
    manager.set_keep_alive("op", False)
    assert operator_keepalive.ensure(manager, operators=ops) == ["op"]
    assert manager.sessions["op"].sdef.keep_alive is True


def test_unreadable_readers_change_nothing_and_do_not_raise():
    class Broken:
        def operator_names(self):
            raise OSError("record unreadable")

    manager = FakeManager(FakeSession("op"))
    assert operator_keepalive.ensure(manager, operators=Broken()) == []
    assert _flags(manager) == {"op": False}


def test_hold_clock_runs_the_pre_pass_before_each_sweep(monkeypatch):
    order = []
    monkeypatch.setattr(hold_mod, "sweep", lambda *a, **k: order.append("sweep"))

    def before():
        order.append("before")
        if len(order) == 1:
            raise RuntimeError("first pre-pass fails")

    async def go():
        clock = hold_mod.HoldClock(object(), interval=0.01, config=lambda: {}, before=before)
        clock.start()
        while order.count("sweep") < 2:
            await asyncio.sleep(0.01)
        await clock.stop()

    asyncio.run(go())
    assert order[:4] == ["before", "sweep", "before", "sweep"]


def test_start_route_sets_keep_alive_on_the_bound_session(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon import operators_v2

    monkeypatch.setattr(operators_v2.projects, "require", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(operators_v2.projects, "names", lambda doc=None: ["beta"])
    manager = FakeManager()
    app = web.Application()
    app["manager"] = manager

    async def create(request, body):
        session = FakeSession("op-new")
        session.sdef = SimpleNamespace(name="op-new", project="beta", cwd="", task=None,
                                       issue=None, keep_alive=False)
        manager.sessions["op-new"] = session
        return 201, {"name": "op-new"}

    operators_v2.install(app, create=create)
    app["operators_v2"].root = tmp_path / "operators"
    app["operators_v2"].cache = None

    async def go():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            op = await (await client.post("/api/operators", json={"name": "Beta", "projects": ["beta"]})).json()
            resp = await client.post(f"/api/operators/{op['id']}/start", json={"profile": "p"})
            assert resp.status == 201
            return await resp.json()
        finally:
            await client.close()

    payload = asyncio.run(go())
    assert payload["keep_alive"] is True
    assert manager.set_calls == [("op-new", True)]
    assert manager.sessions["op-new"].sdef.keep_alive is True
