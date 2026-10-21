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
T7 a "none" answer delivers nothing and writes no message row anywhere
T8 every call failing is "error", delivers nothing, and is still charged
T9 one failed call beside one hit offers the hit instead of delivering to it
T10 a thread deleted under the cluster is that member's error, not a 500
T11 the manual path writes only to the threads it names, once each
T12 the budget boundary is inclusive, and a routing that would cross it is
   refused
T13 a closed mega thread takes no routing
T14 a settings value that is not a finite number is refused
T15 two routings of one cluster do not both pass the same budget check
T16 a routing log that cannot be written does not undo a delivery
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
            async def raw(method, path, body=None):
                resp = await client.request(method, path, json=body)
                return resp.status, await resp.json()

            async def req(method, path, body=None, status=200):
                code, data = await raw(method, path, body)
                assert code == status, data
                return data

            req.raw = raw
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


async def two_member_cluster(req, titles=("a", "b")):
    """Two threads (s1, s2) and one cluster holding both."""
    a = (await req("POST", "/api/threads", {"to": "s1", "title": titles[0]}))["thread"]
    b = (await req("POST", "/api/threads", {"to": "s2", "title": titles[1]}))["thread"]
    mega = (await req("POST", "/api/megathreads",
                      {"title": "m", "members": [{"session": "s1"}, {"session": "s2"}]}))["mega"]
    return a, b, mega


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


def test_t7_none_delivers_nothing(tmp_path, monkeypatch):
    async def low(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.2)

    monkeypatch.setattr(jev, "system_one", low)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "anyone?"})
        assert res["verdict"] == "none" and len(res["options"]) == 2
        assert res["delivered"] == []
        assert manager.sessions["s1"].delivered == []
        assert manager.sessions["s2"].delivered == []
        # Not merely untyped: no message row exists in either thread.
        assert threads.tail(a["id"]) == [] and threads.tail(b["id"]) == []

    run(tmp_path, scenario)


def test_t8_error_delivers_nothing_and_is_charged(tmp_path, monkeypatch):
    async def boom(cfg, state, questions, *, key=None, model=None):
        raise jev.JevError("HTTP 500")

    monkeypatch.setattr(jev, "system_one", boom)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "hello?"})
        assert res["verdict"] == "error" and res["delivered"] == []
        assert manager.sessions["s1"].delivered == []
        assert threads.tail(a["id"]) == []
        row = megas.routes(mega["id"])["routes"][0]
        assert row["verdict"] == "error"
        # A call that failed may still have been billed: the row carries the
        # estimate, or the ceiling would measure less than was spent.
        assert row["tokens"] > 0

    run(tmp_path, scenario)


def test_t9_a_failed_call_beside_a_hit_offers_the_hit(tmp_path, monkeypatch):
    async def half(cfg, state, questions, *, key=None, model=None):
        if state["thread"]["title"] == "a":
            raise jev.JevError("HTTP 500")
        return jev_answer(0.97)

    monkeypatch.setattr(jev, "system_one", half)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "the log line"})
        # s2 cleared the threshold, but s1 was never scored. Delivering to s2
        # would tell the user nothing about the member that fell out.
        assert res["verdict"] == "choose"
        assert [o["session"] for o in res["options"]] == ["s2"]
        assert res["delivered"] == []
        assert manager.sessions["s2"].delivered == [] and threads.tail(b["id"]) == []
        row = megas.routes(mega["id"])["routes"][0]
        assert row["verdict"] == "choose" and row["chosen"] == [] and row["tokens"] > 0

    run(tmp_path, scenario)


def test_t10_a_deleted_thread_is_a_member_error(tmp_path, monkeypatch):
    async def high(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.97)

    monkeypatch.setattr(jev, "system_one", high)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        assert threads.delete_owner("s1") == 1
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "hello"})
        assert res["verdict"] == "choose"
        assert [e["thread"] for e in res["errors"]] == [a["id"]]
        assert res["delivered"] == [] and manager.sessions["s2"].delivered == []
        assert megas.routes(mega["id"])["routes"][0]["tokens"] > 0

    run(tmp_path, scenario)


def test_t11_the_manual_path_writes_once_to_the_threads_it_names(tmp_path, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise AssertionError("the manual path must not ask Jev")

    monkeypatch.setattr(jev, "system_one", boom)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        # The same id twice is one thread; posting twice would put the
        # message in it twice.
        res = await req("POST", f"/api/megathreads/{mega['id']}/route",
                        {"text": "only you", "choose": [a["id"], a["id"]]})
        assert res["verdict"] == "manual"
        assert [d["thread"] for d in res["delivered"]] == [a["id"]]
        assert res["route"]["chosen"] == [a["id"]]
        assert len(threads.tail(a["id"])) == 1
        assert len(manager.sessions["s1"].delivered) == 1
        assert manager.sessions["s2"].delivered == [] and threads.tail(b["id"]) == []

    run(tmp_path, scenario)


def test_t12_the_budget_boundary_is_inclusive(tmp_path, monkeypatch):
    async def ok(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.97 if state["thread"]["title"] == "a" else 0.2, tokens=100)

    monkeypatch.setattr(jev, "system_one", ok)
    text = "x" * 60
    # The handler's own estimate: members * (max_chars // 3 + 200) + len // 3.
    estimate = 2 * (300 // 3 + 200) + len(text) // 3

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        megas.record_route(mega["id"], "earlier", verdict="auto", threshold=0, tokens=5000)
        # used + estimate == limit is still allowed.
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": text})
        assert res["verdict"] == "auto"
        # 5,000 + the 200 this routing spent + the same estimate crosses now.
        refused = await req("POST", f"/api/megathreads/{mega['id']}/route",
                            {"text": text}, status=429)
        assert refused["used_tokens"] == 5200
        assert refused["estimated_tokens"] == estimate

    run(tmp_path, scenario, cfg={**CFG, "megathread_max_chars": 300,
                                 "megathread_jev_budget_tokens": 5000 + estimate})


def test_t13_a_closed_mega_thread_takes_no_routing(tmp_path, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise AssertionError("a closed cluster must not be scored")

    monkeypatch.setattr(jev, "system_one", boom)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        megas.set_status(mega["id"], "closed")
        res = await req("POST", f"/api/megathreads/{mega['id']}/route",
                        {"text": "hi"}, status=409)
        assert "closed" in res["error"]
        assert manager.sessions["s1"].delivered == []

    run(tmp_path, scenario)


def test_t14_a_non_finite_setting_is_refused(tmp_path, monkeypatch):
    written = {}
    monkeypatch.setattr(megathread_api.store_mod, "set_daemon_field",
                        lambda key, value: written.__setitem__(key, value))

    async def scenario(req, manager, threads, megas):
        # NaN fails every comparison, so the span check alone lets it through.
        await req("PUT", "/api/megathreads/settings", {"budget_tokens": float("nan")}, status=400)
        await req("PUT", "/api/megathreads/settings", {"threshold_ratio": float("inf")}, status=400)
        await req("PUT", "/api/megathreads/settings", {"concurrency": float("-inf")}, status=400)
        assert written == {}

    run(tmp_path, scenario)


def test_t15_concurrent_routings_share_one_budget_check(tmp_path, monkeypatch):
    async def slow(cfg, state, questions, *, key=None, model=None):
        await asyncio.sleep(0.05)
        return jev_answer(0.97 if state["thread"]["title"] == "a" else 0.2, tokens=1000)

    monkeypatch.setattr(jev, "system_one", slow)
    text = "x" * 60

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        pairs = await asyncio.gather(
            req.raw("POST", f"/api/megathreads/{mega['id']}/route", {"text": text}),
            req.raw("POST", f"/api/megathreads/{mega['id']}/route", {"text": text}))
        # Each routing spends 2,000; only one fits under 2,000 once the first
        # is recorded. Without the per-cluster lock both read 0 and both pass.
        assert sorted(code for code, _ in pairs) == [200, 429], pairs
        assert megas.usage_tokens(mega=mega["id"]) == 2000

    run(tmp_path, scenario, cfg={**CFG, "megathread_max_chars": 300,
                                 "megathread_jev_budget_tokens": 2000})


def test_t16_a_log_failure_does_not_undo_the_delivery(tmp_path, monkeypatch):
    async def ok(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.97 if state["thread"]["title"] == "a" else 0.2)

    monkeypatch.setattr(jev, "system_one", ok)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)

        def boom(*_args, **_kwargs):
            raise RuntimeError("disk full")

        megas.record_route = boom
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "hello"})
        # The message is out; a 500 here would report it as not sent.
        assert res["verdict"] == "auto" and res["delivered"][0]["delivered"] is True
        assert res["route"] is None and "disk full" in res["ledger_error"]
        assert len(manager.sessions["s1"].delivered) == 1

    run(tmp_path, scenario)


def test_t17_a_delivery_to_an_exited_session_is_accepted_and_queued(tmp_path, monkeypatch):
    """claunch-sgxfh: an exited session has no terminal, so the message is
    accepted into the durable thread-notice queue instead of being refused,
    and the answer says which hold it waits on. The ledger's ``delivered`` is
    the same "accepted for that session" the thread post reports."""
    from claude_launcher.daemon import thread_notices

    async def high(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.97 if state["thread"]["title"] == "a" else 0.2)

    monkeypatch.setattr(jev, "system_one", high)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        # The message still becomes a row in the thread; it is typed in when
        # the session is launched again, not now.
        manager.sessions["s1"].exited = True
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "hello"})
        assert res["verdict"] == "auto"
        assert [(d["delivered"], d["hold"]) for d in res["delivered"]] == [(True, "exited")]
        assert len(threads.tail(a["id"])) == 1
        assert "hello" in thread_notices.pending("s1")[0]["text"]
        # The log records the acceptance, not a failing attempt.
        row = megas.routes(mega["id"])["routes"][0]
        assert row["chosen"] == [a["id"]] and row["delivered"] is True

    run(tmp_path, scenario)


def test_t18_close_and_reopen(tmp_path, monkeypatch):
    async def ok(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.97 if state["thread"]["title"] == "a" else 0.2)

    monkeypatch.setattr(jev, "system_one", ok)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        closed = await req("POST", f"/api/megathreads/{mega['id']}/status",
                           {"status": "closed"})
        assert closed["mega"]["status"] == "closed"
        # A closed cluster takes no routing (409), and the transition above is
        # the only way back.
        await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "hi"}, status=409)
        await req("POST", f"/api/megathreads/{mega['id']}/status",
                  {"status": "paused"}, status=400)
        await req("POST", "/api/megathreads/mt-nope/status", {"status": "closed"}, status=404)

        opened = await req("POST", f"/api/megathreads/{mega['id']}/status",
                           {"status": "open"})
        assert opened["mega"]["status"] == "open"
        res = await req("POST", f"/api/megathreads/{mega['id']}/route", {"text": "hello"})
        assert res["verdict"] == "auto"
        assert [d["thread"] for d in res["delivered"]] == [a["id"]]

    run(tmp_path, scenario)


def test_t19_the_rail_rows_carry_every_field_the_member_picker_reads():
    """The member picker (megathread-page.js) draws each session row from the
    rail poll, not from a per-session detail call.

    It was written against `task`, which the rail leaves out on purpose, and
    against `role`, which the session record keeps but no live row fills — so
    every live row's second line came out empty while both page checks stayed
    green, because their fixtures hand-wrote the fields the daemon does not
    send (claunch-cn8v6, REVIEWED: changes). Reading the constant here is what
    keeps a future trim of the poll from doing that again: these are exactly
    the keys `sessionLine`, `matchesFilter` and `statusTone` read.
    """
    from claude_launcher.daemon.api import RAIL_FIELDS

    picker_reads = {"name", "status", "progress", "issue", "branch", "note", "role", "kind"}
    assert picker_reads <= RAIL_FIELDS, sorted(picker_reads - RAIL_FIELDS)
