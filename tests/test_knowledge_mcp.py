from urllib.parse import parse_qs, urlsplit

import pytest

from claude_launcher import knowledge_mcp, mcp_server


class Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        return {"cwd": "/repo/.claude/worktrees/w"} if path.startswith("/api/sessions/") else {"records": []}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"record": {"id": "r1"}}


def test_tools_are_registered():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"knowledge_search", "knowledge_record", "knowledge_history"} <= names


def test_record_uses_session_workspace_and_author(monkeypatch):
    client = Client()
    monkeypatch.setattr(knowledge_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    knowledge_mcp.call_tool("knowledge_record", {"kind": "need", "title": "t", "body": "b",
                                                 "refs": ["claunch-1"]})
    assert client.calls[0] == ("GET", "/api/sessions/s7")
    method, path, body = client.calls[1]
    assert (method, path) == ("POST", "/api/knowledge")
    assert body["author"] == "s7" and body["cwd"].endswith("/w") and body["refs"] == ["claunch-1"]


def test_search_with_explicit_workspace(monkeypatch):
    client = Client()
    monkeypatch.setattr(knowledge_mcp, "_client", lambda: client)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    knowledge_mcp.call_tool("knowledge_search", {"workspace": "alpha", "query": "착지", "kind": "decision"})
    params = parse_qs(urlsplit(client.calls[0][1]).query)
    assert params == {"workspace": ["alpha"], "q": ["착지"], "kind": ["decision"], "limit": ["10"]}
    with pytest.raises(knowledge_mcp.KnowledgeMcpError):
        knowledge_mcp.call_tool("knowledge_delete", {})


def test_search_passes_the_given_limit(monkeypatch):
    client = Client()
    monkeypatch.setattr(knowledge_mcp, "_client", lambda: client)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    knowledge_mcp.call_tool("knowledge_search", {"workspace": "alpha", "query": "착지", "limit": 3})
    assert parse_qs(urlsplit(client.calls[0][1]).query)["limit"] == ["3"]


def test_history_reads_the_record_versions_path(monkeypatch):
    client = Client()
    monkeypatch.setattr(knowledge_mcp, "_client", lambda: client)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    knowledge_mcp.call_tool("knowledge_history", {"workspace": "alpha", "id": "md:v", "limit": 5})
    method, path = client.calls[0]
    assert method == "GET" and urlsplit(path).path == "/api/knowledge/md%3Av/history"
    assert parse_qs(urlsplit(path).query) == {"workspace": ["alpha"], "limit": ["5"]}
    with pytest.raises(knowledge_mcp.KnowledgeMcpError):
        knowledge_mcp.call_tool("knowledge_history", {"workspace": "alpha"})
