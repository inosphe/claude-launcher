"""Progress overview of the active sessions (plan/overview.py and GET
/api/plan/overview, claunch-7jzp8.2): the stage each row is grouped by, the
run's and the plan's fields side by side, running sessions by default."""

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import plan_api
from claude_launcher.plan import overview as O
from claude_launcher.plan import store as P

from test_qa_api import FakeManager, FakeSession


def test_stages():
    assert O.stage({"waiting": 1}, None) == "user"
    assert O.stage({"waiting": 0}, {"owed": ["choose: land?"]}) == "user"
    assert O.stage({"waiting": 0, "blocked": 1}, None) == "blocked"
    assert O.stage({"waiting": 0, "closable": True}, {"owed": []}) == "closable"
    assert O.stage(None, {"owed": []}) == "working"
    assert O.stage({"waiting": 0}, None) == "working"
    assert O.stage(None, None) == "idle"


def test_rows_are_grouped_by_stage_and_counted():
    sessions = [{"session": n} for n in ("z", "a", "m", "q")]
    plans = {"z": {"title": "Z", "waiting": 2, "steps": 3, "done": 1}, "m": {"closable": True}}
    runs = {"a": {"workflow": "improv-worker-v2", "current": ["work"], "owed": []},
            "z": {"workflow": "w", "current": ["commit"], "owed": [], "inputs": {"secret": 1}}}
    out = O.overview(sessions, plans, runs)
    assert [(r["session"], r["stage"]) for r in out["rows"]] == [
        ("z", "user"), ("m", "closable"), ("a", "working"), ("q", "idle")]
    assert out["counts"] == {"user": 1, "blocked": 0, "closable": 1, "working": 1, "idle": 1}
    z = out["rows"][0]
    assert z["plan"]["title"] == "Z" and z["run"]["current"] == ["commit"] and "inputs" not in z["run"]


class Manager(FakeManager):
    def list(self):
        return list(self.sessions.values())


def test_route_reads_running_sessions_their_plans_and_runs(tmp_path):
    s1, s2, s3 = FakeSession("s1"), FakeSession("s2"), FakeSession("s3", held=True)
    for s in (s1, s2, s3):
        s.sdef = SimpleNamespace(name=s.sdef.name, project="claunch", cwd=str(tmp_path), parent="s29")
        s.status = lambda: "busy"
    mgr = Manager([s1, s2, s3])
    app = web.Application()
    app["manager"] = mgr
    seen = []

    def runs_of(pairs):
        seen.append([n for n, _ in pairs])
        return {"s2": {"workflow": "improv-worker-v2", "current": ["work"], "owed": ["choose: land?"]}}

    plan_api.install(app, P.PlanStore(tmp_path / "plan.sqlite3"), runs_of=runs_of)

    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/plan", json={"session": "s1", "by": "s1", "revision": 0, "title": "T",
                                                        "steps": [{"title": "a", "status": "blocked"}]})
            assert resp.status == 200
            out = await (await client.get("/api/plan/overview")).json()
            assert [(r["session"], r["stage"]) for r in out["rows"]] == [("s2", "user"), ("s1", "blocked")]
            assert out["rows"][1]["plan"]["blocked"] == 1 and out["rows"][0]["project"] == "claunch"
            assert seen[-1] == ["s1", "s2"]                      # the held (exited) one is left out
            out = await (await client.get("/api/plan/overview?all=1")).json()
            held = next(r for r in out["rows"] if r["session"] == "s3")
            assert held["status"] == "exited" and held["category"] == "hold" and held["stage"] == "idle"

    asyncio.run(go())
