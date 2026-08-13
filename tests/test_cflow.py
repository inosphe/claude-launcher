"""cflow: graph model validation, engine state machine, gates, loops, MCP."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher import daemon_client
from claude_launcher.cflow import engine, mcp, model, responders, state as state_mod
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

ASK_FLOW = """
steps:
  impl:
    instructions: implement
    next: ship
  ship:
    ask:
      prompt: ship it?
      from: [{role: leader}]
      on_decline: impl
    instructions: ship it
"""

ASK_HOLD = """
steps:
  ship:
    ask:
      prompt: ship it?
      from: [{role: leader}]
    instructions: ship it
"""

ASK_TWO_GROUPS = """
steps:
  ship:
    ask:
      prompt: ship it?
      from: [{role: reviewer}, {role: leader}]
    instructions: ship it
"""

ASK_SELF = """
steps:
  ship:
    ask:
      prompt: ship it?
      from: [{role: leader}]
      otherwise: self
    instructions: ship it
"""

ASK_LOOP = """
steps:
  ship:
    ask:
      prompt: ship it?
      from: [{role: leader}]
    instructions: ship it
    next: again
  again:
    select:
      prompt: once more?
      chooser: agent
      options:
        "yes": {description: loop, next: ship}
        "no":  {description: done, next: end}
"""

DELEGATED_BRANCH = """
steps:
  verdict:
    select:
      prompt: ready?
      chooser:
        from: [{role: reviewer}]
      options:
        ready:  {description: ship it, next: end}
        rework: {description: another pass, next: impl}
  impl:
    instructions: rework
    next: verdict
"""

BRANCH_SELF = DELEGATED_BRANCH.replace(
    "from: [{role: reviewer}]", "from: [{role: reviewer}]\n        otherwise: self"
)


@pytest.fixture
def flow_dir(home, tmp_path, monkeypatch):
    """An isolated project cwd with a project workflow dir."""
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    monkeypatch.chdir(proj)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    # The MCP fence lives in module state (one server process = one agent);
    # tests share a process, so each starts from a blank one.
    mcp._seen_run = None
    return proj


def _write(proj, name, text):
    path = proj / ".claunch" / "workflows" / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _advance(summary, details=None):
    """The full completion protocol for one executable step: report, then next."""
    engine.report(summary, details)
    return engine.next_step()


def _driving_session(monkeypatch, name="driver"):
    """Run as a managed session, so the run has an identity to delegate FROM."""
    monkeypatch.setenv(state_mod.SESSION_ENV, name)


def _mesh(monkeypatch, *members, parent=None, cut=(), local=True):
    """Stand in for the daemon roster read. ``members`` are (role, session).

    The driving session is handle ``dev1``; each member becomes the handle
    ``role-session``, wired to ``dev1`` unless its handle is in ``cut``. Pass
    ``parent`` to put a handle above the driver, and a third tuple element to
    give a member its own parent (that is how a descendant is built).

    Only the daemon read is faked: the pool, the reach and the matching are the
    real :func:`responders.pool`, so these tests exercise the code that decides
    who may be asked rather than a re-implementation of it.
    """
    roster = [_member("dev1", "driver", "worker", parent=parent)]
    for role, session, *rest in members:
        roster.append(
            _member(
                f"{role}-{session}", session, role,
                parent=(rest[0] if rest else None), local=local,
            )
        )
    known = {m["handle"] for m in roster}
    for handle in [parent] + [m["parent"] for m in roster]:
        # A parent named but not enrolled is a hole in the fixture, not a case:
        # the daemon publishes the nearest *enrolled* ancestor.
        assert handle is None or handle in known, f"unknown parent {handle!r}"
    _roster(
        monkeypatch,
        {
            "name": "team",
            "members": roster,
            "member_links": [
                {"a": "dev1", "b": m["handle"], "enabled": m["handle"] not in cut}
                for m in roster[1:]
            ],
        },
    )
    monkeypatch.setattr(responders, "deliver", lambda ask, **kw: None)


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
    with pytest.raises(CflowError, match="archive"):  # guidance in the error
        engine.start("linear")
    payload = engine.start("linear", force=True)
    assert payload["step_id"] == "one"
    # the forced-out run was retired into the archive, not discarded
    archive = flow_dir / ".cflow" / "runs" / "default" / "archive"
    entries = list(archive.iterdir())
    assert len(entries) == 1
    old = json.loads((entries[0] / "state.json").read_text(encoding="utf-8"))
    assert old["status"] == "aborted"
    events = [
        json.loads(line)["event"]
        for line in (entries[0] / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-2:] == ["aborted", "archived"]


def test_archive_frees_the_slot(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    started = engine.start("linear")
    payload = engine.archive()
    assert payload["status"] == "archived"
    assert payload["was"] == "running"
    assert payload["run"] == started["run"]
    assert not state_mod.has_run()

    # the slot is free: a new run starts cleanly, with a fresh journal
    fresh = engine.start("linear")
    assert fresh["step_id"] == "one"
    assert fresh["run"] != started["run"]
    events = [e["event"] for e in state_mod.read_journal()]
    assert "archived" not in events


def test_archive_without_run_errors(flow_dir):
    with pytest.raises(state_mod.StateError, match="no active cflow run"):
        engine.archive()


def test_start_auto_archives_finished_run(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    _advance("did one")
    assert _advance("did two")["status"] == "done"

    # no error, no force: the finished run is archived out of the way
    payload = engine.start("linear")
    assert payload["step_id"] == "one"
    archive = flow_dir / ".cflow" / "runs" / "default" / "archive"
    entries = list(archive.iterdir())
    assert len(entries) == 1
    old = json.loads((entries[0] / "state.json").read_text(encoding="utf-8"))
    assert old["status"] == "done"  # auto-archive does not rewrite history


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


@pytest.mark.parametrize(
    "scope",
    ["../evil", "..", ".", "a/b", "a\\b", "  ", "has space", "sx;rm"],
)
def test_a_scope_that_is_not_a_session_name_is_refused(flow_dir, scope):
    """The scope becomes a directory name, and the web hands one in from the
    query string — so '../..' would read and write run files anywhere.

    (An *absent* scope is not a bad one: no override means the ambient scope,
    which is what every agent-side call passes.)"""
    _write(flow_dir, "linear", LINEAR)
    with pytest.raises(state_mod.StateError):
        engine.start("linear", scope=scope)
    with pytest.raises(state_mod.StateError):
        engine.status(scope=scope)
    assert not (flow_dir.parent / ".cflow").exists()  # nothing escaped upwards


def test_a_bad_scope_in_the_registry_is_dropped_on_read(flow_dir):
    """Written by an older build, or by hand — either way it names no slot."""
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear", scope="s1")
    state_mod._write_registry(
        state_mod._read_registry() + [{"cwd": str(flow_dir), "scope": "../evil"}]
    )
    assert state_mod.known_runs() == [(str(flow_dir.resolve()), "s1")]


def test_agent_and_daemon_reach_one_slot_from_two_spellings(flow_dir):
    """The agent's MCP server passes the cwd it inherited; every daemon entry
    point passes its own resolved copy. Both have to land on the same run."""
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear", cwd=str(flow_dir), scope="s1")  # as the agent has it
    resolved = str(flow_dir.resolve())
    assert engine.status(cwd=resolved, scope="s1")["status"] == "step"
    # ...and the registry the dashboard scans holds one canonical entry
    assert state_mod.known_runs() == [(resolved, "s1")]


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
# two writers: start requests, the slot lock, and stale results
# --------------------------------------------------------------------------- #
def test_request_is_recorded_and_the_agent_fulfils_it(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    payload = engine.request_start("linear", "ship the thing", by="web")
    assert payload["status"] == "start_requested"
    assert payload["request"]["workflow"] == "linear"

    # nothing was started: the slot is still idle, but it says what is coming
    assert not state_mod.has_run()
    status = engine.status()
    assert status["status"] == "idle"
    assert status["pending_start"]["context"] == "ship the thing"

    # the agent performs the start itself; the request is consumed by it
    started = engine.start("linear", "ship the thing")
    assert started["step_id"] == "one"
    assert engine.status().get("pending_start") is None
    events = [e["event"] for e in state_mod.read_journal()]
    assert events[:2] == ["start_requested", "started"]
    assert "request_fulfilled" in events


def test_request_refused_while_a_run_is_active(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    with pytest.raises(CflowError, match="already active"):
        engine.request_start("linear")


def test_request_rejects_an_unknown_workflow(flow_dir):
    """Resolved in front of the human who asked, not inside the agent later."""
    with pytest.raises(WorkflowError, match="no workflow named"):
        engine.request_start("nope")
    assert engine.status().get("pending_start") is None


def test_request_keeps_the_slot_visible_and_can_be_withdrawn(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.request_start("linear", by="web")
    # the dashboard finds a slot that holds no run at all
    assert state_mod.scopes_in() == ["default"]
    assert (str(flow_dir.resolve()), "default") in state_mod.known_runs()

    engine.cancel_request(by="user")
    assert engine.status().get("pending_start") is None
    assert state_mod.scopes_in() == []
    with pytest.raises(CflowError, match="no pending start request"):
        engine.cancel_request()


def test_starting_something_else_supersedes_the_request(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    _write(flow_dir, "gated", GATED)
    engine.request_start("gated", by="web")
    engine.start("linear")
    events = [e["event"] for e in state_mod.read_journal()]
    assert "request_superseded" in events
    # consumed either way: it cannot be "fulfilled" a second time
    assert engine.status().get("pending_start") is None


def test_slot_lock_is_exclusive_and_reclaims_a_dead_holder(flow_dir):
    import os
    import time

    with state_mod.run_lock():
        with pytest.raises(state_mod.LockBusy):
            with state_mod.run_lock(timeout=0.05):
                pass
    # released on the way out
    with state_mod.run_lock(timeout=0.05):
        pass

    # a lock left behind by a killed process is not a permanent deadlock
    lock = state_mod.scope_dir() / state_mod.LOCK_FILE
    lock.write_text("999999 crashed", encoding="utf-8")
    old = time.time() - state_mod.LOCK_STALE_AFTER - 60
    os.utime(lock, (old, old))
    with state_mod.run_lock(timeout=0.05):
        pass


def test_concurrent_starts_cannot_interleave(flow_dir):
    """Two processes starting at once must not pair one workflow's snapshot
    with another's cursor — the failure the slot lock exists to prevent."""
    import threading

    _write(flow_dir, "linear", LINEAR)
    _write(flow_dir, "gated", GATED)
    results = []

    def go(name):
        try:
            results.append(engine.start(name)["workflow"])
        except (CflowError, state_mod.StateError) as exc:
            results.append(type(exc).__name__)

    threads = [
        threading.Thread(target=go, args=(name,))
        for name in ("linear", "gated", "linear", "gated")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    started = [r for r in results if r in ("linear", "gated")]
    assert len(started) == 1  # the rest were refused, not silently applied
    # cursor and snapshot describe the SAME workflow (the interleaving bug
    # pairs one workflow's first step with the other's graph)
    expected_first = {"linear": "one", "gated": "work"}[started[0]]
    assert engine.status()["step_id"] == expected_first
    assert state_mod.load_snapshot().start == expected_first


def test_verify_result_is_discarded_when_the_run_moved(flow_dir, monkeypatch):
    """A verify command runs unlocked (it can take an hour), so its result is
    only committed if the run is still where it was."""
    _write(flow_dir, "verified", _verified_yaml())
    _write(flow_dir, "linear", LINEAR)
    engine.start("verified")
    engine.report("built it")

    def moving_verify(step, cwd):
        # a human archives the run and starts another while the build runs
        engine.archive(by="user")
        engine.start("linear")
        return None  # ... and the command would have passed

    monkeypatch.setattr(engine, "_run_verify", moving_verify)
    payload = engine.next_step()
    assert payload["workflow"] == "linear"      # the run that is actually here
    assert payload["step_id"] == "one"          # untouched by the stale pass
    assert "discarded" in payload["note"]
    assert "verify_discarded" in [e["event"] for e in state_mod.read_journal()]


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
    assert names == {
        # this session's own run
        "start", "report", "next", "select", "status",
        # decisions other sessions' runs are waiting on it for
        "asks", "answer",
    }
    # still no approve, by design: `answer` decides somebody ELSE's run, and
    # a tool that could unblock this one would put the gate back in the hands
    # of the agent it is a gate on.
    assert "approve" not in names


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


def test_mcp_refuses_to_write_into_a_replaced_run(flow_dir):
    """The agent's report must never land in a run it has not read."""
    _write(flow_dir, "linear", LINEAR)
    resp = _rpc("tools/call", {"name": "start", "arguments": {"workflow": "linear"}})
    first = json.loads(resp["result"]["content"][0]["text"])["run"]

    # someone else (the dashboard, another human) replaces the run
    engine.archive(by="web")
    engine.start("linear")

    resp = _rpc("tools/call", {"name": "report", "arguments": {"summary": "done"}})
    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert first in text and "not the run here any more" in text
    assert engine.status().get("report") is None  # nothing was applied

    # re-reading the position re-arms the fence, and work resumes
    resp = _rpc("tools/call", {"name": "status", "arguments": {}})
    assert json.loads(resp["result"]["content"][0]["text"])["step_id"] == "one"
    resp = _rpc("tools/call", {"name": "report", "arguments": {"summary": "done"}})
    assert resp["result"]["isError"] is False


def test_mcp_status_surfaces_a_pending_start(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.request_start("linear", "from the dashboard", by="web")
    resp = _rpc("tools/call", {"name": "status", "arguments": {}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["pending_start"]["workflow"] == "linear"
    assert "call 'start'" in payload["note"]


def test_mcp_errors_are_soft(flow_dir):
    resp = _rpc("tools/call", {"name": "next", "arguments": {}})
    assert resp["result"]["isError"] is True
    assert "no active cflow run" in resp["result"]["content"][0]["text"]
    resp = _rpc("nonsense")
    assert resp["error"]["code"] == -32601


# --------------------------------------------------------------------------- #
# responders: reading the candidate pool out of the mesh
# --------------------------------------------------------------------------- #
def _member(handle, session, role, parent=None, local=True):
    return {
        "handle": handle, "session": session, "role": role,
        "parent": parent, "local": local, "reachability": "idle",
    }


def _roster(monkeypatch, *meshes, fail=None):
    """Answer `GET /api/mesh` with these mesh_info documents."""

    class FakeClient:
        def get(self, path, **kw):
            if fail:
                raise daemon_client.DaemonClientError(fail)
            if path == "/api/mesh":
                return {"meshes": list(meshes)}
            # The nudge asks for the session list on its way to typing into
            # the waiting run. Nobody is running here, which is the honest
            # answer and the one a best-effort nudge has to survive.
            assert path == "/api/sessions", path
            return {"sessions": []}

        def post(self, path, body, **kw):
            return {}

    monkeypatch.setattr(daemon_client, "connect", lambda: FakeClient())


def _links(*pairs):
    return [{"a": a, "b": b, "enabled": on} for (a, b, on) in pairs]


#: lead1 spawned rev1, which spawned dev1; dev1 also spawned a helper of its
#: own, and is wired to everybody. The awkward shape on purpose: a reviewer
#: that is NOT an ancestor, and a member that dev1 made itself.
TEAM = {
    "name": "team",
    "members": [
        _member("dev1", "dev1", "worker", parent="rev1"),
        _member("rev1", "rev1", "reviewer", parent="lead1"),
        _member("lead1", "lead1", "leader"),
        _member("sib1", "sib1", "reviewer", parent="rev1"),
        _member("kid1", "kid1", "leader", parent="dev1"),
    ],
    "member_links": _links(
        ("dev1", "rev1", True),
        ("dev1", "lead1", True),
        ("dev1", "sib1", True),
        ("dev1", "kid1", True),
    ),
}


def _candidate(role, scope=model.SCOPE_ANY):
    return model.Candidate(role=role, scope=scope)


def test_the_pool_is_the_member_graph_minus_what_this_run_made(monkeypatch):
    _roster(monkeypatch, TEAM)
    found = responders.pool(session="dev1")
    assert found.problem == ""
    assert found.mesh == "team" and found.me == "dev1"
    assert found.ancestors == ["rev1", "lead1"]
    assert found.descendants == {"kid1"}

    # a sibling reviewer answers — it is not an ancestor, and does not have to
    # be: dev1 could not have spawned it, and cannot wire itself to it either
    hit, reason = found.match(_candidate("reviewer"))
    assert reason is None and sorted(r.handle for r in hit) == ["rev1", "sib1"]

    # ...but its own child does not, however the roles line up
    hit, reason = found.match(_candidate("leader"))
    assert [r.handle for r in hit] == ["lead1"]
    assert "kid1" not in [r.handle for r in hit]


def test_a_descendant_is_never_a_candidate(monkeypatch):
    """The one exclusion the whole feature rests on: what a run could make."""
    made_it_all = {
        "name": "team",
        "members": [
            _member("dev1", "dev1", "worker"),
            _member("kid1", "kid1", "leader", parent="dev1"),
            _member("grandkid", "gk", "leader", parent="kid1"),
        ],
        "member_links": _links(("dev1", "kid1", True), ("dev1", "grandkid", True)),
    }
    _roster(monkeypatch, made_it_all)
    found = responders.pool(session="dev1")
    assert found.descendants == {"kid1", "grandkid"}
    hit, reason = found.match(_candidate("leader"))
    assert hit == []
    assert "no member of mesh 'team' holds that role" in reason
    assert "spawned itself are never candidates" in reason


def test_scope_ancestor_narrows_to_the_chain_of_command(monkeypatch):
    _roster(monkeypatch, TEAM)
    found = responders.pool(session="dev1")
    hit, reason = found.match(_candidate("reviewer", model.SCOPE_ANCESTOR))
    assert reason is None and [r.handle for r in hit] == ["rev1"]  # not sib1

    _, reason = found.match(_candidate("worker", model.SCOPE_ANCESTOR))
    assert "no session above dev1" in reason
    assert "rev1 (reviewer), lead1 (leader)" in reason


def test_an_unwired_member_is_named_with_the_command_that_fixes_it(monkeypatch):
    """A spawned member is wired to its parent alone — that is the default."""
    _roster(
        monkeypatch,
        {
            **TEAM,
            "member_links": _links(
                ("dev1", "rev1", True),
                ("dev1", "lead1", False),
                ("dev1", "sib1", False),
                ("dev1", "kid1", True),
            ),
        },
    )
    found = responders.pool(session="dev1")
    assert found.reachable == {"rev1", "kid1"}
    hit, reason = found.match(_candidate("leader"))
    assert hit == []
    assert "lead1 holds it but dev1 is not wired to them" in reason
    assert "claunch mesh connect dev1 lead1" in reason
    # the reviewer above is still reachable, so that group still resolves
    hit, _ = found.match(_candidate("reviewer"))
    assert [r.handle for r in hit] == ["rev1"]


def test_the_pool_needs_an_unambiguous_mesh(monkeypatch):
    other = {"name": "other", "members": [_member("dev1", "dev1", "worker")]}
    _roster(monkeypatch, TEAM, other)
    assert "several meshes (other, team)" in responders.pool(session="dev1").problem
    # naming one settles it
    picked = responders.pool(session="dev1", mesh="team")
    assert picked.problem == "" and picked.ancestors == ["rev1", "lead1"]


def test_the_pool_will_not_mistake_a_remote_namesake_for_us(monkeypatch):
    """Session names are unique per machine, so a roster match is not identity."""
    mirrored = {
        "name": "team",
        "members": [
            _member("their-dev", "dev1", "worker", parent="lead1", local=False),
            _member("lead1", "lead1", "leader"),
        ],
    }
    _roster(monkeypatch, mirrored)
    assert "not a member of any mesh" in responders.pool(session="dev1").problem


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"session": ""}, "no mesh identity"),
        ({"session": "dev1", "mesh": "nope"}, "not a member of mesh 'nope'"),
    ],
)
def test_the_pool_reports_rather_than_raises(monkeypatch, kwargs, expected):
    _roster(monkeypatch, TEAM)
    assert expected in responders.pool(**kwargs).problem


def test_a_dead_daemon_is_a_reason_not_a_crash(monkeypatch):
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    assert "daemon is not running" in responders.pool(session="dev1").problem
    _roster(monkeypatch, TEAM, fail="connection refused")
    assert "could not be read" in responders.pool(session="dev1").problem


def test_a_cycle_in_the_lineage_terminates(monkeypatch):
    looped = {
        "name": "team",
        "members": [
            _member("dev1", "dev1", "worker", parent="rev1"),
            _member("rev1", "rev1", "reviewer", parent="dev1"),
        ],
        "member_links": _links(("dev1", "rev1", True)),
    }
    _roster(monkeypatch, looped)
    found = responders.pool(session="dev1")
    assert found.ancestors == ["rev1"]
    # rev1 is both above and below in this (impossible) roster; the descendant
    # test wins, because it is the one that is load-bearing
    assert found.descendants == {"rev1"}
    assert found.match(_candidate("reviewer"))[0] == []


def test_a_remote_match_is_skipped_with_that_reason(monkeypatch):
    _roster(
        monkeypatch,
        {
            "name": "team",
            "members": [
                _member("dev1", "dev1", "worker", parent="lead1"),
                _member("lead1", "lead1", "leader", local=False),
            ],
            "member_links": _links(("dev1", "lead1", True)),
        },
    )
    found = responders.pool(session="dev1")
    hit, reason = found.match(_candidate("leader"))
    assert hit == []
    assert "another daemon has no access to this run's state" in reason


def test_an_exited_match_is_skipped_as_exited(monkeypatch):
    gone = {
        "name": "team",
        "members": [
            _member("dev1", "dev1", "worker", parent="lead1"),
            {**_member("lead1", "lead1", "leader"), "reachability": "exited"},
        ],
        "member_links": _links(("dev1", "lead1", True)),
    }
    _roster(monkeypatch, gone)
    hit, reason = responders.pool(session="dev1").match(_candidate("leader"))
    assert hit == [] and "the session has exited" in reason


# --------------------------------------------------------------------------- #
# model: delegated decisions
# --------------------------------------------------------------------------- #
def test_ask_parses_a_preference_list():
    wf = model.parse(ASK_TWO_GROUPS)
    ask = wf.steps["ship"].ask
    assert [c.describe() for c in ask.delegate.candidates] == ["reviewer", "leader"]
    assert ask.delegate.otherwise == "human"  # the default, and the safe one
    assert wf.steps["ship"].entry_prompt == "ship it?"
    assert model.parse(ASK_FLOW).steps["ship"].ask.on_decline == "impl"


def test_a_candidate_must_name_a_role():
    """'whoever I happen to be wired to' is not a delegation."""
    bad = """
steps:
  ship:
    ask:
      prompt: ok?
      from: [{scope: ancestor}]
    instructions: ship
"""
    with pytest.raises(WorkflowError, match="needs a 'role'"):
        model.parse(bad)


@pytest.mark.parametrize(
    "entry, match",
    [
        ("boss", "must be a mapping"),              # the bare token is gone
        ("{role: leader, up: 1}", "unknown key"),    # so is the hop range
        ("{role: leader, scope: sideways}", "scope"),
        ("{role: '', scope: any}", "needs a 'role'"),
    ],
)
def test_bad_candidates_are_rejected(entry, match):
    bad = f"""
steps:
  ship:
    ask:
      prompt: ok?
      from: [{entry}]
    instructions: ship
"""
    with pytest.raises(WorkflowError, match=match):
        model.parse(bad)


def test_the_two_axes_are_independent():
    """`from` says who is asked; `otherwise` says what happens if none do."""
    text = """
steps:
  ship:
    ask:
      prompt: ok?
      from: [{role: leader, scope: ancestor}]
      otherwise: self
    instructions: ship
"""
    delegate = model.parse(text).steps["ship"].ask.delegate
    assert delegate.describe() == "leader (ancestor) -> self"
    # and a human is not something you can be asked
    with pytest.raises(WorkflowError, match="must be a mapping"):
        model.parse(text.replace("{role: leader, scope: ancestor}", "human"))
    with pytest.raises(WorkflowError, match="'otherwise' must be one of"):
        model.parse(text.replace("otherwise: self", "otherwise: nobody"))


def test_from_is_optional_and_that_is_a_human_gate():
    text = """
steps:
  ship:
    ask:
      prompt: ok?
    instructions: ship
"""
    delegate = model.parse(text).steps["ship"].ask.delegate
    assert delegate.candidates == [] and delegate.otherwise == "human"


def test_gate_and_ask_cannot_both_guard_a_step():
    bad = """
steps:
  ship:
    gate: approve
    ask:
      prompt: ok?
    instructions: ship
"""
    with pytest.raises(WorkflowError, match="both entry approvals"):
        model.parse(bad)


def test_decline_target_must_exist():
    bad = """
steps:
  ship:
    ask:
      prompt: ok?
      on_decline: nowhere
    instructions: ship
"""
    with pytest.raises(WorkflowError, match="unknown step 'nowhere'"):
        model.parse(bad)


def test_a_decline_route_is_a_real_edge():
    """A loop whose only exit is a decline still counts as finishable."""
    text = """
steps:
  work:
    ask:
      prompt: again?
      on_decline: end
    instructions: work
    next: work
"""
    wf = model.parse(text)  # no 'no termination is reachable' error
    assert set(wf.steps["work"].successors()) == {None, "work"}


def test_gate_is_deprecated_without_becoming_a_run_warning():
    wf = model.parse(GATED)
    assert wf.warnings == []
    assert len(wf.deprecations) == 1
    assert "'gate:' is deprecated" in wf.deprecations[0]
    assert "ask: {prompt: <the gate message>}" in wf.deprecations[0]
    assert model.parse(ASK_FLOW).deprecations == []


def test_select_chooser_accepts_a_delegation():
    select = model.parse(DELEGATED_BRANCH).steps["verdict"].select
    assert select.chooser == "delegate"
    assert select.delegate.describe() == "reviewer -> human"
    assert model.parse(BRANCH_SELF).steps["verdict"].select.delegate.otherwise == "self"


def test_unknown_chooser_names_the_delegation_form():
    bad = """
steps:
  s:
    select:
      prompt: p
      chooser: nobody
      options:
        a: {description: d}
"""
    with pytest.raises(WorkflowError, match="from:"):
        model.parse(bad)


# --------------------------------------------------------------------------- #
# engine: delegated approvals
# --------------------------------------------------------------------------- #
def test_ask_withholds_the_step_until_a_responder_answers(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")

    payload = _advance("implemented")
    assert payload["status"] == "waiting_answer"
    assert payload["reason"] == "approval"
    assert "instructions" not in payload
    assert [e["handle"] for e in payload["ask"]["asked"]] == ["leader-boss"]
    # the driver cannot talk its own way past it
    with pytest.raises(CflowError, match="not been delivered"):
        engine.report("pretending")
    assert engine.next_step()["status"] == "waiting_answer"

    ask_id = payload["ask"]["id"]
    receipt = engine.answer(ask_id, "approve", "diff looks right", by_session="boss")
    assert receipt["status"] == "answered"
    assert receipt["decision"] == "approve"
    assert "instructions" not in receipt  # a responder never gets the step

    payload = engine.next_step()
    assert payload["status"] == "step"
    assert payload["instructions"].strip() == "ship it"


def test_a_run_cannot_answer_its_own_ask(flow_dir, monkeypatch):
    _driving_session(monkeypatch, "driver")
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    ask_id = _advance("implemented")["ask"]["id"]

    with pytest.raises(CflowError, match="cannot approve itself"):
        engine.answer(ask_id, "approve", by_session="driver")
    assert engine.status()["status"] == "waiting_answer"


def test_a_session_that_was_not_asked_is_refused(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    ask_id = _advance("implemented")["ask"]["id"]

    with pytest.raises(CflowError, match="was not asked this"):
        engine.answer(ask_id, "approve", by_session="bystander")
    with pytest.raises(CflowError, match="cannot be attributed"):
        engine.answer(ask_id, "approve", by_session="")
    assert engine.status()["status"] == "waiting_answer"


def test_the_decision_set_is_closed(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    ask_id = _advance("implemented")["ask"]["id"]

    for wording in ("lgtm", "yes", "approved!", ""):
        with pytest.raises(CflowError, match="unknown decision"):
            engine.answer(ask_id, wording, by_session="boss")
    assert engine.status()["status"] == "waiting_answer"


def test_an_answer_lands_once(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    ask_id = _advance("implemented")["ask"]["id"]

    engine.answer(ask_id, "approve", by_session="boss")
    with pytest.raises(CflowError, match="not open any more"):
        engine.answer(ask_id, "decline", by_session="boss")


def test_decline_routes_where_the_workflow_declared(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    ask_id = _advance("implemented")["ask"]["id"]

    engine.answer(ask_id, "decline", "the tests do not cover it", by_session="boss")
    payload = engine.status()
    assert payload["step_id"] == "impl"
    assert payload["visit"] == 2
    events = [e["event"] for e in state_mod.read_journal()]
    assert "ask_declined" in events


def test_decline_without_a_route_holds_for_a_human(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "hold", ASK_HOLD)
    engine.start("hold")
    ask_id = engine.status()["ask"]["id"]

    engine.answer(ask_id, "decline", "not yet", by_session="boss")
    payload = engine.status()
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "declined"
    assert "not yet" in payload["gate"]
    assert "instructions" not in payload
    # and it does NOT re-ask the same responder in a loop
    assert engine.next_step()["reason"] == "declined"

    engine.approve(by="user")  # a human overrides
    assert engine.next_step()["status"] == "step"


def test_abstain_escalates_to_the_next_group(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("reviewer", "peer"), ("leader", "boss"))
    _write(flow_dir, "two", ASK_TWO_GROUPS)
    engine.start("two")
    payload = engine.status()
    ask_id = payload["ask"]["id"]
    assert [e["handle"] for e in payload["ask"]["asked"]] == ["reviewer-peer"]

    receipt = engine.answer(ask_id, "abstain", "not my call", by_session="peer")
    assert receipt["prompt"] == "ship it?"  # the question comes back with it
    assert receipt["reason"] == "not my call"
    payload = engine.status()
    assert payload["status"] == "waiting_answer"
    assert [e["handle"] for e in payload["ask"]["asked"]] == ["leader-boss"]
    assert payload["ask"]["id"] == ask_id  # the same decision, further along
    assert "not my call" in payload["ask"]["skipped"][-1]["reason"]

    # the group that already passed cannot answer any more
    with pytest.raises(CflowError, match="no longer yours"):
        engine.answer(ask_id, "approve", by_session="peer")
    engine.answer(ask_id, "approve", by_session="boss")
    assert engine.next_step()["status"] == "step"


def test_an_unreachable_group_falls_through_to_the_human(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch)  # nobody to ask at all
    _write(flow_dir, "two", ASK_TWO_GROUPS)
    engine.start("two")
    payload = engine.status()
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "ask"
    # nobody is IN the group: `otherwise: human` is the default, so the run
    # holds rather than proceeding, and the CLI settles it as it always has
    assert payload["ask"]["asked"] == []
    assert [s["candidate"] for s in payload["ask"]["skipped"]] == [
        "reviewer",
        "leader",
    ]
    engine.approve(by="user")
    assert engine.next_step()["status"] == "step"


def test_nobody_at_all_still_reaches_a_human(flow_dir, monkeypatch):
    """Nothing resolvable, and no configuration that turns that into approval."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch)
    _write(flow_dir, "hold", ASK_HOLD)
    engine.start("hold")
    payload = engine.status()
    assert payload["status"] == "waiting_approval"
    assert payload["ask"]["asked"] == []
    assert "no candidate could be reached" in payload["note"]
    with pytest.raises(CflowError, match="was not asked this"):
        engine.answer(payload["ask"]["id"], "approve", by_session="boss")
    engine.approve(by="user")
    assert engine.next_step()["status"] == "step"


def test_a_sibling_reviewer_can_approve(flow_dir, monkeypatch):
    """The common shape: two children of one parent, one reviewing the other."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"), ("reviewer", "sib", "leader-boss"),
          parent="leader-boss")
    _write(flow_dir, "two", ASK_TWO_GROUPS)
    engine.start("two")
    payload = engine.status()
    assert [e["handle"] for e in payload["ask"]["asked"]] == ["reviewer-sib"]

    engine.answer(payload["ask"]["id"], "approve", "read the diff", by_session="sib")
    assert engine.next_step()["status"] == "step"


def test_a_run_cannot_approve_itself_through_a_session_it_spawned(
    flow_dir, monkeypatch
):
    """The invariant, end to end: a child holding the role is not a candidate."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "mine", "dev1"))  # spawned by the driver
    _write(flow_dir, "hold", ASK_HOLD)
    engine.start("hold")
    payload = engine.status()
    assert payload["status"] == "waiting_approval"  # a human, not the child
    assert payload["ask"]["asked"] == []
    assert "spawned itself are never candidates" in payload["ask"]["skipped"][0]["reason"]
    with pytest.raises(CflowError, match="was not asked this"):
        engine.answer(payload["ask"]["id"], "approve", by_session="mine")


def test_otherwise_self_proceeds_unapproved_and_says_so(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch)  # no leader to ask
    _write(flow_dir, "solo", ASK_SELF)
    engine.start("solo")
    payload = engine.status()
    assert payload["status"] == "step"  # the run carries on
    assert payload["instructions"].strip() == "ship it"

    events = [e["event"] for e in state_mod.read_journal()]
    assert "ask_unanswered_proceeded" in events
    # ...and NOT as an approval: nothing in the record says anybody approved
    assert "ask_answered" not in events
    entry = next(
        e for e in state_mod.read_journal() if e["event"] == "ask_unanswered_proceeded"
    )
    assert entry["kind"] == "approval"
    assert "no member of mesh" in entry["skipped"][0]


def test_otherwise_self_still_asks_when_there_is_somebody(flow_dir, monkeypatch):
    """`self` is the fallback, not a shortcut past a responder who exists."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "solo", ASK_SELF)
    engine.start("solo")
    assert engine.status()["status"] == "waiting_answer"


def test_otherwise_self_hands_a_branch_back_to_the_driver(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch)  # no reviewer
    _write(flow_dir, "branch", BRANCH_SELF)
    engine.start("branch")
    payload = engine.status()
    assert payload["status"] == "select"
    assert payload["chooser"] == "agent"
    assert "meant to be somebody else's decision" in payload["note"]

    # and the driver may now take it, where a delegated select refuses it
    engine.select("rework", "I judged it myself", by="agent")
    assert engine.status()["step_id"] == "impl"


def test_a_delegated_branch_falls_through_once_per_visit(flow_dir, monkeypatch):
    """The fall-through is keyed to the visit, so a loop asks again."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch)
    _write(flow_dir, "branch", BRANCH_SELF)
    engine.start("branch")
    engine.select("rework", "nobody about", by="agent")

    # a reviewer has appeared in the meantime; the next pass asks them
    _mesh(monkeypatch, ("reviewer", "peer"))
    payload = _advance("reworked")  # back to verdict, second visit
    assert payload["status"] == "waiting_answer"
    assert [e["handle"] for e in payload["ask"]["asked"]] == ["reviewer-peer"]


def test_start_reports_what_the_delegations_resolve_to(flow_dir, monkeypatch):
    """Never blocking: a leader that has not spawned yet is legitimate."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("reviewer", "peer"))
    _write(flow_dir, "two", ASK_TWO_GROUPS)

    check = engine.request_start("two", by="user")["delegation_check"]
    assert check["steps"] == [
        {
            "step": "ship",
            "decision": "approval",
            "from": "reviewer -> leader",
            "resolves": ["reviewer-peer"],
            "otherwise": "human",
        }
    ]

    _mesh(monkeypatch)  # everybody has gone home
    check = engine.start("two")["delegation_check"]
    assert check["steps"][0]["resolves"] == []
    assert "no member of mesh 'team' holds that role" in check["steps"][0]["reason"]
    assert "would reach a human" in check["note"]


def test_a_workflow_that_delegates_nothing_gets_no_check(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch)
    _write(flow_dir, "linear", LINEAR)
    assert "delegation_check" not in engine.start("linear")


def test_an_ask_closes_again_on_every_visit(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askloop", ASK_LOOP)
    engine.start("askloop")
    first = engine.status()["ask"]["id"]
    engine.answer(first, "approve", by_session="boss")
    # an approval unblocks; the step is still fetched by the agent itself
    assert engine.next_step()["status"] == "step"
    _advance("shipped once")
    engine.select("yes", "round two", by="agent")

    payload = engine.status()
    assert payload["status"] == "waiting_answer"
    assert payload["visit"] == 2
    assert payload["ask"]["id"] != first  # a new decision, not a kept approval


def test_goto_discards_an_open_ask(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    ask_id = _advance("implemented")["ask"]["id"]

    engine.goto("impl", by="user", reason="redo it")
    assert "ask_discarded" in [e["event"] for e in state_mod.read_journal()]
    with pytest.raises(CflowError, match="not open any more"):
        engine.answer(ask_id, "approve", by_session="boss")


# --------------------------------------------------------------------------- #
# engine: delegated branches
# --------------------------------------------------------------------------- #
def test_a_responder_picks_the_branch(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("reviewer", "peer"))
    _write(flow_dir, "branch", DELEGATED_BRANCH)
    engine.start("branch")
    payload = engine.status()
    assert payload["status"] == "waiting_answer"
    assert payload["reason"] == "branch"
    assert {o["name"] for o in payload["ask"]["options"]} == {"ready", "rework"}

    ask_id = payload["ask"]["id"]
    with pytest.raises(CflowError, match="unknown decision"):
        engine.answer(ask_id, "ship", by_session="peer")

    engine.answer(ask_id, "rework", "tests are thin", by_session="peer")
    payload = engine.status()
    assert payload["step_id"] == "impl"
    assert payload["steps_completed"] == 1  # the decision counted as a step


def test_the_driver_cannot_take_a_delegated_branch(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("reviewer", "peer"))
    _write(flow_dir, "branch", DELEGATED_BRANCH)
    engine.start("branch")
    with pytest.raises(CflowError, match="not yours to make"):
        engine.select("ready", "looks fine to me", by="agent")
    assert engine.status()["status"] == "waiting_answer"


def test_a_deadline_moves_the_question_up(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("reviewer", "peer"), ("leader", "boss"))
    _write(flow_dir, "two", ASK_TWO_GROUPS.replace("from:", "timeout: 600\n      from:"))
    engine.start("two")
    payload = engine.status()
    ask_id = payload["ask"]["id"]
    assert payload["ask"]["deadline"]

    # not due yet: the clock is not a nudge to hurry up
    assert engine.expire_ask() is None
    assert [e["handle"] for e in engine.status()["ask"]["asked"]] == ["reviewer-peer"]

    later = datetime.now(timezone.utc) + timedelta(seconds=601)
    moved = engine.expire_ask(now=later)
    assert moved["expired"] == "reviewer-peer"
    assert moved["now_with"] == ["leader-boss"]
    payload = engine.status()
    assert payload["ask"]["id"] == ask_id  # the same decision, further along
    assert "did not answer by" in payload["ask"]["skipped"][-1]["reason"]

    # the group that lapsed cannot answer late
    with pytest.raises(CflowError, match="no longer yours"):
        engine.answer(ask_id, "approve", by_session="peer")


def test_expiry_walks_off_the_end_to_a_human(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "hold", ASK_HOLD.replace("from:", "timeout: 60\n      from:"))
    engine.start("hold")
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    engine.expire_ask(now=later)

    payload = engine.status()
    assert payload["status"] == "waiting_approval"  # never "proceed anyway"
    assert payload["ask"]["asked"] == []
    assert payload["ask"]["deadline"] is None  # nobody left to run a clock on
    assert engine.expire_ask(now=later) is None


def test_an_unclocked_ask_waits_forever(flow_dir, monkeypatch):
    """No timeout means no timeout — it must not lapse into proceeding."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    _advance("implemented")
    far = datetime.now(timezone.utc) + timedelta(days=365)
    assert engine.expire_ask(now=far) is None
    assert engine.status()["status"] == "waiting_answer"


def test_a_responder_finds_and_answers_by_id_alone(flow_dir, monkeypatch):
    """The responder never learns the asking run's directory or scope."""
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow")
    ask_id = _advance("implemented")["ask"]["id"]

    waiting = engine.open_asks("boss")
    assert [e["ask"] for e in waiting] == [ask_id]
    assert waiting[0]["from_session"] == "driver"
    assert waiting[0]["prompt"] == "ship it?"
    assert [o["name"] for o in waiting[0]["options"]] == ["approve", "decline"]

    assert engine.open_asks("bystander") == []
    with pytest.raises(CflowError, match="no open request"):
        engine.answer_ask(ask_id, "approve", by_session="bystander")

    engine.answer_ask(ask_id, "approve", "checked it", by_session="boss")
    assert engine.open_asks("boss") == []
    assert engine.next_step()["status"] == "step"


def test_answering_does_not_fence_the_responders_own_run(flow_dir, monkeypatch):
    """`answer` names somebody else's run; adopting that id would wedge ours."""
    _driving_session(monkeypatch, "boss")
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "linear", LINEAR)
    _write(flow_dir, "askflow", ASK_FLOW)

    # the responder is driving its own run in another scope
    engine.start("linear", scope="boss")
    mcp.call_tool("status", {})
    own = mcp._seen_run

    engine.start("askflow", scope="driver")
    engine.report("implemented", scope="driver")
    ask_id = engine.next_step(scope="driver")["ask"]["id"]

    payload = mcp.call_tool("answer", {"ask": ask_id, "decision": "approve"})
    assert payload["status"] == "answered"
    assert mcp._seen_run == own  # still fenced to our own run, not theirs
    assert mcp.call_tool("report", {"summary": "did one"})["status"] == "reported"


def test_asks_is_scoped_to_the_calling_session(flow_dir, monkeypatch):
    _driving_session(monkeypatch, "boss")
    _mesh(monkeypatch, ("leader", "boss"))
    _write(flow_dir, "askflow", ASK_FLOW)
    engine.start("askflow", scope="driver")
    engine.report("implemented", scope="driver")
    engine.next_step(scope="driver")

    payload = mcp.call_tool("asks", {})
    assert [e["step"] for e in payload["waiting_on_you"]] == ["ship"]
    assert "abstain" in payload["note"]

    monkeypatch.setenv(state_mod.SESSION_ENV, "nobody")
    assert mcp.call_tool("asks", {})["waiting_on_you"] == []


def test_a_human_can_settle_a_delegated_branch(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _mesh(monkeypatch)  # no reviewer above us
    _write(flow_dir, "branch", DELEGATED_BRANCH)
    engine.start("branch")
    payload = engine.status()
    assert payload["status"] == "waiting_selection"
    assert payload["prompt"] == "ready?"

    engine.select("ready", "I looked myself", by="user")
    assert engine.status()["status"] == "done"
    events = [e["event"] for e in state_mod.read_journal()]
    assert "ask_answered" in events and "ask_discarded" not in events


# --------------------------------------------------------------------------- #
# the authoring skill
# --------------------------------------------------------------------------- #
def test_the_authoring_skill_only_shows_yaml_the_parser_accepts(tmp_path):
    """A skill that teaches a spelling the parser rejects is worse than none.

    So its examples are parsed rather than eyeballed — which is the whole
    reason the text lives in a module beside the parser instead of in a loose
    markdown file that nothing reads.
    """
    from claude_launcher.cflow import authoring

    path = authoring.write_skill(tmp_path / "skills")
    text = path.read_text(encoding="utf-8")
    assert path.name == "SKILL.md" and path.parent.name == "cflow-author"
    assert text.startswith("---\nname: cflow-author\n")
    assert "NOT for running one" in text  # it must not trigger on execution

    blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
    assert blocks, "the skill shows no YAML at all"
    for block in blocks:
        # The fragments are step definitions that route to steps they do not
        # define, so give those somewhere real to land.
        wf = model.parse(
            "steps:\n"
            + "".join(f"  {line}\n" for line in block.splitlines())
            + "  impl:\n    instructions: x\n"
            "  netverify:\n    instructions: x\n"
            "  review:\n    instructions: x\n"
        )
        assert wf.deprecations == [], f"deprecated spelling shown: {wf.deprecations}"


def test_the_authoring_skill_states_the_rule_it_exists_for():
    """The incentive rule is the one line here that prevents an incident."""
    from claude_launcher.cflow import authoring

    # Whitespace-normalized: the source is hard-wrapped prose, so a phrase
    # asserted against it must not also be asserting where the line broke.
    text = " ".join(authoring.SKILL_MD.split())
    assert "whose answer lets it skip work" in text
    # ...and the mechanics that make a delegation worth anything, since an
    # author who does not know them will design around them
    assert "descendants are never candidates" in text
    assert "never as an approval" in text  # what `otherwise: self` records
    assert "never receives the asking step's instructions" in text
