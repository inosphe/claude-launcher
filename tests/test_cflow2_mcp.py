"""cflow v2 flow_* MCP tools."""

from __future__ import annotations

import json
import textwrap

import pytest

from claude_launcher import mcp_server
from claude_launcher.cflow2 import mcp as cflow2_mcp

FLOW_TOOLS = {"flow_start", "flow_status", "flow_report", "flow_select", "flow_fork", "flow_call",
              "flow_delegate", "flow_rule", "flow_collection", "flow_input",
              "flow_request_move"}

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



def _start_why(project):
    """demo2 with require_reason on its choose, started through the tool."""
    src = DEF.replace("name: demo2", "name: why2").replace(
        "- choose:", "- choose:\n        require_reason: true")
    (project / ".claunch" / "workflows" / "v2" / "why2.yaml").write_text(src, encoding="utf-8")
    _, err = _call("flow_start", {"workflow": "why2"})
    assert err is None, err
    st, err = _call("flow_report", {"node": "first", "value": {"n": 1}})
    assert err is None and st["choices"][0]["require_reason"] is True


def test_flow_select_holds_the_agent_to_require_reason(project):
    """The MCP tool passes the agent's caller through: a choose with
    require_reason refuses its bare pick end to end (claunch-0obx8)."""
    _start_why(project)
    for bare in ({}, {"reason": ""}, {"reason": "   "}):
        _, err = _call("flow_select", {"option": "go", **bare})
        assert err and "requires a reason" in err
    st, _ = _call("flow_status", {})
    assert st["choices"] and not st["tasks"]  # nothing moved
    st, err = _call("flow_select", {"option": "go", "reason": "first is done"})
    assert err is None and st["tasks"][0]["label"] == "second"


def test_a_person_picks_the_same_choose_without_a_reason(project):
    # the person's channel may leave the reason out (claunch-wm67r)
    from claude_launcher.cflow2 import runtime

    _start_why(project)
    st = runtime.select(None, "go", None, caller=runtime.CALLER_USER, cwd=str(project))
    assert st["tasks"][0]["label"] == "second"


MOVE_DEF = textwrap.dedent("""
    cflow: 2
    name: mv2
    nodes:
      intake: {prompt: I, force_target: true}
      work: {prompt: W}
    body:
      seq: [intake, work]
""")


def test_flow_request_move_files_holds_and_withdraws(project):
    (project / ".claunch" / "workflows" / "v2" / "mv2.yaml").write_text(MOVE_DEF,
                                                                        encoding="utf-8")
    assert "flow_request_move" in {t["name"] for t in mcp_server.TOOLS}
    st, err = _call("flow_start", {"workflow": "mv2"})
    assert err is None, err
    st, err = _call("flow_report", {"node": "intake"})
    assert err is None and st["tasks"][0]["label"] == "work"
    _, err = _call("flow_request_move", {"node": "intake"})
    assert err and "'reason' is required" in err
    st, err = _call("flow_request_move", {"node": "intake", "reason": "a new task came in"})
    assert err is None and st["move_request"]["node"] == "intake"
    _, err = _call("flow_report", {"node": "work"})
    assert err and "nobody has answered" in err
    st, err = _call("flow_request_move", {"cancel": True})
    assert err is None and "move_request" not in st
    st, err = _call("flow_report", {"node": "work"})
    assert err is None and st["status"] == "done"


def test_flow_request_move_with_outcome_asks_for_a_report(project):
    """claunch-vwmoj: the driver asks a person to report its active task
    instead of writing them a 'claunch cflow2 report' line to type."""
    (project / ".claunch" / "workflows" / "v2" / "mv2.yaml").write_text(MOVE_DEF,
                                                                        encoding="utf-8")
    st, err = _call("flow_start", {"workflow": "mv2"})
    assert err is None, err
    st, err = _call("flow_request_move", {"node": "intake", "outcome": "ok",
                                          "reason": "only the user can tell"})
    assert err is None and st["move_request"]["kind"] == "report"
    assert st["move_request"]["outcome"] == "ok"
    _, err = _call("flow_report", {"node": "intake"})
    assert err and "a person to report 'intake'" in err
    _, err = _call("flow_request_move", {"node": "intake", "value": {"a": 1},
                                         "reason": "x"})
    assert err and "give 'outcome' too" in err
    st, err = _call("flow_request_move", {"cancel": True})
    assert err is None and "move_request" not in st


BRIEF_DEF = textwrap.dedent("""
    cflow: 2
    name: brief2
    description: brief payloads
    rules:
      careful: {text: be careful with the shared branch, why: it broke once, deliver: all}
    collections:
      prs:
        kind: queue
        key: number
        item: {number: int}
    nodes:
      first:
        prompt: first
        done_when: noted
        returns: {ok: {note: str, by: str}}
      second:
        prompt: second
        done_when: done
        returns: {ok: {n: int}}
    body:
      seq:
        - try: {seq: [first], id: wrap}
          catch: {fail: second}
        - second
""")


@pytest.fixture
def brief_project(project):
    (project / ".claunch" / "workflows" / "v2" / "brief2.yaml").write_text(BRIEF_DEF,
                                                                         encoding="utf-8")
    return project


def test_brief_payload_folds_aliases_and_elides_long_values(brief_project):
    """claunch-ejvoy.1: a flow_* call returns completed results once each, long
    value fields elided, and rule text the prompt carries by id only; the
    task's prompt, done_when, returns and the collections stay whole."""
    long_note = "x" * 500
    st, err = _call("flow_start", {"workflow": "brief2"})
    assert err is None, err
    st, err = _call("flow_report", {"node": "first", "value": {"note": long_note, "by": "rev"}})
    assert err is None, err
    keys = [k for k in st["result"]]
    holders = [k for k, v in st["result"].items() if "same" in v]
    assert len(holders) == 1  # first, the try body and wrap hold one record
    rec = st["result"][holders[0]]
    assert len(rec["same"]) >= 1 and not set(rec["same"]) & set(keys)
    assert rec["outcome"] == "ok"
    assert rec["value"] == {"note": "<elided: 502 chars>", "by": "rev"}
    assert "results=[key]" in st["result_elided"]
    task = st["tasks"][0]
    assert task["label"] == "second"
    assert "be careful with the shared branch" in task["prompt"]
    assert task["rules"] == [{"id": "careful", "in_prompt": True}]
    assert task["done_when"] == "done" and task["returns"] == {"ok": {"n": "int"}}
    assert st["collections"]["prs"]["kind"] == "queue"
    assert "inputs" not in st["data"] or st.get("inputs") is None

    one, _ = _call("flow_status", {"results": ["first"]})
    whole = one["result"][holders[0]]["value"]
    assert whole == {"note": long_note, "by": "rev"}

    full, _ = _call("flow_status", {"full": True})
    assert "result_elided" not in full
    assert all("same" not in v for v in full["result"].values())
    assert len(full["result"]) == len(keys) + len(rec["same"])
    assert full["result"]["first"]["value"]["note"] == long_note
    assert full["tasks"][0]["rules"][0]["text"].startswith("be careful")


def test_brief_keeps_rule_text_the_prompt_lacks():
    payload = {"run": "r", "tasks": [{"prompt": "p", "rules": [{"id": "a", "text": "t"}]}],
               "result": {}, "inputs": {"x": 1}, "data": {"inputs": {"x": 2}}}
    out = cflow2_mcp.brief(payload)
    assert out["tasks"][0]["rules"] == [{"id": "a", "text": "t"}]
    assert out["data"] == {"inputs": {"x": 2}}  # differs from inputs: kept
    assert payload["tasks"][0]["rules"][0]["text"] == "t"  # input untouched


def test_brief_drops_data_inputs_equal_to_inputs():
    out = cflow2_mcp.brief({"run": "r", "inputs": {"x": 1},
                            "data": {"inputs": {"x": 1}, "landing": {"medium": "pr"}}})
    assert out["data"] == {"landing": {"medium": "pr"}} and out["inputs"] == {"x": 1}


def test_brief_files_a_record_under_its_label():
    """A workflow prompt reads result['peer-review'].value.by: the record sits
    under the label even when an id path was recorded first."""
    rec = {"outcome": "ok", "value": {"by": "unanswered", "decision": "pass"}}
    out = cflow2_mcp.brief({"run": "r", "result": {
        "round/land/peer-review-loop.do.0.try": rec, "peer-review": rec}})
    assert out["result"] == {"peer-review": {**rec, "same": [
        "round/land/peer-review-loop.do.0.try"]}}
