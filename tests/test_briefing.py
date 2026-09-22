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

from claude_launcher import harnesses, profile, store, transcripts
from claude_launcher.daemon import briefing, paths, prompt_presets
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
def _profile_backend_config(
    name: str = "brief",
    *,
    provider: str = "ds",
    api_key: str = "provider-secret",
    models=None,
    endpoints=None,
):
    """A profile whose provider describes an OpenAI-compatible backend.

    The default ``models.default`` carries Claude's ``[1m]`` tag on purpose:
    it is what a real profile aimed at a 1M-context backend records, and an
    OpenAI endpoint asked for the tagged id answers 404.
    """
    prof = profile.create(name)

    def mutate(doc):
        doc.setdefault("providers", {})[provider] = {
            "api_key": api_key,
            "endpoints": {"openai": "https://example.test/v1"}
            if endpoints is None
            else endpoints,
            "models": {"default": "deepseek-flash[1m]", "small": "glm-small"}
            if models is None
            else models,
        }
        doc.setdefault("profiles", {}).setdefault(name, {})["provider"] = provider

    store.update(mutate)
    return prof


def test_llm_config_absent_block_is_disabled_defaults(home):
    cfg = briefing.llm_config({})
    assert cfg == {
        "profile": "",
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


def test_profile_backend_reads_endpoint_key_and_models(config_file):
    """A profile is resolved the way the Observer resolves its own."""
    _profile_backend_config()
    backend = briefing.profile_backend("brief")
    assert backend.endpoint == "https://example.test/v1/chat/completions"
    assert backend.api_key == "provider-secret"
    # the tag is stripped, and 'default' leads the suggestion list
    assert backend.model == "deepseek-flash"
    assert backend.models == ("deepseek-flash", "glm-small")


def test_profile_backend_refuses_a_profile_with_no_openai_endpoint(config_file):
    """Anthropic-only is a real provider shape, and not one this call speaks."""
    _profile_backend_config(endpoints={"anthropic": "https://example.test/anthropic"})
    with pytest.raises(briefing.BriefingError, match="OpenAI-compatible endpoint"):
        briefing.profile_backend("brief")
    with pytest.raises(briefing.BriefingError, match="does not exist"):
        briefing.profile_backend("no-such-profile")


def test_llm_config_takes_the_backend_from_the_named_profile(config_file):
    _profile_backend_config()
    store.update(lambda doc: doc.update({"llm": {"profile": "brief"}}))
    cfg = briefing.llm_config()
    assert cfg["profile"] == "brief"
    assert cfg["endpoint"] == "https://example.test/v1/chat/completions"
    assert cfg["api_key"] == "provider-secret"
    assert cfg["model"] == "deepseek-flash"
    assert briefing.llm_configured(cfg)


def test_llm_config_profile_owns_the_endpoint_key_pair_and_not_the_model(config_file):
    """The block's endpoint/key are dropped, its model is kept.

    The endpoint and the key identify one backend together, so a key typed
    for the direct form must not be sent to the profile's endpoint. The model
    is this feature's own choice about that backend, so the field the
    Settings card writes wins over the profile's default.
    """
    _profile_backend_config()
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "profile": "brief",
                    "model": "glm-small",
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["endpoint"] == "https://example.test/v1/chat/completions"
    assert cfg["api_key"] == "provider-secret"
    assert cfg["model"] == "glm-small"


def test_llm_config_unresolvable_profile_leaves_the_feature_off(config_file):
    """A hand-edited name that cannot serve turns it off rather than raising.

    The direct fields are not used as a fallback either: the profile is the
    operator's statement of which backend to call, and quietly calling the
    previous one instead would be worse than composing nothing.
    """
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "profile": "gone",
                    "model": "m",
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert (cfg["endpoint"], cfg["api_key"]) == ("", "")
    assert not briefing.llm_configured(cfg)


def test_llm_config_direct_endpoint_still_works(config_file):
    """The pre-profile form of the block is unchanged."""
    store.update(
        lambda doc: doc.update(
            {
                "llm": {
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "m",
                    "api_key": "sk-typed",
                }
            }
        )
    )
    cfg = briefing.llm_config()
    assert cfg["profile"] == ""
    assert cfg["endpoint"] == "https://typed.example/v1/chat/completions"
    assert cfg["api_key"] == "sk-typed"
    assert briefing.llm_configured(cfg)


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


def test_codex_rollout_is_located_and_its_messages_are_extracted(tmp_path):
    prof = profile.create("codex-brief")
    codex = harnesses.get("codex")
    assert codex is not None
    rollout = (
        codex.profile_home(prof.config_dir)
        / "sessions"
        / "2026"
        / "08"
        / "29"
        / "rollout.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rows = [
        _jl(type="session_meta", payload={"id": "codex-id", "cwd": str(tmp_path)}),
        _jl(
            type="response_item",
            payload={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fix the Codex briefing"}],
            },
        ),
        _jl(
            type="response_item",
            payload={
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c1",
            },
        ),
        _jl(
            type="response_item",
            payload={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "reading the rollout"}],
            },
        ),
    ]
    rollout.write_text("\n".join(rows) + "\n", encoding="utf-8")
    sdef = SessionDef(
        name="codex-session",
        harness="codex",
        profile=prof.name,
        cwd=str(tmp_path),
        conversation_id="codex-id",
    )

    assert briefing.locate_transcript(sdef) == rollout
    assert briefing.tail_events(rollout) == [
        "user: fix the Codex briefing",
        "assistant tool_use: exec",
        "assistant: reading the rollout",
    ]


def test_an_empty_transcript_does_not_shadow_the_written_one(tmp_path):
    """briefing reads the same predicate a restore reads.

    It used to spell its own: ``is_file()`` on the strict address. A
    zero-byte jsonl there answered yes and the id search never ran, so a
    conversation written under a slug this module spells differently read as
    an empty transcript instead of being found. The canonical predicate had
    already stopped counting an empty file as a conversation, and the two
    answered opposite things about the same session
    (claunch-fork-family-recheck-amnqg.1).
    """
    prof = profile.create("brief-empty")
    cid = "d0d0d0d0-0000-0000-0000-000000000001"
    cwd = tmp_path / "here"
    cwd.mkdir()

    strict = transcripts.project_dir(prof.config_dir, str(cwd))
    strict.mkdir(parents=True)
    (strict / f"{cid}.jsonl").write_bytes(b"")

    elsewhere = prof.config_dir / "projects" / "spelled--differently"
    elsewhere.mkdir(parents=True)
    written = elsewhere / f"{cid}.jsonl"
    written.write_text(
        _jl(type="user", message={"role": "user", "content": "it is here"})
        + "\n",
        encoding="utf-8",
    )

    sdef = SessionDef(
        name="brief-empty-session",
        profile=prof.name,
        cwd=str(cwd),
        conversation_id=cid,
    )

    assert briefing.locate_transcript(sdef) == written
    assert briefing.tail_events(written) == ["user: it is here"]
    # the same session, asked the restore's question: one answer, not two
    assert transcripts.exists(prof.config_dir, cid, str(cwd)) is True
    # the shallow caller cannot afford the id scan, and reads the zero-byte
    # file as absent rather than as a conversation with nothing in it
    assert briefing.locate_transcript(sdef, deep=False) is None


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
    prompt = briefing.build_prompt(
        sdef,
        cflow_info,
        ["user: hello", "assistant: hi"],
        {
            "status": "busy",
            "running": True,
            "last_input_at": "2026-08-30T12:00:00+00:00",
            "last_output_at": "2026-08-30T12:00:01+00:00",
        },
        faq=[{"question": "어떤 브랜치인가?"}],
    )
    for needle in (
        '"state": "working|blocked|waiting|idle|done|unknown"',
        "이름: s9",
        "ship the widget",
        "improv-worker",
        "작업 실행",
        "intake done",
        "user: hello",
        "[데몬 실시간 상태]",
        "상태: busy",
        "opening task 안에 포함된 과거 요약 문구",
        "모든 필드 값은 자료의 언어와",
        "자연스럽고 정확한 한국어",
        "변경하면 안 되는 기술적 값은 원문을 유지한다",
        '"faq": [{"question": "사용자 FAQ 질문"',
        "어떤 브랜치인가?",
        "각 질문에 대해 현재 세션 자료에 근거한 답변",
    ):
        assert needle in prompt


def test_gather_live_reads_the_same_state_as_the_session_rail():
    class Session:
        exited = False
        last_input_at = "in"
        last_output_at = "out"

        @staticmethod
        def status():
            return "busy"

    assert briefing.gather_live(Session()) == {
        "status": "busy",
        "running": True,
        "last_input_at": "in",
        "last_output_at": "out",
    }
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


def test_digest_serves_the_cached_briefing_fields(home):
    """digest() is the list's cheap read: nothing composed -> None, a
    raw/unshaped result -> None, and a parsed briefing -> its short fields.

    The one-line is what the rail row draws and goal/now/progress are what a
    session tab's hover tooltip draws, so the digest carries both and omits
    the fields the summariser left empty. A briefing with no one-line still
    has a tooltip's worth of text, so it yields a digest; one with no text at
    all does not."""
    assert briefing.digest("s1") is None
    briefing._cache["s1"] = (
        ("key",),
        {"briefing": {"goal": "g", "now": "n", "progress": "", "state": "working",
                       "one-line-job-description": "한 줄"}},
    )
    assert briefing.digest("s1") == {
        "one_line": "한 줄", "state": "working", "goal": "g", "now": "n",
    }
    # a raw (unshaped) result: no parsed briefing, hence no digest
    briefing._cache["s2"] = (("key",), {"briefing": None, "raw": "prose"})
    assert briefing.digest("s2") is None
    # no one-line, but a goal the tooltip can show
    briefing._cache["s3"] = (("key",), {"briefing": {"goal": "g", "state": "idle"}})
    assert briefing.digest("s3") == {"one_line": "", "state": "idle", "goal": "g"}
    # every field empty: nothing for either surface to draw
    briefing._cache["s4"] = (("key",), {"briefing": {"goal": "", "state": "idle"}})
    assert briefing.digest("s4") is None


def test_digest_clips_long_fields(home):
    """One digest per session rides every list poll, and both of its readers
    are a glance, so each field is clipped to a bounded length — the card is
    where the summariser's full sentence is read."""
    briefing._cache["s1"] = (
        ("key",),
        {"briefing": {"goal": "가" * 400, "state": "working",
                       "one-line-job-description": "나" * 400}},
    )
    d = briefing.digest("s1")
    limit = briefing.DIGEST_FIELD_CHARS
    assert d["goal"].startswith("가" * limit) and len(d["goal"]) <= limit + 2
    assert d["one_line"].startswith("나" * limit)


def test_briefing_cache_survives_daemon_restart(home):
    key = ("s1", (123, 456), "work", "busy", None, None, (("faq", "Q", "A", True),))
    result = {
        "session": "s1", "generated_at": "2026-01-01T00:00:00+00:00",
        "cached": False, "source": {"jsonl": True, "cflow": False},
        "briefing": {
            "goal": "목표", "now": "진행", "state": "working",
            "progress": "50%", "one-line-job-description": "작업",
        }, "raw": None,
    }
    briefing._cache["s1"] = (key, result)
    briefing._persist_cache()

    # A new daemon process starts with an empty in-memory cache.
    briefing._cache.clear()
    briefing._loaded_cache_path = None
    assert briefing.digest("s1") == {
        "one_line": "작업", "state": "working",
        "goal": "목표", "now": "진행", "progress": "50%",
    }


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


def _llm_answer(content: str, finish: str = "stop", spent: int = 42) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {"completion_tokens": spent},
    }


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
            assert got.text == "the answer"
            assert got.finish_reason == "stop"
            assert got.completion_tokens == 42
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


def test_call_llm_empty_content_is_an_error_not_an_answer(home):
    """A reasoning model that spent max_tokens thinking returns 200 + "".

    Measured against the configured endpoint at ``max_tokens=1024``: 8 of 10
    calls came back exactly like this. Passing "" on made the daemon cache a
    blank briefing and answer 200, which is why the feature looked flaky
    rather than broken.
    """
    from aiohttp import web as aioweb

    async def run():
        for content in ("", "   "):
            async def handler(request, _c=content):
                return aioweb.json_response(_llm_answer(_c, finish="length", spent=1024))

            server = await _start_llm(handler)
            try:
                cfg = {
                    "endpoint": str(server.make_url("/v1/chat/completions")),
                    "model": "m",
                    "api_key": "sk-not-in-the-message",
                    "max_tokens": 1024,
                    "params": {},
                }
                with pytest.raises(briefing.BriefingError) as err:
                    await briefing.call_llm(cfg, "hi")
                msg = str(err.value)
                assert "empty content" in msg
                # the message must name the budget, or nobody can act on it
                assert "1024" in msg and "max_tokens" in msg
                assert cfg["api_key"] not in msg
            finally:
                await server.close()

    asyncio.run(run())


def test_call_llm_blames_the_provider_not_the_budget_when_it_just_stops(home):
    """An empty answer that was NOT cut off is a different fault.

    Measured live while re-checking the budget fix: one session's call came
    back ``finish_reason="stop"`` with ``completion_tokens=1`` and no content.
    Raising ``max_tokens`` cannot touch that, so the error must not send the
    reader to that knob — the two empties look identical without this field.
    """
    from aiohttp import web as aioweb

    async def handler(request):
        return aioweb.json_response(_llm_answer("", finish="stop", spent=1))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "sk-quiet",
                "max_tokens": 4096,
                "params": {},
            }
            with pytest.raises(briefing.BriefingError) as err:
                await briefing.call_llm(cfg, "hi")
            msg = str(err.value)
            assert "provider-side empty completion" in msg
            assert "raise llm.max_tokens" not in msg  # the wrong knob
            assert "finish_reason='stop'" in msg and "completion_tokens=1" in msg
        finally:
            await server.close()

    asyncio.run(run())


def test_call_llm_keeps_a_whole_answer_whatever_the_finish_reason(home):
    """``finish_reason`` is read, not obeyed: real text still comes back."""
    from aiohttp import web as aioweb

    async def handler(request):
        return aioweb.json_response(_llm_answer("plenty of words", finish="length"))

    async def run():
        server = await _start_llm(handler)
        try:
            cfg = {
                "endpoint": str(server.make_url("/v1/chat/completions")),
                "model": "m",
                "api_key": "k",
                "max_tokens": 8,
                "params": {},
            }
            got = await briefing.call_llm(cfg, "hi")
            assert got.text == "plenty of words"
            assert got.finish_reason == "length"
        finally:
            await server.close()

    asyncio.run(run())


def test_default_max_tokens_budgets_for_reasoning_not_just_the_answer():
    """The briefing runs a few hundred characters; the budget is not sized
    for the briefing.

    Pinned because the old 1024 was sized for the answer alone and the
    reasoning tokens are billed to the same allowance — the measured failure.
    """
    assert briefing.DEFAULT_MAX_TOKENS >= 4096


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


def test_briefing_endpoint_contract_cache_and_refresh(home, tmp_path, monkeypatch):
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

            # The cache key carries this session's live state -- status,
            # last_input_at, last_output_at (bd9f2e02) -- and a session that
            # has only just started moves through that state on its own: the
            # harness child's first output flips status from 'starting' to
            # 'busy'/'idle' and fills last_output_at.  A second request that
            # lands after that arrival then misses a cache the first request
            # correctly filled, and the endpoint is right both times.
            # Measured here: with no delay between the two GETs the key held;
            # with 0.6s or 1.5s injected it differed on exactly those two
            # fields, and the assertion below failed 5/5.  What this test
            # pins is that an UNCHANGED key is served from cache, so the live
            # block is stated as a premise rather than raced against the
            # child's first paint.
            monkeypatch.setattr(
                briefing,
                "gather_live",
                lambda session: {
                    "status": "idle",
                    "running": True,
                    "last_input_at": None,
                    "last_output_at": None,
                },
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


def test_briefing_cache_is_invalidated_when_the_live_state_moves(
    home, tmp_path, monkeypatch
):
    """A change in the session's live state forces a fresh briefing.

    This pins the reason ``bd9f2e02`` put ``status``, ``last_input_at`` and
    ``last_output_at`` into the cache key: a briefing describes a session
    that is running right now, so a cached one goes stale the moment that
    state moves. Take any of the three back out of the key and this test
    goes red on that field — which is what stops the next reader from
    dropping them to make a cache hit easier to assert.

    The state is stated (a stubbed :func:`briefing.gather_live`) rather than
    driven through a real harness, because the values are the input under
    test here and a live child moves them on its own schedule.
    """
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

    base = {
        "status": "busy",
        "running": True,
        "last_input_at": "2026-01-01T00:00:00+00:00",
        "last_output_at": "2026-01-01T00:00:01+00:00",
    }

    live = dict(base)

    def pin(**changes):
        # changes ACCUMULATE, so every step below moves exactly one field
        # away from the state the previous request was served on. Restating
        # the whole dict each time would move two at once and let a key that
        # had dropped one of the fields still look correct.
        live.update(changes)
        state = dict(live)
        monkeypatch.setattr(briefing, "gather_live", lambda session: dict(state))

    async def run():
        llm = await _start_llm(handler)
        _set_llm(str(llm.make_url("/v1/chat/completions")))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)

        async def get():
            resp = await client.get("/api/sessions/s1/briefing", headers=BEARER)
            assert resp.status == 200
            return await resp.json()

        try:
            cwd = str(tmp_path / "work")
            (tmp_path / "work").mkdir()
            mgr.create(
                SessionDef(
                    name="s1", harness="py", cwd=cwd,
                    conversation_id="cafe0000-0000-0000-0000-000000000002",
                    task="브리핑 백엔드 구현",
                )
            )

            pin()
            assert (await get())["cached"] is False and len(hits) == 1
            # the same state twice: the key holds, so no second call
            pin()
            assert (await get())["cached"] is True and len(hits) == 1

            # each of the three live fields invalidates on its own
            pin(last_output_at="2026-01-01T00:00:09+00:00")
            assert (await get())["cached"] is False and len(hits) == 2
            pin(status="idle")
            assert (await get())["cached"] is False and len(hits) == 3
            pin(last_input_at="2026-01-01T00:00:08+00:00")
            assert (await get())["cached"] is False and len(hits) == 4

            # back to the original state: the cache holds ONE entry per
            # session (``_cache[name] = (key, result)``), so the entry now
            # carries the last state and the original one is a miss too
            pin(**base)
            assert (await get())["cached"] is False and len(hits) == 5

            await mgr.shutdown_all()
        finally:
            await client.close()
            await llm.close()

    asyncio.run(run())



def test_briefing_faq_can_be_managed_and_is_persisted(home, tmp_path):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.post(
                "/api/briefing/faq", json={"question": "Q", "answer": "A"},
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()
            row = body["entry"]
            assert row["question"] == "Q" and row["enabled"] is True

            resp = await client.get("/api/briefing/faq", headers=BEARER)
            assert (await resp.json())["faq"] == [row]
            resp = await client.put(
                f"/api/briefing/faq/{row['id']}",
                json={**row, "answer": "A2", "enabled": False}, headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["entry"]["answer"] == "A2"
            resp = await client.delete(
                f"/api/briefing/faq/{row['id']}", headers=BEARER
            )
            assert resp.status == 200
            assert (await resp.json())["faq"] == []
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_briefing_faq_is_independent_of_the_global_config(home, tmp_path):
    """FAQ writes do not need to replace the user-wide settings file."""

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            store.path().write_text("{ this: is: not: valid", encoding="utf-8")
            resp = await client.get("/api/briefing/faq", headers=BEARER)
            assert resp.status == 200 and (await resp.json())["faq"] == []
            resp = await client.post(
                "/api/briefing/faq", json={"question": "Q"}, headers=BEARER,
            )
            assert resp.status == 201
            row = (await resp.json())["entry"]
            resp = await client.put(
                f"/api/briefing/faq/{row['id']}",
                json={**row, "answer": "A"}, headers=BEARER,
            )
            assert resp.status == 200
            resp = await client.delete(
                f"/api/briefing/faq/{row['id']}", headers=BEARER,
            )
            assert resp.status == 200
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_faq_imports_the_legacy_global_setting(home):
    store.save({"briefing": {"faq": [{"question": "기존 질문"}]}})

    entries = briefing.faq_entries()

    assert entries[0]["question"] == "기존 질문"
    assert json.loads(paths.briefing_faq_json().read_text(encoding="utf-8")) == {
        "faq": entries
    }


def test_prompt_presets_can_be_managed_and_are_daemon_local(home):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.post(
                "/api/prompt-presets",
                json={"name": "Review", "text": "Please review this change."},
                headers=BEARER,
            )
            assert resp.status == 201
            row = (await resp.json())["preset"]
            assert row["name"] == "Review" and row["enabled"] is True

            resp = await client.get("/api/prompt-presets", headers=BEARER)
            assert (await resp.json())["presets"] == [row]

            resp = await client.put(
                f"/api/prompt-presets/{row['id']}",
                json={**row, "text": "Please review the latest change.", "enabled": False},
                headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["preset"]["enabled"] is False
            assert prompt_presets.entries()[0]["text"] == "Please review the latest change."

            resp = await client.delete(
                f"/api/prompt-presets/{row['id']}", headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["presets"] == []
        finally:
            await mgr.shutdown_all()
            await client.close()

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


def test_briefing_endpoint_502s_on_a_truncated_answer(home, tmp_path):
    """Cut off at the budget is a failure, not a ``raw`` briefing.

    The live symptom this pins: one of 30 sessions came back with 68 bytes of
    a JSON object that stops mid-string, and the endpoint served it 200 with
    ``raw`` set — indistinguishable, to the UI and to the cache, from a model
    that simply answered in prose. ``finish_reason`` tells them apart, and
    so does ``completion_tokens``: over the 48-call budget sweep it equalled
    ``max_tokens`` in 5 of 5 cut-off calls and in 0 of 43 that finished (the
    largest of those spent 1855). This test pins the first because that is
    the field the contract defines for the purpose; the second is a
    corroborator, not a substitute.
    """
    from aiohttp import web as aioweb

    async def cut_off(request):
        return aioweb.json_response(
            _llm_answer('{"goal": "half a sen', finish="length", spent=1024)
        )

    async def empty(request):
        return aioweb.json_response(_llm_answer("", finish="length", spent=1024))

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            for handler, fragment in ((cut_off, "truncated"), (empty, "empty content")):
                llm = await _start_llm(handler)
                try:
                    _set_llm(str(llm.make_url("/v1/chat/completions")))
                    resp = await client.get(
                        "/api/sessions/s1/briefing?refresh=1", headers=BEARER
                    )
                    assert resp.status == 502
                    assert fragment in (await resp.json())["error"]
                finally:
                    await llm.close()
            # nothing was cached on the way out: the next good answer serves
            assert briefing.digest("s1") is None
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
            assert row["briefing"] == {
                "one_line": "한 줄", "state": "waiting", "goal": "g",
            }

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the settings endpoint the web dashboard's Briefing model card edits
# --------------------------------------------------------------------------- #
def test_briefing_llm_settings_endpoint_reads_and_saves(home):
    """``GET``/``PUT /api/briefing/llm``: the card's two calls.

    What is pinned here is the contract the card draws from — the written
    fields beside the resolved ones, the profile list it offers, and that the
    key is reported as a boolean and never returned.
    """
    _profile_backend_config()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.get("/api/briefing/llm", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["profile"] == "" and body["configured"] is False
            assert body["api_key_set"] is False
            assert [row["name"] for row in body["profiles"]] == ["brief"]
            assert body["profiles"][0]["models"] == ["deepseek-flash", "glm-small"]
            assert body["profiles"][0]["has_key"] is True

            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"profile": "brief", "model": "glm-small", "max_tokens": 2048},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["configured"] is True
            assert body["resolved"] == {
                "endpoint": "https://example.test/v1/chat/completions",
                "model": "glm-small",
                "has_key": True,
            }
            # no secret crosses the boundary, in either direction of the save
            assert "api_key" not in json.dumps(body) or body["api_key_set"] is False

            # and it is the file that changed, so the next reader agrees
            assert briefing.llm_config()["model"] == "glm-small"
            resp = await client.get("/api/briefing/llm", headers=BEARER)
            assert (await resp.json())["profile"] == "brief"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_settings_key_handling_and_refusals(home):
    """The api_key convention, and what a save refuses.

    A blank password field arrives on every reload, so blank keeps the stored
    key and ``null`` is the explicit removal. The refusals are the values a
    form cannot produce but a script can: an unknown field, an unknown
    profile, a URL carrying credentials, a budget that is not a number.
    """
    _profile_backend_config()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={
                    "endpoint": "https://typed.example/v1/chat/completions",
                    "model": "m",
                    "api_key": "sk-typed",
                },
            )
            assert resp.status == 200 and (await resp.json())["api_key_set"] is True

            # blank keeps it
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER, json={"api_key": ""},
            )
            assert (await resp.json())["api_key_set"] is True
            assert briefing.llm_config()["api_key"] == "sk-typed"

            # null removes it
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER, json={"api_key": None},
            )
            body = await resp.json()
            assert body["api_key_set"] is False and body["configured"] is False
            assert briefing.llm_config()["api_key"] == ""

            for payload, fragment in [
                ({"params": {"temperature": 0.2}}, "unsupported"),
                ({"profile": "no-such-profile"}, "does not exist"),
                ({"endpoint": "https://user:pw@host/v1"}, "without credentials"),
                ({"endpoint": "ftp://host/v1"}, "without credentials"),
                ({"max_tokens": "lots"}, "whole number"),
                ({"max_tokens": 0}, "between 1"),
            ]:
                resp = await client.put(
                    "/api/briefing/llm", headers=BEARER, json=payload,
                )
                assert resp.status == 400, payload
                assert fragment in (await resp.json())["error"], payload
            # a refused save changed nothing
            assert briefing.llm_config()["model"] == "m"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_briefing_llm_save_drops_cached_briefings_when_the_backend_changes(home):
    """A model change takes effect on the next card, not on the next commit.

    The cache key is a session's evidence, so a briefing written by the
    previous model would otherwise stay on the card until that session's
    transcript moved. A save that leaves the resolved backend alone (here,
    the token budget) keeps the cache, because the answers are still that
    model's.
    """
    _profile_backend_config()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            seed = lambda: briefing._cache.__setitem__(
                "s1",
                (("key",), {"session": "s1", "briefing": {
                    "goal": "g", "state": "working",
                    "one-line-job-description": "한 줄",
                }}),
            )
            seed()
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER,
                json={"profile": "brief", "model": "glm-small"},
            )
            assert resp.status == 200
            assert briefing.digest("s1") is None

            seed()
            resp = await client.put(
                "/api/briefing/llm", headers=BEARER, json={"max_tokens": 8192},
            )
            assert resp.status == 200
            assert briefing.digest("s1") is not None

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
