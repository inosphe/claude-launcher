"""Per-request throughput records for API-key providers.

The pure parts (the SSE/JSON reader, the config switches, the session header,
the aggregation) are asserted directly. The part that earns its keep is the
end-to-end one: a real shim in front of a throwaway upstream that streams an
Anthropic-shaped (or, for the ``pi`` harness, an OpenAI-shaped) completion,
and a record with this session's name, its token counts and a TPS lands in
the launcher home.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_launcher import (
    cli,
    credentials,
    harnesses,
    lineage,
    metering,
    pi_provider,
    profile,
    routing,
    runner,
    settings,
    store,
)


# --------------------------------------------------------------------------- #
# the reader
# --------------------------------------------------------------------------- #
def _sse(events):
    out = b""
    for ev in events:
        out += f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()
    return out


STREAM = [
    {
        "type": "message_start",
        "message": {
            "model": "deepseek-chat",
            "usage": {"input_tokens": 120, "cache_read_input_tokens": 30, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "!"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40}},
    {"type": "message_stop"},
]


def _openai_sse(chunks):
    out = b""
    for ch in chunks:
        out += b"data: " + (ch if isinstance(ch, bytes) else json.dumps(ch).encode()) + b"\n\n"
    return out


def _oa(**kw):
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "deepseek-flash", **kw}


OPENAI_STREAM = [
    _oa(choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}}]),
    _oa(choices=[{"index": 0, "delta": {"content": "Hi"}}]),
    _oa(choices=[{"index": 0, "delta": {"content": "!"}, "finish_reason": "stop"}]),
    # the usage-only chunk a backend sends for stream_options.include_usage
    _oa(
        choices=[],
        usage={
            "prompt_tokens": 90,
            "completion_tokens": 25,
            "total_tokens": 115,
            "prompt_tokens_details": {"cached_tokens": 64},
        },
    ),
    b"[DONE]",
]


class _Headers(dict):
    def get(self, key, default=None):  # aiohttp headers are case-insensitive
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def test_reader_follows_an_sse_stream_split_at_arbitrary_points():
    m = metering.Meter(session="s1", path="/v1/messages")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    body = _sse(STREAM)
    for i in range(0, len(body), 7):  # chunks that cut events anywhere
        m.chunk(body[i:i + 7])
    rec = m.finish()
    assert rec["model"] == "deepseek-chat"
    assert rec["input_tokens"] == 120 and rec["cache_read"] == 30
    assert rec["output_tokens"] == 40  # message_delta overrides message_start's 1
    assert rec["stream"] is True and rec["counted"] is True
    assert rec["ttft_ms"] is not None and rec["tps"] is not None
    assert rec["session"] == "s1" and rec["status"] == 200


def test_reader_measures_time_to_first_token_from_the_first_delta():
    m = metering.Meter(session=None, path="/v1/messages")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    m.chunk(_sse(STREAM[:2]))  # message_start + block start: no token yet
    assert m.first_token is None
    time.sleep(0.03)
    m.chunk(_sse(STREAM[2:3]))
    assert m.first_token is not None
    time.sleep(0.03)
    m.chunk(_sse(STREAM[3:]))
    rec = m.finish()
    assert rec["ttft_ms"] >= 25
    # 40 tokens over the ~30ms after the first token: well above 40 tok/s
    assert rec["tps"] > rec["tps_total"] > 0


def test_reader_reads_a_non_streamed_json_answer():
    m = metering.Meter(session="s2", path="/v1/messages")
    m.headers(200, _Headers({"content-type": "application/json"}))
    doc = {"model": "glm-5", "usage": {"input_tokens": 7, "output_tokens": 12}}
    raw = json.dumps(doc).encode()
    m.chunk(raw[:5])
    m.chunk(raw[5:])
    rec = m.finish()
    assert rec["stream"] is False
    assert rec["model"] == "glm-5" and rec["output_tokens"] == 12
    assert rec["ttfb_ms"] is not None
    assert rec["counted"] is True


def test_a_non_streamed_answer_reports_no_generation_rate(home):
    """The body arrives whole, so there is no generation to time.

    Dividing the token count by the sliver the finished body took to cross
    the socket is how a five-token answer was recorded as 26709 tok/s; the
    record says null instead. What every call does have is its own whole-call
    rate, and that is what a reader is shown.
    """
    m = metering.Meter(session="s2", path="/v1/messages")
    time.sleep(0.4)  # the upstream thinks, before a byte of body exists
    m.headers(200, _Headers({"content-type": "application/json"}))
    m.chunk(json.dumps({"model": "m", "usage": {"output_tokens": 5}}).encode())
    rec = m.finish()
    assert rec["stream"] is False and rec["streamed"] is False
    assert rec["tps"] is None and rec["counted"] is True
    assert rec["tps_total"] is not None and rec["tps_total"] < 100
    assert metering.generation_tps(rec) is None
    assert metering.reported_tps(rec) == rec["tps_total"]


def test_a_streamed_answer_keeps_its_generation_rate(home):
    m = metering.Meter(session="s1", path="/v1/messages")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    for ev in STREAM:
        m.chunk(_sse([ev]))
        time.sleep(0.02)
    rec = m.finish()
    assert rec["stream"] is True and rec["streamed"] is True
    assert metering.generation_tps(rec) == rec["tps"]
    # The window is the span the content was seen over, and the rate is the
    # count divided by it. `generation_ms` is that window truncated to whole
    # milliseconds, so the rate falls in the band the truncation allows.
    ms = rec["generation_ms"]
    assert ms is not None and ms >= 20  # two 20ms sleeps, the second being content
    assert rec["output_tokens"] * 1000 / (ms + 1) < rec["tps"] <= rec["output_tokens"] * 1000 / ms


def test_the_shown_rate_is_the_whole_call_even_when_generation_was_timed(home):
    """One definition over every row, whatever the row's answer looked like.

    The generation rate is kept and reported as itself, but the number the
    rail shows is the whole call's -- otherwise a row whose answer streamed
    and a row whose answer did not would be showing two different
    measurements side by side, and a reader could not compare them.
    """
    m = metering.Meter(session="s1", path="/v1/messages")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    for ev in STREAM:
        m.chunk(_sse([ev]))
        time.sleep(0.02)
    rec = m.finish()
    assert metering.generation_tps(rec) is not None
    assert metering.reported_tps(rec) == rec["tps_total"]
    assert rec["tps_total"] < rec["tps"]  # the wait for the first token is inside it
    assert metering.reported_tps({"stream": False, "output_tokens": 5, "tps_total": 2.0}) == 2.0


def test_content_that_arrived_in_one_read_has_no_generation_rate(home):
    """A streamed answer can still be untimeable, and says so the same way.

    Every event of the answer, deltas included, arrives in a single read --
    what a backend that generates and then flushes looks like from here. The
    content was never observed to take any time, so the window is zero and
    dividing by it invents a rate; the record reports none. This is the shape
    behind the recorded 435483 tok/s.
    """
    m = metering.Meter(session="s1", path="/v1/messages")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    m.chunk(_sse(STREAM))  # the whole answer, one read
    rec = m.finish()
    assert rec["stream"] is True and rec["streamed"] is True
    assert rec["tps"] is None and rec["generation_ms"] is None
    assert metering.generation_tps(rec) is None
    assert metering.reported_tps(rec) == rec["tps_total"]


def test_a_record_written_before_the_fix_is_read_by_its_own_shape(home):
    """The inflated numbers are still in the old records; the reader ignores them.

    Two residues have to be turned away and neither can be recognised by the
    presence of ``tps``: the records of answers that did not stream, whose
    denominator was the socket rather than the model, and the records of
    answers whose content arrived in one read. Reading is decided from the
    record's own shape, so none of them needs a rewrite. A record that says
    nothing either way keeps its generation rate.
    """
    whole = {"stream": False, "output_tokens": 5, "tps": 26709.41, "tps_total": 0.98}
    assert metering.generation_tps(whole) is None
    assert metering.reported_tps(whole) == 0.98
    # A record with no `stream` at all is read as one that did not stream.
    assert metering.generation_tps({"output_tokens": 5, "tps": 99.0}) is None
    # Streamed, but the call did not outlast its own ttft: the window the rate
    # was divided by is inside the rounding error of the two fields that record
    # it, so the rate is not one the record establishes.
    flushed = {"stream": True, "output_tokens": 27, "tps": 435483.83,
               "tps_total": 10.49, "total_ms": 2575, "ttft_ms": 2575}
    assert metering.generation_tps(flushed) is None
    # One millisecond of difference is still rounding; two establish a window.
    assert metering.generation_tps({**flushed, "ttft_ms": 2574}) is None
    assert metering.generation_tps({**flushed, "ttft_ms": 2500}) == 435483.83
    s = metering.summarize([whole, flushed, {"stream": True, "output_tokens": 5, "tps": 50.0,
                                             "tps_total": 40.0, "generation_ms": 200}])
    # The shown rates include every counted call, and none of them is 435483.
    assert s["counted"] == 3 and s["tps_max"] == 40.0
    # The generation median is over the one call that has a generation rate.
    assert s["generation_n"] == 1 and s["generation_median"] == 50.0


def test_a_final_event_with_no_blank_line_is_read_at_finish(home):
    """The last event of a stream often arrives without its blank line."""
    m = metering.Meter(session="s1", path="/v1/messages")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    m.chunk(_sse(STREAM[:3]))  # through the first content delta
    time.sleep(0.03)
    m.chunk(_sse([STREAM[3]]))  # the second content delta, terminated
    tail = _sse([STREAM[4], STREAM[5]])
    m.chunk(tail[:-2])  # the closing events, with no blank line after them
    assert m._sse_tail.strip()
    rec = m.finish()
    assert rec["output_tokens"] == 40  # message_delta in the tail was read
    assert rec["tps"] is not None and rec["generation_ms"] >= 25


def test_reader_follows_an_openai_chat_completion_stream():
    m = metering.Meter(session="s1", path="/v1/chat/completions")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    m.chunk(_openai_sse(OPENAI_STREAM[:1]))  # role-only delta: no token yet
    assert m.first_token is None
    body = _openai_sse(OPENAI_STREAM[1:])
    for i in range(0, len(body), 5):
        m.chunk(body[i:i + 5])
    rec = m.finish()
    assert rec["model"] == "deepseek-flash"
    assert rec["input_tokens"] == 90 and rec["cache_read"] == 64
    assert rec["cache_write"] is None  # OpenAI has no such count
    assert rec["output_tokens"] == 25 and rec["counted"] is True
    assert rec["stream"] is True and rec["ttft_ms"] is not None and rec["tps"]


def test_reader_leaves_an_openai_stream_without_usage_uncounted():
    m = metering.Meter(session=None, path="/v1/chat/completions")
    m.headers(200, _Headers({"content-type": "text/event-stream"}))
    m.chunk(_openai_sse(OPENAI_STREAM[:3] + [b"[DONE]"]))  # no include_usage
    rec = m.finish()
    assert rec["model"] == "deepseek-flash"
    assert rec["output_tokens"] is None and rec["counted"] is False
    assert rec["ttft_ms"] is not None  # timing is still there


def test_reader_reads_a_non_streamed_openai_answer():
    m = metering.Meter(session="s2", path="/v1/chat/completions")
    m.headers(200, _Headers({"content-type": "application/json"}))
    doc = {
        "object": "chat.completion",
        "model": "glm-5",
        "choices": [{"message": {"role": "assistant", "content": "x"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 12},
    }
    m.chunk(json.dumps(doc).encode())
    rec = m.finish()
    assert rec["stream"] is False
    assert rec["model"] == "glm-5" and rec["output_tokens"] == 12 and rec["input_tokens"] == 7
    assert rec["cache_read"] is None and rec["counted"] is True


def test_reader_records_a_compressed_or_error_body_without_counts():
    m = metering.Meter(session=None, path="/v1/messages")
    m.headers(200, _Headers({"content-type": "application/json", "content-encoding": "gzip"}))
    m.chunk(b"\x1f\x8b garbage")
    rec = m.finish()
    assert rec["counted"] is False and rec["tps"] is None
    assert rec["total_ms"] >= 0

    m = metering.Meter(session=None, path="/v1/messages")
    m.headers(429, _Headers({"content-type": "application/json"}))
    m.chunk(b'{"type":"error","error":{"type":"rate_limit_error"}}')
    rec = m.finish()
    assert rec["status"] == 429 and rec["counted"] is False


# --------------------------------------------------------------------------- #
# config switches and the session header
# --------------------------------------------------------------------------- #
def test_metering_is_on_unless_switched_off(home, monkeypatch):
    monkeypatch.delenv("CLAUNCH_METERING", raising=False)
    assert metering.enabled({}) is True
    assert metering.enabled({"metering": False}) is False
    assert metering.enabled({"metering": {"enabled": False}}) is False
    doc = {"providers": {"a": {"metering": False}, "b": {}}}
    assert metering.provider_enabled("a", doc) is False
    assert metering.provider_enabled("b", doc) is True
    assert metering.provider_enabled("b", {"metering": False, "providers": {"b": {}}}) is False
    monkeypatch.setenv("CLAUNCH_METERING", "0")
    assert metering.enabled({}) is False
    monkeypatch.setenv("CLAUNCH_METERING", "1")
    assert metering.enabled({"metering": False}) is True


def test_session_header_is_added_and_replaced_not_duplicated():
    env = {"ANTHROPIC_CUSTOM_HEADERS": "X-Mine: 1\nx-claunch-session: old"}
    metering.apply_session_header(env, "s9")
    lines = env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert lines == ["X-Mine: 1", "X-Claunch-Session: s9"]
    env = {}
    metering.apply_session_header(env, "s10")
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Claunch-Session: s10"


def test_is_shim_url_recognises_only_the_shim_window():
    fp = routing.fingerprint("https://u/", {})
    assert routing.is_shim_url(routing.local_url(routing.candidate_ports(fp)[0]))
    assert not routing.is_shim_url("http://127.0.0.1:8080/")
    assert not routing.is_shim_url("https://api.deepseek.com/anthropic")
    assert not routing.is_shim_url(None)


# --------------------------------------------------------------------------- #
# routing.apply: which providers go through a shim, and what a failure means
# --------------------------------------------------------------------------- #
def _doc(**entry):
    return {"providers": {"deepseek": {"env": {"ANTHROPIC_BASE_URL": "https://d/anthropic"}, **entry}}}


@pytest.fixture
def metering_on(monkeypatch):
    monkeypatch.setenv("CLAUNCH_METERING", "1")


def test_apply_fronts_an_api_key_provider_with_a_metering_shim(monkeypatch, metering_on):
    seen = []
    monkeypatch.setattr(
        routing, "ensure_shim", lambda upstream, block: seen.append((upstream, block)) or "http://127.0.0.1:31600/"
    )
    env = {"ANTHROPIC_BASE_URL": "https://d/anthropic"}
    routing.apply(env, "deepseek", _doc())
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:31600/"
    assert seen == [("https://d/anthropic", {})]


def test_apply_leaves_a_provider_alone_when_metering_is_off(monkeypatch):
    monkeypatch.delenv("CLAUNCH_METERING", raising=False)  # the config decides
    monkeypatch.setattr(routing, "ensure_shim", lambda *_: pytest.fail("no shim expected"))
    env = {"ANTHROPIC_BASE_URL": "https://d/anthropic"}
    routing.apply(env, "deepseek", _doc(metering=False))  # one provider opted out
    assert env["ANTHROPIC_BASE_URL"] == "https://d/anthropic"
    doc = _doc()
    doc["metering"] = False  # or the whole feature
    routing.apply(env, "deepseek", doc)
    assert env["ANTHROPIC_BASE_URL"] == "https://d/anthropic"
    monkeypatch.setenv("CLAUNCH_METERING", "0")  # or this process
    routing.apply(env, "deepseek", _doc())
    assert env["ANTHROPIC_BASE_URL"] == "https://d/anthropic"


def test_apply_without_a_base_url_is_a_no_op_for_metering_only(metering_on):
    env = {}
    routing.apply(env, "deepseek", {"providers": {"deepseek": {}}})
    assert "ANTHROPIC_BASE_URL" not in env


def test_a_metering_shim_that_will_not_start_falls_back_to_the_upstream(monkeypatch, capsys, metering_on):
    def _boom(*_):
        raise routing.RoutingError("port window exhausted")

    monkeypatch.setattr(routing, "ensure_shim", _boom)
    env = {"ANTHROPIC_BASE_URL": "https://d/anthropic"}
    routing.apply(env, "deepseek", _doc())
    assert env["ANTHROPIC_BASE_URL"] == "https://d/anthropic"
    assert "no TPS records" in capsys.readouterr().err


def test_a_routing_shim_that_will_not_start_still_fails_the_launch(monkeypatch, metering_on):
    def _boom(*_):
        raise routing.RoutingError("port window exhausted")

    monkeypatch.setattr(routing, "ensure_shim", _boom)
    env = {"ANTHROPIC_BASE_URL": "https://d/anthropic"}
    with pytest.raises(routing.RoutingError):
        routing.apply(env, "deepseek", _doc(routing={"order": ["x"]}))


def _pi_profile(home, base="https://omlx.example/"):
    p = profile.create("omlx")
    store.update(
        lambda doc: doc.setdefault("providers", {}).update(
            {"omlx": {"env": {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_MODEL": "solar-main"}}}
        )
    )
    store.set_profile_field(p.name, "provider", "omlx")
    credentials.save_token(p, "stored-omlx-token")
    return profile.require_selector("omlx:pi")


def test_pi_projection_goes_through_the_metering_shim_and_asks_for_stream_usage(home, monkeypatch, metering_on):
    seen = []
    shim_url = routing.local_url(routing.candidate_ports(routing.fingerprint("https://omlx.example/v1", {}))[0])
    monkeypatch.setattr(routing, "ensure_shim", lambda upstream, block: seen.append((upstream, block)) or shim_url)
    p = _pi_profile(home)
    env = runner.harness_child_env(p, harnesses.get("pi"), base_env={})
    # the upstream the shim fronts is the OpenAI root, /v1 included; Pi gets
    # the shim without a trailing slash (its client appends /chat/completions)
    assert seen == [("https://omlx.example/v1", {})]
    assert env[pi_provider.ENV_BASE_URL] == shim_url.rstrip("/")
    assert env[pi_provider.ENV_STREAM_USAGE] == "1"
    assert pi_provider.ENV_HEADERS not in env  # only the daemon names a session
    # metering off: the direct URL, and no usage request either
    monkeypatch.setenv("CLAUNCH_METERING", "0")
    env = runner.harness_child_env(p, harnesses.get("pi"), base_env={})
    assert env[pi_provider.ENV_BASE_URL] == "https://omlx.example/v1"
    assert pi_provider.ENV_STREAM_USAGE not in env


def test_pi_session_header_is_set_and_replaced_not_duplicated():
    env = {pi_provider.ENV_HEADERS: json.dumps({"X-Custom": "1", "x-claunch-session": "old"})}
    pi_provider.apply_session_header(env, "s9")
    assert json.loads(env[pi_provider.ENV_HEADERS]) == {"X-Custom": "1", "X-Claunch-Session": "s9"}
    env = {pi_provider.ENV_HEADERS: "not json"}
    pi_provider.apply_session_header(env, "s9")
    assert json.loads(env[pi_provider.ENV_HEADERS]) == {"X-Claunch-Session": "s9"}


def test_pi_extension_registers_the_headers_and_the_usage_flag():
    extension = pi_provider.extension_path().read_text(encoding="utf-8")
    assert 'process.env.CLAUNCH_PI_STREAM_USAGE === "1"' in extension
    assert "process.env.CLAUNCH_PI_HEADERS" in extension
    assert "...(headers ? { headers } : {})" in extension


def test_merge_body_with_an_empty_spec_is_the_identity():
    raw = b'{"model":"m"}'
    assert routing.merge_body(raw, {}) is raw


# --------------------------------------------------------------------------- #
# records and the aggregation `claunch tps` prints
# --------------------------------------------------------------------------- #
def _rec(**kw):
    base = {
        "ts": "2026-09-11T10:00:00+0900", "session": "s1", "model": "m", "status": 200,
        "output_tokens": 100, "input_tokens": 10, "cache_read": 0, "stream": True,
        "ttft_ms": 200, "tps": 50.0, "tps_total": 40.0, "counted": True,
    }
    base.update(kw)
    return base


def test_records_append_load_filter_and_summarize(home):
    metering.append("fp1", _rec(), upstream="https://d/")
    metering.append("fp1", _rec(session="s2", tps=70.0, ttft_ms=400, model="n"), upstream="https://d/")
    metering.append("fp2", _rec(session="s1", tps=None, counted=False, output_tokens=None), upstream="https://e/")
    assert len(metering.load()) == 3
    assert [r["session"] for r in metering.load(session="s2")] == ["s2"]
    assert len(metering.load(upstream="e/")) == 1
    assert len(metering.load(limit=2)) == 2
    s = metering.summarize(metering.load())
    assert s["requests"] == 3 and s["counted"] == 2
    # The shown rates are the whole-call ones. The third call was never counted
    # (no output count), so it has no rate either; the two that do are both 40.0.
    assert s["tps_median"] == 40.0 and s["tps_min"] == 40.0 and s["tps_max"] == 40.0
    # The same two calls' own generation rates are kept in their own column.
    assert s["generation_n"] == 2 and s["generation_median"] == 60.0
    assert s["ttft_ms_median"] == 200
    assert s["output_tokens"] == 200
    assert set(metering.by_key(metering.load(), "model")) == {"m", "n"}
    assert metering.clear() == 2
    assert metering.load() == []


def test_cli_tps_prints_the_summary_and_the_last_rows(home, capsys):
    metering.append("fp1", _rec(), upstream="https://d/")
    assert cli.main(["tps"]) == 0
    out = capsys.readouterr().out
    assert "requests 1 (counted 1)" in out
    assert "tps median 40.0" in out  # the whole-call rate the record carries
    assert "s1" in out and "last 10:" in out
    assert cli.main(["tps", "--json", "-n", "0"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["tps"] == 50.0
    assert cli.main(["tps", "--session", "nobody"]) == 0
    assert "no records yet" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# end to end: a real shim, a streamed answer, a record with the session name
# --------------------------------------------------------------------------- #
class _Upstream:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                if self.path.endswith("/chat/completions"):
                    pieces = [_openai_sse([ch]) for ch in OPENAI_STREAM]
                else:
                    pieces = [_sse([ev]) for ev in STREAM]
                for chunk in pieces:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    time.sleep(0.01)
                self.wfile.write(b"0\r\n\r\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/anthropic/"

    @property
    def openai_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    s = _Upstream()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def shims(home, monkeypatch):
    monkeypatch.setenv("CLAUNCH_METERING", "1")  # conftest turns it off by default
    yield
    routing.stop()


def _wait_for_record(fp, n=1, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recs = [r for r in metering.load() if r["fingerprint"] == fp]
        if len(recs) >= n:
            return recs
        time.sleep(0.05)
    raise AssertionError("no metering record appeared")


@pytest.mark.slow_shim
def test_a_metering_shim_records_a_streamed_completion_with_its_session(home, upstream, shims):
    base = routing.ensure_shim(upstream.url, {})
    fp = routing.fingerprint(upstream.url, {})
    req = urllib.request.Request(
        base + "v1/messages",
        data=json.dumps({"model": "m", "stream": True}).encode(),
        headers={
            "content-type": "application/json",
            "accept-encoding": "gzip",
            "X-Claunch-Session": "s77",
            "x-api-key": "sk-test",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        streamed = resp.read()
    assert b"message_stop" in streamed  # the client got the whole stream
    # the upstream never saw our header, and was asked for a plain body
    seen = {k.lower(): v for k, v in upstream.requests[-1]["headers"].items()}
    assert "x-claunch-session" not in seen
    assert seen.get("accept-encoding") == "identity"
    assert seen.get("x-api-key") == "sk-test"
    assert json.loads(upstream.requests[-1]["body"]) == {"model": "m", "stream": True}
    (rec,) = _wait_for_record(fp)
    assert rec["session"] == "s77"
    assert rec["model"] == "deepseek-chat"
    assert rec["output_tokens"] == 40 and rec["input_tokens"] == 120
    assert rec["ttft_ms"] is not None and rec["tps"] and rec["tps"] > 0
    assert rec["upstream"] == upstream.url
    assert rec["stream"] is True and rec["status"] == 200


@pytest.mark.slow_shim
def test_a_metering_shim_records_an_openai_completion_the_way_pi_sends_it(home, upstream, shims):
    base = pi_provider.fronted_base_url(upstream.openai_url, "omlx")
    assert routing.is_shim_url(base) and not base.endswith("/")
    fp = routing.fingerprint(upstream.openai_url, {})
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({"model": "m", "stream": True, "stream_options": {"include_usage": True}}).encode(),
        headers={
            "content-type": "application/json",
            "accept-encoding": "gzip",
            "X-Claunch-Session": "s78",
            "authorization": "Bearer sk-test",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        streamed = resp.read()
    assert b"[DONE]" in streamed
    seen = {k.lower(): v for k, v in upstream.requests[-1]["headers"].items()}
    assert upstream.requests[-1]["path"] == "/v1/chat/completions"
    assert "x-claunch-session" not in seen
    assert seen.get("accept-encoding") == "identity"
    assert seen.get("authorization") == "Bearer sk-test"
    (rec,) = _wait_for_record(fp)
    assert rec["session"] == "s78" and rec["path"] == "/chat/completions"
    assert rec["model"] == "deepseek-flash"
    assert rec["output_tokens"] == 25 and rec["input_tokens"] == 90 and rec["cache_read"] == 64
    assert rec["ttft_ms"] is not None and rec["tps"] and rec["tps"] > 0
    assert rec["upstream"] == upstream.openai_url


@pytest.mark.slow_shim
def test_child_env_of_an_api_key_provider_points_at_a_metering_shim(home, upstream, shims, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def _mutate(doc):
        doc["providers"] = {
            "deepseek": {"env": {"ANTHROPIC_BASE_URL": upstream.url, "ANTHROPIC_AUTH_TOKEN": "sk-x"}}
        }

    store.update(_mutate)
    p = profile.create("work")
    credentials.save_token(p, "sk-stored")
    store.set_profile_field("work", "provider", "deepseek")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_BASE_URL"] == routing.ensure_shim(upstream.url, {})
    assert routing.is_shim_url(env["ANTHROPIC_BASE_URL"])
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-stored"
    # the OAuth route is never fronted
    store.set_profile_field("work", "provider", "default")
    env = runner.child_env(p, with_token=True)
    assert not routing.is_shim_url(env.get("ANTHROPIC_BASE_URL"))


# --------------------------------------------------------------------------- #
# the daemon names the session in the requests it launches
# --------------------------------------------------------------------------- #
def test_daemon_launch_carries_the_session_header_only_behind_a_shim(home, tmp_path, monkeypatch, metering_on):
    from claude_launcher.daemon import harness
    from claude_launcher.daemon.session import SessionDef

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    # ``build_command`` builds the child env from ``os.environ``, and a
    # metering-on managed session exports its own ``ANTHROPIC_CUSTOM_HEADERS``
    # (the daemon injects it). Without this the "not in env" assertion below
    # measures the shell the suite happens to run in, not what the daemon
    # injects.
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    shim_url = routing.local_url(routing.candidate_ports(routing.fingerprint("https://d/anthropic", {}))[0])
    monkeypatch.setattr(routing, "ensure_shim", lambda upstream, block: shim_url)

    def _mutate(doc):
        doc["providers"] = {
            "deepseek": {"env": {"ANTHROPIC_BASE_URL": "https://d/anthropic", "ANTHROPIC_AUTH_TOKEN": "sk-x"}}
        }

    store.update(_mutate)
    p = profile.create("work")
    credentials.save_token(p, "sk-stored")
    store.set_profile_field("work", "provider", "deepseek")
    sdef = harness.normalize(SessionDef(name="s42", profile="work", cwd=str(tmp_path)))
    _, env, _ = harness.build_command(sdef)
    assert env["ANTHROPIC_BASE_URL"] == shim_url
    assert env["CLAUNCH_SESSION"] == "s42"
    assert "X-Claunch-Session: s42" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()

    # metering off: direct upstream, and no header pointing at a shim that is not there
    monkeypatch.setenv("CLAUNCH_METERING", "0")
    _, env, _ = harness.build_command(sdef)
    assert env["ANTHROPIC_BASE_URL"] == "https://d/anthropic"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env


def test_daemon_pi_launch_names_the_session_in_the_provider_headers(home, tmp_path, monkeypatch, metering_on):
    import sys

    from claude_launcher.daemon import harness
    from claude_launcher.daemon.session import SessionDef

    # Same ambient leak as the test above. This one currently passes with the
    # variable set as well -- ``finalize_harness_env`` drops ``ANTHROPIC_*``
    # for a declared (non-builtin) harness -- so the line is here to keep the
    # assertion hermetic rather than to repair a live failure.
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    shim_url = routing.local_url(routing.candidate_ports(routing.fingerprint("https://omlx.example/v1", {}))[0])
    monkeypatch.setattr(routing, "ensure_shim", lambda upstream, block: shim_url)
    # a declared pi-adapter harness pointed at an executable that exists
    store.update(
        lambda doc: doc.setdefault("harnesses", {}).update(
            {
                "keyed": {
                    "command": sys.executable,
                    "auth": "api-key",
                    "token_env": "KEYED_API_KEY",
                    "home_env": "KEYED_HOME",
                    "provider_adapter": "pi",
                }
            }
        )
    )
    p = profile.create("work")
    lineage.set_harness(p, "keyed")
    credentials.save_token(p, "provider-secret")
    store.update(
        lambda doc: doc.setdefault("providers", {}).update(
            {"omlx": {"env": {"ANTHROPIC_BASE_URL": "https://omlx.example/", "ANTHROPIC_MODEL": "solar-main"}}}
        )
    )
    store.set_profile_field("work", "provider", "omlx")
    sdef = harness.normalize(SessionDef(name="s43", profile="work:keyed", cwd=str(tmp_path)))
    _, env, _ = harness.build_command(sdef)
    assert env[pi_provider.ENV_BASE_URL] == shim_url.rstrip("/")
    assert env[pi_provider.ENV_STREAM_USAGE] == "1"
    assert json.loads(env[pi_provider.ENV_HEADERS]) == {"X-Claunch-Session": "s43"}
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env  # that one is Claude Code's

    monkeypatch.setenv("CLAUNCH_METERING", "0")
    _, env, _ = harness.build_command(sdef)
    assert env[pi_provider.ENV_BASE_URL] == "https://omlx.example/v1"
    assert pi_provider.ENV_HEADERS not in env and pi_provider.ENV_STREAM_USAGE not in env


# --------------------------------------------------------------------------- #
# what the web UI shows: a session's latest throughput
# --------------------------------------------------------------------------- #
def test_session_summary_is_the_last_call_plus_a_rolling_median(home):
    for i in range(12):
        metering.append("fp1", _rec(session="s1", tps=10.0 + i, tps_total=40.0 + i,
                                     ttft_ms=100 + i, output_tokens=5,
                                     ts=f"2026-09-11T10:00:{i:02d}+0900"))
    metering.append("fp2", _rec(session="s1", tps=99.0, tps_total=99.0, ttft_ms=50,
                                 model="other", output_tokens=7,
                                 ts="2026-09-11T10:01:00+0900"))  # newest, on another shim
    metering.append("fp1", _rec(session="s2", tps=1.0, tps_total=1.0, ts="2026-09-11T10:02:00+0900"))
    got = metering.session_summary("s1")
    assert got["tps"] == 99.0 and got["model"] == "other" and got["ts"] == "2026-09-11T10:01:00+0900"
    assert got["window"] == metering.SUMMARY_WINDOW  # the last 10 of 13, across both files
    # Median of the whole-call rates in the window: the last nine of the run
    # (43..51) and the newest 99, of which the middle pair is 47 and 48.
    assert got["tps_median"] == 47.5
    assert got["tps_median_n"] == metering.SUMMARY_WINDOW
    assert got["counted"] is True and got["ttft_ms"] == 50
    assert metering.session_summary("s3") is None  # never measured: absence, not zero
    recs = metering.recent("s1", limit=3)
    assert [r["tps"] for r in recs] == [20.0, 21.0, 99.0]


def test_session_summary_keeps_an_uncounted_last_call_visible(home):
    metering.append("fp1", _rec(session="s1", tps=40.0, ts="2026-09-11T10:00:00+0900"))
    metering.append("fp1", _rec(session="s1", tps=None, counted=False, status=502, output_tokens=None,
                                 ts="2026-09-11T10:00:01+0900"))
    got = metering.session_summary("s1")
    assert got["counted"] is False and got["status"] == 502 and got["tps"] is None
    assert got["tps_median"] == 40.0  # the median is over the counted calls in the window


def test_session_summary_reports_a_whole_body_call_with_its_first_byte(home):
    """A session whose last call did not stream still shows a rate.

    The row is not left blank. What it shows is the call's own whole-call
    rate, and the latency beside it is the first byte -- there is no first
    token to have timed for an answer that arrived in one piece.
    """
    metering.append("fp1", _rec(session="s1", stream=False, tps=None, tps_total=2.41,
                                 ttft_ms=None, ttfb_ms=200, ts="2026-09-11T10:00:00+0900"))
    got = metering.session_summary("s1")
    assert got["tps"] == 2.41
    assert got["ttft_ms"] is None and got["ttfb_ms"] == 200
    assert got["tps_median"] == 2.41 and got["tps_median_n"] == 1


def test_tail_reader_only_reads_the_end_and_rereads_on_change(home, monkeypatch):
    monkeypatch.setattr(metering, "TAIL_BYTES", 600)
    for i in range(20):
        metering.append("fp1", _rec(session="s1", tps=float(i), ts=f"2026-09-11T10:00:{i:02d}+0900"))
    path = metering.record_file("fp1")
    recs = metering._tail_records(path)
    assert 0 < len(recs) < 20 and recs[-1]["tps"] == 19.0  # a bounded read off the end
    assert metering._tail_records(path) is recs  # unchanged file: the cached parse
    metering.append("fp1", _rec(session="s1", tps=77.0, ts="2026-09-11T10:01:00+0900"))
    assert metering._tail_records(path)[-1]["tps"] == 77.0  # it grew: re-read


def test_attach_hangs_tps_on_a_session_record_only_when_there_is_one(home):
    metering.append("fp1", _rec(session="s1", tps=33.0, tps_total=33.0))
    info = metering.attach({"name": "s1", "status": "busy"})
    assert info["tps"]["tps"] == 33.0 and info["status"] == "busy"
    assert "tps" not in metering.attach({"name": "s2"})
    assert "tps" not in metering.attach({})


def test_metering_api_serves_a_sessions_records_and_the_session_list_carries_them(home, tmp_path):
    import asyncio
    import sys

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    store.update(lambda doc: doc.update({
        "harnesses": {"py": {"command": [sys.executable, "-u", "-c", "import time; time.sleep(60)"]}}
    }))
    metering.append("fp1", _rec(session="s1", tps=42.5, tps_total=42.5, ttft_ms=800,
                                 model="deepseek-flash",
                                 ts="2026-09-11T10:00:00+0900"), upstream="https://d/v1")
    metering.append("fp1", _rec(session="other", tps=5.0, ts="2026-09-11T10:00:01+0900"))
    bearer = {"Authorization": "Bearer sekrit"}

    async def run():
        from aiohttp.test_utils import TestClient, TestServer

        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = TestClient(TestServer(build_app(
            mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr)
        )))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            one = await client.get("/api/metering?session=s1", headers=bearer)
            assert one.status == 200
            body = await one.json()
            assert body["session"] == "s1" and body["summary"]["tps"] == 42.5
            assert [r["session"] for r in body["records"]] == ["s1"]
            assert body["records"][0]["upstream"] == "https://d/v1"
            everything = await client.get("/api/metering?limit=1", headers=bearer)
            body = await everything.json()
            assert body["session"] is None and body["summary"]["requests"] == 2
            assert len(body["records"]) == 1 and body["records"][0]["session"] == "other"
            bad = await client.get("/api/metering?limit=x", headers=bearer)
            assert bad.status == 400
            rail = await client.get("/api/sessions?view=rail&state=current", headers=bearer)
            rows = {s["name"]: s for s in (await rail.json())["sessions"]}
            assert rows["s1"]["tps"]["tps"] == 42.5 and rows["s1"]["tps"]["model"] == "deepseek-flash"
            detail = await client.get("/api/sessions/s1/meta", headers=bearer)
            assert (await detail.json())["session"]["tps"]["tps"] == 42.5
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_web_page_draws_tps_in_the_rail_the_card_the_header_and_over_the_pty():
    from pathlib import Path

    static = Path(metering.__file__).with_name("web") / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    assert 'id="term-tps"' in html and 'id="term-tps-overlay"' in html
    for fn in ("tpsRailLine", "tpsChip", "renderTermTps", "tpsTooltip"):
        assert f"function {fn}(" in js
    assert 'typeof tpsRailLine === "function" ? tpsRailLine(s)' in js  # on every rail row
    assert 'typeof tpsChip === "function" ? tpsChip(name)' in js  # on the open card's head
    assert 'if (typeof renderTermTps === "function") renderTermTps();' in js  # every poll
    for sel in ("#session-list .rail-tps-line", ".sess-brief-tps", ".badge.tps", "#term-tps-overlay"):
        assert sel in css
    assert "pointer-events: none" in css.split("#term-tps-overlay {", 1)[1].split("}", 1)[0]
