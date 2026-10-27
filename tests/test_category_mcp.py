import os
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_launcher import category_mcp, mcp_server


class Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        return {"cwd": "/registered/repo/.claude/worktrees/w"} if path.startswith("/api/sessions/") else {}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"path": ["web"]}


@pytest.fixture
def client(monkeypatch):
    c = Client()
    monkeypatch.setattr(category_mcp, "_client", lambda: c)
    return c


def test_tools_are_registered_and_offer_no_review():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"category_tree", "category_search", "category_items", "category_assign"} <= names
    # Accepting a new category is a person's: no review tool for agents.
    assert all("review" not in t["name"] and "accept" not in t["name"] for t in category_mcp.TOOLS)


def test_reads_default_to_the_registered_session_cwd(client, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    category_mcp.call_tool("category_tree", {})
    assert client.calls[0] == ("GET", "/api/sessions/s1")
    assert parse_qs(urlsplit(client.calls[1][1]).query)["cwd"] == ["/registered/repo/.claude/worktrees/w"]
    category_mcp.call_tool("category_search", {"workspace": "alpha", "query": "탭", "limit": 3})
    assert parse_qs(urlsplit(client.calls[2][1]).query) == {"workspace": ["alpha"], "q": ["탭"], "limit": ["3"]}
    category_mcp.call_tool("category_items", {"workspace": "alpha", "node_id": "n1", "descendants": False})
    path = urlsplit(client.calls[3][1])
    assert path.path == "/api/category/nodes/n1/items"
    assert parse_qs(path.query)["descendants"] == ["0"]


def test_assign_posts_as_the_calling_session(client, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1-cat1")
    category_mcp.call_tool("category_assign", {"node_id": "n1", "reason": "UI"})
    assert client.calls[0] == ("POST", "/api/category/agent-assign",
                               {"node_id": "n1", "reason": "UI", "session": "s1-cat1"})
    category_mcp.call_tool("category_assign", {"new": {"label": "tabs"}, "reason": "r"})
    assert client.calls[1][2]["new"] == {"label": "tabs"}


def test_the_proposal_flag_is_forwarded_and_off_by_default(client, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1-cat1")
    category_mcp.call_tool("category_assign", {"new": {"label": "tabs"}, "reason": "r",
                                               "proposal": True})
    assert client.calls[0][2]["proposal"] is True
    # Omitted: the daemon creates the node now (no review card).
    category_mcp.call_tool("category_assign", {"new": {"label": "panes"}, "reason": "r"})
    assert "proposal" not in client.calls[1][2]


def test_assign_outside_a_session_and_unknown_tools_are_refused(client, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    with pytest.raises(category_mcp.CategoryMcpError):
        category_mcp.call_tool("category_assign", {"node_id": "n1"})
    with pytest.raises(category_mcp.CategoryMcpError):
        category_mcp.call_tool("category_review", {})
    category_mcp.call_tool("category_tree", {})
    assert parse_qs(urlsplit(client.calls[0][1]).query)["cwd"] == [os.getcwd()]


def test_fold_and_split_reach_the_agent_route(client, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    category_mcp.call_tool("category_assign", {
        "fold": {"label": "pages", "nodes": ["n1", "n2"]}, "reason": "flat"})
    body = client.calls[-1][2]
    assert body["fold"] == {"label": "pages", "nodes": ["n1", "n2"]}
    assert body["session"] == "s1"
    category_mcp.call_tool("category_assign", {
        "split": {"parts": [{"label": "a"}, {"label": "b"}]}, "reason": "two"})
    assert client.calls[-1][2]["split"]["parts"] == [{"label": "a"}, {"label": "b"}]
    category_mcp.call_tool("category_assign", {
        "group": {"label": "pages", "nodes": ["n1", "n2"], "place": "n1"}, "reason": "siblings"})
    assert client.calls[-1][2]["group"] == {"label": "pages", "nodes": ["n1", "n2"], "place": "n1"}


def test_the_tools_name_fold_and_split(client, monkeypatch):
    assign = next(t for t in category_mcp.TOOLS if t["name"] == "category_assign")
    props = assign["inputSchema"]["properties"]
    assert {"fold", "split", "group"} <= set(props)
    assert props["group"]["required"] == ["label", "nodes"]
    assert assign["inputSchema"]["properties"]["fold"]["required"] == ["label", "nodes"]
    assert assign["inputSchema"]["properties"]["split"]["required"] == ["parts"]
