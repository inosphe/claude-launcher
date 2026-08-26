"""The session briefing backend: config, transcript tail, LLM call, endpoint.

``GET /api/sessions/{name}/briefing`` turns a session's hybrid evidence
(record + cflow position + transcript tail) into a fixed JSON shape via an
OpenAI-compatible endpoint. These tests pin the four seams: the ``llm:``
config block's parsing, the jsonl tail extractor's caps, the chat/completions
request contract (against a local fake), and the endpoint's frozen response
contract (200/400/404, cache and refresh semantics).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import store, transcripts
from claude_launcher.daemon import briefing
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = "import time\nprint('READY')\ntime.sleep(60)\n"
BEARER = {"Authorization": "Bearer sekrit"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    briefing._cache.clear()
    yield
    briefing._cache.clear()


# --------------------------------------------------------------------------- #
# llm config parsing
# --------------------------------------------------------------------------- #
def test_llm_config_absent_block_is_disabled_defaults(home):
    cfg = briefing.llm_config({})
    assert cfg == {
        "endpoint": "",
        "model": "",
        "api_key": "",
        "max_tokens": briefing.DEFAULT_MAX_TOKENS,
        "params": {},
    }
    assert not briefing.llm_configured(cfg)
    # malformed block: same disabled shape, no crash
    assert not briefing.llm_configured(briefing.llm_config({"llm": "oops"}))


def test_llm_config_empty_api_key_disables(home):
    doc = {"llm": {"endpoint": "https://x/v1/chat/completions", "model": "m"}}
    assert not briefing.llm_configured(briefing.llm_config(doc))
    doc["llm"]["api_key"] = "   "
    assert not briefing.llm_configured(briefing.llm_config(doc))
    doc["llm"]["api_key"] = "sk-abc"
    assert briefing.llm_configured(briefing.llm_config(doc))


def test_llm_config_reads_the_store_and_keeps_params(config_file):
    store.save(
        {
            "llm": {
                "endpoint": "https://api.example/v1/chat/completions",
                "model": "some/model",
                "api_key": "sk-live",
                "max_tokens": 256,
                "params": {"top_k": 40, "temperature": 0.2},
            }
        }
    )
    cfg = briefing.llm_config()
    assert cfg["endpoint"] == "https://api.example/v1/chat/completions"
    assert cfg["max_tokens"] == 256
    assert cfg["params"] == {"top_k": 40, "temperature": 0.2}
    # a non-numeric max_tokens falls back rather than erroring the endpoint
    cfg = briefing.llm_config({"llm": {"max_tokens": "lots"}})
    assert cfg["max_tokens"] == briefing.DEFAULT_MAX_TOKENS


# --------------------------------------------------------------------------- #
# transcript tail extraction
# --------------------------------------------------------------------------- #
def _jl(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def test_tail_events_extracts_text_and_tool_names(tmp_path):
    p = tmp_path / "conv.jsonl"
    lines = [
        _jl(type="user", message={"role": "user", "content": "fix the bug"}),
        "not json at all",
        _jl(
            type="assistant",
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "looking\nat it"},
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ],
            },
        ),
        # tool results ride user entries as blocks; they must not leak in
        _jl(
            type="user",
            message={
                "role": "user",
                "content": [{"type": "tool_result", "content": "big dump"}],
            },
        ),
        _jl(type="summary", summary="ignored"),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events = briefing.tail_events(p)
    assert events == [
        "user: fix the bug",
        "assistant: looking at it",
        "assistant tool_use: Read, Bash",
    ]


def test_tail_events_caps_events_text_and_total(tmp_path):
    p = tmp_path / "conv.jsonl"
    rows = [
        _jl(type="user", message={"role": "user", "content": f"msg {i} " + "x" * 900})
        for i in range(120)
    ]
    p.write_text("\n".join(rows), encoding="utf-8")
    events = briefing.tail_events(p)
    # newest EVENT_TAIL survive, oldest first, each clipped to TEXT_LIMIT
    assert len(events) <= briefing.EVENT_TAIL
    assert events[-1].startswith("user: msg 119 ")
    assert all(len(e) <= briefing.TEXT_LIMIT + len("user: ") + 2 for e in events)
    # a tiny total budget keeps only the newest events
    few = briefing.tail_events(p, total_limit=2000)
    assert few and few[-1].startswith("user: msg 119")
    assert sum(len(e) + 1 for e in few) <= 2000


def test_tail_events_missing_file_is_empty(tmp_path):
    assert briefing.tail_events(tmp_path / "absent.jsonl") == []


# --------------------------------------------------------------------------- #
# prompt gathering
# --------------------------------------------------------------------------- #
def test_build_prompt_carries_all_signals():
    sdef = SessionDef(name="s9", cwd="F:/repo", task="ship the widget")
    cflow_info = {
        "workflow": "improv-worker",
        "status": "step",
        "step": "work",
        "title": "작업 실행",
        "last_report": "intake done",
    }
    prompt = briefing.build_prompt(sdef, cflow_info, ["user: hello", "assistant: hi"])
    for needle in (
        '"state": "working|blocked|waiting|idle|done|unknown"',
        "이름: s9",
        "ship the widget",
        "improv-worker",
        "작업 실행",
        "intake done",
        "user: hello",
    ):
        assert needle in prompt
    # absent signals leave no dangling sections
    bare = briefing.build_prompt(SessionDef(name="s0"), None, [])
    assert "[cflow 런]" not in bare and "[대화 로그 꼬리" not in bare


def test_gather_cflow_maps_status_and_last_report(monkeypatch, tmp_path):
    payload = {
        "run": "run-1",
        "workflow": "improv-worker",
        "status": "step",
        "step_id": "work",
        "title": "작업 실행",
    }
    journal = [
        {"event": "step_report", "summary": "first"},
        {"event": "gate", "summary": "not a report"},
        {"event": "step_report", "summary": "latest"},
    ]
    monkeypatch.setattr(briefing.cflow_engine, "status", lambda cwd, scope=None: payload)
    monkeypatch.setattr(
        briefing.cflow_state, "read_journal", lambda cwd, scope, run_id=None: journal
    )
    info = briefing.gather_cflow(str(tmp_path), "s9")
    assert info == {
        "workflow": "improv-worker",
        "status": "step",
        "step": "work",
        "title": "작업 실행",
        "last_report": "latest",
    }
    # idle slot -> no cflow signal; empty cwd -> never even asks
    monkeypatch.setattr(
        briefing.cflow_engine, "status", lambda cwd, scope=None: {"status": "idle"}
    )
    assert briefing.gather_cflow(str(tmp_path), "s9") is None
    assert briefing.gather_cflow("", "s9") is None


# --------------------------------------------------------------------------- #
# lenient answer parsing
# --------------------------------------------------------------------------- #
def test_parse_briefing_plain_fenced_and_embedded():
    body = {
        "goal": "g", "now": "n", "state": "working", "progress": "p",
        "one-line-job-description": "한 줄 설명",
    }
    expect = dict(body)
    assert briefing.parse_briefing(json.dumps(body)) == expect
    assert briefing.parse_briefing(f"```json\n{json.dumps(body)}\n```") == expect
    assert briefing.parse_briefing(f"Sure! Here it is:\n{json.dumps(body)}\nDone.") == expect


def test_parse_briefing_absent_one_line_is_empty(home):
    # the one-line is optional input — absent, it parses to "" rather than
    # erroring, so an older model's shape still yields a usable briefing
    parsed = briefing.parse_briefing(
        '{"goal": "g", "now": "n", "state": "working", "progress": "p"}'
    )
    assert parsed["one-line-job-description"] == ""
    assert parsed["goal"] == "g"


def test_digest_serves_the_cached_one_line_only(home):
    """digest() is the list's cheap read: nothing composed -> None, and only a
    parsed one-line makes it into the digest — a raw/unshaped result or an
    empty one-line yields no digest (the rail falls back to the record)."""
    assert briefing.digest("s1") is None
    briefing._cache["s1"] = (
        ("key",),
        {"briefing": {"goal": "g", "state": "working",
                       "one-line-job-description": "한 줄"}},
    )
    assert briefing.digest("s1") == {"one_line": "한 줄", "state": "working"}
    # a raw (unshaped) result: no parsed briefing, hence no digest
    briefing._cache["s2"] = (("key",), {"briefing": None, "raw": "prose"})
    assert briefing.digest("s2") is None
    # a parsed briefing with no one-line: nothing to put on the row
    briefing._cache["s3"] = (("key",), {"briefing": {"goal": "g", "state": "idle"}})
    assert briefing.digest("s3") is None


def test_parse_briefing_bad_state_and_garbage():
    parsed = briefing.parse_briefing('{"goal": "g", "state": "SHRUGGING"}')
    assert parsed["state"] == "unknown" and parsed["now"] == ""
    assert briefing.parse_briefing("no json here") is None
    assert briefing.parse_briefing("") is None
    assert briefing.parse_briefing('["a", "list"]') is None


# --------------------------------------------------------------------------- #
# the LLM HTTP contract, against a local fake endpoint
# --------------------------------------------------------------------------- #
async def _start_llm(handler):
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestServer

    app = aioweb.Application()
    app.router.add_post("/v1/chat/completions", handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _llm_answer(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_call_llm_sends_the_openai_shape_and_reads_the_answer(home):
    from aiohttp import web as aioweb

    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return aioweb.json_response(_llm_answer("the answer"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "acc/model",
                "api_key": "sk-test",
                "max_tokens": 128,
                "params": {"top_k": 7},
            }
            got = await briefing.call_llm(cfg, "summarize this")
            assert got == "the answer"
        finally:
            await server.close()

    asyncio.run(run())
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "acc/model"
    assert seen["body"]["max_tokens"] == 128
    assert seen["body"]["top_k"] == 7  # params merged into the body
    assert seen["body"]["messages"] == [{"role": "user", "content": "summarize this"}]


def test_call_llm_http_error_and_bad_shape_raise(home):
    from aiohttp import web as aioweb

    async def failing(request):
        return aioweb.json_response({"error": "nope"}, status=500)

    async def shapeless(request):
        return aioweb.json_response({"choices": []})

    async def run():
        for handler, fragment in ((failing, "answered 500"), (shapeless, "choices")):
            server = await _start_llm(handler)
            try:
                cfg = {
                    "endpoint": str(server.make_url("/v1/chat/completions")),
                    "model": "m",
                    "api_key": "k",
                    "max_tokens": 64,
                    "params": {},
                }
                with pytest.raises(briefing.BriefingError) as err:
                    await briefing.call_llm(cfg, "hi")
                assert fragment in str(err.value)
                # the key must never surface through the error path
                assert "k" == cfg["api_key"] and cfg["api_key"] not in str(err.value)
            finally:
                await server.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the endpoint: frozen response contract, cache, refresh
# --------------------------------------------------------------------------- #
def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _set_llm(endpoint: str) -> None:
    store.update(
        lambda doc: doc.update(
            {"llm": {"endpoint": endpoint, "model": "m", "api_key": "sk", "params": {}}}
        )
    )


async def _serve(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(
        mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr)
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_briefing_endpoint_contract_cache_and_refresh(home, tmp_path):
    from aiohttp import web as aioweb

    _register_py_harness()
    hits = []
    answer = {
        "goal": "목표", "now": "작업 중", "state": "working", "progress": "70%",
        "one-line-job-description": "브리핑 백엔드",
    }

    async def handler(request):
        hits.append(await request.json())
        return aioweb.json_response(_llm_answer(json.dumps(answer, ensure_ascii=False)))

    async def run():
        llm = await _start_llm(handler)
        _set_llm(str(llm.make_url("/v1/chat/completions")))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            cwd = str(tmp_path / "work")
            (tmp_path / "work").mkdir()
            mgr.create(
                SessionDef(
                    name="s1", harness="py", cwd=cwd,
                    conversation_id="cafe0000-0000-0000-0000-000000000001",
                    task="브리핑 백엔드 구현",
                )
            )
            # the transcript claude would keep for this conversation, under
            # the config dir the conftest points CLAUDE_CONFIG_DIR at
            pdir = transcripts.project_dir(
                tmp_path / ".claude-config", cwd
            )
            pdir.mkdir(parents=True)
            (pdir / "cafe0000-0000-0000-0000-000000000001.jsonl").write_text(
                _jl(type="user", message={"role": "user", "content": "please build it"})
                + "\n",
                encoding="utf-8",
            )

            resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["session"] == "s1"
            assert body["cached"] is False
            assert body["generated_at"]
            assert body["source"] == {"jsonl": True, "cflow": False}
            assert body["briefing"] == answer
            assert body["raw"] is None
            # the prompt actually carried the evidence
            sent = hits[0]["messages"][0]["content"]
            assert "please build it" in sent and "브리핑 백엔드 구현" in sent

            # unchanged inputs: served from cache, no second LLM call
            resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
            body = await resp.json()
            assert body["cached"] is True and len(hits) == 1

            # refresh=1 bypasses the cache
            resp = await client.get(
                "/api/sessions/s1/briefing?refresh=1", headers=BEARER
            )
            body = await resp.json()
            assert body["cached"] is False and len(hits) == 2

            await mgr.shutdown_all()
        finally:
            await client.close()
            await llm.close()

    asyncio.run(run())


def test_briefing_endpoint_unconfigured_unknown_and_raw(home, tmp_path):
    from aiohttp import web as aioweb

    _register_py_harness()

    async def rambling(request):
        return aioweb.json_response(_llm_answer("I cannot answer in JSON, sorry"))

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))

            # no llm: block at all -> the frozen 400
            resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
            assert resp.status == 400
            assert (await resp.json()) == {"error": "llm not configured"}

            # unknown session -> 404, and it wins over the config check
            resp = await client.get("/api/sessions/nope/briefing", headers=BEARER)
            assert resp.status == 404

            # a model that ignores the JSON instruction: raw, not an error
            llm = await _start_llm(rambling)
            try:
                _set_llm(str(llm.make_url("/v1/chat/completions")))
                resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
                assert resp.status == 200
                body = await resp.json()
                assert body["briefing"] is None
                assert body["raw"] == "I cannot answer in JSON, sorry"
                assert body["source"] == {"jsonl": False, "cflow": False}
            finally:
                await llm.close()

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_sessions_list_says_whether_llm_is_configured(home, tmp_path):
    """The session list carries ``llm_configured`` so the web rail can show
    the briefing toggles disabled (with the why) before anyone clicks one."""

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            # no llm: block -> the rail is told the feature is off
            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            assert (await resp.json())["llm_configured"] is False

            # writing the config flips the very next poll, no restart
            _set_llm("http://llm.example/v1/chat/completions")
            resp = await client.get("/api/sessions", headers=BEARER)
            assert (await resp.json())["llm_configured"] is True
        finally:
            await client.close()

    asyncio.run(run())


def test_sessions_list_survives_a_config_it_cannot_read(home, tmp_path):
    """An unreadable config costs the caller one toggle, never the rail.

    ``llm_configured`` is recomputed from disk on every poll (that is what
    makes writing the config flip the very next one, above), so a file that
    cannot be parsed reaches this handler as a ``StoreError`` — which
    ``error_middleware`` does not list, and which would therefore turn the
    one request the web UI cannot do without into a 500. The rail rebuilds
    its entire list off this response, so a poll that fails takes every row
    with it; the toggle it could not answer for is the cheaper thing to
    lose.
    """

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            # Broken *after* the session exists — the harness registry lives
            # in this same file, and the point is a config that goes bad
            # under a daemon that is already running.
            store.path().write_text("{ this: is: not: valid", encoding="utf-8")

            resp = await client.get("/api/sessions", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert [s["name"] for s in body["sessions"]] == ["s1"]
            assert body["llm_configured"] is False

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_sessions_list_attaches_the_cached_briefing_digest(home, tmp_path):
    """The list poll pours each session's cached one-liner: a rail row can
    show it folded or open, and a browser refresh repaints it from the
    daemon's session state instead of regenerating — the list itself never
    calls the LLM, it only reads what compose() already left."""

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            cwd = str(tmp_path / "work")
            (tmp_path / "work").mkdir()
            mgr.create(
                SessionDef(
                    name="s1", harness="py", cwd=cwd,
                    task="브리핑을 한 줄로 담기",
                )
            )
            # nothing composed yet: no digest, but the recorded task is there
            # for the row to fall back on
            resp = await client.get("/api/sessions", headers=BEARER)
            row = (await resp.json())["sessions"][0]
            assert "briefing" not in row
            assert row["task"] == "브리핑을 한 줄로 담기"

            # what one compose() would have left in the cache — the very next
            # poll carries it as the digest, with no LLM call involved
            briefing._cache["s1"] = (
                ("key",),
                {"session": "s1", "briefing": {
                    "goal": "g", "state": "waiting",
                    "one-line-job-description": "한 줄",
                }},
            )
            resp = await client.get("/api/sessions", headers=BEARER)
            row = (await resp.json())["sessions"][0]
            assert row["briefing"] == {"one_line": "한 줄", "state": "waiting"}

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
