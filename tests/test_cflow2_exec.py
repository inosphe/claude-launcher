"""cflow v2 delegate prompts: the brief a ``session`` child gets
(docs/cflow-v2.md 6.4, 7). The ``headless`` executor was removed
(claunch-xjbrh)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_launcher import daemon_client
from claude_launcher.cflow2 import executors


def test_prompt_always_has_the_fenced_json_instruction():
    brief = {"delegation": "d1", "node": "t", "tasks": [{"node": "t", "prompt": "do it"}],
             "returns": {"ok": {"hits": "int"}}, "rules": [{"id": "r", "text": "be nice"}]}
    text = executors.build_prompt(brief, errors=["missing field 'hits'"], readonly=True)
    assert "```json" in text and "do it" in text and "be nice" in text
    assert "READ-ONLY" in text and "missing field 'hits'" in text


def test_a_nested_session_delegation_is_told_to_drive_its_run():
    brief = {"delegation": "d1", "node": "n", "tasks": [], "returns": {},
             "nested": {"cwd": "/w", "scope": "s1~d1"},
             "submit": "drive the nested run under scope 's1~d1' with flow_*"}
    text = executors.session_task(brief, cwd="/w", scope="s1", receipt_path="/w/r.json")
    assert "scope 's1~d1'" in text
    assert "flow_delegate" not in text


def test_a_session_child_is_told_whose_run_it_works_for():
    """The child's own $CLAUNCH_SESSION is not the run's: a round report it
    writes under its own name lands outside the parent's Reports block."""
    brief = {"delegation": "d1", "node": "n", "tasks": [], "returns": {"ok": {}}}
    for mcp in (True, False):
        text = executors.session_task(brief, cwd="/w", scope="s1",
                                      receipt_path="/w/r.json", mcp=mcp)
        assert "run of session 's1' in /w" in text
        assert "--session s1" in text


# --------------------------------------------------------------------------- #
# the seam: an HTTP answer becomes a retryable/permanent SpawnRefused
# --------------------------------------------------------------------------- #
@pytest.fixture
def answering(monkeypatch):
    """A real HTTP server that answers every spawn with a status of the test's
    choosing, wired in where ``default_spawn`` reaches the daemon.

    The delivered failure on 2026-10-27 was a 400 from this route
    (claunch-k3acp), and the four calls that decide what happens next — the
    HTTP status, ``DaemonClientError.status``, ``daemon_client.permanent`` and
    ``default_spawn``'s wrapping — had no test between them: dropping
    ``status=exc.code`` would have brought the 34 retries a minute back with
    every test still green (review of 4f3f8b85).
    """
    from claude_launcher.daemon import cflow2_exec

    box = {"status": 400, "detail": "session 'leader' is in 2 meshes (claunch, leader)"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server's spelling
            body = json.dumps({"error": box["detail"]}).encode()
            self.send_response(box["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    client = daemon_client.DaemonClient(f"http://{host}:{port}", "token")
    monkeypatch.setattr(daemon_client, "connect", lambda: client)
    try:
        yield box
    finally:
        server.shutdown()
        server.server_close()
    # The call under test is the daemon's own spawn, not a stub of it.
    assert cflow2_exec.default_spawn.__module__ == "claude_launcher.daemon.cflow2_exec"


def test_a_refusal_about_the_request_is_carried_as_permanent(answering):
    from claude_launcher.daemon.cflow2_exec import SpawnRefused, default_spawn

    with pytest.raises(SpawnRefused) as raised:
        default_spawn("leader", {"name": "w1"})
    assert raised.value.permanent is True
    assert "2 meshes" in str(raised.value), "the daemon's own sentence is not carried"


@pytest.mark.parametrize("status", [500, 502, 503, 408, 429])
def test_every_other_answer_stays_retryable(answering, status):
    """A 5xx, a timed-out request and the two 4xx that mean "ask again"."""
    from claude_launcher.daemon.cflow2_exec import SpawnRefused, default_spawn

    answering["status"] = status
    with pytest.raises(SpawnRefused) as raised:
        default_spawn("leader", {"name": "w1"})
    assert raised.value.permanent is False
