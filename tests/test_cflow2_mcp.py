"""cflow v2 flow_* MCP tools."""

from __future__ import annotations

import json
import textwrap

import pytest

from claude_launcher import mcp_server
from claude_launcher.cflow2 import mcp as cflow2_mcp

FLOW_TOOLS = {"flow_start", "flow_status", "flow_report", "flow_select", "flow_fork",
              "flow_delegate", "flow_rule", "flow_collection"}

DEF = textwrap.dedent("""
    cflow: 2
    name: demo2
    description: demo
    rules:
      careful: {text: be careful, why: it broke once, deliver: all}
    nodes:
      first: {prompt: "first for ${params.who}", returns: {ok: {n: int}}}
      second: {prompt: second}
    params:
      who: {default: me}
    body:
      seq:
        - first
        - choose:
            options:
              go: {then: second}
              stop: {then: end}
""")


@pytest.fixture
def project(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows" / "v2").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "v2" / "demo2.yaml").write_text(DEF, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    return d


def _call(name, args):
    resp = mcp_server.SERVER.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    result = resp["result"]
    text = result["content"][0]["text"]
    if result["isError"]:
        return None, text
    return json.loads(text), None


def test_tools_listed_with_schemas():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert FLOW_TOOLS <= names
    for tool in cflow2_mcp.TOOLS:
        assert tool["inputSchema"]["type"] == "object"
        assert len(tool["description"]) > 60


def test_round_trip(project):
    st, err = _call("flow_status", {})
    assert err is None and st["status"] == "idle"
    st, err = _call("flow_start", {"workflow": "demo2"})
    assert err is None, err
    task = st["tasks"][0]
    assert task["prompt"].startswith("first for me")
    assert task["rules"][0]["id"] == "careful"
    _, err = _call("flow_report", {"node": task["instance"], "value": {"n": "x"}})
    assert err and "must be int" in err
    st, err = _call("flow_report", {"node": "first", "value": {"n": 1}, "text": "did it"})
    assert err is None and st["choices"][0]["options"][0]["name"] == "go"
    st, err = _call("flow_select", {"option": "go", "reason": "fine"})
    assert st["tasks"][0]["label"] == "second"
    rule, _ = _call("flow_rule", {"name": "careful"})
    assert rule["why"] == "it broke once"
    st, err = _call("flow_report", {})
    assert err is None and st["status"] == "done"
    assert (project / ".cflow2" / "runs" / "default" / "journal.jsonl").is_file()


def test_start_refused_while_live_and_abort(project):
    _call("flow_start", {"workflow": "demo2"})
    _, err = _call("flow_start", {"workflow": "demo2"})
    assert err and "still" in err
    assert "flow_abort" not in {t["name"] for t in mcp_server.TOOLS}  # a person's act
    _, err = _call("flow_abort", {"reason": "user asked"})
    assert err


def test_unknown_definition_is_a_tool_error(project):
    _, err = _call("flow_start", {"workflow": "nope"})
    assert err and "no v2 definition" in err
