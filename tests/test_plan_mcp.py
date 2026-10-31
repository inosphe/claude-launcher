"""MCP tools for session plans (plan_mcp.py, claunch-7jzp8): every write is
the calling session's own plan, and plan_get reads any session's."""

from claude_launcher import mcp_server, plan_mcp


class Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        return {"plan": None}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"plan": {}}


def test_tools_are_registered_and_deciding_is_not_one():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert set(plan_mcp.TOOL_NAMES) <= names
    assert "plan_decide" not in names   # deciding is the user's


def test_calls_carry_the_session(monkeypatch):
    client = Client()
    monkeypatch.setattr(plan_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    plan_mcp.call_tool("plan_set", {"revision": 0, "title": "T", "steps": [{"title": "a"}]})
    assert client.calls[-1] == ("POST", "/api/plan", {"revision": 0, "title": "T", "steps": [{"title": "a"}],
                                                      "session": "s7", "by": "s7"})
    plan_mcp.call_tool("plan_node", {"id": "s 1", "status": "done"})
    assert client.calls[-1] == ("POST", "/api/plan/node/s%201", {"status": "done", "session": "s7", "by": "s7"})
    plan_mcp.call_tool("plan_get", {})
    assert client.calls[-1] == ("GET", "/api/plan?session=s7")
    plan_mcp.call_tool("plan_get", {"session": "s9"})
    assert client.calls[-1] == ("GET", "/api/plan?session=s9")
    plan_mcp.call_tool("plan_get", {"joint": True})
    assert client.calls[-1] == ("GET", "/api/plan/joint?session=s7")
    plan_mcp.call_tool("plan_overview", {})
    assert client.calls[-1] == ("GET", "/api/plan/overview")
    plan_mcp.call_tool("plan_overview", {"project": "claunch", "all": True})
    assert client.calls[-1] == ("GET", "/api/plan/overview?project=claunch&all=1")
    plan_mcp.call_tool("plan_clear", {"revision": 3})
    assert client.calls[-1] == ("POST", "/api/plan/clear", {"revision": 3, "session": "s7", "by": "s7"})
