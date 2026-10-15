"""HTTP routes for session threads (daemon/threads_api.py, claunch-doj98):
caller identity, and the [claunch thread <id>] block typed into the owning
session when the user writes."""

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import threads_api
from claude_launcher.threads import store as S


class FakeSession:
    def __init__(self, name):
        self.sdef = SimpleNamespace(name=name)
        self.exited = False
        self.held_at = None
        self.delivered = []
        self.draft = False
        self.typing = False

    def draft_open(self):
        return self.draft

    def keyboard_busy(self):
        return self.draft or self.typing

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


def run(tmp_path, scenario):
    mgr = FakeManager([FakeSession("s1"), FakeSession("s2")])
    app = web.Application()
    app["manager"] = mgr
    threads_api.install(app, S.ThreadStore(tmp_path / "threads.sqlite3"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data
            await scenario(req, mgr)

    asyncio.run(go())


def test_user_opens_and_writes_and_the_session_is_told(tmp_path):
    async def scenario(req, mgr):
        res = await req("POST", "/api/threads", {"to": "s1", "title": "Log format", "text": "why json?"})
        tid = res["thread"]["id"]
        assert res["delivered"] is True
        block = mgr.sessions["s1"].delivered[-1]
        assert block.startswith(f'{threads_api.PREFIX} {tid} "Log format"]')
        assert "opened a new thread" in block and "  > why json?" in block
        assert f'thread_reply(thread_id="{tid}"' in block
        assert "in markdown" in block                      # the reply format is said (claunch-5usj6)

        await req("POST", f"/api/threads/{tid}/messages", {"session": "s1", "text": "because tools"})
        assert len(mgr.sessions["s1"].delivered) == 1       # the agent's own reply is not typed back
        res = await req("POST", f"/api/threads/{tid}/messages", {"text": "ok, and yaml?"})
        assert res["delivered"] is True and "wrote in this thread" in mgr.sessions["s1"].delivered[-1]

        await req("POST", f"/api/threads/{tid}/close", {})
        await req("POST", f"/api/threads/{tid}/messages", {"text": "one more"})
        assert "reopened this thread" in mgr.sessions["s1"].delivered[-1]
        assert mgr.sessions["s2"].delivered == []

        read = await req("GET", f"/api/threads/{tid}")
        assert [m["author"] for m in read["messages"]] == ["user", "agent", "user", "user"]
        assert read["thread"]["agent_unread"] == 2
        read = await req("GET", f"/api/threads/{tid}?reader=s1&after={read['messages'][1]['seq']}")
        assert len(read["messages"]) == 2 and read["thread"]["agent_unread"] == 0

        listed = await req("GET", "/api/threads?session=s1")
        assert listed["open"] == 1 and listed["agent_seq"] > 0
        assert (await req("GET", "/api/threads/rail"))["sessions"]["s1"]["open"] == 1
        await req("POST", f"/api/threads/{tid}/title", {"title": "Log format v2"})
        assert (await req("GET", "/api/threads?session=s1&closed=0"))["threads"][0]["title"] == "Log format v2"
    run(tmp_path, scenario)


def test_an_untitled_empty_thread_is_opened_quietly_and_written_later(tmp_path):
    """claunch-bvbmi: neither a title nor a first message is required; an
    empty thread types nothing into the session until its first message."""
    async def scenario(req, mgr):
        res = await req("POST", "/api/threads", {"to": "s1", "title": ""})
        tid = res["thread"]["id"]
        assert res["thread"]["title"] == "Thread #1" and res["message"] is None
        assert "delivered" not in res and mgr.sessions["s1"].delivered == []
        res = await req("POST", f"/api/threads/{tid}/messages", {"text": "now the question"})
        assert res["delivered"] is True
        assert mgr.sessions["s1"].delivered[-1].startswith(f'{threads_api.PREFIX} {tid} "Thread #1"]')
        assert (await req("POST", "/api/threads", {"to": "s1"}))["thread"]["title"] == "Thread #2"
    run(tmp_path, scenario)


def test_identity_rules(tmp_path):
    async def scenario(req, mgr):
        await req("POST", "/api/threads", {"title": "no target"}, status=400)
        await req("POST", "/api/threads", {"to": "ghost", "title": "t"}, status=404)
        await req("POST", "/api/threads", {"session": "ghost", "title": "t"}, status=404)
        own = (await req("POST", "/api/threads", {"session": "s1", "title": "side topic", "text": "fyi"}))
        assert own["thread"]["opened_by"] == "agent" and "delivered" not in own
        assert mgr.sessions["s1"].delivered == []            # the agent's own words are not typed back
        tid = own["thread"]["id"]
        await req("POST", f"/api/threads/{tid}/messages", {"session": "s2", "text": "x"}, status=403)
        await req("POST", f"/api/threads/{tid}/close", {"session": "s2"}, status=403)
        await req("POST", f"/api/threads/{tid}/messages", {"text": ""}, status=400)
        await req("POST", "/api/threads/th-none/messages", {"text": "x"}, status=404)
        await req("GET", "/api/threads", status=400)
        await req("GET", "/api/threads/th-none", status=404)
    run(tmp_path, scenario)


def test_history_lists_every_sessions_threads(tmp_path):
    async def scenario(req, mgr):
        one = (await req("POST", "/api/threads", {"to": "s1", "title": "one", "text": "hi"}))["thread"]["id"]
        two = (await req("POST", "/api/threads", {"session": "s2", "title": "two", "text": "fyi"}))["thread"]["id"]
        h = await req("GET", "/api/threads/history")
        assert {t["id"] for t in h["threads"]} == {one, two} and h["total"] == 2
        assert {t["session"]: t["messages"][0]["body"] for t in h["threads"]} == {"s1": "hi", "s2": "fyi"}
        assert {s["session"] for s in h["sessions"]} == {"s1", "s2"}
        h = await req("GET", "/api/threads/history?session=s2&per_page=1&page=1")
        assert [t["id"] for t in h["threads"]] == [two] and h["per_page"] == 1
        await req("POST", f"/api/threads/{one}/close", {})
        assert [t["id"] for t in (await req("GET", "/api/threads/history?status=closed"))["threads"]] == [one]
        await req("GET", "/api/threads/history?page=x", status=400)
        await req("GET", "/api/threads/history?status=nope", status=400)
        assert mgr.sessions["s2"].delivered == []      # reading the history types nothing anywhere
    run(tmp_path, scenario)


def test_index_lists_threads_without_messages_grouped_by_session(tmp_path, monkeypatch):
    clock = iter(f"2026-10-15T10:{m:02d}:00+00:00" for m in range(60))
    monkeypatch.setattr(S, "_now", lambda: next(clock))

    async def scenario(req, mgr):
        one = (await req("POST", "/api/threads", {"to": "s1", "title": "one", "text": "hi"}))["thread"]["id"]
        two = (await req("POST", "/api/threads", {"session": "s2", "title": "two", "text": "fyi\nmore"}))["thread"]["id"]
        three = (await req("POST", "/api/threads", {"to": "s1", "title": "three", "text": "x"}))["thread"]["id"]
        await req("POST", f"/api/threads/{one}/messages", {"text": "again"})   # one is now the newest
        ix = await req("GET", "/api/threads/index")
        assert [t["id"] for t in ix["threads"]] == [one, three, two]
        assert ix["total"] == 3 and ix["truncated"] is False
        first = ix["threads"][0]
        assert first["messages"] == 2 and first["excerpt"] == "again" and "last" not in first
        assert ix["threads"][2]["excerpt"] == "fyi"
        assert [(g["session"], g["threads"], g["open"]) for g in ix["sessions"]] == [("s1", 2, 2), ("s2", 1, 1)]
        await req("POST", f"/api/threads/{three}/close", {})
        assert [s["session"] for s in ix["all_sessions"]] == ["s1", "s2"]
        ix = await req("GET", "/api/threads/index?status=open&session=s1")
        assert [t["id"] for t in ix["threads"]] == [one]
        # The filter narrows the threads and their groups, never the session list it picks from.
        assert [g["session"] for g in ix["sessions"]] == ["s1"]
        assert {s["session"]: s["threads"] for s in ix["all_sessions"]} == {"s1": 2, "s2": 1}
        closed = await req("GET", "/api/threads/index?status=closed")
        assert [t["id"] for t in closed["threads"]] == [three]
        assert [(g["session"], g["threads"], g["open"]) for g in closed["sessions"]] == [("s1", 1, 0)]
        assert {s["session"] for s in closed["all_sessions"]} == {"s1", "s2"}
        await req("GET", "/api/threads/index?status=nope", status=400)
        assert mgr.sessions["s2"].delivered == []
    run(tmp_path, scenario)


def test_a_held_session_is_woken_and_an_exited_one_keeps_the_message(tmp_path):
    async def scenario(req, mgr):
        tid = (await req("POST", "/api/threads", {"to": "s1", "title": "t"}))["thread"]["id"]
        s1 = mgr.sessions["s1"]
        s1.exited, s1.held_at = True, "now"
        res = await req("POST", f"/api/threads/{tid}/messages", {"text": "wake up"})
        assert res["delivered"] is True and res["hold"] == "asleep"
        assert mgr.woken[0][:2] == ("s1", "thread") and threads_api.PREFIX in mgr.woken[0][2]
        s1.held_at = None
        res = await req("POST", f"/api/threads/{tid}/messages", {"text": "anyone?"})
        assert res["delivered"] is False and res["hold"] is None
        assert [m["body"] for m in (await req("GET", f"/api/threads/{tid}"))["messages"]] == ["wake up", "anyone?"]
    run(tmp_path, scenario)


def test_a_message_held_behind_the_terminal_says_why(tmp_path):
    """s29, 2026-10-14: a thread question sat ~100 s behind an unsent line in
    that terminal's composer while the pane showed nothing (claunch-a41i5)."""
    async def scenario(req, mgr):
        s1 = mgr.sessions["s1"]
        res = await req("POST", "/api/threads", {"to": "s1", "title": "t", "text": "plain"})
        assert res["delivered"] is True and res["hold"] is None
        tid = res["thread"]["id"]
        s1.draft = True
        res = await req("POST", "/api/threads", {"to": "s1", "title": "t2", "text": "behind a draft"})
        assert res["delivered"] is True and res["hold"] == "draft"
        res = await req("POST", f"/api/threads/{tid}/messages", {"text": "still behind it"})
        assert res["hold"] == "draft"
        s1.draft, s1.typing = False, True
        assert (await req("POST", f"/api/threads/{tid}/messages", {"text": "keys"}))["hold"] == "typing"
        s1.typing = False
        assert (await req("POST", f"/api/threads/{tid}/messages", {"text": "clear"}))["hold"] is None
        assert len(s1.delivered) == 5                       # every one was accepted for delivery
    run(tmp_path, scenario)
