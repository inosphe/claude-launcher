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


def build(tmp_path, sessions, created):
    mgr = KeepAlive(sessions)
    app = web.Application()
    app["manager"] = mgr

    async def create(_request, spec):
        if spec["profile"] == "broken":
            return 400, {"error": "no such profile"}
        created.append(spec)
        name = f"agg{len(created)}"
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
