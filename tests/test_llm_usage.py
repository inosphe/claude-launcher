"""The daemon's own LLM calls, metered (claunch-60y0d, daemon/llm_usage.py).

Each feature that calls a model outside the routing shim -- briefing,
observer, search embedding/rerank, Jev -- appends one line per call to the
daemon's LLM ledger, and the Stats page reads it back per window, provider,
feature and session. The endpoints here are local aiohttp servers answering
in each protocol's shape.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiohttp import web as aioweb
from aiohttp.test_utils import TestServer, make_mocked_request

from claude_launcher import store
from claude_launcher.daemon import api, briefing, jev, llm_usage, megathread_route, observer, rag


def _lines():
    now = time.time()
    return llm_usage.load(now - 86400, now + 60)


# --------------------------------------------------------------------------- #
# the usage shapes
# --------------------------------------------------------------------------- #
def test_parts_of_reads_each_protocols_usage_shape():
    # OpenAI: the prompt holds the cached part, which is taken out of input.
    assert llm_usage.parts_of({"prompt_tokens": 100, "completion_tokens": 20,
                               "prompt_tokens_details": {"cached_tokens": 30}}) == {
        "input": 70, "cache_read": 30, "cache_write": 0, "output": 20}
    # DeepSeek's own cache counter (the observer's provider).
    assert llm_usage.parts_of({"prompt_tokens": 100, "completion_tokens": 5,
                               "prompt_cache_hit_tokens": 90})["input"] == 10
    # Anthropic counts the three input parts apart.
    assert llm_usage.parts_of({"input_tokens": 7, "output_tokens": 3,
                               "cache_read_input_tokens": 50,
                               "cache_creation_input_tokens": 4}) == {
        "input": 7, "cache_read": 50, "cache_write": 4, "output": 3}
    # An embeddings / rerank answer that reports only a total is all input.
    assert llm_usage.parts_of({"total_tokens": 12})["input"] == 12
    assert llm_usage.parts_of({"prompt_tokens": 9, "total_tokens": 9})["input"] == 9
    # No count at all is "not reported", never zero.
    assert llm_usage.parts_of({}) is None
    assert llm_usage.parts_of(None) is None
    assert llm_usage.parts_of({"billed_units": {"search_units": 1}}) is None


def test_split_usage_shares_sum_to_the_reported_count():
    shares = llm_usage.split_usage({"prompt_tokens": 101}, [1, 1, 1])
    assert [llm_usage.parts_of(s)["input"] for s in shares] == [33, 33, 35]
    shares = llm_usage.split_usage({"prompt_tokens": 10}, [0, 0])
    assert sum(llm_usage.parts_of(s)["input"] for s in shares) == 10
    assert llm_usage.split_usage(None, [3, 4]) == [None, None]


# --------------------------------------------------------------------------- #
# the ledger and its reading
# --------------------------------------------------------------------------- #
def test_record_and_overview_group_by_feature_provider_and_session():
    now = time.time()
    old = now - 40 * 86400
    llm_usage.record("briefing", provider="ds", model="m1", session="s1",
                     usage={"prompt_tokens": 10, "completion_tokens": 5}, at=now - 5)
    llm_usage.record("observer", provider="ds", model="m2", session="s1",
                     usage={"prompt_tokens": 100, "completion_tokens": 1,
                            "prompt_cache_hit_tokens": 60}, at=now - 4)
    llm_usage.record("embedding", endpoint="https://emb.example/v1", model="e",
                     purpose="index", session="s2", usage={"total_tokens": 7}, at=now - 3)
    llm_usage.record("rerank", provider="fw", model="r", purpose="query",
                     usage={"billed_units": {"search_units": 1}}, at=now - 2)
    llm_usage.record("briefing", provider="ds", model="m1", session="s2",
                     ok=False, error="HTTP 500", at=now - 1)
    llm_usage.record("briefing", provider="ds", model="m1", session="s1",
                     usage={"prompt_tokens": 1000}, at=old)

    d = llm_usage.overview("today", now=now)
    t = d["totals"]
    assert t["calls"] == 5 and t["errors"] == 1 and t["unreported"] == 2
    assert t["total"] == 15 + 101 + 7
    assert t["cache_read"] == 60 and t["input"] == 10 + 40 + 7

    features = {r["feature"]: r for r in d["features"]}
    assert [r["feature"] for r in d["features"]] == ["briefing", "observer", "embedding", "rerank"]
    assert features["briefing"]["calls"] == 2 and features["briefing"]["errors"] == 1
    assert features["rerank"]["unreported"] == 1 and features["rerank"]["total"] == 0

    providers = {r["provider"]: r for r in d["providers"]}
    # A typed endpoint belongs to no provider; its host names it.
    assert set(providers) == {"ds", "emb.example", "fw"}
    assert {m["model"] for m in providers["ds"]["models"]} == {"m1", "m2"}

    sessions = {r["session"]: r for r in d["sessions"]}
    assert sessions["s1"]["total"] == 116
    assert {f["feature"] for f in sessions["s1"]["features"]} == {"briefing", "observer"}
    assert sessions[""]["calls"] == 1          # the rerank, about no session
    assert d["days"][-1]["features"]["observer"] == 101

    # The 40-day-old line is outside every window the page offers.
    month = llm_usage.overview("month", now=now)
    assert month["totals"]["calls"] == 5
    assert len(month["days"]) == 30

    narrowed = llm_usage.overview("today", provider="ds", now=now)
    assert narrowed["totals"]["calls"] == 3
    assert {p["provider"] for p in narrowed["all_providers"]} == {"ds", "emb.example", "fw"}
    one = llm_usage.overview("today", session="s2", now=now)
    assert one["totals"]["calls"] == 2


def test_record_never_raises_and_a_torn_line_is_skipped(monkeypatch):
    llm_usage.record("briefing", provider="p", usage={"prompt_tokens": 1})
    path = next(llm_usage.ledger_dir().glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "feat')
    assert len(_lines()) == 1
    monkeypatch.setattr(llm_usage, "ledger_dir", lambda: (_ for _ in ()).throw(OSError("full")))
    assert llm_usage.record("briefing") is None


def test_record_shared_writes_one_line_per_owner():
    llm_usage.record_shared("embedding", ["s1", "s2", "s1", ""], [10, 10, 10, 10],
                            {"prompt_tokens": 40}, model="e", purpose="index")
    by = {l["session"]: l["input"] for l in _lines()}
    assert by == {"s1": 20, "s2": 10, "": 10}


def test_a_shared_request_counts_as_one_call():
    # Two embedding requests over three sessions' inputs, the second failed:
    # two calls and one error overall, while each session row counts each
    # request it took part in once (review of claunch-60y0d @ 038f589c).
    llm_usage.record_shared("embedding", ["s1", "s2", "s3"], [1, 1, 1],
                            {"prompt_tokens": 30}, model="e", purpose="index")
    llm_usage.record_shared("embedding", ["s1", "s2", "s3"], [1, 1, 1],
                            None, model="e", purpose="index", ok=False, error="HTTP 500")
    out = llm_usage.overview("today")
    assert out["totals"]["calls"] == 2
    assert out["totals"]["errors"] == 1
    assert out["totals"]["unreported"] == 1
    assert out["totals"]["input"] == 30
    (feature,) = [f for f in out["features"] if f["feature"] == "embedding"]
    assert (feature["calls"], feature["errors"]) == (2, 1)
    assert [(p["provider"], p["calls"]) for p in out["providers"]] == [("unknown", 2)]
    assert sum(d["calls"] for d in out["days"]) == 2
    rows = {s["session"]: (s["calls"], s["errors"], s["input"]) for s in out["sessions"]}
    assert rows == {"s1": (2, 1, 10), "s2": (2, 1, 10), "s3": (2, 1, 10)}
    # A provider-narrowed view keeps the same count; a session-narrowed view
    # counts the requests that session took part in.
    assert llm_usage.overview("today", provider="unknown")["totals"]["calls"] == 2
    one = llm_usage.overview("today", session="s3")
    assert (one["totals"]["calls"], one["totals"]["errors"], one["totals"]["input"]) == (2, 1, 10)


# --------------------------------------------------------------------------- #
# the call sites
# --------------------------------------------------------------------------- #
async def _serve(handler, path):
    app = aioweb.Application()
    app.router.add_post(path, handler)
    server = TestServer(app)
    await server.start_server()
    return server


def test_briefing_call_meters_success_and_failure():
    async def run():
        state = {"status": 200}

        async def chat(request):
            if state["status"] != 200:
                return aioweb.json_response({"error": "x"}, status=state["status"])
            return aioweb.json_response({
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2}})

        server = await _serve(chat, "/v1/chat/completions")
        cfg = {"endpoint": str(server.make_url("/v1/chat/completions")), "model": "bm",
               "api_key": "k", "max_tokens": 64, "params": {}, "provider": ""}
        try:
            await briefing.call_llm(cfg, "p", session="s7", purpose="card")
            state["status"] = 503
            try:
                await briefing.call_llm(cfg, "p", session="s7", purpose="card")
            except briefing.BriefingError:
                pass
            else:
                raise AssertionError("503 must raise")
        finally:
            await server.close()

    asyncio.run(run())
    first, second = _lines()
    assert first["feature"] == "briefing" and first["session"] == "s7"
    assert first["purpose"] == "card" and first["model"] == "bm"
    assert first["provider"] == "127.0.0.1" and first["total"] == 13 and first["ok"]
    assert second["ok"] is False and second["error"] == "HTTP 503"
    assert second["reported"] is False


def test_observer_call_meters_the_session_it_was_about():
    async def run():
        async def chat(request):
            return aioweb.json_response({
                "choices": [{"message": {"content": '{"events": [], "summary": "s"}'},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 4,
                          "prompt_cache_hit_tokens": 40}})

        server = await _serve(chat, "/v1/chat/completions")
        cfg = {"endpoint": str(server.make_url("/v1/chat/completions")), "model": "om",
               "api_key": "k", "provider": "", "profile": "p"}
        try:
            await observer.complete(cfg, [{"role": "user", "content": "x"}], session="s9")
        finally:
            await server.close()

    asyncio.run(run())
    [line] = _lines()
    assert line["feature"] == "observer" and line["session"] == "s9"
    assert line["input"] == 10 and line["cache_read"] == 40 and line["output"] == 4


def test_embedding_is_shared_over_the_documents_sessions():
    async def run():
        async def emb(request):
            body = await request.json()
            rows = [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(body["input"])]
            return aioweb.json_response({"data": rows, "usage": {"prompt_tokens": 30,
                                                                 "total_tokens": 30}})

        server = await _serve(emb, "/v1/embeddings")
        cfg = dict(store.RAG_DEFAULTS)
        cfg.update({"base_url": str(server.make_url("/v1")), "api_key": "k",
                    "embedding_model": "e", "batch": 8})
        try:
            client = rag.RagClient(cfg)
            await client.embed(["aaaa", "bb", "bb"], owners=["s1", "s2", "s2"])
            await client.embed(["query"], purpose="query")
        finally:
            await server.close()

    asyncio.run(run())
    lines = _lines()
    index = {l["session"]: l["input"] for l in lines if l["purpose"] == "index"}
    assert index == {"s1": 15, "s2": 15}
    [query] = [l for l in lines if l["purpose"] == "query"]
    assert query["session"] == "" and query["input"] == 30


def test_rerank_meters_its_model_and_a_bad_body():
    async def run():
        state = {"body": None}

        async def rr(request):
            if state["body"] is not None:
                return aioweb.Response(text=state["body"], status=200)
            return aioweb.json_response({"results": [{"index": 0, "relevance_score": 0.5}],
                                         "usage": {"total_tokens": 7}})

        server = await _serve(rr, "/v1/rerank")
        cfg = dict(store.RAG_DEFAULTS)
        cfg.update({"base_url": str(server.make_url("/v1")), "api_key": "k",
                    "rerank_model": "rr-1"})
        try:
            client = rag.RagClient(cfg)
            assert await client.rerank("q", ["d"], 1) == [(0, 0.5)]
            state["body"] = "<html>"
            try:
                await client.rerank("q", ["d"], 1)
            except rag.RagError:
                pass
            else:
                raise AssertionError("a non-JSON body must raise")
        finally:
            await server.close()

    asyncio.run(run())
    good, bad = _lines()
    assert good["feature"] == "rerank" and good["purpose"] == "query"
    assert good["model"] == "rr-1" and good["ok"]
    assert bad["ok"] is False and bad["error"] == "response is not JSON"


def test_doc_session_reads_who_a_document_is_about():
    doc = rag.Doc("event:s1:e:0", "h", ["t"], {"kind": "observer", "sessions": [{"name": "s1"}]})
    assert rag.doc_session(doc) == "s1"
    assert rag.doc_session(rag.Doc("s4", "h", ["t"], {"name": "s4", "status": "running"})) == "s4"
    issue = rag.Doc("beads:x:i-1:0", "h", ["t"], {"kind": "beads", "sessions": [], "assignee": "s5"})
    assert rag.doc_session(issue) == "s5"
    assert rag.doc_session(rag.Doc("beads:x:i-2:0", "h", ["t"], {"kind": "beads"})) == ""


def test_jev_call_meters_feature_and_session():
    async def run():
        async def one(request):
            return aioweb.json_response({"model": "jev-1.13.0", "answers": {"q": {}},
                                         "usage": {"input_tokens": 8, "output_tokens": 1}})

        server = await _serve(one, "/v1/systemone")
        cfg = {"jev_base_url": str(server.make_url("")).rstrip("/")}
        try:
            await jev.system_one(cfg, {"a": 1}, {"q": {"type": "noul"}}, key="kk",
                                 feature="qa_nudge", session="s3")
        finally:
            await server.close()

    asyncio.run(run())
    [line] = _lines()
    assert line["feature"] == "jev" and line["purpose"] == "qa_nudge"
    assert line["session"] == "s3" and line["model"] == "jev-1.13.0"
    assert line["total"] == 9


def test_briefing_call_meters_a_200_whose_body_is_not_json():
    async def run():
        async def chat(request):
            return aioweb.Response(text="<html>gateway</html>", status=200)

        server = await _serve(chat, "/v1/chat/completions")
        cfg = {"endpoint": str(server.make_url("/v1/chat/completions")), "model": "bm",
               "api_key": "k", "max_tokens": 64, "params": {}, "provider": ""}
        try:
            await briefing.call_llm(cfg, "p", session="s7", purpose="card")
        except briefing.BriefingError as exc:
            assert "not JSON" in str(exc)
        else:
            raise AssertionError("a non-JSON body must raise")
        finally:
            await server.close()

    asyncio.run(run())
    [line] = _lines()
    assert line["session"] == "s7" and line["ok"] is False
    assert line["error"] == "response is not JSON"


# --------------------------------------------------------------------------- #
# the call sites pass who a call is for (claunch-60y0d)
# --------------------------------------------------------------------------- #
def test_briefing_compose_charges_the_session_it_briefs(monkeypatch):
    seen = {}

    async def fake_call(cfg, prompt, **kw):
        seen.update(kw)
        raise briefing.BriefingError("stop here")

    monkeypatch.setattr(briefing, "gather_live", lambda session: None)
    monkeypatch.setattr(briefing, "gather_faq", lambda: [])
    monkeypatch.setattr(briefing, "gather_cflow", lambda cwd, name: None)
    monkeypatch.setattr(briefing, "locate_transcript", lambda sdef: None)
    monkeypatch.setattr(briefing, "build_prompt", lambda *a: "p")
    monkeypatch.setattr(briefing, "call_llm", fake_call)
    session = SimpleNamespace(sdef=SimpleNamespace(name="s8", cwd=""))
    try:
        asyncio.run(briefing.compose(session, {}, refresh=True))
    except briefing.BriefingError:
        pass
    assert seen == {"session": "s8", "purpose": "card"}


class _Client:
    def __init__(self):
        self.calls = []

    async def embed(self, texts, **kw):
        self.calls.append((list(texts), kw))
        return [[1.0, 0.0] for _ in texts]


def test_rag_index_charges_each_chunk_to_its_documents_session():
    group = [rag.Doc("s1", "h", ["a", "b"], {"name": "s1"}),
             rag.Doc("beads:x:i-1:0", "h", ["c"], {"kind": "beads", "assignee": "s5"})]
    client = _Client()
    svc = SimpleNamespace(_put_group=lambda index, group, vecs: None)
    asyncio.run(rag.RagService._embed_group(svc, client, None, group, rag.Progress()))
    [(texts, kw)] = client.calls
    assert texts == ["a", "b", "c"]
    assert kw == {"owners": ["s1", "s1", "s5"]}


def test_rag_query_embedding_is_marked_as_a_query():
    client = _Client()
    svc = SimpleNamespace(_qvecs=OrderedDict())
    asyncio.run(rag.RagService._query_vector(svc, client, {}, "find me"))
    assert client.calls == [(["find me"], {"purpose": "query"})]


def test_megathread_routing_charges_the_megathread_feature(monkeypatch):
    seen = {}

    async def fake(cfg, state, qs, **kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(jev, "system_one", fake)
    asyncio.run(megathread_route.default_ask({}, {}, {}, "m"))
    assert seen == {"model": "m", "feature": "megathread"}


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
def test_route_answers_the_window_and_refuses_an_unknown_one():
    llm_usage.record("observer", provider="ds", session="s1", usage={"prompt_tokens": 3})

    async def run():
        ok = await api.h_stats_llm_usage(make_mocked_request("GET", "/api/stats/llm-usage?window=week"))
        bad = await api.h_stats_llm_usage(make_mocked_request("GET", "/api/stats/llm-usage?window=year"))
        return ok, bad

    ok, bad = asyncio.run(run())
    assert ok.status == 200 and bad.status == 400
    import json
    body = json.loads(ok.text)
    assert body["window"] == "week" and body["totals"]["calls"] == 1
    assert body["sessions"][0]["session"] == "s1"
