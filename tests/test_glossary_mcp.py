import os
from urllib.parse import parse_qs, urlsplit
import pytest
from claude_launcher import glossary_mcp, mcp_server


class Client:
    def __init__(self):
        self.calls = []
    def get(self, path):
        self.calls.append(("GET", path))
        return {"cwd": "/registered/repo/.claude/worktrees/worker"} if path.startswith("/api/sessions/") else {"terms": []}
    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"suggestion": {"status": "pending"}}


def test_tools_are_registered_and_offer_proposals_and_reads():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"glossary_suggest", "glossary_search", "glossary_resolve"} <= names
    assert all("review" not in t["name"] for t in glossary_mcp.TOOLS)


def test_default_workspace_uses_registered_session_cwd(monkeypatch):
    client = Client()
    monkeypatch.setattr(glossary_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s94")
    glossary_mcp.call_tool("glossary_suggest", {"term": "base model", "definition": "starting model"})
    assert client.calls[0] == ("GET", "/api/sessions/s94")
    assert client.calls[1][1] == "/api/glossary/suggestions"
    assert client.calls[1][2]["proposed_by"] == "s94"
    assert client.calls[1][2]["cwd"].endswith("/worker")


def test_explicit_workspace_and_resolution(monkeypatch):
    client = Client()
    monkeypatch.setattr(glossary_mcp, "_client", lambda: client)
    glossary_mcp.call_tool("glossary_search", {"workspace": "alpha", "query": "기반 모델", "limit": 7})
    params = parse_qs(urlsplit(client.calls[0][1]).query)
    assert params == {"workspace": ["alpha"], "q": ["기반 모델"], "limit": ["7"]}
    glossary_mcp.call_tool("glossary_resolve", {"workspace": "alpha", "text": "[[term:base model]]"})
    assert client.calls[1][1] == "/api/glossary/resolve"
    assert client.calls[1][2]["text"] == "[[term:base model]]"
    with pytest.raises(glossary_mcp.GlossaryMcpError):
        glossary_mcp.call_tool("glossary_accept", {})


def test_unmanaged_server_defaults_to_current_directory(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    client = Client()
    monkeypatch.setattr(glossary_mcp, "_client", lambda: client)
    glossary_mcp.call_tool("glossary_search", {"query": "base"})
    assert parse_qs(urlsplit(client.calls[0][1]).query)["cwd"] == [os.getcwd()]
