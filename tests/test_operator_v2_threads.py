"""Operator v2's conversation as threads (claunch-iw5i8.12.1).

What has to hold:
  - the threads store moves an older messages table to v4 (kind, ref,
    data, a system author) with every row and seq kept, and a session's
    threads read as they did;
  - an older daemon's single feed is moved into one thread 「이전 대화」 the
    first time the record is read, with ``<id>.feed-backup.json`` holding
    the record as it was; doing it again adds nothing; entry ids, answers
    and the read position survive the move;
  - every entry belongs to a thread: a post, ask or dispatch goes to the
    thread it names, else to the thread of what it answers, else to the most
    recently active one; another operator's thread is not found;
  - the user's message carries images, and the inbox hands their paths over;
  - the operator opens threads of its own, and the user opens them in the
    view;
  - operator threads stay out of the session-thread surfaces (rail, history,
    index) and their generic write routes.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import opening_images, operators_v2, threads_api
from claude_launcher.threads import store as S

from test_operator_v2 import FakeManager, FakeObserver, FakeSession, PROJECTS

PNG = bytes.fromhex("89504e470d0a1a0a") + b"a pretend png body"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def require(name):
        if name not in PROJECTS:
            raise operators_v2.projects.ProjectError(f"unknown project {name!r}")
        return SimpleNamespace(name=name)

    monkeypatch.setattr(operators_v2.projects, "require", require)
    monkeypatch.setattr(operators_v2.projects, "names", lambda doc=None: list(PROJECTS))
    monkeypatch.setattr(operators_v2.projects, "default_cwd", lambda name, doc=None: None)
    sessions = {n: FakeSession(n, p) for n, p in [("opa", "alpha"), ("opb", "alpha"), ("a1", "alpha")]}
    manager = FakeManager(sessions)
    ops = operators_v2.OperatorsV2(tmp_path, manager, FakeObserver())
    alpha = ops.create({"name": "Alpha", "projects": ["alpha"], "profile": "p"})
    other = ops.create({"name": "Other", "projects": ["alpha"], "profile": "p"})
    ops.bind(alpha, "opa")
    ops.bind(other, "opb")
    return SimpleNamespace(ops=ops, sessions=sessions, manager=manager, alpha=alpha, other=other, root=tmp_path)


# -- the store ----------------------------------------------------------------
def test_a_v3_database_is_rebuilt_to_v4_with_its_rows(tmp_path):
    path = tmp_path / "threads.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript("""
    CREATE TABLE threads (id TEXT PRIMARY KEY, session TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', opened_by TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
        agent_read INTEGER NOT NULL DEFAULT 0, qa TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL REFERENCES threads (id),
        session TEXT NOT NULL, author TEXT NOT NULL CHECK (author IN ('user', 'agent')),
        body TEXT NOT NULL, at TEXT NOT NULL, images TEXT,
        CHECK (length(body) > 0 OR images IS NOT NULL));
    INSERT INTO threads VALUES ('th-aaaaaa', 's1', 'Old', 'open', 'user', 9, 4, NULL, 't', 't');
    INSERT INTO messages VALUES (4, 'th-aaaaaa', 's1', 'user', 'hello', 't', NULL);
    INSERT INTO messages VALUES (9, 'th-aaaaaa', 's1', 'agent', '',  't',
        '[{"name": "0123456789abcdef0123.png", "type": "image/png", "bytes": 3}]');
    PRAGMA user_version = 3;
    """)
    db.close()
    st = S.ThreadStore(path)
    got = st.read("th-aaaaaa")["messages"]
    assert [(m["seq"], m["author"], m["body"], m["kind"], len(m["images"])) for m in got] == [
        (4, "user", "hello", "message", 0), (9, "agent", "", "message", 1)]
    assert "ref" not in got[0] and "data" not in got[0]
    assert st.list("s1")["threads"][0]["agent_read"] == 4
    assert st.post("s1", "th-aaaaaa", "next")["message"]["seq"] == 10
    S.ThreadStore(path)  # a second open is a no-op
    assert len(st.read("th-aaaaaa")["messages"]) == 3
    with sqlite3.connect(str(path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == S.SCHEMA_VERSION == 4


def test_a_ref_is_unique_per_owner(tmp_path):
    st = S.ThreadStore(tmp_path / "t.sqlite3")
    owner = S.operator_owner("abc123abc123")
    tid = st.open(owner, owner, "x")["thread"]["id"]
    st.post(owner, tid, "one", ref="r1", data={"level": "info"})
    with pytest.raises(S.Conflict):
        st.post(owner, tid, "two", ref="r1")
    other = st.open("s1", "s1", "y")["thread"]["id"]
    st.post("s1", other, "same ref, other owner", ref="r1")
    assert st.by_ref(owner, "r1")["data"] == {"level": "info"}


# -- moving an older feed -----------------------------------------------------
OLD_FEED = [
    {"id": "u1", "at": "2026-10-10T00:00:01+00:00", "kind": "user", "role": "user", "text": "a1 상태?"},
    {"id": "p1", "at": "2026-10-10T00:00:02+00:00", "kind": "post", "role": "bot", "text": "a1 작업 중",
     "level": "info", "refs": ["a1"], "request_id": "r-p1"},
    {"id": "k1", "at": "2026-10-10T00:00:03+00:00", "kind": "ask", "role": "bot", "type": "approve",
     "text": "배포?", "choices": [], "level": "attention", "refs": [], "request_id": "r-k1",
     "answer": {"decision": "approve", "text": None, "at": "2026-10-10T00:00:04+00:00"}},
    {"id": "k2", "at": "2026-10-10T00:00:05+00:00", "kind": "ask", "role": "bot", "type": "text",
     "text": "이름?", "choices": [], "level": "attention", "refs": [], "request_id": "r-k2", "answer": None},
    {"id": "d1", "at": "2026-10-10T00:00:06+00:00", "kind": "dispatch", "role": "bot", "target": "a1",
     "text": "멈추십시오", "on_behalf_of": "u1", "delivery": "sent"},
    {"id": "s1", "at": "2026-10-10T00:00:07+00:00", "kind": "system", "role": "system", "text": "설정 변경"},
    {"id": "u2", "at": "2026-10-10T00:00:08+00:00", "kind": "user", "role": "user", "text": "아직 안 읽음"},
]


def _old_record(root, op_id="0123456789ab", session="opa"):
    folder = root / "operators"
    folder.mkdir(parents=True, exist_ok=True)
    rec = {"id": op_id, "name": "Legacy", "all": False, "projects": ["alpha"], "watch": True,
           "session": session, "feed": OLD_FEED, "read": ["u1", "k1:answer"], "badge_seen": [],
           "progress": {}, "watched": [], "created_at": "2026-10-10T00:00:00+00:00"}
    (folder / f"{op_id}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec


def test_an_older_feed_moves_into_one_thread_with_a_backup(world):
    original = _old_record(world.root)
    ops = operators_v2.OperatorsV2(world.root, world.manager, FakeObserver())
    rec = ops.get("0123456789ab")
    backup = world.root / "operators" / "0123456789ab.feed-backup.json"
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    saved = json.loads((world.root / "operators" / "0123456789ab.json").read_text(encoding="utf-8"))
    assert "feed" not in saved and "read" not in saved and saved["legacy_thread"]
    threads = ops.thread_rows(rec)
    assert [(t["title"], t["messages"], t["pending"]) for t in threads] == [(operators_v2.LEGACY_TITLE, 7, 1)]
    feed = ops.feed(rec)
    assert [(e["id"], e["kind"], e["role"]) for e in feed] == [
        ("u1", "user", "user"), ("p1", "post", "bot"), ("k1", "ask", "bot"), ("k2", "ask", "bot"),
        ("d1", "dispatch", "bot"), ("s1", "system", "system"), ("u2", "user", "user")]
    assert [e["at"] for e in feed] == [e["at"] for e in OLD_FEED]
    assert feed[1]["refs"] == ["a1"] and feed[2]["answer"]["decision"] == "approve"
    assert feed[4]["target"] == "a1" and feed[4]["on_behalf_of"] == "u1"
    # the read position moved: only u2 is new (k1's answer was read)
    assert [m["id"] for m in ops.inbox("opa")["messages"]] == ["u2"]
    # migrated entries keep working: k2 can be answered, d1's basis is found
    run(ops.answer(rec["id"], "k2", {"text": "beta"}))
    assert [m["id"] for m in ops.inbox("opa")["messages"]] == ["k2"]
    assert ops.basis(rec, "u1")["thread"] == threads[0]["id"]
    # a retried post finds its old request id
    assert ops.post("opa", {"text": "a1 작업 중", "level": "info", "refs": ["a1"], "request_id": "r-p1"})["id"] == "p1"


def test_moving_a_feed_twice_adds_nothing(world):
    _old_record(world.root)
    ops = operators_v2.OperatorsV2(world.root, world.manager, FakeObserver())
    first = ops.thread_rows(ops.get("0123456789ab"))
    # the record lost its save after the import (a crash between the two):
    # the next read sees the feed again and must not import it twice
    path = world.root / "operators" / "0123456789ab.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["feed"], rec["read"] = OLD_FEED, ["u1"]
    path.write_text(json.dumps(rec), encoding="utf-8")
    again = operators_v2.OperatorsV2(world.root, world.manager, FakeObserver())
    assert again.thread_rows(again.get("0123456789ab")) == first
    assert len(again.feed(again.get("0123456789ab"))) == len(OLD_FEED)
    # the backup is the first record, never overwritten
    backup = json.loads((world.root / "operators" / "0123456789ab.feed-backup.json").read_text(encoding="utf-8"))
    assert backup["read"] == ["u1", "k1:answer"]
    # a third read of a migrated record changes nothing
    third = operators_v2.OperatorsV2(world.root, world.manager, FakeObserver())
    assert third.thread_rows(third.get("0123456789ab")) == first


def test_an_empty_feed_moves_to_no_thread(world):
    folder = world.root / "operators"
    (folder / "00000000aaaa.json").write_text(json.dumps(
        {"id": "00000000aaaa", "name": "Empty", "projects": ["alpha"], "feed": [], "read": []}), encoding="utf-8")
    ops = operators_v2.OperatorsV2(world.root, world.manager, FakeObserver())
    assert ops.thread_rows(ops.get("00000000aaaa")) == []
    assert (folder / "00000000aaaa.feed-backup.json").exists()


# -- writes go to a thread ----------------------------------------------------
def test_posts_asks_and_dispatches_go_to_their_thread(world):
    ops, aid = world.ops, world.alpha["id"]
    first = run(ops.open_user_thread(aid, {"title": "배포", "text": "배포 상태?"}))
    second = run(ops.open_user_thread(aid, {"title": "버그", "text": "a1 멈춰"}))
    a, b = first["thread"]["id"], second["thread"]["id"]
    inbox = ops.inbox("opa")["messages"]
    assert [(m["thread"], m["thread_title"]) for m in inbox] == [(a, "배포"), (b, "버그")]
    # named thread
    post = ops.post("opa", {"text": "배포 중", "thread": a, "request_id": "1"})
    assert post["thread"] == a
    # no thread: the most recently active one (a, just written)
    assert ops.post("opa", {"text": "기본", "request_id": "2"})["thread"] == a
    # reply_to follows its entry's thread
    ask = ops.ask("opa", {"text": "a1 멈출까요?", "type": "approve", "thread": b, "request_id": "3"})
    assert ask["thread"] == b
    assert ops.post("opa", {"text": "후속", "reply_to": ask["id"], "request_id": "4"})["thread"] == b
    # the dispatch report goes to the instruction's thread
    basis = inbox[1]["id"]
    sent = run(ops.dispatch("opa", {"target": "a1", "text": "멈추십시오", "on_behalf_of": basis}))
    assert sent["thread"] == b and sent["kind"] == "dispatch" and sent["target"] == "a1"
    assert world.sessions["a1"].sent[-1].endswith("멈추십시오")
    # the view shows one thread at a time; the list says where the ask waits
    view = run(ops.view(aid, b))
    assert view["thread"] == b and [e["kind"] for e in view["feed"]] == ["user", "ask", "post", "dispatch"]
    pending = {t["id"]: t["pending"] for t in view["threads"]}
    assert pending[b] == 1 and pending[a] == 0
    # unknown or another operator's thread is not found
    with pytest.raises(web.HTTPNotFound):
        ops.post("opa", {"text": "x", "thread": "th-nope00", "request_id": "5"})
    theirs = run(ops.open_user_thread(world.other["id"], {"title": "남의 것"}))["thread"]["id"]
    with pytest.raises(web.HTTPNotFound):
        ops.post("opa", {"text": "x", "thread": theirs, "request_id": "6"})
    with pytest.raises(web.HTTPNotFound):
        run(ops.message(aid, {"text": "x", "thread": theirs}))
    # history filters by thread
    assert [e["text"] for e in ops.history("opa", {"thread": a, "kind": "post"})["entries"]] == ["기본", "배포 중"]


def test_the_user_message_carries_images_to_the_inbox(world):
    ops, aid = world.ops, world.alpha["id"]
    image = opening_images.stage([PNG], "image/png")
    msg = run(ops.message(aid, {"text": "", "images": [image]}))
    assert msg["kind"] == "user" and msg["images"][0]["name"] == image and msg["text"] == ""
    assert not (opening_images.stage_dir() / image).exists()
    item = ops.inbox("opa")["messages"][0]
    assert item["images"][0]["path"].endswith(image)
    assert ops.threads().has_image(item["thread"], image)
    with pytest.raises(ValueError):  # neither text nor an image
        run(ops.message(aid, {"text": " "}))
    with pytest.raises(ValueError):  # not a staged id
        run(ops.message(aid, {"text": "x", "images": ["../../etc/passwd"]}))


def test_the_operator_opens_threads_of_its_own(world):
    ops = world.ops
    out = ops.open_thread("opa", {"title": "야간 감시", "text": "a1 실패 3회", "refs": ["a1"], "level": "urgent"})
    tid = out["thread"]["id"]
    rows = ops.agent_threads("opa")["threads"]
    assert rows[0]["id"] == tid and rows[0]["opened_by"] == "agent" and rows[0]["title"] == "야간 감시"
    assert rows[0]["last"]["refs"] == ["a1"] and rows[0]["last"]["level"] == "urgent"
    with pytest.raises(web.HTTPForbidden):
        ops.open_thread("a1", {"title": "t", "text": "x"})
    with pytest.raises(ValueError):
        ops.open_thread("opa", {"title": "", "text": "x"})
    # the user's later message without a thread lands in the most recent one
    assert run(ops.message(world.alpha["id"], {"text": "확인"}))["thread"] == tid


def test_retrying_a_thread_open_returns_the_same_thread(world):
    ops = world.ops
    first = ops.open_thread("opa", {"title": "배포", "text": "시작", "request_id": "t-1"})
    again = ops.open_thread("opa", {"title": "배포", "text": "시작", "request_id": "t-1"})
    assert again["thread"] == first["thread"] and again["entry"]["id"] == first["entry"]["id"]
    assert [t["title"] for t in ops.agent_threads("opa")["threads"]].count("배포") == 1
    with pytest.raises(web.HTTPConflict):
        ops.open_thread("opa", {"title": "다른 제목", "text": "시작", "request_id": "t-1"})
    with pytest.raises(web.HTTPConflict):
        ops.open_thread("opa", {"title": "배포", "text": "다른 본문", "request_id": "t-1"})


def test_reading_an_answer_does_not_make_its_thread_the_active_one(world, monkeypatch):
    ops, aid = world.ops, world.alpha["id"]
    # one second per write, after the threads the fixture opened
    tick = itertools.count()
    monkeypatch.setattr(S, "_now", lambda: f"2099-01-01T00:{next(tick):05d}")
    a = ops.open_thread("opa", {"title": "A", "text": "a"})["thread"]["id"]
    ask = ops.ask("opa", {"text": "멈출까요?", "type": "approve", "thread": a, "request_id": "k"})
    run(ops.answer(aid, ask["id"], {"decision": "approve"}))
    b = run(ops.open_user_thread(aid, {"title": "B", "text": "새 일"}))["thread"]["id"]
    assert ops.thread_of(ops.get(aid)) == b
    inbox = ops.inbox("opa")
    assert ask["id"] in [m["id"] for m in inbox["messages"] if m["kind"] == "ask"]
    assert ops.thread_of(ops.get(aid)) == b
    assert [t["id"] for t in ops.agent_threads("opa")["threads"]][0] == b


def test_deleting_an_operator_removes_its_threads(world):
    ops = world.ops
    run(ops.message(world.alpha["id"], {"text": "x"}))
    world.sessions["opa"].exited = True
    ops.delete(world.alpha["id"])
    assert ops.threads().list(S.operator_owner(world.alpha["id"]))["threads"] == []


# -- the session-thread surfaces ---------------------------------------------
class _Manager(FakeManager):
    def get(self, name):
        return self.sessions[name]


def test_operator_threads_stay_out_of_session_surfaces_and_routes(world):
    ops = world.ops
    store = ops.threads()
    run(ops.message(world.alpha["id"], {"text": "operator 대화"}))
    ops.post("opa", {"text": "답", "request_id": "x"})
    store.open(S.USER, "a1", "session thread", "hi")
    assert set(store.rail()) == {"a1"}
    assert {t["session"] for t in store.history()["threads"]} == {"a1"}
    assert [s["session"] for s in store.index()["all_sessions"]] == ["a1"]
    tid = ops.thread_of(world.alpha)

    async def go():
        app = web.Application()
        app["manager"] = _Manager(world.sessions)
        threads_api.install(app, store)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(f"/api/threads/{tid}/messages", json={"text": "sideways"})
            assert resp.status == 400 and "Operator" in (await resp.json())["error"]
            assert (await client.post(f"/api/threads/{tid}/close", json={})).status == 400
            assert (await client.get(f"/api/threads/{tid}")).status == 200  # reading is fine
        finally:
            await client.close()

    run(go())


def test_routes_open_threads_post_to_one_and_view_it(world):
    async def go():
        app = web.Application()
        app["manager"] = world.manager
        operators_v2.install(app, create=None)
        ops = app["operators_v2"]
        ops.root, ops.cache, ops._threads = world.root / "operators", None, world.ops.threads()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            base = f"/api/operators/{world.alpha['id']}"
            opened = await (await client.post(base + "/threads", json={"title": "새 주제", "text": "질문"})).json()
            tid = opened["thread"]["id"]
            assert opened["nudge"] == "sent" and opened["entry"]["thread"] == tid
            plain = await (await client.post(base + "/threads", json={})).json()
            assert plain["thread"]["title"].startswith("Thread #") and "entry" not in plain
            sent = await (await client.post(base + "/message", json={"text": "둘째", "thread": tid})).json()
            assert sent["thread"] == tid
            view = await (await client.get(base + f"?thread={tid}")).json()
            assert view["thread"] == tid and [e["text"] for e in view["feed"]] == ["질문", "둘째"]
            assert {t["title"] for t in view["threads"]} >= {"새 주제"}
            assert (await client.get(base + "?thread=th-zzzzzz")).status == 404
            closed = await (await client.post(base + f"/threads/{tid}/close", json={})).json()
            assert {t["id"]: t["status"] for t in closed["threads"]}[tid] == "closed"
            renamed = await (await client.post(base + f"/threads/{tid}/title", json={"title": "바뀐 제목"})).json()
            assert {t["id"]: t["title"] for t in renamed["threads"]}[tid] == "바뀐 제목"
            bad = await client.post(base + f"/threads/{tid}/title", json={"title": ""})
            assert bad.status == 400
            agent = "/api/operators/agent/opa"
            made = await (await client.post(agent + "/threads", json={"title": "봇 주제", "text": "보고"})).json()
            listed = await (await client.get(agent + "/threads")).json()
            assert listed["threads"][0]["id"] == made["thread"]["id"]  # the newest open one
            posted = await (await client.post(agent + "/post", json={"text": "여기", "thread": tid,
                                                                    "request_id": "q"})).json()
            assert posted["thread"] == tid
        finally:
            await client.close()

    run(go())


def test_the_mcp_tools_pass_the_thread(monkeypatch):
    from claude_launcher import operator_v2_mcp as M

    calls = []

    class Client:
        def get(self, path):
            calls.append(("GET", path))
            return {}

        def post(self, path, body):
            calls.append(("POST", path, body))
            return {}

    monkeypatch.setenv("CLAUNCH_SESSION", "opa")
    monkeypatch.setattr(M.daemon_client, "connect_with_diagnosis", lambda: (Client(), None))
    M.call_tool("opv2_post", {"text": "t", "thread": "th-aaaaaa", "request_id": "r"})
    M.call_tool("opv2_dispatch", {"target": "a1", "text": "t", "on_behalf_of": "u", "thread": "th-aaaaaa"})
    M.call_tool("opv2_thread_open", {"title": "x", "text": "y", "request_id": "r-t"})
    M.call_tool("opv2_threads", {})
    M.call_tool("opv2_history", {"thread": "th-aaaaaa"})
    assert calls[0] == ("POST", "/api/operators/agent/opa/post",
                        {"text": "t", "thread": "th-aaaaaa", "request_id": "r"})
    assert calls[1][2]["thread"] == "th-aaaaaa"
    assert calls[2] == ("POST", "/api/operators/agent/opa/threads", {"title": "x", "text": "y", "request_id": "r-t"})
    assert calls[3] == ("GET", "/api/operators/agent/opa/threads")
    assert calls[4] == ("GET", "/api/operators/agent/opa/history?thread=th-aaaaaa")
    schema = {t["name"]: t["inputSchema"] for t in M.TOOLS}
    for name in ("opv2_post", "opv2_ask", "opv2_dispatch"):
        assert "thread" in schema[name]["properties"]
