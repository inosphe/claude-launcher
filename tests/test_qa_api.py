"""HTTP routes for session QA (daemon/qa_api.py, claunch-l8vyi): caller
identity, the 409 that carries the current question, and the [claunch qa]
block typed into the owning session when the user saves an answer."""

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import qa_api
from claude_launcher.qa import store as Q


class FakeSession:
    def __init__(self, name, held=False):
        self.sdef = SimpleNamespace(name=name)
        self.exited = held
        self.held_at = "now" if held else None
        self.delivered = []

    def queue_delivery(self, text):
        if self.exited:
            return False
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.woken = []

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]

    def wake_soon(self, name, *, by="", deliver=""):
        s = self.sessions.get(name)
        if s is None or not s.held_at:
            return False
        self.woken.append((name, by, deliver))
        return True


def run(tmp_path, scenario, sessions=None):
    sessions = sessions or [FakeSession("s1"), FakeSession("s2")]
    mgr = FakeManager(sessions)
    app = web.Application()
    app["manager"] = mgr
    qa_api.install(app, Q.QaStore(tmp_path / "qa.sqlite3"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data
            await scenario(req, mgr)

    asyncio.run(go())


def test_ask_answer_rewrite_and_the_block_the_session_receives(tmp_path):
    async def scenario(req, mgr):
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Ship it?",
                                           "options": ["yes", "no"]}))["question"]
        listed = await req("GET", "/api/qa?session=s1")
        assert listed["unresolved"] == 1 and listed["agent_seq"] > 0
        assert (await req("GET", "/api/qa/rail"))["sessions"]["s1"]["unresolved"] == 1

        res = await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "choices": ["yes"]})
        assert res["delivered"] is True
        block = mgr.sessions["s1"].delivered[-1]
        assert block.startswith(qa_api.PREFIX)
        assert "answered your question" in block and "Ship it?" in block and "choices: yes" in block
        assert "previous answer" not in block

        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 2, "choices": ["no"], "text": "wait"})
        block = mgr.sessions["s1"].delivered[-1]
        assert "changed their answer" in block
        assert "choices: no" in block and "> wait" in block
        assert "previous answer (replaced):\n  choices: yes" in block
        assert mgr.sessions["s2"].delivered == []
    run(tmp_path, scenario)


def test_conflicts_carry_the_current_question(tmp_path):
    async def scenario(req, mgr):
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Q"}))["question"]
        await req("POST", f"/api/qa/{q['id']}", {"session": "s1", "revision": 1, "title": "Q2"})
        err = await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "x"}, status=409)
        assert err["current"]["title"] == "Q2"
        assert mgr.sessions["s1"].delivered == []      # a refused save tells nobody
        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 2, "text": "x"})
        err = await req("POST", f"/api/qa/{q['id']}/close", {"session": "s1", "revision": 2}, status=409)
        assert err["current"]["answer"]["text"] == "x"
    run(tmp_path, scenario)


def test_identity_rules(tmp_path):
    async def scenario(req, mgr):
        await req("POST", "/api/qa", {"session": "ghost", "title": "Q"}, status=404)
        await req("POST", "/api/qa", {"title": "user cannot ask"}, status=403)
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Q"}))["question"]
        await req("POST", f"/api/qa/{q['id']}", {"session": "s2", "revision": 1, "title": "x"}, status=403)
        await req("POST", f"/api/qa/{q['id']}", {"revision": 1, "title": "user edit"}, status=403)
        await req("POST", f"/api/qa/{q['id']}/answer", {"session": "s1", "revision": 1, "text": "x"}, status=403)
        await req("POST", f"/api/qa/{q['id']}/delete", {"session": "s1", "revision": 1})
        await req("GET", "/api/qa", status=400)
    run(tmp_path, scenario)


def test_a_held_session_is_woken_with_the_answer(tmp_path):
    async def scenario(req, mgr):
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Q"}))["question"]
        mgr.sessions["s1"].exited = True
        mgr.sessions["s1"].held_at = "now"
        res = await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "x"})
        assert res["delivered"] is True
        assert mgr.woken and mgr.woken[0][:2] == ("s1", "qa") and qa_api.PREFIX in mgr.woken[0][2]
    run(tmp_path, scenario)


def test_an_exited_session_still_gets_the_answer_filed(tmp_path):
    async def scenario(req, mgr):
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Q"}))["question"]
        mgr.sessions["s1"].exited = True
        res = await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "x"})
        assert res["delivered"] is False
        assert res["question"]["status"] == "answered"
    run(tmp_path, scenario)
