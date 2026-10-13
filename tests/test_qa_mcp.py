"""MCP tools for session QA (qa_mcp.py, claunch-l8vyi): every call is the
calling session, and a refused compare-and-swap hands back the current
question."""

from urllib.parse import parse_qs, urlsplit

import pytest

from claude_launcher import daemon_client, mcp_server, qa_mcp


class Client:
    def __init__(self, refuse=False):
        self.calls = []
        self.refuse = refuse

    def get(self, path):
        self.calls.append(("GET", path))
        return {"questions": [{"id": "qa-1", "revision": 3, "status": "answered"}]}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if self.refuse:
            raise daemon_client.DaemonClientError(f"POST {path}: question qa-1 is at revision 3, not 1")
        return {"question": {"id": "qa-1"}}


def test_tools_are_registered():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"qa_ask", "qa_update", "qa_close", "qa_delete", "qa_list"} <= names
    assert "qa_answer" not in names   # answering is the user's


def test_calls_carry_the_session(monkeypatch):
    client = Client()
    monkeypatch.setattr(qa_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    qa_mcp.call_tool("qa_ask", {"title": "Q", "options": ["a"], "multi": False})
    assert client.calls[-1] == ("POST", "/api/qa", {"title": "Q", "options": ["a"], "multi": False, "session": "s7"})
    qa_mcp.call_tool("qa_update", {"id": "qa-1", "revision": 1, "body": "b"})
    assert client.calls[-1] == ("POST", "/api/qa/qa-1", {"revision": 1, "body": "b", "session": "s7"})
    qa_mcp.call_tool("qa_close", {"id": "qa-1", "revision": 2, "note": "done"})
    assert client.calls[-1] == ("POST", "/api/qa/qa-1/close", {"revision": 2, "note": "done", "session": "s7"})
    qa_mcp.call_tool("qa_delete", {"id": "qa-1", "revision": 3})
    assert client.calls[-1] == ("POST", "/api/qa/qa-1/delete", {"revision": 3, "session": "s7"})
    qa_mcp.call_tool("qa_list", {})
    assert parse_qs(urlsplit(client.calls[-1][1]).query) == {"session": ["s7"], "closed": ["0"]}


def test_refused_write_returns_the_current_question(monkeypatch):
    client = Client(refuse=True)
    monkeypatch.setattr(qa_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    with pytest.raises(qa_mcp.QaMcpError) as err:
        qa_mcp.call_tool("qa_update", {"id": "qa-1", "revision": 1, "title": "x"})
    assert "current:" in str(err.value) and '"revision": 3' in str(err.value)


def test_needs_a_managed_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    with pytest.raises(qa_mcp.QaMcpError, match="CLAUNCH_SESSION"):
        qa_mcp.call_tool("qa_list", {})
