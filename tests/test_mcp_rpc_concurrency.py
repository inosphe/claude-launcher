"""``mcp_rpc.Server.serve`` answers a call while another is still running, and
a cancelled call stops (claunch-368ef)."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import types

from claude_launcher import mcp_rpc


class _Boom(Exception):
    pass


def _slow(name: str, args: dict) -> dict:
    if name == "slow":
        cancelled = mcp_rpc.cancel_event().wait(30)
        return {"cancelled": cancelled}
    return {"tool": name}


SERVER = mcp_rpc.Server(
    name="test",
    tools=({"name": "slow"}, {"name": "fast"}),
    dispatch=_slow,
    errors=(_Boom,),
)


class _Wire:
    """``serve`` on a thread, speaking to it over two pipes."""

    def __init__(self, monkeypatch):
        in_r, self._in_w = os.pipe()
        self._out_r, out_w = os.pipe()
        monkeypatch.setattr(
            mcp_rpc.sys, "stdin", types.SimpleNamespace(buffer=os.fdopen(in_r, "rb")))
        monkeypatch.setattr(
            mcp_rpc.sys, "stdout", types.SimpleNamespace(buffer=os.fdopen(out_w, "wb")))
        self._writer = os.fdopen(self._in_w, "wb")
        self._lines: queue.Queue = queue.Queue()
        reader = os.fdopen(self._out_r, "rb")
        threading.Thread(
            target=lambda: [self._lines.put(line) for line in iter(reader.readline, b"")],
            daemon=True,
        ).start()
        self.thread = threading.Thread(target=SERVER.serve, daemon=True)
        self.thread.start()

    def send(self, msg: dict) -> None:
        self._writer.write(json.dumps(msg).encode() + b"\n")
        self._writer.flush()

    def call(self, msg_id, tool: str) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                   "params": {"name": tool, "arguments": {}}})

    def cancel(self, msg_id) -> None:
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                   "params": {"requestId": msg_id}})

    def read(self) -> dict:
        # A loop that answers one line at a time never answers here; fail
        # instead of hanging the run.
        return json.loads(self._lines.get(timeout=10))

    def close(self) -> None:
        self._writer.close()
        self.thread.join(10)


def test_a_call_is_answered_while_an_earlier_one_still_runs(monkeypatch):
    wire = _Wire(monkeypatch)
    started = time.monotonic()
    wire.call(1, "slow")
    wire.call(2, "fast")

    response = wire.read()

    assert response["id"] == 2
    assert time.monotonic() - started < 5
    wire.cancel(1)
    wire.close()
    assert not wire.thread.is_alive()


def test_a_cancelled_call_stops_and_sends_no_answer(monkeypatch):
    wire = _Wire(monkeypatch)
    started = time.monotonic()
    wire.call(1, "slow")
    wire.cancel(1)
    wire.call(2, "fast")

    # The next line on the wire is call 2's answer: call 1 sent none.
    assert wire.read()["id"] == 2
    wire.close()
    assert not wire.thread.is_alive()
    assert time.monotonic() - started < 5


def test_closing_stdin_ends_the_calls_in_flight(monkeypatch):
    wire = _Wire(monkeypatch)
    started = time.monotonic()
    wire.call(1, "slow")
    wire.close()

    assert not wire.thread.is_alive()
    assert time.monotonic() - started < 5


def test_requests_other_than_tools_call_are_answered_in_order(monkeypatch):
    wire = _Wire(monkeypatch)
    wire.call(1, "slow")
    wire.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    wire.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})

    assert [wire.read()["id"], wire.read()["id"]] == [2, 3]
    wire.cancel(1)
    wire.close()
