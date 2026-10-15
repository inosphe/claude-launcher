"""MCP tools for session threads (threads_mcp.py, claunch-doj98): every call
is the calling session, and reads name it as the reader."""

from urllib.parse import parse_qs, urlsplit

import pytest

from claude_launcher import prompt_packs, mcp_server, threads_mcp


class Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        return {"threads": []}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"thread": {"id": "th-1"}}


def test_tools_are_registered():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"thread_list", "thread_read", "thread_reply", "thread_open", "thread_close"} <= names
    assert "[claunch thread" in threads_mcp.INSTRUCTIONS


def test_the_reply_format_is_markdown():
    """The agent is told the pane renders markdown, in the mcp-threads pack
    and on both tools that write text (claunch-5usj6); the server
    instructions point at that pack (claunch-8do04)."""
    assert "GitHub-flavored markdown" in prompt_packs.shipped("mcp-threads").body
    assert "markdown" in threads_mcp.INSTRUCTIONS
    assert "'mcp-threads'" in threads_mcp.INSTRUCTIONS
    tools = {t["name"]: t for t in threads_mcp.TOOLS}
    assert "markdown" in tools["thread_reply"]["description"]
    for name in ("thread_reply", "thread_open"):
        assert "markdown" in tools[name]["inputSchema"]["properties"]["text"]["description"]


def test_calls_carry_the_session(monkeypatch):
    client = Client()
    monkeypatch.setattr(threads_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    threads_mcp.call_tool("thread_list", {})
    assert parse_qs(urlsplit(client.calls[-1][1]).query) == {"session": ["s7"], "closed": ["0"]}
    threads_mcp.call_tool("thread_list", {"closed": True})
    assert parse_qs(urlsplit(client.calls[-1][1]).query)["closed"] == ["1"]
    threads_mcp.call_tool("thread_read", {"thread_id": "th-1", "after": 4})
    path = urlsplit(client.calls[-1][1])
    assert path.path == "/api/threads/th-1" and parse_qs(path.query) == {"reader": ["s7"], "after": ["4"]}
    threads_mcp.call_tool("thread_reply", {"thread_id": "th-1", "text": "done"})
    assert client.calls[-1] == ("POST", "/api/threads/th-1/messages", {"session": "s7", "text": "done"})
    threads_mcp.call_tool("thread_open", {"title": "side", "text": "hi"})
    assert client.calls[-1] == ("POST", "/api/threads", {"session": "s7", "title": "side", "text": "hi"})
    threads_mcp.call_tool("thread_close", {"thread_id": "th-1"})
    assert client.calls[-1] == ("POST", "/api/threads/th-1/close", {"session": "s7"})


def test_needs_a_session_and_a_thread_id(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    with pytest.raises(threads_mcp.ThreadMcpError, match="CLAUNCH_SESSION"):
        threads_mcp.call_tool("thread_list", {})
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    monkeypatch.setattr(threads_mcp, "_client", lambda: Client())
    with pytest.raises(threads_mcp.ThreadMcpError, match="thread_id"):
        threads_mcp.call_tool("thread_reply", {"text": "x"})
