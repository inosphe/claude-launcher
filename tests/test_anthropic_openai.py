"""Anthropic Messages -> OpenAI Chat Completions translation (claunch-1tcgq).

Two layers: the pure mapping in :mod:`claude_launcher.anthropic_openai`, and
the shim (:func:`routing_shim.build_app`, in-process) in front of a fake
OpenAI upstream, switched on by ``providers.<name>.anthropic_translation``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import anthropic_openai as ao
from claude_launcher import provider_install, routing, routing_shim, store


# --------------------------------------------------------------------------- #
# request
# --------------------------------------------------------------------------- #
def test_request_maps_system_messages_tools_and_parameters():
    body = {
        "model": "deepseek/deepseek-v4.1-flash",
        "max_tokens": 1024,
        "temperature": 0.2,
        "top_k": 5,
        "stop_sequences": ["END"],
        "system": [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
        "tools": [
            {"name": "Read", "description": "read a file",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"type": "web_search_20250305", "name": "web_search"},
        ],
        "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "file body"}]},
                {"type": "text", "text": "now summarise"},
            ]},
        ],
        "stream": True,
    }
    out = ao.request(body)
    assert out["model"] == "deepseek/deepseek-v4.1-flash"
    assert out["max_tokens"] == 1024
    assert out["temperature"] == 0.2
    assert "top_k" not in out
    assert out["stop"] == ["END"]
    assert out["stream"] is True and out["stream_options"] == {"include_usage": True}
    assert out["tool_choice"] == "required"
    assert out["parallel_tool_calls"] is False
    assert out["tools"] == [{"type": "function", "function": {
        "name": "Read", "description": "read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    assert out["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "reading", "tool_calls": [{
            "id": "toolu_1", "type": "function",
            "function": {"name": "Read", "arguments": '{"path": "a"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "file body"},
        {"role": "user", "content": "now summarise"},
    ]


def test_request_carries_images_and_error_results():
    out = ao.request({"model": "m", "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
            {"type": "text", "text": "what is this"},
        ]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": "boom"},
        ]},
    ]})
    assert out["messages"][0] == {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "what is this"},
    ]}
    assert out["messages"][1]["content"] is None
    assert out["messages"][2] == {"role": "tool", "tool_call_id": "t", "content": "Error: boom"}


@pytest.mark.parametrize(
    "fmt, thinking, effort, expect",
    [
        (None, {"type": "enabled", "budget_tokens": 2000}, None, {}),
        ("openai", {"type": "enabled", "budget_tokens": 2000}, None, {"reasoning_effort": "low"}),
        ("openai", {"type": "enabled", "budget_tokens": 32000}, None, {"reasoning_effort": "high"}),
        ("deepseek", {"type": "enabled", "budget_tokens": 8000}, None,
         {"thinking": {"type": "enabled"}, "reasoning_effort": "medium"}),
        ("deepseek", {"type": "enabled", "budget_tokens": 8000}, "max",
         {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}),
        ("deepseek", {"type": "disabled"}, None, {"thinking": {"type": "disabled"}}),
    ],
)
def test_thinking_follows_the_providers_reasoning_format(fmt, thinking, effort, expect):
    out = ao.request({"model": "m", "messages": [], "thinking": thinking},
                     reasoning_format=fmt, reasoning_effort=effort)
    got = {k: out[k] for k in ("thinking", "reasoning_effort") if k in out}
    assert got == expect


def test_deepseek_gets_its_reasoning_back_on_assistant_turns():
    turns = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "plan", "signature": ""},
        {"type": "tool_use", "id": "t", "name": "x", "input": {}},
    ]}]
    assert ao.request({"model": "m", "messages": turns},
                      reasoning_format="deepseek")["messages"][0]["reasoning_content"] == "plan"
    assert "reasoning_content" not in ao.request(
        {"model": "m", "messages": turns}, reasoning_format="openai")["messages"][0]


def test_a_body_that_is_not_an_object_is_refused():
    with pytest.raises(ao.TranslationError):
        ao.request([])


# --------------------------------------------------------------------------- #
# response
# --------------------------------------------------------------------------- #
def test_response_maps_reasoning_text_tool_calls_and_usage():
    doc = {
        "id": "chatcmpl-1", "model": "deepseek/deepseek-v4.1-flash",
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "ok", "reasoning_content": "think",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "Read", "arguments": '{"path":"a"}'}}],
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 7,
                  "prompt_tokens_details": {"cached_tokens": 40}},
    }
    out = ao.response(doc)
    assert out["id"] == "msg_chatcmpl-1"
    assert out["type"] == "message" and out["role"] == "assistant"
    assert out["content"] == [
        {"type": "thinking", "thinking": "think", "signature": ""},
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"path": "a"}},
    ]
    assert out["stop_reason"] == "tool_use"
    assert out["usage"] == {"input_tokens": 60, "output_tokens": 7,
                            "cache_read_input_tokens": 40, "cache_creation_input_tokens": 0}


@pytest.mark.parametrize("finish, stop", [("stop", "end_turn"), ("length", "max_tokens"),
                                          (None, "end_turn")])
def test_finish_reason_maps_to_stop_reason(finish, stop):
    doc = {"choices": [{"finish_reason": finish, "message": {"content": "x"}}]}
    assert ao.response(doc)["stop_reason"] == stop


def test_deepseek_cache_hit_tokens_count_as_cache_read():
    assert ao.usage({"prompt_tokens": 10, "completion_tokens": 1,
                     "prompt_cache_hit_tokens": 4})["cache_read_input_tokens"] == 4


def test_error_body_keeps_the_upstream_message():
    body = json.dumps({"error": {"message": "Model is not supported"}}).encode()
    assert ao.error(400, body) == {"type": "error", "error": {
        "type": "invalid_request_error", "message": "Model is not supported"}}
    assert ao.error(503, b"down")["error"] == {"type": "api_error", "message": "down"}
    assert ao.error(429, b"{}")["error"]["type"] == "rate_limit_error"


# --------------------------------------------------------------------------- #
# stream
# --------------------------------------------------------------------------- #
def _chunk(delta=None, finish=None, usage=None, choices=True):
    doc = {"id": "c1", "object": "chat.completion.chunk", "model": "m"}
    doc["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if choices else []
    if usage is not None:
        doc["usage"] = usage
    return f"data: {json.dumps(doc)}\n\n".encode()


def _events(raw: bytes):
    out = []
    for block in raw.decode().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


OPENAI_STREAM = b"".join([
    _chunk({"role": "assistant", "reasoning_content": "hm"}),
    _chunk({"reasoning_content": "m"}),
    _chunk({"content": "Hel"}),
    _chunk({"content": "lo"}),
    _chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                            "function": {"name": "Read", "arguments": ""}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"pa'}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th":"a"}'}}]}),
    _chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                            "function": {"name": "Ls", "arguments": "{}"}}]}),
    _chunk({}, finish="tool_calls"),
    _chunk(choices=False, usage={"prompt_tokens": 12, "completion_tokens": 5}),
    b"data: [DONE]\n\n",
])


def test_stream_translator_emits_the_messages_event_sequence():
    translator = ao.StreamTranslator(model="m")
    raw = b""
    # split mid-line to prove the buffer joins partial chunks
    for i in range(0, len(OPENAI_STREAM), 37):
        raw += b"".join(translator.feed(OPENAI_STREAM[i:i + 37]))
    raw += b"".join(translator.close())
    events = _events(raw)
    names = [e for e, _ in events]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    starts = [d["content_block"] for e, d in events if e == "content_block_start"]
    assert [b["type"] for b in starts] == ["thinking", "text", "tool_use", "tool_use"]
    assert starts[2]["id"] == "call_a" and starts[2]["name"] == "Read"
    deltas = [d["delta"] for e, d in events if e == "content_block_delta"]
    assert "".join(d.get("thinking", "") for d in deltas) == "hmm"
    assert "".join(d.get("text", "") for d in deltas) == "Hello"
    json_parts = [(d["partial_json"]) for d in deltas if d["type"] == "input_json_delta"]
    assert json_parts == ['{"pa', 'th":"a"}', "{}"]
    assert names.count("content_block_start") == names.count("content_block_stop") == 4
    final = events[-2][1]
    assert final["delta"]["stop_reason"] == "tool_use"
    assert final["usage"]["input_tokens"] == 12 and final["usage"]["output_tokens"] == 5


def test_an_empty_stream_still_ends_the_message():
    events = _events(b"".join(ao.StreamTranslator().close()))
    assert [e for e, _ in events] == ["message_start", "message_delta", "message_stop"]


def test_a_mid_stream_error_chunk_becomes_an_error_event():
    t = ao.StreamTranslator()
    raw = b"".join(t.feed(_chunk({"content": "a"}) + b'data: {"error": {"message": "x"}}\n\n'))
    assert ("error", {"type": "error", "error": {"type": "api_error", "message": "x"}}) \
        in _events(raw)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
OPENAI_ONLY = {
    "endpoints": {"openai": "https://api.commandcode.ai/provider/v1"},
    "models": {"default": "deepseek/deepseek-v4.1-flash"},
    "openai_reasoning_format": "deepseek",
}


def _write(name: str, entry: dict) -> None:
    store.update(lambda doc: doc.setdefault("providers", {}).update({name: dict(entry)}))


def test_translation_is_off_by_default_and_opt_in(home):
    _write("cmdc", OPENAI_ONLY)
    assert routing.anthropic_translation("cmdc") is None
    assert routing.requires_shim("cmdc") is False
    assert routing.translation_options("cmdc") is None
    _write("cmdc", {**OPENAI_ONLY, "anthropic_translation": "openai"})
    assert routing.anthropic_translation("cmdc") == "openai"
    assert routing.requires_shim("cmdc") is True
    assert routing.translation_upstream("cmdc") == "https://api.commandcode.ai/provider/v1"
    assert routing.translation_options("cmdc") == {
        "to": "openai", "reasoning_format": "deepseek", "reasoning_effort": None}


@pytest.mark.parametrize("bad", [True, "anthropic", 1, ""])
def test_a_malformed_translation_value_is_refused(home, bad):
    _write("cmdc", {**OPENAI_ONLY, "anthropic_translation": bad})
    with pytest.raises(routing.RoutingError):
        routing.anthropic_translation("cmdc")


def test_translation_without_an_openai_endpoint_is_refused(home):
    _write("cmdc", {"endpoints": {"anthropic": "https://x.example"},
                    "models": {"default": "m"}, "anthropic_translation": "openai"})
    with pytest.raises(routing.RoutingError, match="endpoints.openai"):
        routing.translation_upstream("cmdc")
    problems = provider_install.validate(
        "cmdc", {"endpoints": {"anthropic": "https://x.example"},
                 "models": {"default": "m"}, "anthropic_translation": "openai"})
    assert any("endpoints.openai" in p for p in problems)


def test_install_form_accepts_a_translating_provider(home):
    assert provider_install.validate("cmdc", {**OPENAI_ONLY, "anthropic_translation": "openai"}) == []


def test_apply_points_claude_at_a_shim_over_the_openai_base(home, monkeypatch):
    _write("cmdc", {**OPENAI_ONLY, "anthropic_translation": "openai"})
    seen = {}

    def _fake_ensure(upstream, block, provider=None, route=None):
        seen["args"] = (upstream, block, provider)
        return "http://127.0.0.1:1/"

    monkeypatch.setattr(routing, "ensure_shim", _fake_ensure)
    env: dict = {}  # the claude translator set no ANTHROPIC_BASE_URL: no anthropic endpoint
    routing.apply(env, "cmdc")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1/"
    assert seen["args"] == ("https://api.commandcode.ai/provider/v1", {}, "cmdc")


def test_a_dynamic_translating_route_forwards_to_the_openai_base(home):
    _write("cmdc", {**OPENAI_ONLY, "anthropic_translation": "openai", "dynamic": True})
    assert routing.route_upstream("provider:cmdc") == "https://api.commandcode.ai/provider/v1"
    assert routing.translation_options(route="provider:cmdc")["to"] == "openai"


# --------------------------------------------------------------------------- #
# the shim, in-process, in front of a fake OpenAI upstream
# --------------------------------------------------------------------------- #
class _FakeOpenAI:
    def __init__(self, *, stream_body: bytes = OPENAI_STREAM, status: int = 200):
        self.requests = []
        self.stream_body = stream_body
        self.status = status
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self._handle)

    async def _handle(self, request: web.Request):
        body = await request.read()
        self.requests.append({"path": request.path, "query": request.query_string,
                              "headers": dict(request.headers),
                              "body": json.loads(body) if body else None})
        if self.status >= 400:
            return web.json_response({"error": {"message": "Model is not supported"}},
                                     status=self.status)
        doc = self.requests[-1]["body"] or {}
        if doc.get("stream"):
            resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
            await resp.prepare(request)
            for i in range(0, len(self.stream_body), 50):
                await resp.write(self.stream_body[i:i + 50])
            await resp.write_eof()
            return resp
        if request.path.endswith("/chat/completions") and "messages" in doc:
            return web.json_response({
                "id": "c9", "model": doc.get("model"),
                "choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            })
        return web.json_response({"passthrough": request.path})


def _run(coro):
    return asyncio.run(coro)


async def _with_shim(fake: _FakeOpenAI, provider: str, fn):
    upstream_server = TestServer(fake.app)
    await upstream_server.start_server()
    try:
        base = str(upstream_server.make_url("/provider/v1"))
        shim = routing_shim.build_app(base, {}, "fp-test", provider=provider)
        async with TestClient(TestServer(shim)) as client:
            return await fn(client)
    finally:
        await upstream_server.close()


def _translating(home):
    _write("cmdc", {**OPENAI_ONLY, "anthropic_translation": "openai"})


def test_shim_translates_a_non_streamed_messages_request(home):
    _translating(home)
    fake = _FakeOpenAI()

    async def go(client):
        resp = await client.post(
            "/s/s1/v1/messages?beta=true",
            json={"model": "deepseek/deepseek-v4.1-flash", "max_tokens": 10,
                  "messages": [{"role": "user", "content": "ping"}]},
            headers={"authorization": "Bearer sk-1", "anthropic-version": "2023-06-01",
                     "anthropic-beta": "x"})
        return resp.status, await resp.json()

    status, doc = _run(_with_shim(fake, "cmdc", go))
    assert status == 200
    assert doc["content"] == [{"type": "text", "text": "pong"}]
    assert doc["stop_reason"] == "end_turn"
    assert doc["usage"]["input_tokens"] == 3
    sent = fake.requests[0]
    assert sent["path"] == "/provider/v1/chat/completions"
    assert sent["query"] == ""
    assert sent["body"]["messages"] == [{"role": "user", "content": "ping"}]
    assert sent["headers"]["Authorization"] == "Bearer sk-1"
    assert not any(k.lower().startswith("anthropic-") for k in sent["headers"])


def test_shim_translates_a_streamed_messages_request(home):
    _translating(home)
    fake = _FakeOpenAI()

    async def go(client):
        resp = await client.post(
            "/v1/messages",
            json={"model": "m", "max_tokens": 10, "stream": True,
                  "thinking": {"type": "enabled", "budget_tokens": 2000},
                  "messages": [{"role": "user", "content": "go"}]},
            headers={"x-api-key": "sk-2"})
        return resp.status, resp.headers.get("content-type"), await resp.read()

    status, ctype, raw = _run(_with_shim(fake, "cmdc", go))
    assert status == 200 and "text/event-stream" in ctype
    names = [e for e, _ in _events(raw)]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    sent = fake.requests[0]
    assert sent["headers"]["Authorization"] == "Bearer sk-2"
    assert "x-api-key" not in {k.lower() for k in sent["headers"]}
    assert sent["body"]["stream_options"] == {"include_usage": True}
    assert sent["body"]["thinking"] == {"type": "enabled"}
    assert sent["body"]["reasoning_effort"] == "low"


def test_shim_turns_an_upstream_error_into_an_anthropic_error(home):
    _translating(home)
    fake = _FakeOpenAI(status=400)

    async def go(client):
        resp = await client.post("/v1/messages", json={"model": "m", "messages": []})
        return resp.status, await resp.json()

    status, doc = _run(_with_shim(fake, "cmdc", go))
    assert status == 400
    assert doc == {"type": "error", "error": {"type": "invalid_request_error",
                                              "message": "Model is not supported"}}


def test_shim_answers_count_tokens_locally(home):
    _translating(home)
    fake = _FakeOpenAI()

    async def go(client):
        resp = await client.post("/v1/messages/count_tokens",
                                 json={"model": "m", "messages": [{"role": "user", "content": "x" * 400}]})
        return resp.status, await resp.json()

    status, doc = _run(_with_shim(fake, "cmdc", go))
    assert status == 200 and doc["input_tokens"] > 50
    assert fake.requests == []


def test_shim_passes_chat_completions_through_untouched(home):
    """Pi shares the shim (same upstream): its OpenAI requests are not translated."""
    _translating(home)
    fake = _FakeOpenAI()

    async def go(client):
        resp = await client.post("/chat/completions",
                                 json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        return await resp.json()

    doc = _run(_with_shim(fake, "cmdc", go))
    assert doc["choices"][0]["message"]["content"] == "pong"  # the raw OpenAI shape
    assert fake.requests[0]["body"] == {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def test_shim_without_the_key_forwards_messages_unchanged(home):
    _write("cmdc", OPENAI_ONLY)
    fake = _FakeOpenAI()

    async def go(client):
        resp = await client.post("/v1/messages", json={"model": "m", "messages": []})
        return await resp.json()

    assert _run(_with_shim(fake, "cmdc", go)) == {"passthrough": "/provider/v1/v1/messages"}


def test_shim_refuses_a_malformed_key_with_502(home):
    _write("cmdc", {**OPENAI_ONLY, "anthropic_translation": "bogus"})
    fake = _FakeOpenAI()

    async def go(client):
        resp = await client.post("/v1/messages", json={"model": "m", "messages": []})
        return resp.status, await resp.json()

    status, doc = _run(_with_shim(fake, "cmdc", go))
    assert status == 502 and "anthropic_translation" in doc["error"]["message"]
    assert fake.requests == []
