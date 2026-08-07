"""cflow: graph model validation, engine state machine, gates, loops, MCP."""

from __future__ import annotations

import json
import sys

import pytest

from claude_launcher.cflow import engine, mcp, model, state as state_mod
from claude_launcher.cflow.engine import CflowError
from claude_launcher.cflow.model import WorkflowError

LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
"""

GATED = """
steps:
  work:
    instructions: do work
    next: ship
  ship:
    gate: approve shipping
    instructions: ship it
"""

BRANCHED = """
steps:
  triage:
    select:
      prompt: pick a path
      chooser: agent
      options:
        a: {description: path a, next: a1}
        b: {description: skip straight on, next: common}
  a1:
    instructions: do a1
    next: common
  common:
    instructions: after the branch
"""

USER_BRANCH = """
steps:
  triage:
    select:
      prompt: risky?
      chooser: user
      options:
        auto:  {description: go, next: after}
        human: {description: review, next: after}
  after:
    instructions: continue
"""

LOOP = """
max_visits: 2
steps:
  impl:
    instructions: implement or rework
    next: review
  review:
    select:
      prompt: good enough?
      chooser: agent
      options:
        again: {description: loop back, next: impl}
        done:  {description: finish, next: end}
"""

GATED_LOOP = """
steps:
  fix:
    instructions: fix things
    next: review
  review:
    gate: human review required
    instructions: relay the review verdict
    next: decide
  decide:
    select:
      prompt: reviewer satisfied?
      chooser: agent
      options:
        "no":  {description: another pass, next: fix}
        "yes": {description: done, next: end}
"""


@pytest.fixture
def flow_dir(home, tmp_path, monkeypatch):
    """An isolated project cwd with a project workflow dir."""
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    monkeypatch.chdir(proj)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    return proj


def _write(proj, name, text):
    path = proj / ".claunch" / "workflows" / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _advance(summary, details=None):
    """The full completion protocol for one executable step: report, then next."""
    engine.report(summary, details)
    return engine.next_step()


# --------------------------------------------------------------------------- #
# model: parsing + graph validation
# --------------------------------------------------------------------------- #
def test_parse_valid_graph():
    wf = model.parse(BRANCHED, default_name="x")
    assert wf.start == "triage"
    assert wf.steps["triage"].is_select
    assert wf.steps["a1"].next == "common"
    assert wf.steps["common"].next is None  # implicit termination
    assert wf.warnings == []


def test_shared_target_needs_no_duplication():
    wf = model.parse(USER_BRANCH)
    opts = wf.steps["triage"].select.options
    assert opts["auto"].next == opts["human"].next == "after"


def test_unknown_target_rejected():
    bad = "steps:\n  a:\n    instructions: x\n    next: nope\n"
    with pytest.raises(WorkflowError, match="unknown step 'nope'"):
        model.parse(bad)


def test_end_is_reserved():
    bad = "steps:\n  end:\n    instructions: x\n"
    with pytest.raises(WorkflowError, match="reserved"):
        model.parse(bad)


def test_select_step_may_not_have_next():
    bad = """
steps:
  s:
    next: s2
    select:
      prompt: p
      options:
        a: {description: d, next: s2}
  s2:
    instructions: x
"""
    with pytest.raises(WorkflowError, match="'next' is not allowed"):
        model.parse(bad)


def test_cycle_is_warned_not_rejected():
    wf = model.parse(LOOP)
    assert any("cycle" in w for w in wf.warnings)


def test_endless_loop_is_an_error():
    bad = """
steps:
  a:
    instructions: x
    next: b
  b:
    instructions: y
    next: a
"""
    with pytest.raises(WorkflowError, match="no termination is reachable"):
        model.parse(bad)


def test_trapped_and_unreachable_warnings():
    text = """
steps:
  start:
    select:
      prompt: p
      chooser: agent
      options:
        out:  {description: finishes, next: end}
        trap: {description: never returns, next: t1}
  t1:
    instructions: spin
    next: t2
  t2:
    instructions: spin more
    next: t1
  orphan:
    instructions: nobody points here
"""
    wf = model.parse(text)
    assert any("can never reach a termination" in w and "t1" in w for w in wf.warnings)
    assert any("unreachable" in w and "orphan" in w for w in wf.warnings)


def test_bad_chooser_rejected():
    bad = """
steps:
  s:
    select:
      prompt: p
      chooser: nobody
      options:
        a: {description: d}
"""
    with pytest.raises(WorkflowError, match="chooser"):
        model.parse(bad)


# --------------------------------------------------------------------------- #
# engine: linear flow + reports
# --------------------------------------------------------------------------- #
def test_linear_run(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    payload = engine.start("linear", context="the task")
    assert payload["status"] == "step"
    assert payload["step_id"] == "one"
    assert payload["visit"] == 1

    payload = _advance("did one", details="evidence: foo.py touched")
    assert payload["status"] == "step"
    assert payload["step_id"] == "two"

    payload = _advance("did two")
    assert payload["status"] == "done"
    summaries = {e["step"]: e["summary"] for e in payload["journal"]}
    assert summaries == {"one": "did one", "two": "did two"}
    details = {e["step"]: e["details"] for e in payload["journal"]}
    assert details["one"] == "evidence: foo.py touched"


def test_next_requires_report(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    payload = engine.next_step()
    assert payload["status"] == "report_required"
    assert payload["step_id"] == "one"
    # still stuck on the same step until a report is filed
    assert engine.next_step()["status"] == "report_required"

    payload = engine.report("did one")
    assert payload["status"] == "reported"
    assert engine.next_step()["step_id"] == "two"


def test_report_needs_summary_and_delivery(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    with pytest.raises(CflowError, match="non-empty"):
        engine.report("   ")

    # re-filing overwrites; the completed step carries the latest account
    engine.report("first take")
    engine.report("second take")
    engine.next_step()
    completed = [
        e for e in state_mod.read_journal() if e["event"] == "step_completed"
    ]
    assert completed[0]["summary"] == "second take"
    filed = [e for e in state_mod.read_journal() if e["event"] == "step_report"]
    assert [e["summary"] for e in filed] == ["first take", "second take"]


def test_status_shows_filed_report(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    assert "report" not in engine.status()
    engine.report("one is done")
    assert engine.status()["report"]["summary"] == "one is done"


def test_start_twice_requires_force(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    with pytest.raises(CflowError, match="already active"):
        engine.start("linear")
    payload = engine.start("linear", force=True)
    assert payload["step_id"] == "one"


def test_status_is_readonly(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    before = state_mod.load_state()
    engine.status()
    assert state_mod.load_state() == before


# --------------------------------------------------------------------------- #
# engine: gates
# --------------------------------------------------------------------------- #
def test_gate_blocks_until_cli_approves(flow_dir):
    _write(flow_dir, "gated", GATED)
    engine.start("gated")
    payload = _advance("worked")
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "gate"
    assert "instructions" not in payload  # gated content is withheld

    # the agent hammering next() gets the same wall, and cannot report either
    payload = engine.next_step()
    assert payload["status"] == "waiting_approval"
    with pytest.raises(CflowError, match="not been delivered"):
        engine.report("pretending")

    engine.approve(by="user")
    payload = engine.next_step()
    assert payload["status"] == "step"
    assert payload["step_id"] == "ship"

    payload = _advance("shipped")
    assert payload["status"] == "done"


def test_approve_without_gate_errors(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    with pytest.raises(CflowError, match="nothing waiting"):
        engine.approve(by="user")


def test_gate_reapproves_on_every_visit(flow_dir):
    _write(flow_dir, "gloop", GATED_LOOP)
    engine.start("gloop")
    _advance("fixed round 1")
    assert engine.status()["status"] == "waiting_approval"
    engine.approve(by="user")
    payload = engine.next_step()
    assert payload["step_id"] == "review"
    payload = _advance("relayed verdict")
    assert payload["status"] == "select"
    payload = engine.select("no", "reviewer wants changes", by="agent")
    assert payload["step_id"] == "fix"
    assert payload["visit"] == 2

    # second pass: the gate closes again — one approval is not forever
    payload = _advance("fixed round 2")
    assert payload["status"] == "waiting_approval"
    engine.approve(by="user")
    payload = engine.next_step()
    assert payload["step_id"] == "review"
    assert payload["visit"] == 2


# --------------------------------------------------------------------------- #
# engine: selects
# --------------------------------------------------------------------------- #
def test_agent_select_routes_and_shared_target(flow_dir):
    _write(flow_dir, "branched", BRANCHED)
    payload = engine.start("branched")
    assert payload["status"] == "select"
    assert payload["chooser"] == "agent"

    with pytest.raises(CflowError, match="unknown option"):
        engine.select("zzz", by="agent")

    payload = engine.select("a", "path a fits", by="agent")
    assert (payload["status"], payload["step_id"]) == ("step", "a1")
    payload = _advance("did a1")
    assert payload["step_id"] == "common"
    payload = _advance("done")
    assert payload["status"] == "done"


def test_select_can_jump_straight_to_shared_step(flow_dir):
    _write(flow_dir, "branched", BRANCHED)
    engine.start("branched")
    payload = engine.select("b", "no extra work", by="agent")
    assert (payload["status"], payload["step_id"]) == ("step", "common")


def test_next_on_select_step_redirects(flow_dir):
    _write(flow_dir, "branched", BRANCHED)
    engine.start("branched")
    payload = engine.next_step()
    assert payload["status"] == "select"
    assert "select" in payload["note"]
    # reports have no place at a decision point either
    with pytest.raises(CflowError, match="decision point"):
        engine.report("choosing a")


def test_user_chooser_blocks_agent_selection(flow_dir):
    _write(flow_dir, "ub", USER_BRANCH)
    engine.start("ub")
    payload = engine.select("auto", "looks easy", by="agent")
    assert payload["status"] == "waiting_selection"
    assert payload["proposal"]["option"] == "auto"

    # agent cannot force it through by repeating
    payload = engine.select("auto", by="agent")
    assert payload["status"] == "waiting_selection"

    # the human confirms a DIFFERENT option; agent is not handed the step yet
    payload = engine.select("human", by="user")
    assert payload["status"] == "selected"

    payload = engine.next_step()
    assert (payload["status"], payload["step_id"]) == ("step", "after")
    confirmed = [
        e for e in state_mod.read_journal() if e["event"] == "select_confirmed"
    ]
    assert confirmed[0]["option"] == "human"
    assert confirmed[0]["by"] == "user"


# --------------------------------------------------------------------------- #
# engine: loops + guard
# --------------------------------------------------------------------------- #
def test_loop_iterates_and_select_exits(flow_dir):
    _write(flow_dir, "loop", LOOP)
    payload = engine.start("loop")
    assert payload.get("workflow_warnings")  # cycle warning surfaced at start
    _advance("round 1")
    payload = engine.select("again", "not good yet", by="agent")
    assert (payload["step_id"], payload["visit"]) == ("impl", 2)
    _advance("round 2")
    payload = engine.select("done", "good now", by="agent")
    assert payload["status"] == "done"


def test_loop_limit_pauses_and_approve_extends(flow_dir):
    _write(flow_dir, "loop", LOOP)  # max_visits: 2
    engine.start("loop")
    _advance("round 1")
    engine.select("again", by="agent")  # impl visit 2
    _advance("round 2")
    payload = engine.select("again", by="agent")  # impl visit 3 > limit
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "loop_limit"

    payload = engine.approve(by="user")  # extends the limit
    assert "loop limit extended" in payload["note"]
    payload = engine.next_step()
    assert (payload["status"], payload["step_id"]) == ("step", "impl")
    assert payload["visit"] == 3


# --------------------------------------------------------------------------- #
# engine: verify
# --------------------------------------------------------------------------- #
def _verified_yaml() -> str:
    import yaml

    check = (
        f'"{sys.executable}" -c '
        '"import pathlib,sys; sys.exit(0 if pathlib.Path(\'ok.txt\').exists() else 1)"'
    )
    return yaml.safe_dump(
        {"steps": {"build": {"instructions": "make it pass", "verify": check}}}
    )


def test_verify_blocks_then_passes(flow_dir):
    _write(flow_dir, "verified", _verified_yaml())
    engine.start("verified")

    payload = _advance("claims it works")
    assert payload["status"] == "verify_failed"
    assert payload["exit_code"] == 1

    # the failed verify discarded the report: the fix must be re-reported
    (flow_dir / "ok.txt").write_text("done", encoding="utf-8")
    assert engine.next_step()["status"] == "report_required"
    payload = _advance("now it works")
    assert payload["status"] == "done"
    events = [e["event"] for e in state_mod.read_journal()]
    assert "verify_failed" in events
    assert "verify_passed" in events


# --------------------------------------------------------------------------- #
# engine: forced state (goto)
# --------------------------------------------------------------------------- #
def test_goto_forces_position_without_delivering(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    payload = engine.goto("two", by="user", reason="one already done elsewhere")
    assert (payload["status"], payload["step_id"]) == ("state_set", "two")

    # not delivered by goto itself: the agent fetches it on its own call
    payload = engine.next_step()
    assert (payload["status"], payload["step_id"]) == ("step", "two")
    forced = [e for e in state_mod.read_journal() if e["event"] == "state_forced"]
    assert forced[0]["from"] == "one"
    assert forced[0]["to"] == "two"


def test_goto_unknown_step_rejected(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    with pytest.raises(WorkflowError, match="unknown step"):
        engine.goto("nope")


def test_goto_regates_and_counts_visits(flow_dir):
    _write(flow_dir, "gated", GATED)
    engine.start("gated")
    _advance("worked")
    engine.approve(by="user")
    engine.next_step()  # ship delivered (visit 1, gate opened)

    payload = engine.goto("ship")
    assert payload["visit"] == 2
    # per-visit semantics survive the override: the gate closes again
    assert engine.next_step()["status"] == "waiting_approval"


def test_goto_end_finishes_and_goto_reopens(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    payload = engine.goto("end")
    assert payload["status"] == "done"
    assert engine.status()["status"] == "done"

    payload = engine.goto("two")  # reopen the finished run
    assert payload["status"] == "state_set"
    assert engine.status()["status"] == "step"


# --------------------------------------------------------------------------- #
# run registry (dashboard discovery)
# --------------------------------------------------------------------------- #
def test_start_registers_run_dir_and_prunes(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    assert (str(flow_dir.resolve()), "default") in state_mod.known_runs()

    # clearing the run state prunes the registry on the next read
    engine.reset()
    assert (str(flow_dir.resolve()), "default") not in state_mod.known_runs()


# --------------------------------------------------------------------------- #
# scopes: one run per session, not per directory
# --------------------------------------------------------------------------- #
def test_scopes_isolate_runs_per_session(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear", scope="s1")
    engine.start("linear", scope="s2")  # no already-active clash across scopes

    engine.report("one done in s1", scope="s1")
    engine.next_step(scope="s1")
    assert engine.status(scope="s1")["step_id"] == "two"
    assert engine.status(scope="s2")["step_id"] == "one"  # untouched
    assert engine.status()["status"] == "idle"  # default scope untouched

    assert set(state_mod.scopes_in()) == {"s1", "s2"}
    runs = state_mod.known_runs()
    assert (str(flow_dir.resolve()), "s1") in runs
    assert (str(flow_dir.resolve()), "s2") in runs


def test_scope_resolves_from_session_env(flow_dir, monkeypatch):
    """The daemon exports CLAUNCH_SESSION; claude's MCP server inherits it,
    so the run keys itself to the session with no explicit plumbing."""
    _write(flow_dir, "linear", LINEAR)
    monkeypatch.setenv(state_mod.SESSION_ENV, "sx")
    engine.start("linear")
    assert (flow_dir / ".cflow" / "runs" / "sx" / "state.json").is_file()
    assert engine.status()["status"] == "step"  # same ambient scope

    monkeypatch.delenv(state_mod.SESSION_ENV)
    assert engine.status()["status"] == "idle"  # a different (default) scope
    assert engine.status(scope="sx")["status"] == "step"  # explicit override


def test_legacy_flat_layout_migrates_to_default_scope(flow_dir):
    import shutil

    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    base = flow_dir / ".cflow"
    for name in ("state.json", "workflow.yaml", "journal.jsonl"):
        (base / "runs" / "default" / name).rename(base / name)
    shutil.rmtree(base / "runs")

    assert engine.status()["status"] == "step"  # transparently migrated
    assert (base / "runs" / "default" / "state.json").is_file()
    assert not (base / "state.json").exists()


# --------------------------------------------------------------------------- #
# engine: abort / reset
# --------------------------------------------------------------------------- #
def test_abort_and_reset(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    payload = engine.abort(by="user")
    assert payload["status"] == "aborted"
    assert engine.status()["status"] == "aborted"
    engine.reset()
    assert engine.status()["status"] == "idle"


# --------------------------------------------------------------------------- #
# MCP dispatch
# --------------------------------------------------------------------------- #
def _rpc(method, params=None, msg_id=1):
    return mcp._handle({"jsonrpc": "2.0", "id": msg_id, "method": method,
                        "params": params or {}})


def test_mcp_initialize_and_tools():
    resp = _rpc("initialize", {"protocolVersion": "2024-11-05"})
    assert resp["result"]["serverInfo"]["name"] == "cflow"
    resp = _rpc("tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    # no approve, by design
    assert names == {"start", "report", "next", "select", "status"}


def test_mcp_tool_call_flow(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    resp = _rpc("tools/call", {"name": "start", "arguments": {"workflow": "linear"}})
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["step_id"] == "one"

    # next without a report is refused; report, then next advances
    resp = _rpc("tools/call", {"name": "next", "arguments": {}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "report_required"

    resp = _rpc("tools/call", {"name": "report",
                               "arguments": {"summary": "ok", "details": "d"}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "reported"

    resp = _rpc("tools/call", {"name": "next", "arguments": {}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["step_id"] == "two"


def test_mcp_errors_are_soft(flow_dir):
    resp = _rpc("tools/call", {"name": "next", "arguments": {}})
    assert resp["result"]["isError"] is True
    assert "no active cflow run" in resp["result"]["content"][0]["text"]
    resp = _rpc("nonsense")
    assert resp["error"]["code"] == -32601
