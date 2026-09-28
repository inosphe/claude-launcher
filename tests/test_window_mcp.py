from __future__ import annotations

from claude_launcher import window_mcp


class _Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("get", path, None))
        return {"holders": [], "queue": []}

    def post(self, path, body):
        self.calls.append(("post", path, body))
        return {"cancelled": 3}


def test_window_mcp_reads_status_and_cancels_only_the_callers_waits(monkeypatch):
    client = _Client()
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    monkeypatch.setattr(window_mcp, "_client", lambda: client)

    assert window_mcp.call_tool("window_status", {}) == {"holders": [], "queue": []}
    assert window_mcp.call_tool("window_cancel", {}) == {"cancelled": 3}
    assert client.calls == [
        ("get", "/api/window", None),
        ("post", "/api/window/cancel", {"session": "s1"}),
    ]


def test_window_mcp_reads_the_history_with_its_filters(monkeypatch):
    client = _Client()
    monkeypatch.setattr(window_mcp, "_client", lambda: client)
    window_mcp.call_tool("window_history", {"days": 2, "class": "sweep", "limit": 5})
    window_mcp.call_tool("window_history", {})
    assert client.calls == [
        ("get", "/api/window/history?limit=5&days=2&class=sweep", None),
        ("get", "/api/window/history?limit=20", None),
    ]
    tool = next(t for t in window_mcp.TOOLS if t["name"] == "window_history")
    assert "7 days" in tool["description"]


def test_window_mcp_refuses_cancellation_without_a_managed_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    response = window_mcp.SERVER.handle(
        {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "window_cancel", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    assert "$CLAUNCH_SESSION" in response["result"]["content"][0]["text"]
