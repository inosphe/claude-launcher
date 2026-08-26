"""Body routing: the spec merge, the config surface, and a real shim process.

The pure parts (:func:`routing.merge_body`, fingerprints) are cheap to assert.
The part that actually earns the tests is the process: a shim is spawned for
real, in front of a throwaway upstream, and asked to prove that the routing
field reaches that upstream — because the whole feature exists to stop a pin
from being silently dropped, and only an end-to-end request can show it isn't.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_launcher import credentials, profile, routing, runner, store

SPEC = {"order": ["coreweave"], "allow_fallbacks": False}


# --------------------------------------------------------------------------- #
# the pure merge
# --------------------------------------------------------------------------- #
def test_merge_adds_the_routing_field():
    out = json.loads(routing.merge_body(b'{"model":"m","max_tokens":1}', SPEC))
    assert out["provider"] == SPEC
    assert out["model"] == "m"  # and leaves everything else alone


def test_merge_keeps_an_explicit_choice():
    raw = b'{"model":"m","provider":{"order":["together"]}}'
    assert json.loads(routing.merge_body(raw, SPEC))["provider"] == {
        "order": ["together"]
    }


@pytest.mark.parametrize(
    "raw", [b"", b"not json at all", b"[1,2,3]", b'"a string"', b"\xff\xfe binary"]
)
def test_merge_passes_through_anything_that_is_not_a_json_object(raw):
    assert routing.merge_body(raw, SPEC) == raw


def test_fingerprint_ignores_key_order_but_not_content():
    a = routing.fingerprint("https://u/", {"order": ["cw"], "allow_fallbacks": False})
    b = routing.fingerprint("https://u/", {"allow_fallbacks": False, "order": ["cw"]})
    assert a == b
    assert a != routing.fingerprint("https://u/", {"order": ["other"]})
    assert a != routing.fingerprint("https://other/", {"order": ["cw"]})


def test_ports_are_derived_from_the_fingerprint(home):
    fp = routing.fingerprint("https://u/", SPEC)
    ports = routing.candidate_ports(fp)
    assert ports == routing.candidate_ports(fp)  # stable across calls
    assert len(set(ports)) == len(ports)
    assert all(1024 < p < 49152 for p in ports)  # clear of the ephemeral range


# --------------------------------------------------------------------------- #
# the config surface
# --------------------------------------------------------------------------- #
def _write_provider(routing_block=None):
    def _mutate(doc: dict) -> None:
        entry = {"env": {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api/"}}
        if routing_block is not None:
            entry["routing"] = routing_block
        doc["providers"] = {"openrouter": entry}

    store.update(_mutate)


def test_spec_reads_the_provider_block(home):
    _write_provider(SPEC)
    assert routing.spec("openrouter") == SPEC
    assert routing.configured() == {"openrouter": SPEC}


def test_no_routing_block_is_not_an_error(home):
    _write_provider()
    assert routing.spec("openrouter") is None
    assert routing.configured() == {}
    assert routing.spec("nonexistent") is None


@pytest.mark.parametrize("bad", ["coreweave", [], {}, 3])
def test_a_malformed_routing_block_is_rejected(home, bad):
    _write_provider(bad)
    with pytest.raises(routing.RoutingError):
        routing.spec("openrouter")


def test_set_and_clear_round_trip(home):
    _write_provider()
    routing.set_spec("openrouter", SPEC)
    assert routing.spec("openrouter") == SPEC
    routing.set_spec("openrouter", None)
    assert routing.spec("openrouter") is None
    # and the rest of the provider survived the edit
    assert store.load()["providers"]["openrouter"]["env"]["ANTHROPIC_BASE_URL"]


def test_set_on_an_unknown_provider_is_refused(home):
    _write_provider()
    with pytest.raises(routing.RoutingError):
        routing.set_spec("nope", SPEC)


def test_routing_without_a_base_url_is_refused(home):
    with pytest.raises(routing.RoutingError):
        routing.apply({}, "openrouter", {"providers": {"openrouter": {"routing": SPEC}}})


# --------------------------------------------------------------------------- #
# a real shim in front of a real (throwaway) upstream
# --------------------------------------------------------------------------- #
class _Upstream:
    """Records what it was asked, answers what the test told it to."""

    def __init__(self):
        self.requests = []
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/api/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # keep pytest output clean
                pass

            def _record(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "headers": dict(self.headers),
                    }
                )
                return body

            def do_GET(self):
                self._record()
                payload = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                self._record()
                if self.path.endswith("/stream"):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for i in range(3):
                        chunk = f"data: {i}\n\n".encode()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                payload = json.dumps({"seen": len(outer.requests)}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler


@pytest.fixture
def upstream():
    server = _Upstream()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def shims(home):
    """Stop whatever a test started — the shim outlives the process that spawns it."""
    yield
    routing.stop()


def _post(url: str, payload, content_type="application/json", timeout=20):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"content-type": content_type}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


@pytest.mark.slow_shim
def test_shim_merges_the_spec_into_what_the_upstream_receives(upstream, shims):
    base = routing.ensure_shim(upstream.url, SPEC)
    status, _ = _post(base + "v1/messages", {"model": "m", "max_tokens": 1})
    assert status == 200
    seen = upstream.requests[-1]
    assert seen["path"] == "/api/v1/messages"  # upstream path prefix preserved
    assert json.loads(seen["body"])["provider"] == SPEC


@pytest.mark.slow_shim
def test_shim_leaves_non_json_and_reads_headers_through(upstream, shims):
    base = routing.ensure_shim(upstream.url, SPEC)
    _post(base + "v1/upload", b"raw-bytes", content_type="application/octet-stream")
    seen = upstream.requests[-1]
    assert seen["body"] == b"raw-bytes"

    with urllib.request.urlopen(base + "v1/models?limit=2", timeout=20) as resp:
        assert json.loads(resp.read())["path"] == "/api/v1/models?limit=2"


@pytest.mark.slow_shim
def test_shim_streams_a_response_instead_of_buffering_it(upstream, shims):
    base = routing.ensure_shim(upstream.url, SPEC)
    req = urllib.request.Request(
        base + "v1/stream",
        data=b'{"model":"m"}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        assert resp.headers.get("content-type") == "text/event-stream"
        assert resp.read() == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


@pytest.mark.slow_shim
def test_one_shim_serves_every_caller_of_the_same_pair(upstream, shims):
    first = routing.ensure_shim(upstream.url, SPEC)
    second = routing.ensure_shim(upstream.url, dict(reversed(list(SPEC.items()))))
    assert first == second  # same pair, same port — key order is not identity
    assert len(routing.instances()) == 1


@pytest.mark.slow_shim
def test_simultaneous_launches_converge_on_one_shim(upstream, shims):
    """The start race is the reason the port is derived rather than allocated.

    Two sessions launching at the same moment both aim at the same port; one
    wins the bind and the loser must find the winner rather than fail.
    """
    results: list = []
    barrier = threading.Barrier(4)

    def _go():
        barrier.wait()
        try:
            results.append(routing.ensure_shim(upstream.url, SPEC))
        except Exception as exc:  # recorded, so a loser's failure is visible
            results.append(exc)

    threads = [threading.Thread(target=_go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 4
    assert all(isinstance(r, str) for r in results), results
    assert len(set(results)) == 1  # everyone got the same shim
    assert len(routing.instances()) == 1


@pytest.mark.slow_shim
def test_a_changed_spec_gets_its_own_shim(upstream, shims):
    first = routing.ensure_shim(upstream.url, SPEC)
    second = routing.ensure_shim(upstream.url, {"order": ["together"]})
    assert first != second
    assert len(routing.instances()) == 2


@pytest.mark.slow_shim
def test_stop_reaches_a_second_shim_serving_the_same_pair(upstream, shims):
    """Records are per shim, so a duplicate cannot hide from ``stop``.

    Duplicates should no longer happen (the start claim prevents them), but one
    left behind by a crash or an older build is a loopback proxy holding a port,
    and `claunch routing` has to be able to see and end it.
    """
    routing.ensure_shim(upstream.url, SPEC)
    fp = routing.fingerprint(upstream.url, SPEC)
    stray = routing.candidate_ports(fp)[3]
    routing._spawn(upstream.url, SPEC, stray, fp)
    for _ in range(150):
        info = routing.health(stray, timeout=0.5)
        if info is not None:
            routing._record(fp, stray, upstream.url, SPEC, info.get("pid"))
            break
        time.sleep(0.1)
    else:
        pytest.fail("the second shim never came up")

    assert len(routing.instances()) == 2
    assert routing.stop() == [fp, fp]
    assert routing.instances() == []
    assert routing.health(stray) is None


@pytest.mark.slow_shim
def test_stop_takes_the_shim_down_and_forgets_it(upstream, shims):
    base = routing.ensure_shim(upstream.url, SPEC)
    port = int(base.rstrip("/").rsplit(":", 1)[1])
    assert routing.health(port) is not None
    assert routing.stop() == [routing.fingerprint(upstream.url, SPEC)]
    assert routing.health(port) is None
    assert routing.instances() == []


@pytest.mark.slow_shim
def test_child_env_points_claude_at_the_shim(home, upstream, shims, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def _mutate(doc: dict) -> None:
        doc["providers"] = {
            "openrouter": {
                "env": {
                    "ANTHROPIC_BASE_URL": upstream.url,
                    "ANTHROPIC_AUTH_TOKEN": "sk-or-test",
                },
                "routing": SPEC,
            }
        }

    store.update(_mutate)
    p = profile.create("work")
    credentials.save_token(p, "sk-or-stored")
    store.set_profile_field("work", "provider", "openrouter")

    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_BASE_URL"] == routing.ensure_shim(upstream.url, SPEC)
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    # the auth route is untouched by routing
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-stored"

    # and a request through that URL really carries the pin
    _post(env["ANTHROPIC_BASE_URL"] + "v1/messages", {"model": "m"})
    assert json.loads(upstream.requests[-1]["body"])["provider"] == SPEC


def test_child_env_leaves_a_provider_without_routing_alone(home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    _write_provider()
    p = profile.create("work")
    credentials.save_token(p, "sk-or-stored")
    store.set_profile_field("work", "provider", "openrouter")
    env = runner.child_env(p, with_token=True)
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api/"
