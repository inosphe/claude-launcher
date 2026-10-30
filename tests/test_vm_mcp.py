"""The vm_* tools: names, the session they act as, and the epoch they carry."""
from __future__ import annotations

import pytest

from claude_launcher import mcp_server, request_spool, vm_mcp


class Client:
    def __init__(self):
        self.calls = []
        self.answer = {"ok": True, "epoch": 4}

    def post(self, path, body=None, **kw):
        self.calls.append(("POST", path, body))
        return self.answer

    def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        return {"vms": []}


@pytest.fixture
def client(monkeypatch):
    c = Client()
    monkeypatch.setattr(vm_mcp, "_client", lambda: c)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    vm_mcp._epochs.clear()
    return c


def test_the_tools_are_in_the_merged_server_without_collisions():
    names = {t["name"] for t in mcp_server.SERVER.tools}
    assert {t["name"] for t in vm_mcp.TOOLS} <= names
    assert len(vm_mcp.TOOLS) == 7


def test_a_vm_write_is_never_queued_for_later(client):
    # A click delivered after the daemon came back would act on a screen the
    # session has not seen.
    for path in ("/api/vm/a/call", "/api/vm/a/exec", "/api/vm/a/acquire",
                 "/api/vm/a/release"):
        assert not request_spool.spoolable("POST", path), path


def test_acquire_remembers_the_epoch_and_cua_calls_carry_it(client):
    vm_mcp.call_tool("vm_acquire", {"vm": "a"})
    vm_mcp.call_tool("vm_cua", {"vm": "a", "tool": "click", "arguments": {"x": 1}})
    _, path, body = client.calls[-1]
    assert path == "/api/vm/a/call"
    assert body == {"session": "s7", "tool": "click", "arguments": {"x": 1},
                    "epoch": 4, "timeout": 30.0}


def test_a_held_vm_does_not_store_an_epoch(client):
    client.answer = {"ok": False, "held_by": "s1", "epoch": 9}
    vm_mcp.call_tool("vm_acquire", {"vm": "a"})
    assert "a" not in vm_mcp._epochs


def test_release_forgets_the_epoch(client):
    vm_mcp.call_tool("vm_acquire", {"vm": "a"})
    vm_mcp.call_tool("vm_release", {"vm": "a"})
    assert "a" not in vm_mcp._epochs


def test_a_screenshot_is_a_view_call_to_get_desktop_state(client):
    vm_mcp.call_tool("vm_screenshot", {"vm": "a"})
    _, path, body = client.calls[-1]
    assert path == "/api/vm/a/call" and body["tool"] == "get_desktop_state"
    assert "epoch" not in body


def test_exec_carries_the_session_and_epoch(client):
    vm_mcp.call_tool("vm_acquire", {"vm": "a"})
    vm_mcp.call_tool("vm_exec", {"vm": "a", "command": "id"})
    _, path, body = client.calls[-1]
    assert path == "/api/vm/a/exec" and body["command"] == "id" and body["epoch"] == 4


def test_list_asks_only_for_this_sessions_vms(client):
    vm_mcp.call_tool("vm_list", {})
    assert client.calls[-1][1] == "/api/vm?session=s7"


def test_outside_a_session_there_is_no_vm_tool(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    with pytest.raises(vm_mcp.VmMcpError):
        vm_mcp.call_tool("vm_list", {})


def test_a_vm_id_cannot_climb_out_of_its_path(client):
    vm_mcp.call_tool("vm_release", {"vm": "../x"})
    assert client.calls[-1][1] == "/api/vm/..%2Fx/release"
