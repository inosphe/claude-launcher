"""Links between session threads and QA questions (claunch-78dpm,
daemon/talk_links.py): the fixed link each side records at creation, the
checks the routes make before writing one, the ``links`` every row carries,
and what the delivered blocks say about them."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import qa_api, talk_links, threads_api
from claude_launcher.qa import store as Q
from claude_launcher.threads import store as T


class FakeSession:
    def __init__(self, name):
        self.sdef = SimpleNamespace(name=name)
        self.held_at = None
        self.delivered = []

    def draft_open(self):
        return False

    def keyboard_busy(self):
        return False

    def queue_delivery(self, text, on_done=None):
        # The QA outbox hands over a callable and wants to hear back.
        text = text() if callable(text) else text
        if text:
            self.delivered.append(text)
        if on_done:
            on_done(bool(text))
        return True


class FakeManager:
    def __init__(self, names):
        self.sessions = {n: FakeSession(n) for n in names}

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]

    def wake_soon(self, name, *, by="", deliver=""):
        return False


def run(tmp_path, scenario):
    mgr = FakeManager(["s1", "s2"])
    app = web.Application()
    app["manager"] = mgr
    qa_api.install(app, Q.QaStore(tmp_path / "qa.sqlite3"))
    threads_api.install(app, T.ThreadStore(tmp_path / "threads.sqlite3"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                resp = await client.request(method, path, json=body)
                data = await resp.json()
                assert resp.status == status, data
                return data
            await scenario(req, mgr)

    asyncio.run(go())


# -- stores --------------------------------------------------------------


def test_the_stores_keep_the_link_set_at_creation(tmp_path):
    threads = T.ThreadStore(tmp_path / "t.sqlite3")
    qa = Q.QaStore(tmp_path / "q.sqlite3")
    q = qa.ask("s1", title="Which base?")
    assert q["thread"] is None
    t = threads.open(T.USER, "s1", "Q: Which base?", qa=q["id"])["thread"]
    assert t["qa"] == q["id"]
    filed = qa.ask("s1", title="Port?", thread=t["id"])
    assert filed["thread"] == t["id"]
    # An edit moves neither link.
    edited = qa.update("s1", filed["id"], filed["revision"], title="Which port?")
    assert edited["thread"] == t["id"]
    assert threads.rename(T.USER, t["id"], "renamed")["thread"]["qa"] == q["id"]
    assert threads.link_rows("s1") == [{"id": t["id"], "title": "renamed", "status": "open", "qa": q["id"]}]
    assert {r["id"]: r["thread"] for r in qa.link_rows("s1")} == {q["id"]: None, filed["id"]: t["id"]}


def test_a_link_must_look_like_an_id(tmp_path):
    threads = T.ThreadStore(tmp_path / "t.sqlite3")
    qa = Q.QaStore(tmp_path / "q.sqlite3")
    with pytest.raises(T.ThreadError):
        threads.open(T.USER, "s1", "x", qa="th-123abc")
    with pytest.raises(Q.QaError):
        qa.ask("s1", title="x", thread="qa-12345678")


def test_older_files_gain_the_link_columns(tmp_path):
    tpath = tmp_path / "t.sqlite3"
    db = sqlite3.connect(str(tpath))
    db.executescript(T._SCHEMA.replace("    qa TEXT,\n", ""))
    db.execute("INSERT INTO threads (id, session, title, opened_by, created_at, updated_at) "
               "VALUES ('th-000001', 's1', 'old', 'user', 'x', 'x')")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    threads = T.ThreadStore(tpath)
    assert threads.get("th-000001")["qa"] is None
    assert threads.open(T.USER, "s1", "new", qa="qa-00000001")["thread"]["qa"] == "qa-00000001"

    qpath = tmp_path / "q.sqlite3"
    db = sqlite3.connect(str(qpath))
    db.executescript(Q._SCHEMA.replace("    thread TEXT,\n", ""))
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()
    qa = Q.QaStore(qpath)
    assert qa.ask("s1", title="x", thread="th-000001")["thread"] == "th-000001"
    with sqlite3.connect(str(qpath)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == Q.SCHEMA_VERSION


# -- the join ------------------------------------------------------------


def test_links_name_their_relation_from_both_ends():
    threads = [{"id": "th-1", "title": "about", "status": "open", "qa": "qa-1"},
               {"id": "th-2", "title": "talk", "status": "closed", "qa": None}]
    questions = [{"id": "qa-1", "title": "Which?", "status": "open", "thread": None},
                 {"id": "qa-2", "title": "Port?", "status": "answered", "thread": "th-2"}]
    assert talk_links.thread_links(threads[0], questions) == [
        {"id": "qa-1", "title": "Which?", "status": "open", "rel": "subject"}]
    assert talk_links.thread_links(threads[1], questions) == [
        {"id": "qa-2", "title": "Port?", "status": "answered", "rel": "filed"}]
    assert talk_links.question_links(questions[0], threads) == [
        {"id": "th-1", "title": "about", "status": "open", "rel": "discussion"}]
    assert talk_links.question_links(questions[1], threads) == [
        {"id": "th-2", "title": "talk", "status": "closed", "rel": "source"}]
    # The other end gone: kept, marked deleted.
    assert talk_links.thread_links(threads[0], []) == [
        {"id": "qa-1", "title": None, "status": "deleted", "rel": "subject"}]


def test_without_the_other_store_rows_get_no_links():
    rows = [{"id": "th-1", "qa": "qa-1"}]
    talk_links.attach_to_threads(rows, None, "s1")
    assert rows[0]["links"] == []


# -- the routes ----------------------------------------------------------


def test_a_thread_opened_about_a_question_links_both_ways(tmp_path):
    async def scenario(req, mgr):
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Which base?"}))["question"]
        assert q["links"] == []
        res = await req("POST", "/api/threads",
                        {"to": "s1", "title": "Q: Which base?", "text": "why not master?", "qa": q["id"]})
        t = res["thread"]
        assert t["qa"] == q["id"]
        assert t["links"] == [{"id": q["id"], "title": "Which base?", "status": "open", "rel": "subject"}]
        block = mgr.sessions["s1"].delivered[-1]
        assert f'This thread is about your QA question {q["id"]} (open): "Which base?"' in block
        # Every later message in it repeats the subject.
        await req("POST", f"/api/threads/{t['id']}/messages", {"text": "and stack?"})
        assert f"QA question {q['id']}" in mgr.sessions["s1"].delivered[-1]

        listed = (await req("GET", "/api/qa?session=s1"))["questions"][0]
        assert listed["links"] == [{"id": t["id"], "title": "Q: Which base?", "status": "open",
                                    "rel": "discussion"}]
        read = await req("GET", f"/api/threads/{t['id']}")
        assert read["thread"]["links"][0]["rel"] == "subject"
        # The answer's block names the thread.
        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": q["revision"], "text": "master"})
        assert f'Threads on this question: {t["id"]} "Q: Which base?"' in mgr.sessions["s1"].delivered[-1]
    run(tmp_path, scenario)


def test_a_question_filed_from_a_thread_links_back(tmp_path):
    async def scenario(req, mgr):
        t = (await req("POST", "/api/threads", {"session": "s1", "title": "Ports"}))["thread"]
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "Which port?", "thread": t["id"]}))["question"]
        assert q["thread"] == t["id"]
        assert q["links"] == [{"id": t["id"], "title": "Ports", "status": "open", "rel": "source"}]
        listed = (await req("GET", "/api/threads?session=s1"))["threads"][0]
        assert listed["links"] == [{"id": q["id"], "title": "Which port?", "status": "open", "rel": "filed"}]
        await req("POST", f"/api/qa/{q['id']}/answer", {"revision": q["revision"], "text": "8080"})
        assert f'{t["id"]} "Ports" (filed from)' in mgr.sessions["s1"].delivered[-1]
    run(tmp_path, scenario)


def test_links_stay_inside_one_session(tmp_path):
    async def scenario(req, mgr):
        q = (await req("POST", "/api/qa", {"session": "s2", "title": "theirs"}))["question"]
        t = (await req("POST", "/api/threads", {"session": "s2", "title": "theirs"}))["thread"]
        await req("POST", "/api/threads", {"to": "s1", "title": "x", "qa": q["id"]}, status=400)
        await req("POST", "/api/threads", {"session": "s1", "title": "x", "qa": q["id"]}, status=400)
        await req("POST", "/api/threads", {"to": "s1", "title": "x", "qa": "qa-0000beef"}, status=404)
        await req("POST", "/api/qa", {"session": "s1", "title": "x", "thread": t["id"]}, status=400)
        await req("POST", "/api/qa", {"session": "s1", "title": "x", "thread": "th-00beef"}, status=404)
        assert (await req("GET", "/api/threads?session=s1"))["threads"] == []
    run(tmp_path, scenario)


def test_a_deleted_question_leaves_a_marked_link(tmp_path):
    async def scenario(req, mgr):
        q = (await req("POST", "/api/qa", {"session": "s1", "title": "gone soon"}))["question"]
        t = (await req("POST", "/api/threads", {"to": "s1", "title": "about it", "qa": q["id"]}))["thread"]
        await req("POST", f"/api/qa/{q['id']}/delete", {"session": "s1", "revision": q["revision"]})
        listed = (await req("GET", "/api/threads?session=s1"))["threads"][0]
        assert listed["links"] == [{"id": q["id"], "title": None, "status": "deleted", "rel": "subject"}]
        await req("POST", f"/api/threads/{t['id']}/messages", {"text": "still here"})
        assert "(since deleted)" in mgr.sessions["s1"].delivered[-1]
    run(tmp_path, scenario)


def test_asking_for_a_qa_question_from_a_thread(tmp_path):
    async def scenario(req, mgr):
        t = (await req("POST", "/api/threads", {"to": "s1", "title": "Ports", "text": "hi"}))["thread"]
        await req("POST", f"/api/threads/{t['id']}/messages", {"text": "please file it", "ask_qa": True})
        block = mgr.sessions["s1"].delivered[-1]
        assert f'qa_ask(..., thread="{t["id"]}")' in block
        assert "  > please file it" in block
        # A plain message carries no such request.
        await req("POST", f"/api/threads/{t['id']}/messages", {"text": "plain"})
        assert "qa_ask" not in mgr.sessions["s1"].delivered[-1]
    run(tmp_path, scenario)
