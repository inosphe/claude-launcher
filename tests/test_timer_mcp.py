"""The ``timer`` MCP tool (claunch-lvrcp).

The tool is a thin door to the daemon's own route: it carries the calling
session from ``$CLAUNCH_SESSION`` and hands (every, times, message) over.
What the timer *does* is the daemon's (tests/test_agent_timers.py).
"""

from __future__ import annotations

import pytest

from claude_launcher import timer_mcp


class FakeClient:
    def __init__(self):
        self.posts = []

    def post(self, path, body=None, **kwargs):
        self.posts.append((path, body))
        return {"session": "s9", "timer": {"id": "t-1", **(body or {})}}


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        timer_mcp.daemon_client, "connect_with_diagnosis", lambda: (fake, None))
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    return fake


def test_the_tool_is_named_timer_and_asks_for_the_three_values():
    tools = {t["name"]: t for t in timer_mcp.TOOLS}
    assert set(tools) == {"timer"}
    schema = tools["timer"]["inputSchema"]
    assert schema["required"] == ["every", "times", "message"]
    assert schema["properties"]["every"]["minimum"] == 10
    assert schema["properties"]["times"]["type"] == "integer"


def test_call_posts_to_the_sessions_own_route(client):
    got = timer_mcp.call_tool(
        "timer", {"every": 600, "times": 4, "message": "status report"})
    assert client.posts == [
        ("/api/sessions/s9/timers",
         {"every": 600, "times": 4, "message": "status report"})]
    assert got["timer"]["id"] == "t-1"


def test_call_refuses_without_a_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    with pytest.raises(timer_mcp.TimerMcpError, match="CLAUNCH_SESSION"):
        timer_mcp.call_tool("timer", {"every": 60, "times": 1, "message": "go"})


def test_call_refuses_an_empty_message(client):
    with pytest.raises(timer_mcp.TimerMcpError, match="message"):
        timer_mcp.call_tool("timer", {"every": 60, "times": 1, "message": "  "})
    assert client.posts == []


def test_call_refuses_an_unknown_tool():
    with pytest.raises(timer_mcp.TimerMcpError, match="unknown tool"):
        timer_mcp.call_tool("timers", {})


def test_the_merged_server_offers_the_tool():
    from claude_launcher import mcp_server

    assert "timer" in [t["name"] for t in mcp_server.TOOLS]
