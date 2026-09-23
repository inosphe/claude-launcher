"""Observer cursor, durability, API contract, usage meter and prefix reuse regressions."""
import asyncio
import copy
import json
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import observer


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(observer.paths, "daemon_dir", lambda: tmp_path)
    session = SimpleNamespace(sdef=SimpleNamespace(name="s1", task="task", cwd="/repo", conversation_id="c1"),
                              exited=False, info=lambda: {"status": "busy"})
    manager = SimpleNamespace(list=lambda: [session])
    mesh = SimpleNamespace(meshes_for_session=lambda name: [{"mesh": "team"}])
    return observer.Observer(manager, mesh), session


CFG = {"profile": "ds4-official", "model": "deepseek-flash", "endpoint": "http://localhost", "api_key": "secret"}


def evidence(cursor=1, reset=False):
    return ["path", "c1"], cursor, {"status": "busy"}, [{"id": f"transcript:{cursor}", "content": "12 tests passed"}], reset


def answer(source="transcript:1"):
    return {"summary": "테스트 완료", "state": "working", "events": [
        {"kind": "test", "text": "12 tests passed", "source": source, "needs_action": True}]}


def test_prefix_restart_ack_and_no_new_evidence(setup, monkeypatch):
    service, session = setup
    calls = []
    async def fake(cfg, messages):
        calls.append(copy.deepcopy(messages))
        return answer(f"transcript:{len(calls)}"), {"prompt_cache_hit_tokens": 123}
    monkeypatch.setattr(observer, "complete", fake)
    monkeypatch.setattr(service, "evidence", lambda *args: evidence(reset=True))
    asyncio.run(service.observe(session, CFG))
    first = copy.deepcopy(service.data["sessions"]["s1"]["messages"])
    service.data["sessions"]["s1"]["events"][0]["acknowledged"] = True
    service.save()
    restored = observer.Observer(service.manager, service.mesh)
    monkeypatch.setattr(restored, "evidence", lambda *args: evidence(2))
    asyncio.run(restored.observe(session, CFG))
    assert calls[1][:-1] == first
    row = restored.data["sessions"]["s1"]
    assert row["cursor"] == 2 and row["events"][0]["acknowledged"]
    assert row["usage"]["prompt_cache_hit_tokens"] == 123
    assert "messages" not in restored.snapshot()["sessions"][0]
    assert "api_key" not in restored.path.read_text(encoding="utf-8")
    monkeypatch.setattr(restored, "evidence", lambda *args: (["path", "c1"], 2, {}, [], False))
    asyncio.run(restored.observe(session, CFG))
    assert len(calls) == 2


def test_usage_meter_totals_every_call_and_every_local_day(setup, monkeypatch):
    service, session = setup
    days = iter(["2026-09-16", "2026-09-17", "2026-09-17"])
    monkeypatch.setattr(observer, "usage_date", lambda: next(days))
    monkeypatch.setattr(service, "evidence", lambda *args: evidence())
    async def fake(cfg, messages):
        # A provider that reports a subset: the missing counter must read as
        # zero rather than poison the sum with None.
        return answer(), {"prompt_tokens": 100, "completion_tokens": 7, "prompt_cache_hit_tokens": 40}
    monkeypatch.setattr(observer, "complete", fake)
    for _ in range(3):
        asyncio.run(service.observe(session, CFG))
    row = service.data["sessions"]["s1"]
    assert row["usage_totals"] == {"calls": 3, "prompt_tokens": 300, "completion_tokens": 21,
                                   "prompt_cache_hit_tokens": 120, "prompt_cache_miss_tokens": 0}
    assert row["usage_daily"] == {
        "2026-09-17": {"calls": 2, "prompt_tokens": 200, "completion_tokens": 14,
                       "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 0},
        "2026-09-16": {"calls": 1, "prompt_tokens": 100, "completion_tokens": 7,
                       "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 0}}
    # The meter is what the dashboard reads, so it must survive into the snapshot
    # without the model conversation that produced it.
    published = service.snapshot()["sessions"][0]
    assert published["usage_totals"] == row["usage_totals"]
    assert published["usage_daily"] == row["usage_daily"]
    assert "messages" not in published


def test_failed_call_does_not_advance_the_meter(setup, monkeypatch):
    service, session = setup
    monkeypatch.setattr(service, "evidence", lambda *args: evidence())
    async def ok(cfg, messages):
        return answer(), {"prompt_tokens": 100}
    monkeypatch.setattr(observer, "complete", ok)
    asyncio.run(service.observe(session, CFG))
    assert service.data["sessions"]["s1"]["usage_totals"]["calls"] == 1
    async def fail(*args):
        raise ValueError("bad response")
    monkeypatch.setattr(observer, "complete", fail)
    with pytest.raises(ValueError):
        asyncio.run(service.observe(session, CFG))
    row = service.data["sessions"]["s1"]
    assert row["usage_totals"]["calls"] == 1 and row["usage"]["prompt_tokens"] == 100


def test_usage_date_is_the_local_calendar_day():
    """UTC bucketing would move an evening's calls onto the next day here."""
    assert observer.usage_date() == time.strftime("%Y-%m-%d", time.localtime())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", observer.usage_date())


def test_rolling_usage_boundaries_retention_and_restart(setup, monkeypatch):
    service, session = setup
    current = 2_000_000_000

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(current, tz=timezone.utc)

    monkeypatch.setattr(observer, "datetime", Clock)
    usage = {"prompt_tokens": 100, "completion_tokens": 7, "prompt_cache_hit_tokens": 40}
    end = current
    for age in (604801, 604800, 86400, 3600, 0):
        current = end - age
        service.record_usage(usage)
    current = end
    windows = service.usage_windows()["windows"]
    assert [windows[key]["calls"] for key in ("hour", "day", "week")] == [1, 2, 3]
    assert windows["week"]["total_tokens"] == 321  # cache already in input
    assert len(service.data["usage_history"]) == 3
    # Disabled observation, removed sessions and daemon restart retain usage.
    service.manager.list = lambda: []
    restored = observer.Observer(service.manager, service.mesh)
    assert restored.snapshot()["usage_summary"] == service.usage_windows()
    current += 3600
    assert restored.usage_windows()["windows"]["hour"]["total_tokens"] == 0


def test_rolling_usage_counts_all_sessions_without_backfilling_daily(setup, monkeypatch):
    service, session = setup
    service.data["sessions"]["s1"] = {"usage_daily": {"2026-09-18": {"prompt_tokens": 9999}}}
    assert service.snapshot()["usage_summary"]["windows"]["week"]["calls"] == 0
    monkeypatch.setattr(service, "evidence", lambda *args: evidence())

    async def complete(*args):
        return answer(), {"prompt_tokens": 100, "completion_tokens": 7}

    monkeypatch.setattr(observer, "complete", complete)
    asyncio.run(service.observe(session, CFG))
    session.sdef.name = "s2"
    asyncio.run(service.observe(session, CFG))
    assert service.snapshot()["usage_summary"]["windows"]["hour"]["total_tokens"] == 214

    async def fail(*args):
        raise ValueError("failed call")

    monkeypatch.setattr(observer, "complete", fail)
    with pytest.raises(ValueError):
        asyncio.run(service.observe(session, CFG))
    assert service.usage_windows()["windows"]["hour"]["calls"] == 2


def test_failure_does_not_consume_cursor(setup, monkeypatch):
    service, session = setup
    service.data["sessions"]["s1"] = {"cursor": 3, "summary": "old"}
    before = copy.deepcopy(service.data)
    monkeypatch.setattr(service, "evidence", lambda *args: evidence(4))
    async def fail(*args):
        raise ValueError("bad response")
    monkeypatch.setattr(observer, "complete", fail)
    with pytest.raises(ValueError):
        asyncio.run(service.observe(session, CFG))
    assert service.data == before


def test_source_validation_rotation_and_reset(setup, monkeypatch):
    service, session = setup
    service.data["sessions"]["s1"] = {"messages": [{"role": "user", "content": "x"*(observer.MAX_CONTEXT + 1)}],
        "summary": "previous", "events": [], "config": [CFG[k] for k in ("profile", "model", "endpoint")]}
    monkeypatch.setattr(service, "evidence", lambda *args: evidence())
    async def fake(cfg, messages):
        assert len(json.dumps(messages)) < 10000
        response = answer()
        response["events"] += [{"kind":"test","source":["invalid"],"text":"bad"},
                               {"kind":"test","source":"invented","text":"bad"}]
        return response, {}
    monkeypatch.setattr(observer, "complete", fake)
    asyncio.run(service.observe(session, CFG))
    row = service.data["sessions"]["s1"]
    assert len(row["events"]) == 1 and row["rotations"] == 1
    monkeypatch.setattr(service, "evidence", lambda *args: evidence(reset=True))
    asyncio.run(service.observe(session, CFG))
    assert len(service.data["sessions"]["s1"]["events"]) == 1


def test_incremental_evidence_uses_forward_pages(setup, monkeypatch):
    service, session = setup
    calls = []
    def page(name, sdef, **kw):
        calls.append(kw)
        return {"source":"path", "total":90, "records":[
            {"seq":60,"role":"assistant","blocks":[{"type":"text","text":"done"},{"type":"thinking","text":"private"}]}]}
    monkeypatch.setattr(observer.transcript_view, "page", page)
    monkeypatch.setattr(observer.briefing, "gather_cflow", lambda *args: None)
    result = service.evidence(session, {"identity":["path","c1"], "cursor":40,
                                        "state_source":{"cflow":None,"status":"busy"}})
    assert result[1] == 80 and not result[-1]
    assert calls[-1] == {"before":80,"limit":40}
    assert "private" not in json.dumps(result[3])


def test_communication_input_advances_cursor_without_call(setup, monkeypatch):
    service, session = setup
    service.data["sessions"]["s1"] = {"identity": ["path", "c1"], "cursor": 1,
        "state_source": {"cflow": None, "status": "busy"}}
    monkeypatch.setattr(observer.transcript_view, "page", lambda *a, **kw: {
        "source": "path", "total": 2, "records": [{"seq": 2, "role": "assistant",
        "blocks": [{"type": "text", "text": "리더에게 메시지를 전달했습니다."}]}]})
    monkeypatch.setattr(observer.briefing, "gather_cflow", lambda *a: None)
    async def unexpected(*a):
        pytest.fail("Communication alone must not call the model")
    monkeypatch.setattr(observer, "complete", unexpected)
    asyncio.run(service.observe(session, CFG))
    assert service.data["sessions"]["s1"]["cursor"] == 2


def test_communication_output_and_legacy_display_filtered(setup, monkeypatch):
    service, session = setup
    service.data["sessions"]["s1"] = {"summary": "테스트 12개 통과",
        "messages": [{"role": "system", "content": "old observer instructions"}],
        "config": [CFG[k] for k in ("profile", "model", "endpoint")], "events": [
        {"id": "legacy", "text": "리더에게 메시지를 전달했습니다.", "at": "2026-09-18"}]}
    monkeypatch.setattr(service, "evidence", lambda *a: evidence())
    async def fake(cfg, messages):
        assert messages[0]["content"] == observer.SYSTEM
        return {"summary": "응답 대기 중입니다.", "state": "waiting", "events": [
            {"kind": "result", "text": "리더에게 메시지를 전달했습니다.", "source": "transcript:1"},
            {"kind": "test", "text": "테스트 24개 통과", "source": "transcript:1"}]}, {}
    monkeypatch.setattr(observer, "complete", fake)
    asyncio.run(service.observe(session, CFG))
    row = service.snapshot()["sessions"][0]
    assert row["summary"] == "테스트 12개 통과"
    assert [event["text"] for event in row["events"]] == ["테스트 24개 통과"]
    assert service.data["sessions"]["s1"]["events"][0]["id"] == "legacy"
    service.reports.publish("s1", {"text": "확인했습니다."})
    assert service.snapshot()["sessions"][0]["summary"] == "테스트 12개 통과"
    service.reports.publish("s1", {"text": "확인했습니다.", "question": True})
    assert any(e.get("question") for e in service.snapshot()["sessions"][0]["events"])


def test_http_completion_contract_and_cache_usage():
    async def scenario():
        async def completion(request):
            body = await request.json()
            assert body["model"] == "deepseek-flash"
            assert request.headers["Authorization"] == "Bearer secret"
            return web.json_response({"choices":[{"message":{"content":json.dumps(answer())},"finish_reason":"stop"}],
                                      "usage":{"prompt_cache_hit_tokens":1024,"prompt_cache_miss_tokens":22}})
        app = web.Application(); app.router.add_post("/chat/completions", completion)
        async with TestServer(app) as server:
            result, usage = await observer.complete({**CFG,"endpoint":str(server.make_url("/chat/completions"))}, [])
            assert result["summary"] == "테스트 완료" and usage["prompt_cache_hit_tokens"] == 1024
    asyncio.run(scenario())


def test_endpoints_enable_validate_and_ack(setup, monkeypatch):
    service, _ = setup
    async def scenario():
        app=web.Application();app["manager"]=service.manager;app["mesh"]=service.mesh
        observer.install(app)
        monkeypatch.setattr(observer,"configuration",lambda:CFG)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/observer/settings",json={"enabled":"yes"})).status == 400
            response=await client.post("/api/observer/settings",json={"enabled":True})
            assert (await response.json())["enabled"]
            app["observer"].data["sessions"]["s1"]={"events":[{"id":"e1","acknowledged":False}]}
            assert (await client.post("/api/observer/s1/acknowledge",json={"id":"e1"})).status==200
            assert (await client.get("/api/observer")).status==200
            assert app["observer"].data["sessions"]["s1"]["events"][0]["acknowledged"]
            await client.post("/api/observer/settings",json={"enabled":False})
    asyncio.run(scenario())


def test_one_shot_refresh_observes_only_the_named_session(setup, monkeypatch):
    """The button is per session: pressing s2's must not spend on s1's."""
    service, first = setup
    second = SimpleNamespace(sdef=SimpleNamespace(name="s2", task="task", cwd="/repo", conversation_id="c2"),
                             exited=False, info=lambda: {"status": "busy"})
    service.manager.list = lambda: [first, second]
    service.data["enabled"] = True
    monkeypatch.setattr(observer, "configuration", lambda: CFG)
    observed = []
    def evidence_for(session, previous):
        observed.append(session.sdef.name)
        return ["path", "c1"], 1, {"status": "busy"}, [{"id": "transcript:1", "content": "12 tests passed"}], False
    monkeypatch.setattr(service, "evidence", evidence_for)
    async def fake(cfg, messages):
        return answer(), {"prompt_tokens": 10}
    monkeypatch.setattr(observer, "complete", fake)
    assert asyncio.run(service.refresh("s2")) == (200, {"called": True, "events": 1})
    assert observed == ["s2"]
    assert set(service.data["sessions"]) == {"s2"}


def test_one_shot_refresh_spends_no_call_when_nothing_is_new(setup, monkeypatch):
    """A pass the loop would not have made is not one the button makes either."""
    service, _ = setup
    service.data["enabled"] = True
    monkeypatch.setattr(observer, "configuration", lambda: CFG)
    monkeypatch.setattr(service, "evidence", lambda *args: (["path", "c1"], 5, {}, [], False))
    async def unexpected(*args):
        pytest.fail("a pass with nothing new must not call the API")
    monkeypatch.setattr(observer, "complete", unexpected)
    assert asyncio.run(service.refresh("s1")) == (200, {"called": False, "events": 0})


def test_one_shot_refresh_runs_while_observation_is_off(setup, monkeypatch):
    """The switch governs the loop; the button is one call for one session.

    Reading one session without leaving the loop running over the whole fleet
    is what the press is for, so the pass happens with the switch off — and it
    does not turn the loop on behind the operator's back.
    """
    service, _ = setup
    monkeypatch.setattr(observer, "configuration", lambda: CFG)
    monkeypatch.setattr(service, "evidence", lambda *args: evidence())
    async def fake(cfg, messages):
        return answer(), {"prompt_tokens": 10}
    monkeypatch.setattr(observer, "complete", fake)
    assert service.data["enabled"] is False
    assert asyncio.run(service.refresh("s1")) == (200, {"called": True, "events": 1})
    assert service.data["sessions"]["s1"]["summary"] == "테스트 완료"
    assert service.data["enabled"] is False


def test_one_shot_refresh_reports_an_unknown_session(setup, monkeypatch):
    service, _ = setup
    service.data["enabled"] = True
    monkeypatch.setattr(observer, "configuration", lambda: pytest.fail("no session, no configuration"))
    assert asyncio.run(service.refresh("nope")) == (404, {"error": "세션을 찾을 수 없습니다."})


def test_the_refresh_route_carries_the_pass_outcome(setup, monkeypatch):
    service, _ = setup
    async def scenario():
        app=web.Application();app["manager"]=service.manager;app["mesh"]=service.mesh
        observer.install(app)
        # The loop would observe the same session on its own schedule and race
        # the button this test is about, so the route is driven alone. The
        # app builds its own Observer, so the evidence patch goes on that one.
        app.on_startup.remove(app["observer"].start)
        monkeypatch.setattr(observer,"configuration",lambda:CFG)
        # Each press reads one record further, so both of them have something
        # new to report: a second press over the same evidence would dedupe to
        # zero events and say nothing about the switch this test is about.
        passes = []
        monkeypatch.setattr(app["observer"],"evidence",lambda *args: evidence(len(passes) + 1))
        async def fake(cfg, messages):
            passes.append(messages)
            return answer(f"transcript:{len(passes)}"), {"prompt_tokens": 10}
        monkeypatch.setattr(observer,"complete",fake)
        async with TestClient(TestServer(app)) as client:
            # The route is the same answer with the switch either way: off
            # first, and the loop is still off after it (settings say so).
            off=await client.post("/api/observer/s1/refresh")
            assert off.status==200 and await off.json()=={"called":True,"events":1}
            assert (await (await client.get("/api/observer")).json())["enabled"] is False
            app["observer"].data["enabled"]=True
            assert (await client.post("/api/observer/nope/refresh")).status==404
            done=await client.post("/api/observer/s1/refresh")
            assert done.status==200 and await done.json()=={"called":True,"events":1}
            await client.post("/api/observer/settings",json={"enabled":False})
    asyncio.run(scenario())


def two_sessions(setup):
    """The fixture's session plus a second one that the pin distinguishes."""
    service, first = setup
    first.sdef.observe_pin = False
    second = SimpleNamespace(sdef=SimpleNamespace(name="s2", task="task", cwd="/repo",
                                                 conversation_id="c2", observe_pin=True),
                             exited=False, info=lambda: {"status": "busy"})
    service.manager.list = lambda: [first, second]
    return service, first, second


def test_the_pinned_scope_spends_only_on_pinned_sessions(setup, monkeypatch):
    """The scope is a cost switch: an unpinned session must not reach the model.

    Driven through :meth:`Observer.pass_once` rather than through ``observes``
    alone, because what the operator buys is the loop's behaviour — a
    predicate read correctly but not consulted by the sweep would pass the
    weaker test and pay for the whole fleet.
    """
    service, first, second = two_sessions(setup)
    service.data["enabled"] = True
    service.data["scope"] = observer.SCOPE_PINNED
    observed = []
    def evidence_for(session, previous):
        observed.append(session.sdef.name)
        return ["path", "c1"], 1, {"status": "busy"}, [{"id": "transcript:1", "content": "12 tests passed"}], False
    monkeypatch.setattr(service, "evidence", evidence_for)
    async def fake(cfg, messages):
        return answer(), {"prompt_tokens": 10}
    monkeypatch.setattr(observer, "complete", fake)
    asyncio.run(service.pass_once(CFG))
    assert observed == ["s2"], "only the pinned session is read at all"
    assert set(service.data["sessions"]) == {"s2"}


def test_the_default_scope_still_covers_everything(setup, monkeypatch):
    """A daemon that never chose a scope must behave exactly as it did before."""
    service, first, second = two_sessions(setup)
    service.data["enabled"] = True
    assert service.data["scope"] == observer.SCOPE_ALL
    assert service.observes(first) and service.observes(second)
    observed = []
    def evidence_for(session, previous):
        observed.append(session.sdef.name)
        return ["path", "c1"], 1, {}, [], False
    monkeypatch.setattr(service, "evidence", evidence_for)
    asyncio.run(service.pass_once(CFG))
    assert observed == ["s1", "s2"]


def test_the_scope_persists_and_an_unknown_value_reads_as_all(setup):
    service, _ = setup
    service.data["scope"] = observer.SCOPE_PINNED
    service.save()
    assert json.loads(service.path.read_text(encoding="utf-8"))["scope"] == observer.SCOPE_PINNED
    assert observer.Observer(service.manager, service.mesh).data["scope"] == observer.SCOPE_PINNED
    # An unrecognised value must not silently widen the scope: an operator who
    # chose the narrower one would start paying for the whole fleet again
    # without saying so. Only the two named scopes are readings.
    settings = json.loads(service.path.read_text(encoding="utf-8"))
    settings["scope"] = "evrything"
    service.path.write_text(json.dumps(settings), encoding="utf-8")
    assert observer.Observer(service.manager, service.mesh).data["scope"] == observer.SCOPE_ALL


def test_the_pin_route_writes_the_definition_field(setup, monkeypatch):
    service, session = setup
    # The fixture's manager only answers ``list``; the route reaches the pin
    # through the manager exactly as the rail's own routes do, so the stub
    # records the call instead of standing in for it.
    written = []
    def set_observe_pin(name, on):
        if name != session.sdef.name:
            raise KeyError(name)
        session.sdef.observe_pin = bool(on)
        written.append((name, bool(on)))
        return session
    service.manager.set_observe_pin = set_observe_pin
    async def scenario():
        app=web.Application();app["manager"]=service.manager;app["mesh"]=service.mesh
        observer.install(app)
        app.on_startup.remove(app["observer"].start)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/observer/s1/pin", json={"pinned":"yes"})).status == 400
            assert (await client.post("/api/observer/nope/pin", json={"pinned":True})).status == 404
            said = await client.post("/api/observer/s1/pin", json={"pinned":True})
            assert said.status == 200 and await said.json() == {"name":"s1","pinned":True}
            off = await client.post("/api/observer/s1/pin", json={"pinned":False})
            assert await off.json() == {"name":"s1","pinned":False}
    asyncio.run(scenario())
    assert written == [("s1", True), ("s1", False)]


def test_the_settings_route_carries_the_scope_without_undoing_it(setup, monkeypatch):
    """The monitor button posts ``enabled`` alone; that must not reset the mode."""
    service, _ = setup
    async def scenario():
        app=web.Application();app["manager"]=service.manager;app["mesh"]=service.mesh
        observer.install(app)
        app.on_startup.remove(app["observer"].start)
        monkeypatch.setattr(observer,"configuration",lambda:CFG)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/observer/settings", json={"enabled":False,"scope":"narrow"})).status == 400
            first = await client.post("/api/observer/settings", json={"enabled":True,"scope":"pinned"})
            assert await first.json() == {"enabled":True,"scope":"pinned"}
            # No scope in the body: the button's own call leaves the mode alone.
            second = await client.post("/api/observer/settings", json={"enabled":False})
            assert await second.json() == {"enabled":False,"scope":"pinned"}
            assert app["observer"].data["scope"] == "pinned"
            published = await (await client.get("/api/observer")).json()
            assert published["scope"] == "pinned"
    asyncio.run(scenario())


def test_the_snapshot_carries_the_pin_the_card_draws(setup, monkeypatch):
    service, first, second = two_sessions(setup)
    published = {row["name"]: row for row in service.snapshot()["sessions"]}
    assert published["s1"]["observe_pin"] is False
    assert published["s2"]["observe_pin"] is True


def test_ignored_records_advance_without_api_call(setup, monkeypatch):
    service, session = setup
    service.data["sessions"]["s1"] = {"cursor": 10}
    monkeypatch.setattr(service, "evidence", lambda *args: (["path", "c1"], 50, {}, [], False))
    async def unexpected(*args):
        pytest.fail("thinking-only evidence must not call the API")
    monkeypatch.setattr(observer, "complete", unexpected)
    asyncio.run(service.observe(session, CFG))
    assert service.data["sessions"]["s1"]["cursor"] == 50
    assert json.loads(service.session_path("s1").read_text(encoding="utf-8"))["cursor"] == 50


def test_profile_configuration_uses_named_backend(config_file, monkeypatch):
    from claude_launcher import store
    store.save({"providers":{"ds":{"api_key":"provider-secret", "endpoints":{"openai":"https://example.test/v1"}}},
                "profiles":{"ds4-official":{"provider":"ds"}}})
    monkeypatch.setattr(observer.lineage, "stored_auth_token", lambda p: "profile-secret")
    cfg = observer.configuration()
    assert cfg["model"] == "deepseek-flash" and cfg["profile"] == "ds4-official"
    assert cfg["api_key"] == "profile-secret"
    assert cfg["endpoint"] == "https://example.test/v1/chat/completions"


@pytest.mark.parametrize("finish,content", [("length", '{}'), ("stop", 'not json'), ("stop", '{"summary":[],"events":[]}')])
def test_unusable_completions_fail(finish, content):
    async def scenario():
        async def completion(request):
            return web.json_response({"choices":[{"message":{"content":content},"finish_reason":finish}]})
        app=web.Application();app.router.add_post("/", completion)
        async with TestServer(app) as server:
            with pytest.raises(ValueError):
                await observer.complete({**CFG,"endpoint":str(server.make_url("/"))}, [])
    asyncio.run(scenario())


def test_direct_reports_survive_inflight_observation(setup, monkeypatch):
    service,session=setup
    monkeypatch.setattr(service,'evidence',lambda *args:evidence())
    async def check():
        entered=asyncio.Event();release=asyncio.Event()
        async def complete(*args):
            entered.set();await release.wait();return answer(),{}
        monkeypatch.setattr(observer,'complete',complete)
        task=asyncio.create_task(service.observe(session,CFG))
        await entered.wait()
        direct=service.reports.publish('s1',{'text':'Screenshot review needed','question':True})
        release.set();await task
        events=service.snapshot()['sessions'][0]['events']
        assert any(e['id']==direct['id'] for e in events)
        assert any(e['source']=='transcript:1' for e in events)
        restored=observer.Observer(service.manager,service.mesh)
        assert any(e['id']==direct['id'] for e in restored.snapshot()['sessions'][0]['events'])
    asyncio.run(check())


# What a pass may spend a call on. The model is told to report results, not
# liveness, so a busy/idle flip with nothing new to read must not buy the whole
# conversation again. Measured on this daemon's own rows, 668 of 1116
# evidence-bearing calls across 58 sessions carried only that flip.
def watching(service, session, monkeypatch, statuses, records=None):
    """Drive ``evidence`` off a scripted status and a transcript of nothing."""
    queue = iter(statuses)
    monkeypatch.setattr(session, "info", lambda: {"status": next(queue)})
    monkeypatch.setattr(observer.transcript_view, "page",
                        lambda *a, **kw: {"source": "path", "total": 1 + len(records or []),
                                          "records": list(records or [])})
    monkeypatch.setattr(observer.briefing, "gather_cflow", lambda *a: None)
    service.data["sessions"]["s1"] = {"identity": ["path", "c1"], "cursor": 1,
                                      "state_source": {"cflow": None, "status": "busy"}}
    sent = []
    async def fake(cfg, messages):
        sent.append(json.loads(messages[-1]["content"]))
        return answer(), {}
    monkeypatch.setattr(observer, "complete", fake)
    return sent


def test_a_status_flip_alone_spends_no_call(setup, monkeypatch):
    service, session = setup
    sent = watching(service, session, monkeypatch, ["busy", "idle", "busy", "idle"])
    for _ in range(3):
        asyncio.run(service.observe(session, CFG))
    assert sent == []
    # The flip is deferred rather than written away, so the call that new
    # records later justify still carries it.
    assert service.data["sessions"]["s1"]["state_source"] == {"cflow": None, "status": "busy"}


def test_a_status_flip_rides_on_the_next_record_call(setup, monkeypatch):
    service, session = setup
    records = []
    sent = watching(service, session, monkeypatch, ["idle", "idle"], records=records)
    asyncio.run(service.observe(session, CFG))
    assert sent == []
    records.append({"seq": 1, "role": "assistant", "blocks": [{"type": "text", "text": "테스트 12개 통과"}]})
    asyncio.run(service.observe(session, CFG))
    assert len(sent) == 1
    state_row = [row for row in sent[0] if row["id"] == "daemon:state"]
    assert state_row and state_row[0]["content"]["status"] == "idle"


def test_cflow_movement_alone_calls_the_model(setup, monkeypatch):
    service, session = setup
    sent = watching(service, session, monkeypatch, ["busy"])
    monkeypatch.setattr(observer.briefing, "gather_cflow",
                        lambda *a: {"workflow": "improv-worker", "status": "step",
                                    "step": "work", "title": "작업 실행"})
    asyncio.run(service.observe(session, CFG))
    assert len(sent) == 1
    state_row = [row for row in sent[0] if row["id"] == "daemon:state"]
    assert state_row and state_row[0]["content"]["cflow"]["step"] == "work"
    assert state_row[0]["content"]["status"] == "busy"


def test_an_exit_is_reported_once(setup, monkeypatch):
    """An exit is terminal, not a flip: it spends one call, and only one."""
    service, session = setup
    sent = watching(service, session, monkeypatch, ["exited", "exited"])
    session.exited = True
    asyncio.run(service.observe(session, CFG))
    asyncio.run(service.observe(session, CFG))
    assert len(sent) == 1
    assert sent[0][0]["content"]["status"] == "exited"


def test_tool_only_records_stay_in_the_evidence(setup, monkeypatch):
    """Filtering tool traffic out of the evidence was measured and rejected.

    Tool records are 63% of what the builder serializes, which makes them look
    like an obvious saving. They are the evidence: of the 4071 events the
    observer actually reported (replayed 2026-09-20 over 58 session rows), 68%
    cite a tool-only record as their source — the file paths, test names and
    counts a report is made of arrive as tool results rather than as prose.
    Dropping them costs about six of every ten reports; dropping only the calls
    still costs one in nine. This test fails if someone adds that filter, so the
    measurement has to be answered rather than rediscovered.
    """
    service, session = setup
    records = [
        {"seq": 1, "role": "assistant",
         "blocks": [{"type": "tool_use", "name": "Read", "input": {"path": "a.py"}}]},
        {"seq": 2, "role": "user",
         "blocks": [{"type": "tool_result", "content": "tests/test_a.py:12 3 passed"}]},
    ]
    sent = watching(service, session, monkeypatch, ["busy"], records=records)
    asyncio.run(service.observe(session, CFG))
    assert [row["id"] for row in sent[0]] == ["transcript:1", "transcript:2"]


def test_context_rotates_at_the_ceiling_and_keeps_the_summary(setup, monkeypatch):
    """The ceiling is what the replayed share scales with, so it is pinned.

    The append-only conversation is billed again on every call. Replaying the
    daemon's own 58 session windows at lower ceilings priced the trade
    (2026-09-20): 100,000 bills 100% of today's characters, 24,000 bills 35.6%,
    12,000 bills 22.9%, and dropping the history outright — which this design
    does not do — floors at 12.7%. The constant is set to hold at least three
    p90-sized exchanges (3 x 3,122 characters of batch and answer), so this
    test measures against the constant rather than a size of its own.
    """
    service, session = setup

    def conversation(size):
        """A stored conversation whose serialized length is exactly ``size``."""
        msgs = [{"role": "system", "content": observer.SYSTEM},
                {"role": "user", "content": ""}]
        msgs[1]["content"] = "x" * (size - len(json.dumps(msgs)))
        assert len(json.dumps(msgs)) == size
        return msgs

    monkeypatch.setattr(service, "evidence", lambda *args: evidence())
    async def fake(cfg, messages):
        return answer(), {}
    monkeypatch.setattr(observer, "complete", fake)

    for size, rotations in ((observer.MAX_CONTEXT, 0), (observer.MAX_CONTEXT + 1, 1)):
        service.data["sessions"]["s1"] = {
            "messages": conversation(size), "summary": "이전 요약",
            "events": [], "config": [CFG[k] for k in ("profile", "model", "endpoint")]}
        asyncio.run(service.observe(session, CFG))
        row = service.data["sessions"]["s1"]
        assert row.get("rotations", 0) == rotations, f"ceiling {observer.MAX_CONTEXT}, size {size}"
        assert row["messages"][0]["content"] == observer.SYSTEM
        # Whether it rotated or not, the model keeps the thread: the summary is
        # carried into the init message exactly when the history is dropped.
        # (ensure_ascii=False: the default dump escapes the Korean, so a search
        # for the summary would miss the very text it is looking for.)
        init = json.dumps(row["messages"][1], ensure_ascii=False)
        assert ("이전 요약" in init) == bool(rotations)


def test_observed_events_sit_at_the_time_of_the_record_they_cite(setup, monkeypatch):
    """One pass stamps all its events with one time; the timeline uses the record's.

    On s697 (2026-09-23) five events of one pass all read 07:26:34 while the
    records they cite ran from 07:17:21 to 07:26:09. The record time is in the
    stored evidence, so the snapshot places every event — stored before this
    rule or after — by it, and keeps the pass time as ``observed_at``.
    """
    service, session = setup
    rows = [{"id": "transcript:1", "at": "2026-09-23T07:17:21.332Z", "role": "assistant", "content": "a"},
            {"id": "transcript:2", "at": "2026-09-23T07:26:09.038Z", "role": "assistant", "content": "b"},
            {"id": "daemon:state", "content": {"status": "busy"}}]
    monkeypatch.setattr(service, "evidence", lambda *args: (["path", "c1"], 2, {"status": "busy"}, rows, True))

    async def fake(cfg, messages):
        return {"summary": "s", "state": "working", "events": [
            {"kind": "test", "text": "late", "source": "transcript:2"},
            {"kind": "cflow", "text": "early", "source": "transcript:1", "pivot": True},
            {"kind": "result", "text": "state", "source": "daemon:state"}]}, {}
    monkeypatch.setattr(observer, "complete", fake)
    monkeypatch.setattr(observer, "now", lambda: "2026-09-23T07:26:34+00:00")
    asyncio.run(service.observe(session, CFG))

    stored = service.data["sessions"]["s1"]["events"]
    assert [e["at"] for e in stored] == ["2026-09-23T07:26:34+00:00"] * 3
    assert [e["pivot"] for e in stored] == [False, True, False]
    shown = [e for e in service.snapshot()["sessions"][0]["events"] if e["kind"] in {"test", "cflow", "result"}]
    assert [(e["text"], e["at"], e.get("observed_at")) for e in shown] == [
        ("early", "2026-09-23T07:17:21.332Z", "2026-09-23T07:26:34+00:00"),
        ("late", "2026-09-23T07:26:09.038Z", "2026-09-23T07:26:34+00:00"),
        # The state row carries no time, so its event stays at the pass time.
        ("state", "2026-09-23T07:26:34+00:00", None)]
    assert all("evidence" not in e for e in shown)


def test_the_prompt_asks_for_one_line_chips_and_pivots():
    """The model is asked for the shape the page draws; the page does not depend on it."""
    for phrase in ("ONE line of at most 120 characters", "Wrap identifiers in backticks",
                   "@s123", '"pivot":true', "expected X → observed Y → now Z"):
        assert phrase in observer.SYSTEM
