"""HTTP routes for mega threads (daemon/megathread_api.py, claunch-v5nhp).

T1 creating a cluster joins each session by its most recent open thread, and
   opens one for a session that has none
T2 a thread belonging to another session is refused; an unknown session is 404
T3 a routing whose single candidate clears the threshold is delivered to that
   member only -- the other member is not written to at all
T4 two members over the threshold come back as a choice and nothing is
   delivered; the user's picks then carry the message to exactly those
T5 the budget refuses a routing before any call, with 429 and the numbers
T6 the settings endpoint reports the policy and the rolling usage, and takes
   a partial update
"""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import jev, megathread_api, megathread_route, threads_api
from claude_launcher.megathread import store as M
from claude_launcher.threads import store as T


class FakeSession:
    def __init__(self, name):
        self.sdef = SimpleNamespace(
            name=name, cwd="/tmp/" + name, task=f"{name} task", issue="", model="m",
            project="p", kind="worker", role="worker", conversation_id=None,
            harness="claude", profile="",
        )
        self.exited = False
        self.held_at = None
        self.delivered = []
        self.draft = False
        self.typing = False

    def info(self):
        return {"status": "idle"}

    def draft_open(self):
        return self.draft

    def keyboard_busy(self):
        return self.draft or self.typing

    def queue_delivery(self, text, **_kwargs):
        if self.exited:
            return False
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

    def wake_soon(self, name, *, by="", deliver=""):
        return False


CFG = {
    "jev_model_megathread": "jev-latest",
    "megathread_threshold_ratio": 9.0,
    "megathread_jev_budget_tokens": 10_000_000,
    "megathread_max_chars": 12000,
    "megathread_concurrency": 4,
}


def jev_answer(p_mine: float, tokens: int = 100):
    return {
        "model": "jev-latest",
        "usage": {"input_tokens": tokens},
        "answers": {
            megathread_route.QUESTION_ID: {
                "type": "choice",
                "choice": "mine" if p_mine >= 0.5 else "not_mine",
                "confidence": max(p_mine, 1 - p_mine),
                "probabilities": {"mine": p_mine, "not_mine": 1 - p_mine},
            }
        },
    }


def run(tmp_path, scenario, names=("s1", "s2"), cfg=None):
    manager = FakeManager([FakeSession(n) for n in names])
    app = web.Application()
    app["manager"] = manager
    threads = threads_api.install(app, T.ThreadStore(tmp_path / "threads.sqlite3"))
    megas = megathread_api.install(app, M.MegaThreadStore(tmp_path / "megathread.sqlite3"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data

            await scenario(req, manager, threads, megas)

    original = megathread_api._cfg
    megathread_api._cfg = lambda: dict(cfg or CFG)
    try:
        asyncio.run(go())
    finally:
        megathread_api._cfg = original


def test_t1_create_joins_open_threads_and_opens_one_when_missing(tmp_path):
    async def scenario(req, manager, threads, megas):
        # s1 has a thread already; s2 has none and must get one.
        tid = (await req("POST", "/api/threads", {"to": "s1", "title": "log format"}))["thread"]["id"]
        res = await req("POST", "/api/megathreads",
                        {"title": "frontend", "members": [{"session": "s1"}, {"session": "s2"}]})
        mega = res["mega"]
        assert len(mega["members"]) == 2
        by_session = {m["session"]: m for m in mega["members"]}
        assert by_session["s1"]["thread"] == tid
        opened = threads.get(by_session["s2"]["thread"])
        assert opened["session"] == "s2" and opened["title"] == "s2"
        assert by_session["s1"]["thread_title"] == "log format"
        assert all(m["status"] == "idle" for m in mega["members"])

    run(tmp_path, scenario)


def test_t2_foreign_thread_and_unknown_session(tmp_path):
    async def scenario(req, manager, threads, megas):
        tid = (await req("POST", "/api/threads", {"to": "s1", "title": "x"}))["thread"]["id"]
        mega = (await req("POST", "/api/megathreads", {"title": "m"}))["mega"]
        await req("POST", f"/api/megathreads/{mega['id']}/members",
                  {"session": "s2", "thread": tid}, status=409)
        await req("POST", f"/api/megathreads/{mega['id']}/members",
                  {"session": "s9"}, status=404)

    run(tmp_path, scenario)


def test_t3_auto_routes_to_one_member_only(tmp_path, monkeypatch):
    async def fake_system_one(cfg, state, questions, *, key=None, model=None):
        title = state["thread"]["title"]
        return jev_answer(0.97 if title == "log format" else 0.02)

    monkeypatch.setattr(jev, "system_one", fake_system_one)

    async def scenario(req, manager, threads, megas):
        log = (await req("POST", "/api/threads", {"to": "s1", "title": "log format"}))["thread"]
        dep = (await req("POST", "/api/threads", {"to": "s2", "title": "deploy"}))["thread"]
        mega = (await req("POST", "/api/megathreads",
                          {"title": "m", "members": [{"session": "s1"}, {"session": "s2"}]}))["mega"]
        res = await req("POST", f"/api/megathreads/{mega['id']}/route",
                        {"text": "can you look at the log line?"})
        assert res["verdict"] == "auto"
        assert [d["thread"] for d in res["delivered"]] == [log["id"]]
        assert len(manager.sessions["s1"].delivered) == 1
        assert manager.sessions["s2"].delivered == []  # not written to at all
        assert log["id"] in manager.sessions["s1"].delivered[0]
        rows = megas.routes(mega["id"])["routes"]
        assert rows[0]["verdict"] == "auto" and rows[0]["tokens"] == 200
        assert rows[0]["chosen"] == [log["id"]]

    run(tmp_path, scenario)


def test_t4_choose_then_deliver_to_the_picks(tmp_path, monkeypatch):
    async def fake_system_one(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.9)

    monkeypatch.setattr(jev, "system_one", fake_system_one)

    async def scenario(req, manager, threads, megas):
        a = (await req("POST", "/api/threads", {"to": "s1", "title": "a"}))["thread"]
        b = (await req("POST", "/api/threads", {"to": "s2", "title": "b"}))["thread"]
        mega = (await req("POST", "/api/megathreads",
                          {"title": "m", "members": [{"session": "s1"}, {"session": "s2"}]}))["mega"]
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "both of you"})
        assert res["verdict"] == "choose" and len(res["options"]) == 2
        assert res["delivered"] == []
        assert manager.sessions["s1"].delivered == []
        assert manager.sessions["s2"].delivered == []

        picked = await req("POST", f"/api/megathreads/{mega['id']}/route",
                           {"text": "both of you", "choose": [a["id"], b["id"]]})
        assert picked["verdict"] == "manual"
        assert len(manager.sessions["s1"].delivered) == 1
        assert len(manager.sessions["s2"].delivered) == 1
        assert megas.routes(mega["id"])["routes"][0]["verdict"] == "manual"

        # A thread that is not a member is refused.
        await req("POST", f"/api/megathreads/{mega['id']}/route",
                  {"text": "x", "choose": ["th-nope"]}, status=400)

    run(tmp_path, scenario)


def test_t5_budget_refuses_before_asking(tmp_path, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise AssertionError("Jev must not be called when the budget is spent")

    monkeypatch.setattr(jev, "system_one", boom)

    async def scenario(req, manager, threads, megas):
        await req("POST", "/api/threads", {"to": "s1", "title": "a"})
        await req("POST", "/api/threads", {"to": "s2", "title": "b"})
        mega = (await req("POST", "/api/megathreads",
                          {"title": "m", "members": [{"session": "s1"}, {"session": "s2"}]}))["mega"]
        megas.record_route(mega["id"], "yesterday's routing", verdict="auto",
                           threshold=2.197, tokens=10_000_000)
        res = await req("POST", f"/api/megathreads/{mega['id']}/route",
                        {"text": "anyone?"}, status=429)
        assert "budget" in res["error"]
        assert res["used_tokens"] == 10_000_000 and res["estimated_tokens"] > 0
        assert manager.sessions["s1"].delivered == []

    run(tmp_path, scenario, cfg={**CFG, "megathread_jev_budget_tokens": 5_000_000})


def test_t6_settings_report_and_partial_update(tmp_path, monkeypatch):
    written = {}

    def fake_set(key, value):
        written[key] = value

    monkeypatch.setattr(megathread_api.store_mod, "set_daemon_field", fake_set)

    async def scenario(req, manager, threads, megas):
        res = await req("GET", "/api/megathreads/settings")
        settings = res["settings"]
        assert settings["threshold"] == pytest.approx(2.1972245773362196)
        assert settings["budget_tokens"] == 10_000_000
        assert settings["budget_remaining_tokens"] == 10_000_000
        assert settings["model"] == "jev-latest"
        assert "key_set" in settings

        await req("PUT", "/api/megathreads/settings", {"budget_tokens": 1234})
        assert written == {"megathread_jev_budget_tokens": 1234}
        await req("PUT", "/api/megathreads/settings", {"threshold_ratio": 0.5}, status=400)

    run(tmp_path, scenario)
