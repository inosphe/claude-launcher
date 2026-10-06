"""cflow v2 capabilities: layering, fallback planning (docs/cflow-v2.md 6)."""

from __future__ import annotations

import pytest

from claude_launcher import harnesses, provider_spec
from claude_launcher.cflow2 import caps, ir
from claude_launcher.cflow2.caps import Capability


def test_capability_ids_match_doc_table():
    assert set(caps.CAPABILITIES) == {
        "subagent.spawn", "subagent.parallel", "subagent.tools.restrict",
        "subagent.worktree", "headless.run", "headless.output.schema",
        "headless.output.json", "headless.tools.restrict",
        "headless.sandbox.readonly", "headless.budget.usd", "headless.resume",
        "mcp.claunch", "session.spawn",
    }


def test_registry_exposes_package_capabilities():
    reg = harnesses.registry({})
    claude = reg["claude"].capabilities
    assert claude["headless.run"] == {"enabled": True, "args": ["-p"]}
    assert claude["headless.output.schema"]["args"] == ["--json-schema", "{schema}"]
    assert claude["subagent.spawn"] == {"enabled": True}
    codex = reg["codex"].capabilities
    assert codex["headless.run"]["args"] == ["exec"]
    assert codex["headless.output.schema"]["args"] == ["--output-schema", "{schema_file}"]
    assert codex["headless.sandbox.readonly"]["args"] == ["-s", "read-only"]
    assert "subagent.spawn" not in codex
    assert reg["pi"].capabilities["headless.output.json"]["args"] == ["--mode", "json"]
    assert reg["devin"].capabilities == {"headless.run": {"enabled": True, "args": ["-p"]}}
    assert reg["kimi"].capabilities == {}
    assert reg["claude"].to_dict()["capabilities"]["headless.run"]["args"] == ["-p"]


def test_bad_harness_capabilities_block_drops_that_entry_only():
    reg = harnesses.registry({"harnesses": {"x": {"command": "x", "capabilities": ["no"]}}})
    assert "x" not in reg and "claude" in reg
    with pytest.raises(harnesses.HarnessConfigError):
        harnesses.parse({"harnesses": {"y": {"capabilities": {"a": {"enabled": "yes"}}}}})


def test_resolve_with_no_config():
    got = caps.resolve("claude", doc={})
    assert list(got) == list(caps.CAPABILITIES)
    assert got["headless.run"] == Capability(True, ["-p"], "harness:claude")
    assert got["headless.sandbox.readonly"] == Capability(False, [], "default")
    assert got["session.spawn"] == Capability(True, [], "daemon-policy")
    assert got.warnings == []
    off = caps.resolve("claude", doc={}, session_spawn=False)
    assert off["session.spawn"].enabled is False
    assert off["session.spawn"].source == "daemon-policy"
    codex = caps.resolve("codex", doc={})
    assert codex["subagent.spawn"].enabled is False
    assert codex["headless.sandbox.readonly"].enabled is True


DOC = {
    "provider": "ds4",
    "providers": {
        "ds4": {
            "capabilities": {
                "headless.output.schema": False,
                "headless.resume": False,
                "subagent.parallel": False,
                "bogus.cap": True,
            },
            "harness_options": {
                "claude": {"capabilities": {
                    "headless.resume": True,
                    "subagent.worktree": False,
                }},
            },
        },
        "other": {"capabilities": {"mcp.claunch": False}},
    },
    "profiles": {
        "base": {"capabilities": {"subagent.worktree": True,
                                  "headless.budget.usd": {"enabled": True, "args": ["--cap", "{usd}"]}}},
        "nc": {
            "parent": "base",
            "capabilities": {"subagent.parallel": True, "headless.tools.restrict": False},
            "harness_options": {"claude": {"capabilities": {"headless.tools.restrict": True}}},
        },
    },
}


def test_layering_precedence():
    got = caps.resolve("claude", profile="nc", doc=DOC)
    # provider level overrides the harness default
    assert got["headless.output.schema"] == Capability(
        False, ["--json-schema", "{schema}"], "provider:ds4")
    # provider/harness overrides provider
    assert got["headless.resume"].enabled is True
    assert got["headless.resume"].source == "provider:ds4/harness:claude"
    assert got["headless.resume"].args == ["--resume", "{session_id}"]
    # ancestor profile overrides provider/harness, child overrides ancestor
    assert got["subagent.worktree"] == Capability(True, [], "profile:base")
    assert got["subagent.parallel"] == Capability(True, [], "profile:nc")
    # args replaced by a mapping value
    assert got["headless.budget.usd"].args == ["--cap", "{usd}"]
    # profile/harness overrides profile
    assert got["headless.tools.restrict"].enabled is True
    assert got["headless.tools.restrict"].source == "profile:nc/harness:claude"
    assert any("bogus.cap" in w for w in got.warnings)


def test_harness_block_only_for_that_harness():
    got = caps.resolve("codex", profile="nc", doc=DOC)
    assert got["headless.resume"].enabled is False
    assert got["headless.resume"].source == "provider:ds4"
    assert got["headless.tools.restrict"].source == "profile:nc"


def test_explicit_provider_and_unknowns():
    got = caps.resolve("claude", provider="other", doc=DOC)
    assert got["mcp.claunch"] == Capability(False, [], "provider:other")
    assert got["headless.output.schema"].enabled is True
    unknown = caps.resolve("nope", provider="missing", doc={})
    assert any("nope" in w for w in unknown.warnings)
    assert any("missing" in w for w in unknown.warnings)
    assert caps.resolve("claude", provider="default", doc={}).warnings == []


def test_provider_spec_accepts_capabilities():
    entry = DOC["providers"]["ds4"]
    spec = provider_spec.from_entry(entry, "providers.ds4")
    assert spec.capabilities["headless.output.schema"] is False
    assert spec.options("claude")["capabilities"]["headless.resume"] is True
    prof = provider_spec.from_entry(DOC["profiles"]["nc"], "profiles.nc", profile=True)
    merged = provider_spec.overlay(spec, prof)
    assert merged.capabilities["subagent.parallel"] is True
    assert merged.capabilities["headless.resume"] is False
    assert merged.options("claude")["capabilities"] == {
        "headless.resume": True, "subagent.worktree": False,
        "headless.tools.restrict": True}
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
    got = caps.choose_executor(["headless", "subagent", "inline"], {**NONE, "subagent.spawn": True})
    assert got.executor == "subagent"
    assert got.skipped == [("headless", "headless.run")]
    assert got.degraded and got.route == "subagent"
    got = caps.choose_executor(["headless", "subagent", "session"], {**NONE, "session.spawn": False})
    assert got.executor == "inline"
    assert [s[0] for s in got.skipped] == ["headless", "subagent", "session"]
    assert caps.choose_executor([], NONE).executor == "inline"
    got = caps.choose_executor(["headless", "subagent"], {**ALL, "headless.output.schema": False},
                               needs=["headless.output.schema"])
    assert got.executor == "subagent"
    assert got.skipped == [("headless", "headless.output.schema")]


def test_plan_return_routes():
    r = caps.plan_return
    assert r("headless", ALL, True).route == "schema-flag"
    assert r("headless", ALL, True).capability == "headless.output.schema"
    p = r("headless", {**ALL, "headless.output.schema": False}, True)
    assert p.route == "json-output+fenced-block" and p.degraded
    p = r("headless", NONE, True)
    assert p.route == "text+fenced-block" and len(p.degraded) == 2
    assert r("headless", ALL, False).route == "json-output+fenced-block"
    assert r("subagent", ALL, True).route == "mcp-submit"
    p = r("subagent", NONE, True)
    assert p.route == "receipt-file" and p.degraded
    assert r("session", ALL, True).route == "mcp-submit"
    assert r("session", NONE, True).route == "receipt-file"
    assert r("inline", NONE, True).route == "mcp-submit"


def test_plan_readonly_routes():
    ro = caps.plan_readonly
    assert ro("headless", ALL).route == "sandbox-readonly"
    assert ro("headless", {**ALL, "headless.sandbox.readonly": False}).route == "tools-restrict"
    p = ro("headless", NONE)
    assert p.route == "throwaway-worktree" and p.degraded
    assert ro("subagent", ALL).route == "tools-restrict"
    assert ro("subagent", {**NONE, "subagent.worktree": True}).route == "worktree"
    assert ro("subagent", NONE).route == "brief+porcelain-check"
    assert ro("session", NONE).route == "child-worktree"
    assert ro("inline", ALL).route == "brief-only"


def test_plan_parallel_routes():
    pp = caps.plan_parallel
    assert pp("headless", NONE, 3, False).route == "parallel"
    p = pp("headless", NONE, 3, True)
    assert p.route == "parallel-isolated-worktrees" and p.isolate
    assert pp("session", NONE, 2, True).route == "parallel-isolated-worktrees"
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
    assert pb("headless", ALL, None, False).route == "none"
    p = pb("subagent", NONE, {"seconds": 60}, True)
    assert p.route == "seconds-only" and p.seconds == 60 and not p.blocked
    p = pb("headless", ALL, {"usd": 2, "seconds": 10}, True)
    assert p.route == "usd-flag" and p.usd_enforced and not p.blocked
    p = pb("headless", NONE, {"usd": 2}, False)
    assert p.route == "unenforced" and p.degraded and not p.blocked
    p = pb("subagent", ALL, {"usd": 2}, True)
    assert p.route == "unenforced" and p.blocked
    p = pb("inline", ALL, {"usd": 2}, True)
    assert p.route == "unenforced" and not p.blocked


def test_plan_retry_carry_routes():
    rc = caps.plan_retry_carry
    assert rc("headless", ALL, "resume").route == "resume"
    p = rc("headless", NONE, "resume")
    assert p.route == "summary" and p.degraded
    assert rc("subagent", ALL, "resume").route == "summary"
    p = rc("headless", ALL, "summary")
    assert p.route == "summary" and not p.degraded


def _node(**kw):
    props = kw.pop("props", {})
    return ir.Node(id=kw.pop("id", "n1"), type=kw.pop("type", ir.T_TASK), props=props, **kw)


def test_plan_node_combines():
    node = _node(executor=["headless", "subagent"], needs=[],
                 props={"effects": ["fs.read"], "returns": {"ok": {"x": "str"}}})
    plan = caps.plan_node(node, caps.resolve("codex", doc={}))
    assert plan.route == "headless"
    assert plan.returns.route == "schema-flag"
    assert plan.readonly.route == "sandbox-readonly"
    assert plan.parallel is None and plan.budget is None
    assert plan.to_dict()["route"] == "headless"


def test_plan_node_strict_budget_skips_executor():
    node = _node(executor=["headless", "subagent"], budget={"usd": 1},
                 props={"strict_budget": True, "effects": ["fs.write"]})
    plan = caps.plan_node(node, caps.resolve("codex", doc={}))
    # codex has no headless.budget.usd; subagent.spawn is off for codex
    assert plan.route == "inline"
    assert plan.executor.skipped == [("headless", "headless.budget.usd"),
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
    loop = _node(type=ir.T_LOOP, executor=["headless"],
                 props={"mode": "retry", "max": 2, "carry": "resume"})
    assert caps.plan_node(loop, claude).carry.route == "resume"
    no_need = _node(executor=["subagent"], needs=["subagent.worktree"])
    assert caps.plan_node(no_need, {**ALL, "subagent.worktree": False}).route == "inline"


def test_render_args():
    claude = caps.resolve("claude", doc={})
    assert caps.render_args(claude["headless.output.schema"], schema='{"type":"object"}') == [
        "--json-schema", '{"type":"object"}']
    assert caps.render_args(claude["headless.tools.restrict"], tools=["Read", "Grep"]) == [
        "--allowedTools", "Read,Grep"]
    assert caps.render_args(claude["headless.budget.usd"], usd=1.5) == ["--max-budget-usd", "1.5"]
    assert caps.render_args(claude["headless.resume"], session_id="abc") == ["--resume", "abc"]
    codex = caps.resolve("codex", doc={})
    assert caps.render_args(codex["headless.output.schema"], schema_file="/s.json") == [
        "--output-schema", "/s.json"]
    assert caps.render_args(codex["headless.run"]) == ["exec"]
    assert caps.render_args(Capability(False, ["-x"], "default")) == []
    with pytest.raises(ValueError):
        caps.render_args(claude["headless.resume"])
