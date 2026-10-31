"""HTTP routes for session plans (daemon/plan_api.py, claunch-7jzp8): the
caller told by ``by``, the daemon-written context, the 409 that carries the
current plan, and the [claunch plan] block typed into the owning session
when the user decides."""

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import plan_api
from claude_launcher.plan import store as P

from test_qa_api import FakeManager, FakeSession

OPTS = [{"label": "A", "effect": "fast", "risk": "untested", "recommended": True},
        {"label": "B", "effect": "safe", "risk": "slow"}]
PLAN = {"session": "s1", "by": "s1", "revision": 0, "title": "Ship X",
        "steps": [{"id": "d1", "kind": "decision", "title": "storage", "options": OPTS}],
        "closure": [{"id": "v1", "kind": "verify", "title": "open the pane"},
                    {"id": "f1", "kind": "followup", "title": "joint tree"}]}


def run(tmp_path, scenario):
    s1 = FakeSession("s1")
    s1.sdef = SimpleNamespace(name="s1", project="claunch", cwd="/w/claunch", parent="s29")
    mgr = FakeManager([s1, FakeSession("s2")])
    app = web.Application()
    app["manager"] = mgr
    plan_api.install(app, P.PlanStore(tmp_path / "plan.sqlite3"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data
            await scenario(req, mgr)

    asyncio.run(go())


def test_set_read_decide_and_the_block_the_session_receives(tmp_path):
    async def scenario(req, mgr):
        p = (await req("POST", "/api/plan", PLAN))["plan"]
        assert p["context"] == {"project": "claunch", "cwd": "/w/claunch", "parent": "s29"}
        assert (await req("GET", "/api/plan?session=s1"))["plan"]["revision"] == 1
        assert (await req("GET", "/api/plan?session=s2"))["plan"] is None
        rail = (await req("GET", "/api/plan/rail"))["sessions"]["s1"]
        assert rail["waiting"] == 3 and rail["title"] == "Ship X"

        res = await req("POST", "/api/plan/node/d1/decide", {"session": "s1", "rev": 1, "choice": "B",
                                                              "note": "keep it safe"})
        assert res["delivered"] is True
        block = mgr.sessions["s1"].delivered[-1]
        assert block.startswith(plan_api.PREFIX)
        assert 'chose "B"' in block and "risk: slow" in block and "> keep it safe" in block
        listed = (await req("GET", "/api/plan?session=s1"))["plan"]
        assert listed["steps"][0]["delivery"]["state"] == "delivered"

        await req("POST", "/api/plan/node/v1/decide", {"session": "s1", "rev": 1, "verdict": "failed",
                                                        "note": "blank pane"})
        assert "failed by hand" in mgr.sessions["s1"].delivered[-1]
        await req("POST", "/api/plan/node/f1/decide", {"session": "s1", "rev": 1, "action": "issue"})
        assert "claunch beads create" in mgr.sessions["s1"].delivered[-1]
        assert mgr.sessions["s2"].delivered == []
    run(tmp_path, scenario)


def test_who_may_write(tmp_path):
    async def scenario(req, mgr):
        await req("POST", "/api/plan", {**PLAN, "by": "s2"}, status=403)       # another session's plan
        await req("POST", "/api/plan", {**PLAN, "by": None}, status=403)       # the user does not write it
        await req("POST", "/api/plan", {**PLAN, "by": "ghost", "session": "ghost"}, status=404)
        await req("POST", "/api/plan", PLAN)
        await req("POST", "/api/plan/node/d1/decide", {"session": "s1", "by": "s1", "rev": 1, "choice": "A"},
                  status=403)
        await req("POST", "/api/plan/node/d1", {"title": "x"}, status=400)     # no session named
    run(tmp_path, scenario)


def test_conflicts_carry_the_current_plan(tmp_path):
    async def scenario(req, mgr):
        await req("POST", "/api/plan", PLAN)
        await req("POST", "/api/plan/node/d1/decide", {"session": "s1", "rev": 1, "choice": "A"})
        res = await req("POST", "/api/plan", {**PLAN, "revision": 1}, status=409)
        assert res["current"]["revision"] == 2
        await req("POST", "/api/plan/node/d1", {"session": "s1", "by": "s1", "detail": "reworded"})
        res = await req("POST", "/api/plan/node/d1/decide", {"session": "s1", "rev": 1, "choice": "B"},
                        status=409)
        assert res["current"]["steps"][0]["rev"] == 2
    run(tmp_path, scenario)


def test_feedback_is_typed_into_the_session(tmp_path):
    async def scenario(req, mgr):
        await req("POST", "/api/plan", PLAN)
        res = await req("POST", "/api/plan/feedback", {"session": "s1", "text": "split d1", "node": "d1"})
        assert res["delivered"] is True
        block = mgr.sessions["s1"].delivered[-1]
        assert "commented on step d1" in block and "> split d1" in block
        await req("POST", "/api/plan/feedback", {"session": "s1", "text": ""}, status=400)
    run(tmp_path, scenario)


def test_joint_tree_reads_the_live_parent(tmp_path):
    async def scenario(req, mgr):
        await req("POST", "/api/plan", PLAN)
        mgr.sessions["s2"].sdef.parent = "s1"
        await req("POST", "/api/plan", {**PLAN, "session": "s2", "by": "s2", "issue": "claunch-x.1"})
        g = (await req("GET", "/api/plan/joint?session=s2"))["group"]
        # s1's own spawn parent s29 has no plan (and no record here): drawn from s1's context
        assert [(m["session"], m["via"], m["has_plan"]) for m in g["members"]] == [
            ("s29", None, False), ("s1", "spawn", True), ("s2", "spawn", True)]
        assert g["waiting"] == 6
        assert (await req("GET", "/api/plan/joint?session=ghost"))["group"] is None
        assert len((await req("GET", "/api/plan/joint"))["groups"]) == 1
    run(tmp_path, scenario)
