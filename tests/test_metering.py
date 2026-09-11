"""Per-request throughput records for API-key providers.

The pure parts (the SSE/JSON reader, the config switches, the session header,
the aggregation) are asserted directly. The part that earns its keep is the
end-to-end one: a real shim in front of a throwaway upstream that streams an
Anthropic-shaped completion, and a record with this session's name, its
token counts and a TPS lands in the launcher home.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_launcher import cli, credentials, metering, profile, routing, runner, store


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
    assert rec["ttft_ms"] == rec["ttfb_ms"]
    assert rec["counted"] is True


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


def test_merge_body_with_an_empty_spec_is_the_identity():
    raw = b'{"model":"m"}'
    assert routing.merge_body(raw, {}) is raw


# --------------------------------------------------------------------------- #
# records and the aggregation `claunch tps` prints
# --------------------------------------------------------------------------- #
def _rec(**kw):
    base = {
        "ts": "2026-09-11T10:00:00+0900", "session": "s1", "model": "m", "status": 200,
        "output_tokens": 100, "input_tokens": 10, "cache_read": 0,
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
    assert s["tps_median"] == 60.0 and s["tps_min"] == 50.0 and s["tps_max"] == 70.0
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
    assert "tps median 50.0" in out
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
                for ev in STREAM:
                    chunk = _sse([ev])
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
