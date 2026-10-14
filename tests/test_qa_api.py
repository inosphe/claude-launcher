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
    """Types at once unless ``waiting``: then the queue keeps (factory,
    on_done) pairs the way a session holds them behind a composer draft,
    and ``release`` plays them in order."""

    def __init__(self, name, held=False):
        self.sdef = SimpleNamespace(name=name)
        self.exited = held
        self.held_at = "now" if held else None
        self.delivered = []
        self.waiting = False
        self.queue = []
        self.draft = False
        self.forced = []

    def queue_delivery(self, text, on_done=None):
        if self.exited:
            return False
        if self.waiting:
            self.queue.append((text, on_done))
            return True
        text = text() if callable(text) else text
        if text:
            self.delivered.append(text)
        if on_done:
            on_done(bool(text))
        return True

    def release(self):
        self.waiting = False
        for text, on_done in self.queue:
            text = text()
            if text:
                self.delivered.append(text)
            if on_done:
                on_done(bool(text))
        self.queue = []

    def draft_open(self):
        return self.draft

    async def deliver(self, text, *, force=False):
        # The queued copy reaches the terminal first, the way it can while
        # a forced send waits out the typing grace -- and types nothing.
        self.release()
        self.forced.append(force)
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.woken = []
        self.restoring = set()

    def message_restore_pending(self, name):
        return name in self.restoring

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


def test_nudge_is_typed_into_the_session_and_answered_by_qa_nothing(tmp_path):
    async def scenario(req, mgr):
        res = await req("POST", "/api/qa/nudge", {"to": "s1"})
        nid = res["nudge"]["id"]
        assert res["delivered"] is True and res["nudge"]["status"] == "sent"
        block = mgr.sessions["s1"].delivered[-1]
        assert block.startswith(qa_api.PREFIX) and nid in block
        assert "qa_ask" in block and f'qa_nothing (nudge "{nid}")' in block
        assert (await req("GET", "/api/qa?session=s1"))["nudge"]["id"] == nid

        await req("POST", f"/api/qa/nudge/{nid}/nothing", {"session": "s2"}, status=403)
        done = await req("POST", "/api/qa/nudge/latest/nothing", {"session": "s1", "note": "none"})
        assert (done["nudge"]["status"], done["nudge"]["reply"]) == ("nothing", "none")
        err = await req("POST", f"/api/qa/nudge/{nid}/nothing", {"session": "s1"}, status=409)
        assert err["current"]["status"] == "nothing"
        await req("POST", "/api/qa/nudge/latest/nothing", {"session": "s1"}, status=404)
    run(tmp_path, scenario)


def test_nudge_identity_rules(tmp_path):
    async def scenario(req, mgr):
        await req("POST", "/api/qa/nudge", {}, status=400)
        await req("POST", "/api/qa/nudge", {"to": "ghost"}, status=404)
        await req("POST", "/api/qa/nudge", {"to": "s1", "session": "s2"}, status=403)
        mgr.sessions["s1"].exited = True
        res = await req("POST", "/api/qa/nudge", {"to": "s1"})
        assert res["delivered"] is False and res["nudge"]["status"] == "sent"
    run(tmp_path, scenario)


def test_a_waiting_answer_is_reported_as_waiting_and_only_the_latest_is_typed(tmp_path):
    # qa-d12e3c41: five saves while a draft sat in the composer were typed
    # together, and the pane had said "delivered" after each.
    async def scenario(req, mgr):
        s1 = mgr.sessions["s1"]
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Ship it?"}))["question"]
        s1.waiting, s1.draft = True, True
        res = await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "yes"})
        assert res["delivery"]["state"] == "pending" and res["delivery"]["draft"] is True
        assert res["delivered"] is True       # accepted, which is all this flag ever said
        listed = (await req("GET", "/api/qa?session=s1"))["questions"][0]
        assert listed["delivery"]["state"] == "pending" and listed["delivery"]["kind"] == "answer"

        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 2, "text": "yes, today"})
        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 3, "text": "yes, now"})
        s1.release()
        assert len(s1.delivered) == 1, "the replaced notifications type nothing"
        assert "> yes, now" in s1.delivered[0]
        listed = (await req("GET", "/api/qa?session=s1"))["questions"][0]
        assert listed["delivery"]["state"] == "delivered" and listed["delivery"]["settled_at"]
    run(tmp_path, scenario)


def test_deliver_now_forces_the_waiting_notification_once(tmp_path):
    async def scenario(req, mgr):
        s1 = mgr.sessions["s1"]
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Ship it?"}))["question"]
        await req("POST", f"/api/qa/{q['id']}/deliver", {}, status=409)   # nothing waits
        s1.waiting = True
        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "yes"})
        res = await req("POST", f"/api/qa/{q['id']}/deliver", {})
        assert res["delivery"]["state"] == "delivered" and res["delivery"]["forced"] is True
        assert s1.forced == [True] and "> yes" in s1.delivered[0]
        assert len(s1.delivered) == 1, "the queued copy no longer types"
        assert (await req("GET", "/api/qa?session=s1"))["questions"][0]["delivery"]["state"] == "delivered"
        await req("POST", f"/api/qa/{q['id']}/deliver", {}, status=409)
    run(tmp_path, scenario)


def test_a_closed_question_is_reanswered_and_rechecked(tmp_path):
    async def scenario(req, mgr):
        s1 = mgr.sessions["s1"]
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Which base?"}))["question"]
        await req("POST", f"/api/qa/{q['id']}/recheck", {"revision": 1}, status=400)   # no answer yet
        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "master"})
        await req("POST", f"/api/qa/{q['id']}/close", {"session": "s1", "revision": 2})

        res = await req("POST", f"/api/qa/{q['id']}/recheck", {"revision": 3, "note": "you rebased onto stack"})
        assert res["reopened"] is True and res["question"]["status"] == "answered"
        assert res["delivery"]["state"] == "delivered" and res["delivery"]["kind"] == "recheck"
        block = s1.delivered[-1]
        assert block.startswith(qa_api.PREFIX) and "direction check" in block
        assert "> master" in block and "> you rebased onto stack" in block and "reopens it" in block
        assert f"qa_close it (revision {res['question']['revision']})" in block
        await req("POST", f"/api/qa/{q['id']}/recheck", {"session": "s1", "revision": 4}, status=403)

        await req("POST", f"/api/qa/{q['id']}/close", {"session": "s1", "revision": 4, "note": "back on master"})
        listed = (await req("GET", "/api/qa?session=s1"))["questions"][0]
        assert listed["check"]["settled"] is True and listed["check"]["reply"] == "back on master"

        res = await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 5, "text": "stack after all"})
        assert res["reopened"] is True and res["question"]["status"] == "answered"
        block = s1.delivered[-1]
        assert "You had closed this question" in block and "> stack after all" in block
    run(tmp_path, scenario)


def test_an_answer_saved_while_the_session_is_being_woken_is_held_not_pending(tmp_path):
    # Review of 90b6c293: between the wake's stage() and start_async the
    # registry holds a Session with no held_at whose messages still go to
    # the wake queue -- which never reports back. "pending" there would
    # stay forever and "지금 전달" would type the answer a second time.
    async def scenario(req, mgr):
        s1 = mgr.sessions["s1"]
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Ship it?"}))["question"]
        mgr.restoring.add("s1")
        wake_queue = []

        def into_wake_queue(text, on_done=None):
            wake_queue.append(text() if callable(text) else text)
            return True
        s1.queue_delivery = into_wake_queue
        res = await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "yes"})
        assert res["delivery"]["state"] == "held"
        assert len(wake_queue) == 1 and "> yes" in wake_queue[0]
        await req("POST", f"/api/qa/{q['id']}/deliver", {}, status=409)
        assert s1.delivered == []
    run(tmp_path, scenario)


def test_deliver_now_leaves_a_notification_that_is_already_being_typed(tmp_path):
    async def scenario(req, mgr):
        s1 = mgr.sessions["s1"]
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Ship it?"}))["question"]
        s1.waiting = True
        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": 1, "text": "yes"})
        (factory, on_done), = s1.queue
        typed = factory()                      # the queued copy reaches the paste
        assert "> yes" in typed
        listed = (await req("GET", "/api/qa?session=s1"))["questions"][0]
        assert listed["delivery"]["state"] == "typing"
        await req("POST", f"/api/qa/{q['id']}/deliver", {}, status=409)
        assert s1.forced == [], "no second copy"
        on_done(True)
        assert (await req("GET", "/api/qa?session=s1"))["questions"][0]["delivery"]["state"] == "delivered"
    run(tmp_path, scenario)
