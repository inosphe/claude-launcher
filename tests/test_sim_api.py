"""HTTP routes for simulations (daemon/sim_api.py): caller identity, owner
notices (end of campaign, answers, user requests), exit-hook release."""

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import sim_api
from claude_launcher.sim import store as S

SPEC = "The door opens when closed.\n"


class FakeSession:
    def __init__(self, name, cwd, project="proj"):
        self.sdef = SimpleNamespace(name=name, cwd=cwd, project=project)
        self.exited = False
        self.held_at = None
        self.delivered = []

    def queue_delivery(self, text):
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.exit_hooks = []

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]

    def end(self, name):
        s = self.sessions[name]
        s.exited = True
        for hook in self.exit_hooks:
            hook(s)


PATCH = {
    "machines": [{"id": "door", "states": ["closed", "open"], "initial": "closed"}],
    "events": [{"name": "open", "kind": "actor"}],
    "rules": [{"id": "r.open", "on": {"event": "open"}, "when": {"sm": "door", "is": "closed"},
               "do": [{"sm": "door", "to": "open"}],
               "source": [{"ref": "spec.md", "quote": "The door opens when closed."}]}],
}


def run(tmp_path, scenario):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "spec.md").write_text(SPEC, encoding="utf-8")
    s1, s2 = FakeSession("s1", str(root)), FakeSession("s2", str(root))
    mgr = FakeManager([s1, s2])
    app = web.Application()
    app["manager"] = mgr
    sim_api.install(app, S.SimStore(tmp_path / "sims", live=lambda n: sim_api._live(mgr, n)))

    async def go():
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data
            await scenario(req, mgr, s1, s2)

    asyncio.run(go())


def test_session_creates_owns_and_is_told_when_it_ends(tmp_path):
    async def scenario(req, mgr, s1, s2):
        made = await req("POST", "/api/sims", {"title": "door", "session": "s1",
                                               "settings": {"max_rounds": 1}}, status=201)
        sim = made["sim"]
        assert sim["owner"] == "s1" and sim["project"] == "proj" and sim["root"].endswith("repo")
        assert not made["nudged"] and not s1.delivered  # the session asked; nothing typed back
        sid = sim["id"]
        bad = await req("POST", f"/api/sims/{sid}/model", {"session": "s2", "patch": PATCH}, status=403)
        assert "owned by s1" in bad["error"]
        assert (await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH}))["ok"]
        rejected = await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": {
            "rules": [{**PATCH["rules"][0], "source": [{"ref": "spec.md", "quote": "nope"}]}]}})
        assert rejected["ok"] is False and rejected["rejected"]
        started = await req("POST", f"/api/sims/{sid}/start", {"session": "s1"})
        assert started["result"]["ended"]["state"] == "stopped"
        assert started["notified"] and "finished" in s1.delivered[-1] and sid in s1.delivered[-1]
        # Another session reads it.
        shown = await req("GET", f"/api/sims/{sid}")
        assert shown["rounds"][-1]["round"] == 1 and shown["owner_live"]
        assert (await req("GET", f"/api/sims/{sid}/findings?status=open"))["total"] >= 1
        assert (await req("GET", f"/api/sims/{sid}/gaps"))["batches"]
    run(tmp_path, scenario)


def test_user_creates_for_a_session_and_answers(tmp_path):
    async def scenario(req, mgr, s1, s2):
        made = await req("POST", "/api/sims", {"title": "door", "goal": "find gaps", "owner": "s2"}, status=201)
        sid = made["sim"]["id"]
        assert made["nudged"] and "sim_show " + sid in s2.delivered[-1]
        await req("POST", f"/api/sims/{sid}/model", {"session": "s2", "patch": PATCH})
        await req("POST", f"/api/sims/{sid}/round", {"session": "s2"})
        key = "unhandled|door|open|open"
        await req("POST", f"/api/sims/{sid}/resolve", {"session": "s2", "items": [
            {"key": key, "action": "ask", "question": "and then?"}]})
        await req("POST", f"/api/sims/{sid}/answer", {"session": "s2", "key": key, "text": "x"}, status=403)
        ans = await req("POST", f"/api/sims/{sid}/answer", {"key": key, "text": "stays open"})
        assert ans["nudged"] and "stays open" in s2.delivered[-1]
        n = await req("POST", f"/api/sims/{sid}/nudge", {"text": "look at the lock too"})
        assert n["to"] == "s2" and "look at the lock too" in s2.delivered[-1]
    run(tmp_path, scenario)


def test_held_owner_keeps_ownership(tmp_path):
    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        s1.exited, s1.held_at = True, "2026-10-11T00:00:00+00:00"
        assert sim_api._live(mgr, "s1")
        await req("POST", f"/api/sims/{sid}/claim", {"session": "s2"}, status=409)
    run(tmp_path, scenario)


def test_exit_releases_and_user_transfers(tmp_path):
    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH})
        await req("POST", f"/api/sims/{sid}/start", {"session": "s1"})
        await req("POST", f"/api/sims/{sid}/claim", {"session": "s2"}, status=409)
        mgr.end("s1")
        got = (await req("GET", f"/api/sims/{sid}"))["sim"]
        assert got["owner"] == "" and got["state"] == "stopped"
        await req("POST", f"/api/sims/{sid}/transfer", {"session": "s2", "to": "s2"}, status=403)
        moved = await req("POST", f"/api/sims/{sid}/transfer", {"to": "s2"})
        assert moved["sim"]["owner"] == "s2" and "owner of simulation" in s2.delivered[-1]
        await req("POST", f"/api/sims/{sid}/nudge", {}, status=200)
        await req("POST", f"/api/sims/{sid}/release", {"session": "s2"})
        await req("POST", f"/api/sims/{sid}/nudge", {}, status=409)
        listed = await req("GET", "/api/sims?project=proj")
        assert [s["id"] for s in listed["sims"]] == [sid]
        await req("DELETE", f"/api/sims/{sid}")
        await req("GET", f"/api/sims/{sid}", status=404)
    run(tmp_path, scenario)


def test_user_accepts_an_edit_proposal_and_the_owner_is_told(tmp_path):
    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH})
        await req("POST", f"/api/sims/{sid}/round", {"session": "s1"})
        key = "unhandled|door|open|open"
        item = {"file": "spec.md", "old": "", "new": "Opening an open door does nothing.", "kind": "complete",
                "reason": "user answer", "findings": [key]}
        await req("POST", f"/api/sims/{sid}/proposals", {"session": "s2", "items": [item]}, status=403)
        made = await req("POST", f"/api/sims/{sid}/proposals", {"session": "s1", "items": [item]})
        pid = made["done"][0]
        assert (await req("GET", f"/api/sims/{sid}/proposals"))["proposals"][0]["id"] == pid
        await req("POST", f"/api/sims/{sid}/proposals/{pid}/decide", {"session": "s1", "accept": True}, status=403)
        res = await req("POST", f"/api/sims/{sid}/proposals/{pid}/decide", {"accept": True, "note": "go"})
        assert res["nudged"] and pid in s1.delivered[-1] and "append to spec.md" in s1.delivered[-1]
        assert "Opening an open door does nothing." in s1.delivered[-1]
    run(tmp_path, scenario)
