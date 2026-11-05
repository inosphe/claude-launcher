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


def test_a_note_pick_reaches_the_session_and_gates_nothing(tmp_path):
    """claunch-i0whx: a note's optional 「여기서 진행」/「이슈로 넘김」 is typed
    into the session like a followup's; 버림 is not offered."""
    async def scenario(req, mgr):
        await req("POST", "/api/plan", {**PLAN, "steps": [], "closure": [
            {"id": "n1", "kind": "note", "title": "could also cache X"}]})
        res = await req("POST", "/api/plan/node/n1/decide", {"session": "s1", "rev": 1, "action": "here",
                                                              "note": "this round"})
        assert res["delivered"] is True and res["plan"]["summary"]["closable"] is True
        block = mgr.sessions["s1"].delivered[-1]
        assert "picked up your plan's note n1: here" in block and "> this round" in block
        assert "plan_set" in block
        await req("POST", "/api/plan/node/n1/decide", {"session": "s1", "rev": 1, "action": "issue"})
        block = mgr.sessions["s1"].delivered[-1]
        assert "claunch beads create" in block and "(this replaces an earlier result: here)" in block
        await req("POST", "/api/plan/node/n1/decide", {"session": "s1", "rev": 1, "action": "drop"}, status=400)
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
        assert "tree" not in g["members"][1]                                  # only when asked for

        await req("POST", "/api/plan/node/d1/decide", {"session": "s1", "rev": 1, "choice": "B"})
        g, = (await req("GET", "/api/plan/joint?steps=1"))["groups"]
        s29, s1, s2 = g["members"]
        assert "tree" not in s29                                              # no plan, nothing to draw
        d1, = s1["tree"]
        assert (d1["kind"], d1["choice"], [o["label"] for o in d1["options"]]) == ("decision", "B", ["A", "B"])
        assert d1["children"] == [] and "rev" not in d1 and "result" not in d1
        assert "choice" not in s2["tree"][0]                                  # s2 has not decided
        one = (await req("GET", "/api/plan/joint?session=s2&steps=1"))["group"]
        assert one["members"][1]["tree"][0]["choice"] == "B"
    run(tmp_path, scenario)


def test_rounds_open_seal_list_and_read(tmp_path):
    """claunch-r3j1o.1: POST /api/plan/round, GET /api/plan/rounds and
    GET /api/plan?round=."""
    async def scenario(req, mgr):
        opened = await req("POST", "/api/plan/round", {"session": "s1", "by": "s1", "run": "r1", "round": 1})
        assert opened["opened"] is True and opened["plan"]["context"]["project"] == "claunch"
        await req("POST", "/api/plan", {**PLAN, "revision": opened["plan"]["revision"]})
        again = await req("POST", "/api/plan/round", {"session": "s1", "by": "s1", "run": "r1", "round": 1})
        assert again["opened"] is False
        # v1 and f1 are still owed: require_disposed refuses and seals nothing
        await req("POST", "/api/plan/round", {"session": "s1", "by": "s1", "run": "r1", "round": 2,
                                              "leftover": "require_disposed"}, status=409)
        await req("POST", "/api/plan/round", {"session": "s1", "by": "s1", "run": "r1", "round": 2,
                                              "leftover": "bogus"}, status=400)
        assert (await req("GET", "/api/plan/rounds?session=s1"))["rounds"] == []
        nxt = await req("POST", "/api/plan/round", {"session": "s1", "by": "s1", "run": "r1", "round": 2})
        assert nxt["sealed"]["key"] == "r1/1" and set(nxt["carried"]) == {"v1", "f1"}
        listed = await req("GET", "/api/plan/rounds?session=s1")
        assert [r["key"] for r in listed["rounds"]] == ["r1/1"] and listed["current"]["round"] == 2
        old = await req("GET", "/api/plan?session=s1&round=r1/1")
        assert old["plan"]["sealed"] is True and old["plan"]["steps"][0]["id"] == "d1"
        assert (await req("GET", "/api/plan?session=s1&round=r1/7"))["plan"] is None
        await req("GET", "/api/plan?session=s1&round=bad", status=400)
        await req("POST", "/api/plan/round", {"session": "s1", "run": "r1", "round": 3}, status=403)
    run(tmp_path, scenario)
