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
    ("improv-worker-v2", "plan-panel", ["coding-plan", "work", "wrapup"], ["plan_set", "plan_node", "plan_get"]),
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
