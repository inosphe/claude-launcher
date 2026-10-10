"""cflow v2 capabilities: layering, fallback planning (docs/cflow-v2.md 6)."""

from __future__ import annotations

import pytest

from claude_launcher import harnesses, provider_spec
from claude_launcher.cflow2 import caps, ir
from claude_launcher.cflow2.caps import Capability


def test_capability_ids_match_doc_table():
    assert set(caps.CAPABILITIES) == {
        "subagent.spawn", "subagent.parallel", "subagent.tools.restrict",
        "subagent.worktree", "mcp.claunch", "session.spawn",
    }


def test_registry_exposes_package_capabilities():
    reg = harnesses.registry({})
    claude = reg["claude"].capabilities
    assert claude["subagent.spawn"] == {"enabled": True}
    assert claude["mcp.claunch"] == {"enabled": True}
    assert reg["codex"].capabilities == {"mcp.claunch": {"enabled": True}}
    assert reg["pi"].capabilities == {}
    assert reg["devin"].capabilities == {}
    assert reg["kimi"].capabilities == {}
    assert not any(k.startswith("headless.") for h in reg.values() for k in h.capabilities)


def test_bad_harness_capabilities_block_drops_that_entry_only():
    reg = harnesses.registry({"harnesses": {"x": {"command": "x", "capabilities": ["no"]}}})
    assert "x" not in reg and "claude" in reg
    with pytest.raises(harnesses.HarnessConfigError):
        harnesses.parse({"harnesses": {"y": {"capabilities": {"a": {"enabled": "yes"}}}}})


def test_resolve_with_no_config():
    got = caps.resolve("claude", doc={})
    assert list(got) == list(caps.CAPABILITIES)
    assert got["subagent.spawn"] == Capability(True, [], "harness:claude")
    assert got["session.spawn"] == Capability(True, [], "daemon-policy")
    assert got.warnings == []
    off = caps.resolve("claude", doc={}, session_spawn=False)
    assert off["session.spawn"].enabled is False
    assert off["session.spawn"].source == "daemon-policy"
    codex = caps.resolve("codex", doc={})
    assert codex["subagent.spawn"].enabled is False
    assert codex["mcp.claunch"].enabled is True


def test_a_stale_headless_capability_in_config_is_skipped_silently():
    """claunch-xjbrh removed the headless executor; a config still naming
    its capabilities is stale, not wrong, and resolves without a warning."""
    doc = {"profiles": {"p": {"capabilities": {"headless.run": {"enabled": True,
                                                                "args": ["-p"]},
                                               "headless.resume": False}}}}
    got = caps.resolve("claude", profile="p", doc=doc)
    assert "headless.run" not in got and got.warnings == []


DOC = {
    "provider": "ds4",
    "providers": {
        "ds4": {
            "capabilities": {
                "subagent.tools.restrict": False,
                "mcp.claunch": False,
                "subagent.parallel": False,
                "bogus.cap": True,
            },
            "harness_options": {
                "claude": {"capabilities": {
                    "mcp.claunch": True,
                    "subagent.worktree": False,
                }},
            },
        },
        "other": {"capabilities": {"mcp.claunch": False}},
    },
    "profiles": {
        "base": {"capabilities": {"subagent.worktree": True,
                                  "subagent.spawn": {"enabled": False, "args": ["kept"]}}},
        "nc": {
            "parent": "base",
            "capabilities": {"subagent.parallel": True, "session.spawn": False},
            "harness_options": {"claude": {"capabilities": {"subagent.spawn": True}}},
        },
    },
}


def test_layering_precedence():
    got = caps.resolve("claude", profile="nc", doc=DOC)
    # provider level overrides the harness default
    assert got["subagent.tools.restrict"] == Capability(False, [], "provider:ds4")
    # provider/harness overrides provider
    assert got["mcp.claunch"] == Capability(True, [], "provider:ds4/harness:claude")
    # ancestor profile overrides provider/harness, child overrides ancestor
    assert got["subagent.worktree"] == Capability(True, [], "profile:base")
    assert got["subagent.parallel"] == Capability(True, [], "profile:nc")
    # a bool keeps the args a lower layer gave
    assert got["subagent.spawn"] == Capability(True, ["kept"], "profile:nc/harness:claude")
    assert any("bogus.cap" in w for w in got.warnings)


def test_harness_block_only_for_that_harness():
    got = caps.resolve("codex", profile="nc", doc=DOC)
    assert got["mcp.claunch"].enabled is False
    assert got["mcp.claunch"].source == "provider:ds4"
    assert got["subagent.spawn"].source == "profile:base"


def test_explicit_provider_and_unknowns():
    got = caps.resolve("claude", provider="other", doc=DOC)
    assert got["mcp.claunch"] == Capability(False, [], "provider:other")
    assert got["subagent.tools.restrict"].enabled is True
    unknown = caps.resolve("nope", provider="missing", doc={})
    assert any("nope" in w for w in unknown.warnings)
    assert any("missing" in w for w in unknown.warnings)
    assert caps.resolve("claude", provider="default", doc={}).warnings == []


def test_provider_spec_accepts_capabilities():
    entry = DOC["providers"]["ds4"]
    spec = provider_spec.from_entry(entry, "providers.ds4")
    assert spec.capabilities["subagent.tools.restrict"] is False
    assert spec.options("claude")["capabilities"]["mcp.claunch"] is True
    prof = provider_spec.from_entry(DOC["profiles"]["nc"], "profiles.nc", profile=True)
    merged = provider_spec.overlay(spec, prof)
    assert merged.capabilities["subagent.parallel"] is True
    assert merged.capabilities["mcp.claunch"] is False
    assert merged.options("claude")["capabilities"] == {
        "mcp.claunch": True, "subagent.worktree": False, "subagent.spawn": True}
    with pytest.raises(provider_spec.SpecError):
        provider_spec.from_entry({"capabilities": ["x"]}, "providers.bad")
    with pytest.raises(provider_spec.SpecError):
        provider_spec.from_entry({"capabilities": {"a": "yes"}}, "providers.bad")
    # other channels are still checked
    with pytest.raises(provider_spec.SpecError):
        provider_spec.from_entry({"harness_options": {"claude": {"nope": 1}}}, "p")


ALL = {k: True for k in caps.CAPABILITIES}
NONE = {k: False for k in caps.CAPABILITIES}


def test_choose_executor_chain():
    got = caps.choose_executor(["session", "subagent", "inline"], {**NONE, "subagent.spawn": True})
    assert got.executor == "subagent"
    assert got.skipped == [("session", "session.spawn")]
    assert got.degraded and got.route == "subagent"
    got = caps.choose_executor(["subagent", "session"], {**NONE, "session.spawn": False})
    assert got.executor == "inline"
    assert [s[0] for s in got.skipped] == ["subagent", "session"]
    assert caps.choose_executor([], NONE).executor == "inline"
    got = caps.choose_executor(["subagent", "session"], {**ALL, "subagent.worktree": False},
                               needs=["subagent.worktree"])
    assert got.executor == "session"
    assert got.skipped == [("subagent", "subagent.worktree")]


def test_plan_return_routes():
    r = caps.plan_return
    assert r("subagent", ALL, True).route == "mcp-submit"
    p = r("subagent", NONE, True)
    assert p.route == "receipt-file" and p.degraded
    assert r("session", ALL, True).route == "mcp-submit"
    assert r("session", NONE, True).route == "receipt-file"
    assert r("inline", NONE, True).route == "mcp-submit"


def test_plan_readonly_routes():
    ro = caps.plan_readonly
    assert ro("subagent", ALL).route == "tools-restrict"
    assert ro("subagent", {**NONE, "subagent.worktree": True}).route == "worktree"
    assert ro("subagent", NONE).route == "brief+porcelain-check"
    assert ro("session", NONE).route == "child-worktree"
    assert ro("inline", ALL).route == "brief-only"


def test_plan_parallel_routes():
    pp = caps.plan_parallel
    assert pp("session", NONE, 3, False).route == "parallel"
    p = pp("session", NONE, 2, True)
    assert p.route == "parallel-isolated-worktrees" and p.isolate
    assert pp("subagent", ALL, 2, False).route == "parallel"
    assert pp("subagent", ALL, 2, True).route == "parallel-isolated-worktrees"
    p = pp("subagent", {**ALL, "subagent.worktree": False}, 2, True)
    assert p.route == "serial" and p.degraded and not p.concurrent
    p = pp("subagent", {**ALL, "subagent.parallel": False}, 2, False, settle="race")
    assert p.route == "serial"
    assert any("first success" in d for d in p.degraded)
    p = pp("subagent", {**ALL, "subagent.parallel": False}, 2, False, settle="all")
    assert not any("first success" in d for d in p.degraded)
    assert pp("inline", ALL, 2, False).route == "serial"
    assert pp("subagent", NONE, 1, True).route == "parallel"


def test_plan_budget_routes():
    pb = caps.plan_budget
    assert pb("session", ALL, None, False).route == "none"
    p = pb("subagent", NONE, {"seconds": 60}, True)
    assert p.route == "seconds-only" and p.seconds == 60 and not p.blocked
    p = pb("session", ALL, {"usd": 2}, False)
    assert p.route == "unenforced" and p.degraded and not p.blocked
    p = pb("subagent", ALL, {"usd": 2}, True)
    assert p.route == "unenforced" and p.blocked
    p = pb("inline", ALL, {"usd": 2}, True)
    assert p.route == "unenforced" and not p.blocked


def test_plan_retry_carry_routes():
    rc = caps.plan_retry_carry
    p = rc("session", ALL, "resume")
    assert p.route == "summary" and p.degraded
    assert rc("subagent", ALL, "resume").route == "summary"
    p = rc("session", ALL, "summary")
    assert p.route == "summary" and not p.degraded


def _node(**kw):
    props = kw.pop("props", {})
    return ir.Node(id=kw.pop("id", "n1"), type=kw.pop("type", ir.T_TASK), props=props, **kw)


def test_plan_node_combines():
    node = _node(executor=["session", "subagent"], needs=[],
                 props={"effects": ["fs.read"], "returns": {"ok": {"x": "str"}}})
    plan = caps.plan_node(node, caps.resolve("codex", doc={}))
    assert plan.route == "session"
    assert plan.returns.route == "mcp-submit"
    assert plan.readonly.route == "child-worktree"
    assert plan.parallel is None and plan.budget is None
    assert plan.to_dict()["route"] == "session"


def test_plan_node_strict_budget_skips_executor():
    node = _node(executor=["session", "subagent"], budget={"usd": 1},
                 props={"strict_budget": True, "effects": ["fs.write"]})
    plan = caps.plan_node(node, caps.resolve("codex", doc={}))
    # no executor enforces dollars; subagent.spawn is off for codex
    assert plan.route == "inline"
    assert plan.executor.skipped == [("session", "budget.usd enforcement"),
                                     ("subagent", "subagent.spawn")]
    assert plan.readonly is None
    assert plan.budget.route == "unenforced" and not plan.budget.blocked
    assert plan.degraded


def test_plan_node_needs_and_parallel_and_retry():
    claude = caps.resolve("claude", doc={})
    node = _node(type=ir.T_PAR, executor=["subagent"], needs=["subagent.worktree"],
                 props={"branches": {"a": "x", "b": "y"}, "settle": "all"})
    plan = caps.plan_node(node, claude)
    assert plan.route == "subagent"
    assert plan.parallel.route == "parallel-isolated-worktrees"
    loop = _node(type=ir.T_LOOP, executor=["session"],
                 props={"mode": "retry", "max": 2, "carry": "resume"})
    assert caps.plan_node(loop, claude).carry.route == "summary"
    no_need = _node(executor=["subagent"], needs=["subagent.worktree"])
    assert caps.plan_node(no_need, {**ALL, "subagent.worktree": False}).route == "inline"


def test_enabled_reads_capabilities_dicts_and_bools():
    assert caps.enabled({"x": Capability(True)}, "x")
    assert not caps.enabled({"x": {"enabled": False, "args": ["a"]}}, "x")
    assert caps.enabled({"x": True}, "x") and not caps.enabled({}, "x")


# -- A2: non-flat subtrees ------------------------------------------------------------


def test_plan_node_not_flat_skips_mcp_less_delegates():
    node = _node(type=ir.T_SEQ, executor=["subagent", "session"])
    assert caps.plan_node(node, ALL, flat=False).route == "subagent"
    no_mcp = {**ALL, "mcp.claunch": False}
    plan = caps.plan_node(node, no_mcp, flat=False)
    assert plan.route == "inline"
    assert [s[1] for s in plan.executor.skipped] == [caps.NOT_FLAT] * 2
    assert "executor subagent skipped: subtree not flat" in plan.degraded
    assert caps.plan_node(node, no_mcp).route == "subagent"  # flat (default)
