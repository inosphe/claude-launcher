"""claunch-7jzp8: the worker and leader v2 workflows tell their sessions to
write (worker) and read (leader) the plan pane, naming tools that exist and
nodes the rule is delivered to."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from claude_launcher import plan_mcp

V2 = Path(plan_mcp.__file__).resolve().parent / "workflows" / "v2"


@pytest.mark.parametrize("name, rule, nodes, tools", [
    ("improv-worker-v2", "plan-panel", ["coding-plan", "plan-seed", "work", "wrapup"], ["plan_set", "plan_node", "plan_get"]),
    ("improv-leader-v2", "worker-plans", ["intake", "integrate-preflight"], ["plan_get"]),
])
def test_rule_is_delivered_and_names_real_tools(name, rule, nodes, tools):
    doc = yaml.safe_load((V2 / f"{name}.yaml").read_text(encoding="utf-8"))
    r = doc["rules"][rule]
    assert r["deliver"] == nodes
    assert set(nodes) <= set(doc["nodes"])
    for tool in tools:
        assert tool in r["text"] and tool in plan_mcp.TOOL_NAMES
    assert "claunch-7jzp8" in r["why"]


def test_worker_rule_covers_closure_and_does_not_block_the_round():
    text = yaml.safe_load((V2 / "improv-worker-v2.yaml").read_text(encoding="utf-8"))["rules"]["plan-panel"]["text"]
    assert "verify" in text and "followup" in text and "[claunch plan]" in text
    assert "회차를 막지 않는다" in text


def test_leader_rule_names_the_joint_read():
    text = yaml.safe_load((V2 / "improv-leader-v2.yaml").read_text(encoding="utf-8"))["rules"]["worker-plans"]["text"]
    assert "joint=true" in text and "claunch-7jzp8.1" in text
    assert "plan_overview" in text


def test_worker_rule_writes_the_plan_in_korean():
    """claunch-lp7c6: the plan pane's text is written in Korean."""
    text = yaml.safe_load((V2 / "improv-worker-v2.yaml").read_text(encoding="utf-8"))["rules"]["plan-panel"]["text"]
    assert "한국어로 쓴다" in text and "claunch-lp7c6" in text


def _coordinator():
    return yaml.safe_load((V2 / "improv-coordinator-v2.yaml").read_text(encoding="utf-8"))


def _coordinator_prompts(doc):
    out = {name: node.get("prompt", "") for name, node in doc["nodes"].items()}
    for proc in doc["procedures"].values():
        for name, node in proc["nodes"].items():
            out[name] = node.get("prompt", "")
    return out


def test_coordinator_plan_rule_is_delivered_and_names_real_tools():
    """claunch-6sk66: the coordinator routes user alignment by question kind."""
    doc = _coordinator()
    r = doc["rules"]["coordinator-plan"]
    nodes = ["align", "coordinate", "investigate", "assign", "review-result", "summary"]
    assert r["deliver"] == nodes
    known = set(doc["nodes"]) | {n for p in doc["procedures"].values() for n in p["nodes"]}
    assert set(nodes) <= known
    for tool in ("plan_set", "plan_node", "plan_get", "thread_ask"):
        assert tool in r["text"]
    for tool in ("plan_set", "plan_node", "plan_get"):
        assert tool in plan_mcp.TOOL_NAMES
    assert "claunch-6sk66" in r["why"]


def test_coordinator_plan_rule_keeps_workflow_branches_on_cflow_ask():
    doc = _coordinator()
    text = doc["rules"]["coordinator-plan"]["text"]
    assert "ask-user" in text and "plan-wait를 두지 않는다" in text
    assert "[claunch plan]" in text and "한국어로 쓴다" in text
    # the three workflow branches stay cflow asks
    body = yaml.safe_dump(doc, allow_unicode=True)
    assert body.count("call: ask-user") >= 2


def test_coordinator_prompts_never_recommend_qa_ask():
    doc = _coordinator()
    for name, prompt in _coordinator_prompts(doc).items():
        assert "qa_ask" not in prompt, name
    # the rule itself only forbids it
    assert "qa_ask를 쓰지 않는다" in doc["rules"]["coordinator-plan"]["text"]


def test_coordinator_prompts_point_at_the_rule():
    prompts = _coordinator_prompts(_coordinator())
    for name in ("align", "investigate", "review-result"):
        assert "coordinator-plan" in prompts[name], name
