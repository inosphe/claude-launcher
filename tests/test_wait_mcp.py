from __future__ import annotations

import types

import pytest

from claude_launcher import wait_mcp


def test_wait_mcp_sleeps_for_a_valid_interval(monkeypatch):
    slept = []
    moments = iter((10.0, 40.125))
    # ``wait_mcp.time`` is the process-wide ``time`` module: patching its
    # ``monotonic`` hands the two-value iterator to every other caller (pytest,
    # threads) until teardown, and the third call raises StopIteration. Swap the
    # module's own reference for a stand-in instead.
    monkeypatch.setattr(
        wait_mcp,
        "time",
        types.SimpleNamespace(sleep=slept.append, monotonic=lambda: next(moments)),
    )

    assert wait_mcp.call_tool("wait", {"seconds": 30}) == {
        "requested_seconds": 30.0,
        "elapsed_seconds": 30.125,
    }
    assert slept == [30.0]


@pytest.mark.parametrize("seconds", [None, True, "30", float("inf"), 29.9, 600.1])
def test_wait_mcp_rejects_invalid_intervals(seconds):
    with pytest.raises(wait_mcp.WaitMcpError):
        wait_mcp.call_tool("wait", {"seconds": seconds})


def test_wait_mcp_schema_and_rpc_errors_expose_the_bounds():
    schema = wait_mcp.TOOLS[0]["inputSchema"]
    assert schema["properties"]["seconds"]["minimum"] == 30
    assert schema["properties"]["seconds"]["maximum"] == 600

    response = wait_mcp.SERVER.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "wait", "arguments": {"seconds": 12}},
        }
    )
    assert response["result"]["isError"] is True
    assert "30 to 600" in response["result"]["content"][0]["text"]
