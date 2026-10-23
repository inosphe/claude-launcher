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
T17 a message to an exited session is queued, and the ledger says not
   delivered
T20 a message whose mention names only a guest is delivered to that guest,
   and the ledger follows it (claunch-p9wes.1.1)
T21 an exited guest beside a live owner does not make the row queued
   (claunch-p9wes.1.1): the owner was typed, so ``delivered`` holds
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

    def list(self):
        """The registry, as the draft endpoint reads the whole fleet."""
        return list(self.sessions.values())

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


def test_t17_a_delivery_nobody_accepted_is_not_logged_as_delivered(tmp_path, monkeypatch):
    """claunch-sgxfh: a message to an exited session is queued durably (typed
    in when that session is launched again), and the answer says so
    (``queued``); the ledger's ``delivered`` still means "typed into a
    terminal", so it stays false."""
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
        assert [(d["delivered"], d["queued"], d["hold"]) for d in res["delivered"]] == [
            (False, True, "exited")]
        assert len(threads.tail(a["id"])) == 1
        assert "hello" in thread_notices.pending("s1")[0]["text"]
        # The log says the message was not typed anywhere, not merely attempted.
        row = megas.routes(mega["id"])["routes"][0]
        assert row["chosen"] == [a["id"]] and row["delivered"] is False

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

    The list of keys is not retyped here: it is read off the page source, so
    a future row that starts drawing a new field fails this test instead of
    silently reading a key the poll does not send (a previous review named
    exactly that hole in an earlier hand-written list).
    """
    import pathlib
    import re

    from claude_launcher.daemon.api import RAIL_FIELDS

    page = (pathlib.Path(__file__).resolve().parent.parent
            / "src/claude_launcher/web/static/megathread-page.js").read_text(encoding="utf-8")
    # The picker's model: the lifecycle partition through the filter choices.
    # Everything before it is the ring; everything after is the composer.
    start = page.index("const CATEGORIES")
    end = page.index("function createHint(")
    model = page[start:end]
    # A session line is built from `s.<key>` / `row.<key>`; the two orderings
    # read their key from SORTS, so those keys are named here.
    reads = set(re.findall(r"\b(?:s|row)\.([a-z_][a-z0-9_]*)", model))
    reads |= {"created_at", "last_activity_at"}
    # `progress` is the object the rail sends; nothing below the picker reads
    # into another object's fields, so every name found is a top-level key.
    assert reads <= RAIL_FIELDS, sorted(reads - RAIL_FIELDS)
    # And the direction that matters more: these are the fields a row draws,
    # so a pole that stops sending one is a blank line on every row.
    for key in ("name", "status", "role", "project", "parent", "briefing",
                "created_at", "last_activity_at", "issue", "branch", "note"):
        assert key in reads, f"the picker no longer draws {key!r}"


def test_t20_a_mention_that_names_only_a_guest_is_delivered_to_that_guest(tmp_path, monkeypatch):
    """claunch-p9wes.1.1: a mega thread delivers through threads_api, whose
    mentions decide who is typed into. When the message names a guest and not
    the owner, the ledger row has to follow the guest -- otherwise the ring
    says "대기 중" about a message a session already received."""
    async def high(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.97 if state["thread"]["title"] == "a" else 0.2)

    monkeypatch.setattr(jev, "system_one", high)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        await req("POST", f"/api/threads/{a['id']}/guests", {"session": "s1", "guest": "s3"})
        owner_before = len(manager.sessions["s1"].delivered)
        guest_before = len(manager.sessions["s3"].delivered)

        res = await req("POST", f"/api/megathreads/{mega['id']}/route",
                        {"text": "@s3 please review the log line"})
        assert res["verdict"] == "auto"
        row = res["delivered"][0]
        assert row["thread"] == a["id"]
        assert row["delivered"] is True and row["queued"] is False
        assert row["guests"] == ["s3"] and row["guests_hold"] == {}
        assert len(manager.sessions["s1"].delivered) == owner_before   # not named
        assert len(manager.sessions["s3"].delivered) == guest_before + 1
        assert megas.routes(mega["id"])["routes"][0]["delivered"] is True

    run(tmp_path, scenario, names=("s1", "s2", "s3"))


def test_t21_an_exited_guest_beside_a_live_owner_leaves_the_row_delivered(tmp_path, monkeypatch):
    """claunch-p9wes.1.1: ``@all`` types the message into the owner and every
    guest. If one guest is gone, that one block is queued for its next launch
    -- but the owner's terminal already has it, so the row (and the ring's
    "전달됨") must not read as queued for the whole message."""
    async def high(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.97 if state["thread"]["title"] == "a" else 0.2)

    monkeypatch.setattr(jev, "system_one", high)

    async def scenario(req, manager, threads, megas):
        a, b, mega = await two_member_cluster(req)
        await req("POST", f"/api/threads/{a['id']}/guests", {"session": "s1", "guest": "s3"})
        owner_before = len(manager.sessions["s1"].delivered)
        manager.sessions["s3"].exited = True

        res = await req("POST", f"/api/megathreads/{mega['id']}/route",
                        {"text": "@all 확인 부탁"})
        assert res["verdict"] == "auto"
        row = res["delivered"][0]
        assert row["thread"] == a["id"]
        assert row["delivered"] is True and row["queued"] is False and row["hold"] is None
        assert row["guests"] == ["s3"] and row["guests_hold"] == {"s3": "exited"}
        assert len(manager.sessions["s1"].delivered) == owner_before + 1   # typed
        assert megas.routes(mega["id"])["routes"][0]["delivered"] is True

    run(tmp_path, scenario, names=("s1", "s2", "s3"))


# --------------------------------------------------------------------------- #
# the create form's topic preview (claunch-vz96l)
# --------------------------------------------------------------------------- #
def test_t22_a_topic_preview_scores_the_fleet_and_writes_nothing(tmp_path, monkeypatch):
    """The preview answers "which sessions is this topic for" before any
    cluster exists: it must not open a thread, add a member, or type into a
    session, and its spend lands on the draft ledger."""
    seen = []

    async def fake_system_one(cfg, state, questions, *, key=None, model=None):
        seen.append((questions, state["session"]["name"]))
        return jev_answer(0.95 if state["session"]["name"] == "s1" else 0.05)

    monkeypatch.setattr(jev, "system_one", fake_system_one)

    async def scenario(req, manager, threads, megas):
        res = await req("POST", "/api/megathreads/draft", {"topic": "로그 형식 정리"})
        # The draft question, not the routing one.
        assert seen and seen[0][0][megathread_route.QUESTION_ID]["instructions"] \
            == megathread_route.DRAFT_INSTRUCTIONS
        assert sorted(name for _qs, name in seen) == ["s1", "s2"]
        assert res["recommended"] == ["s1"]
        assert res["scored"] == 2
        scores = {m["session"]: m for m in res["members"]}
        assert scores["s1"]["recommended"] is True and scores["s1"]["logit"] > 0
        assert scores["s2"]["recommended"] is False
        assert scores["s1"]["thread"] == "", "a session with no thread is scored, not given one"

        # Nothing was created and nothing was typed.
        assert manager.sessions["s1"].delivered == []
        assert manager.sessions["s2"].delivered == []
        assert threads.list("s1", closed=False)["threads"] == []
        assert megas.list()["megathreads"] == []
        # The ledger row is the only thing written, and it is a draft.
        assert megas.usage_tokens(kind="draft") == 200
        assert megas.usage_tokens(kind="route") == 0
        assert res["budget_used_tokens"] == 200
        assert res["ledger_error"] is None

    run(tmp_path, scenario)


def test_t23_the_preview_has_its_own_budget(tmp_path, monkeypatch):
    """A routing's spend must not close the preview, and a preview's must not
    close a routing: they are two ceilings over one ledger."""
    async def fake_system_one(cfg, state, questions, *, key=None, model=None):
        return jev_answer(0.9)

    monkeypatch.setattr(jev, "system_one", fake_system_one)

    async def scenario(req, manager, threads, megas):
        # The routing budget is already spent in full.
        await req("POST", "/api/threads", {"to": "s1", "title": "a"})
        mega = (await req("POST", "/api/megathreads",
                          {"title": "m", "members": [{"session": "s1"}]}))["mega"]
        megas.record_route(mega["id"], "spent", verdict="auto", threshold=2.197,
                           tokens=1000)
        # The preview still runs...
        res = await req("POST", "/api/megathreads/draft", {"topic": "무엇이든"})
        assert res["recommended"] == ["s1", "s2"]
        assert res["budget_used_tokens"] == 200
        # ...until its own ceiling is reached (the estimate is one capped
        # state per candidate, which is why the ceiling is not tiny).
        megas.record_route("", "spent on previews", kind="draft", verdict="none",
                           threshold=2.197, tokens=19_000)
        code, data = await req.raw("POST", "/api/megathreads/draft", {"topic": "또"})
        assert code == 429, data
        assert data["limit_tokens"] == 20_000 and data["used_tokens"] == 19_200
        # And the routing budget did not move because of the previews.
        assert megas.usage_tokens(kind="route") == 1000

    run(tmp_path, scenario,
        cfg={**CFG, "megathread_jev_budget_tokens": 1000,
             "megathread_draft_budget_tokens": 20_000})


def test_t24_the_preview_refuses_bad_input(tmp_path):
    async def scenario(req, manager, threads, megas):
        await req("POST", "/api/megathreads/draft", {}, status=400)
        await req("POST", "/api/megathreads/draft", {"topic": "   "}, status=400)
        await req("POST", "/api/megathreads/draft",
                  {"topic": "x", "sessions": ["s1", "s9"]}, status=400)
        await req("POST", "/api/megathreads/draft",
                  {"topic": "x", "sessions": "s1"}, status=400)
        await req("POST", "/api/megathreads/draft",
                  {"topic": "x", "sessions": []}, status=400)
        await req("POST", "/api/megathreads/draft",
                  {"topic": "x", "sessions": [f"s{i}" for i in range(41)]}, status=400)
        # The settings endpoint reports the preview's own rolling usage.
        settings = await req("GET", "/api/megathreads/settings")
        assert settings["settings"]["draft_budget_tokens"] == 2_000_000
        assert settings["settings"]["draft_budget_used_tokens"] == 0

    run(tmp_path, scenario)
