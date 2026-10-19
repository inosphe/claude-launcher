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


# -- images on a user message (claunch-shl2r) --------------------------------

PNG = bytes.fromhex("89504e470d0a1a0a") + b"a pretend png body"


def _stage():
    from claude_launcher.daemon import opening_images
    return opening_images.stage([PNG], "image/png")


def test_images_are_kept_listed_in_the_block_and_served(tmp_path):
    from claude_launcher.daemon import opening_images, thread_images

    async def scenario(req, mgr):
        first, second = _stage(), _stage()
        res = await req("POST", "/api/threads", {"to": "s1", "title": "Bug", "images": [first]})
        tid = res["thread"]["id"]
        img = res["message"]["images"][0]
        assert res["message"]["body"] == "" and img["name"] == first and img["type"] == "image/png"
        kept = thread_images.folder() / first
        assert img["path"] == str(kept) and kept.read_bytes() == PNG
        assert not (opening_images.stage_dir() / first).exists()
        block = mgr.sessions["s1"].delivered[-1]
        assert "  > (images only)" in block and "attached 1 image(s)" in block and f"  - {kept}" in block

        res = await req("POST", f"/api/threads/{tid}/messages", {"text": "and this one", "images": [second]})
        assert "  > and this one" in mgr.sessions["s1"].delivered[-1]
        assert str(thread_images.folder() / second) in mgr.sessions["s1"].delivered[-1]

        # the record keeps them: a read (the agent's thread_read too) carries the paths
        got = await req("GET", f"/api/threads/{tid}?reader=s1")
        assert [[i["name"] for i in m["images"]] for m in got["messages"]] == [[first], [second]]
        assert got["messages"][1]["images"][0]["path"] == str(thread_images.folder() / second)

    run(tmp_path, scenario)


def test_the_agent_attaches_images_to_its_own_message(tmp_path):
    """A session's thread_reply / thread_open carries staged images too
    (claunch-euaz6): they are kept and served like the user's, and nothing
    is typed into the session for its own message."""
    from claude_launcher.daemon import thread_images

    async def scenario(req, mgr):
        first, second = _stage(), _stage()
        res = await req("POST", "/api/threads", {"session": "s1", "title": "Chart", "text": "see",
                                                 "images": [first]})
        tid = res["thread"]["id"]
        assert res["message"]["author"] == "agent"
        assert res["message"]["images"][0]["path"] == str(thread_images.folder() / first)
        res = await req("POST", f"/api/threads/{tid}/messages", {"session": "s1", "images": [second]})
        assert res["message"]["body"] == "" and [i["name"] for i in res["message"]["images"]] == [second]
        assert mgr.sessions["s1"].delivered == []
        got = await req("GET", f"/api/threads/{tid}")
        assert [[i["name"] for i in m["images"]] for m in got["messages"]] == [[first], [second]]
        assert (thread_images.folder() / second).read_bytes() == PNG

    run(tmp_path, scenario)


def test_image_route_serves_only_a_threads_own_images(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mgr = FakeManager([FakeSession("s1")])
    app = web.Application()
    app["manager"] = mgr
    threads_api.install(app, S.ThreadStore(tmp_path / "threads.sqlite3"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            name = _stage()
            res = await (await client.post("/api/threads", json={"to": "s1", "text": "x", "images": [name]})).json()
            other = await (await client.post("/api/threads", json={"to": "s1", "text": "y"})).json()
            tid, oid = res["thread"]["id"], other["thread"]["id"]
            resp = await client.get(f"/api/threads/{tid}/images/{name}")
            assert resp.status == 200 and resp.headers["Content-Type"] == "image/png"
            assert await resp.read() == PNG
            assert (await client.get(f"/api/threads/{oid}/images/{name}")).status == 404
            assert (await client.get(f"/api/threads/{tid}/images/..%2Fthreads.sqlite3")).status == 404

    asyncio.run(go())


def test_bad_images_are_refused_and_leave_nothing_kept(tmp_path):
    from claude_launcher.daemon import opening_images, thread_images

    async def scenario(req, mgr):
        tid = (await req("POST", "/api/threads", {"to": "s1", "text": "hi"}))["thread"]["id"]
        before = len(mgr.sessions["s1"].delivered)
        err = await req("POST", f"/api/threads/{tid}/messages",
                        {"images": ["0123456789abcdef0123.png"]}, status=400)
        assert "not on this daemon" in err["error"]
        name = _stage()
        # refused by the store after the move: the file goes back to staging
        await req("POST", f"/api/threads/{tid}/messages", {"session": "s2", "text": "x", "images": [name]},
                  status=403)
        assert (opening_images.stage_dir() / name).is_file()
        await req("POST", "/api/threads/th-nope00/messages", {"text": "x", "images": [name]}, status=404)
        assert (opening_images.stage_dir() / name).is_file()
        assert not (thread_images.folder() / name).exists()
        assert len(mgr.sessions["s1"].delivered) == before

    run(tmp_path, scenario)


def test_a_live_session_that_takes_images_gets_them_pasted_first(tmp_path, monkeypatch):
    from claude_launcher import harnesses
    from claude_launcher.daemon import api as api_mod

    class Registry(dict):
        def get(self, name, default=None):
            return SimpleNamespace(takes_images=name == "claude")

    monkeypatch.setattr(harnesses, "registry", lambda: Registry())
    steps = []

    def fake_step(session, images, text, *, what="opening images"):
        async def step():
            pass
        steps.append(([str(i.path) for i in images], [i.media_type for i in images], what))
        return step, lambda: text + "\n(pasted)"

    monkeypatch.setattr(api_mod, "_opening_image_step", fake_step)
    befores = []

    class PasteSession(FakeSession):
        def queue_delivery(self, text, before=None):
            befores.append(before)
            return super().queue_delivery(text() if callable(text) else text)

    async def scenario(req, mgr):
        mgr.sessions["s1"] = live = PasteSession("s1")
        live.sdef.harness = "claude"
        mgr.sessions["s2"].sdef.harness = "other"
        name = _stage()
        res = await req("POST", "/api/threads", {"to": "s1", "text": "see", "images": [name]})
        assert steps == [([res["message"]["images"][0]["path"]], ["image/png"], "thread images")]
        assert callable(befores[-1]) and live.delivered[-1].endswith("(pasted)")
        await req("POST", "/api/threads", {"to": "s1", "text": "no picture"})
        assert befores[-1] is None and len(steps) == 1          # a text-only message pastes nothing
        await req("POST", "/api/threads", {"to": "s2", "text": "see", "images": [_stage()]})
        assert len(steps) == 1                                  # a harness without images: paths only

    run(tmp_path, scenario)
