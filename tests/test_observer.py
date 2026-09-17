"""Observer cursor, durability, API contract and prefix reuse regressions."""
import asyncio
import copy
import json
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
    service.data["sessions"]["s1"] = {"messages": [{"role": "user", "content": "x"*101000}],
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
