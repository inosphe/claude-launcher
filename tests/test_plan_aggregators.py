"""A project's plan aggregation session (daemon/plan_api.py, claunch-7jzp8.3):
started by the user from the plan page, one per project, bound and kept
alive; the overview marks it and narrows to its project."""

import asyncio
import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import actors, plan_api
from claude_launcher.plan import store as P

from test_plan_overview import Manager
from test_qa_api import FakeSession


class KeepAlive(Manager):
    def __init__(self, sessions):
        super().__init__(sessions)
        self.kept = []

    def set_keep_alive(self, name, on):
        self.kept.append((name, on))


class Meshes:
    """The two reads plan_api makes of the mesh manager."""

    def __init__(self, members):
        self.members = members                                      # {session: [mesh, ...]}
        self.handles = {}                                           # {(mesh, handle): session}; outlives the session
        self.left = []

    def get(self, name):
        if not any(name in m for m in self.members.values()):
            raise ValueError(f"no mesh named {name!r}")
        held = {h: SimpleNamespace(handle=h, session=s) for (m, h), s in self.handles.items() if m == name}
        return SimpleNamespace(name=name, members=held)

    async def leave(self, name, handle):
        self.left.append((name, handle))
        del self.handles[(name, handle)]

    def meshes_for_session(self, session):
        return [{"mesh": m, "handle": session} for m in self.members.get(session, [])]


def build(tmp_path, sessions, created, meshes=None):
    mgr = KeepAlive(sessions)
    app = web.Application()
    app["manager"] = mgr
    if meshes is not None:
        app["mesh"] = meshes

    async def create(_request, spec):
        if spec["profile"] == "broken":
            return 400, {"error": "no such profile"}
        created.append(spec)
        name = f"agg{len(created)}"
        if meshes is not None and spec.get("mesh"):
            key = (spec["mesh"], spec["handle"])
            assert key not in meshes.handles, f"handle {key} is already taken"   # onboard.preflight's refusal
            meshes.handles[key] = name
        s = FakeSession(name)
        s.sdef = SimpleNamespace(name=name, project=spec["project"], cwd=str(tmp_path), parent=None)
        s.status = lambda: "idle"
        mgr.sessions[name] = s
        return 201, {"name": name}

    plan_api.install(app, P.PlanStore(tmp_path / "plan.sqlite3"), runs_of=lambda pairs: {}, create=create,
                     aggregators=plan_api.Aggregators(tmp_path / "plan_aggregators.json"))
    return app, mgr


def test_start_binds_one_session_per_project(tmp_path):
    w = FakeSession("w1")
    w.sdef = SimpleNamespace(name="w1", project="default", cwd=str(tmp_path), parent=None)
    w.status = lambda: "busy"
    created = []
    app, mgr = build(tmp_path, [w], created)

    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/plan/aggregators/default/start", json={})
            assert resp.status == 400                                   # profile is required
            resp = await client.post("/api/plan/aggregators/nowhere/start", json={"profile": "p"})
            assert resp.status == 400                                   # unknown project
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "broken"})
            assert resp.status == 400 and not created                   # create's refusal is passed on
            agent = {actors.HEADER: actors.header_value(actors.AGENT, "w1")}
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"}, headers=agent)
            assert resp.status == 403 and not created                   # the user's

            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p", "model": "m"})
            assert resp.status == 201
            out = await resp.json()
            assert out["name"] == "agg1" and out["keep_alive"] is True and out["project"] == "default"
            spec, = created
            assert spec["project"] == "default" and spec["beads"] is False and spec["model"] == "m"
            assert json.loads(spec["context"]) == {"plan_aggregator": "default"}
            assert "plan_overview(project='default')" in spec["task"] and "plan_set" in spec["task"]
            assert "mesh" not in spec and "handle" not in spec                  # this daemon has no meshes
            assert "메시로 전달하지 않는다" in spec["task"] and out["mesh"] is None and out["mesh_note"]
            assert mgr.kept == [("agg1", True)]

            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"})
            assert resp.status == 409 and (await resp.json())["session"] == "agg1"

            rows = (await (await client.get("/api/plan/aggregators")).json())["aggregators"]
            assert [(r["project"], r["session"], r["running"]) for r in rows] == [("default", "agg1", True)]

            out = await (await client.get("/api/plan/overview?project=default")).json()
            assert {r["session"]: r["aggregator"] for r in out["rows"]} == {"w1": None, "agg1": "default"}
            assert [a["session"] for a in out["aggregators"]] == ["agg1"]
            out = await (await client.get("/api/plan/overview?project=other")).json()
            assert out["rows"] == [] and out["aggregators"] == []

            mgr.sessions["agg1"].exited = True                          # ended: a new one may start
            rows = (await (await client.get("/api/plan/aggregators")).json())["aggregators"]
            assert rows[0]["running"] is False
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"})
            assert resp.status == 201 and (await resp.json())["name"] == "agg2"

    asyncio.run(go())


def test_the_aggregator_joins_the_projects_mesh(tmp_path):
    def session(name, project):
        s = FakeSession(name)
        s.sdef = SimpleNamespace(name=name, project=project, cwd=str(tmp_path), parent=None)
        s.status = lambda: "busy"
        return s

    meshes = Meshes({"w1": ["team"], "w2": ["team"], "x1": ["other"]})
    created = []
    app, mgr = build(tmp_path, [session("w1", "default"), session("w2", "default"), session("x1", "x")],
                     created, meshes)

    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p", "mesh": "nope"})
            assert resp.status == 400 and "nope" in (await resp.json())["error"] and not created

            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"})
            assert resp.status == 201
            out = await resp.json()
            spec, = created
            assert (spec["mesh"], spec["handle"]) == ("team", "plan-default")       # the one mesh its sessions are on
            assert "메시 'team'의 send로 전달한다" in spec["task"] and "plan-default" in spec["task"]
            assert out["mesh"] == "team" and "mesh_note" not in out

            mgr.sessions[out["name"]].exited = True                                # ended, its handle kept:
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"})
            out = await resp.json()                                                # the start releases it
            assert resp.status == 201 and meshes.left == [("team", "plan-default")]
            assert (created[-1]["mesh"], meshes.handles[("team", "plan-default")]) == ("team", out["name"])

            mgr.sessions[out["name"]].exited = True
            meshes.handles[("team", "plan-default")] = "w1"                        # someone else's: left alone
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"})
            out = await resp.json()
            assert resp.status == 201 and out["mesh"] is None and "held by session 'w1'" in out["mesh_note"]
            assert "mesh" not in created[-1] and meshes.left == [("team", "plan-default")]

            mgr.sessions[out["name"]].exited = True
            del meshes.handles[("team", "plan-default")]
            meshes.members["w2"] = ["team", "side"]                                # several: none is guessed
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"})
            out = await resp.json()
            assert resp.status == 201 and out["mesh"] is None and "several meshes" in out["mesh_note"]
            assert "mesh" not in created[-1] and "메시로 전달하지 않는다" in created[-1]["task"]

            mgr.sessions[out["name"]].exited = True                                # named in the body: that one
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p", "mesh": "side"})
            assert resp.status == 201 and (created[-1]["mesh"], created[-1]["handle"]) == ("side", "plan-default")

    asyncio.run(go())


def test_without_create_the_start_is_unavailable(tmp_path):
    app = web.Application()
    app["manager"] = Manager([])
    plan_api.install(app, P.PlanStore(tmp_path / "plan.sqlite3"),
                     aggregators=plan_api.Aggregators(tmp_path / "a.json"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/plan/aggregators/default/start", json={"profile": "p"})
            assert resp.status == 503

    asyncio.run(go())


def test_a_damaged_record_reads_as_empty(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json", encoding="utf-8")
    assert plan_api.Aggregators(path).load() == {}
    path.write_text(json.dumps({"x": {"profile": "p"}, "y": {"session": "s"}}), encoding="utf-8")
    assert plan_api.Aggregators(path).load() == {"y": {"session": "s"}}
