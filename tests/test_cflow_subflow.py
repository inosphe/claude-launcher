"""Sub runs: N side tracks under one scope, driven by the same session.

A sub run is a full run slot at ``runs/<scope>/sub/<name>/`` beside the
scope's main run. Its definition is an ordinary workflow file marked
``kind: subflow`` — shared by name through the same layers as every other
workflow — and it is addressed on every engine op, MCP tool and CLI command
with ``run=<name>``; a call that names no run is about the main run, exactly
as before sub runs existed.

What this file pins:

* the definition contract (``kind``, ``inputs``, what a sub definition may
  not carry) at parse time;
* the slot: paths, registry, ``run`` omitted = main run;
* the start rules: a main run must be active, the definition must be marked,
  at most ``MAX_SUBFLOWS`` side tracks, inputs resolved and refused;
* independence: two sub runs and the main run move without touching each
  other, and the main run's status lists them;
* the end: a main run's done/abort ends its sub runs, an archive retires
  them with it, and both journals say so;
* the doors: MCP ``run``/``sub``/``inputs`` and the per-run fence, the CLI's
  ``--run`` and ``sub-done``.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from claude_launcher import cli_cflow
from claude_launcher.cflow import engine, mcp, model, state as state_mod
from claude_launcher.cflow.engine import CflowError

MAIN = """
name: main
steps:
  work:
    instructions: do the work
    next: review
  review:
    instructions: review it
"""

SUB = """
name: side
kind: subflow
description: a side track
inputs:
  base: {required: true}
  paths: {default: ''}
steps:
  derive:
    instructions: derive the list
    verify: 'true'
    next: inspect
  inspect:
    instructions: inspect each
"""

SUB_ENV = """
name: envcheck
kind: subflow
inputs:
  base: {required: true}
steps:
  probe:
    instructions: the verify reads the input from its environment
    verify: 'python -c "import os,sys; sys.exit(0 if os.environ.get(\\"CFLOW_IN_BASE\\") == \\"abc123\\" else 3)"'
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    (wf / "main.yaml").write_text(MAIN, encoding="utf-8")
    (wf / "side.yaml").write_text(SUB, encoding="utf-8")
    (wf / "envcheck.yaml").write_text(SUB_ENV, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.setenv(state_mod.SESSION_ENV, "s1")
    mcp._seen_run = None
    mcp._seen_subs.clear()
    # The CLI's --run installs the sub run as the process's ambient run for
    # the rest of that (short-lived) process and never pops it. In one pytest
    # worker that "rest of the process" is every later test, so an earlier
    # CLI test would make this test's start("main") land in a sub slot.
    token = state_mod._run_override.set(None)
    yield d
    state_mod._run_override.reset(token)


def _slot(d, name=None):
    base = d / ".cflow" / "runs" / "s1"
    return base / state_mod.SUB_DIR / name if name else base


# --------------------------------------------------------------------------- #
# the definition contract
# --------------------------------------------------------------------------- #
def test_kind_subflow_parses_with_inputs():
    w = model.parse(SUB)
    assert w.kind == model.KIND_SUBFLOW
    assert sorted(w.inputs) == ["base", "paths"]
    assert w.inputs["base"].required is True
    assert w.inputs["paths"].default == ""
    assert w.inputs["base"].env_name == "CFLOW_IN_BASE"


def test_an_ordinary_workflow_has_no_kind_and_takes_no_inputs():
    assert model.parse(MAIN).kind is None
    with pytest.raises(model.WorkflowError, match="only taken by a 'kind: subflow'"):
        model.parse("inputs: {x: null}\nsteps: {a: {instructions: x}}")
    with pytest.raises(model.WorkflowError, match="'kind' must be one of"):
        model.parse("kind: other\nsteps: {a: {instructions: x}}")


@pytest.mark.parametrize(
    "extra, refused",
    [
        ("recur: true", "'recur'"),
        ("filter_roles: {type: whitelist, roles: [worker]}", "'filter_roles'"),
        ("default_child_cflow: main", "default_child_cflow"),
        ("default_role: worker", "'default_role'"),
    ],
)
def test_a_sub_definition_may_not_carry_session_lifecycle(extra, refused):
    text = f"kind: subflow\n{extra}\nsteps:\n  a:\n    instructions: x\n"
    with pytest.raises(model.WorkflowError, match=refused):
        model.parse(text)


def test_a_sub_definition_may_not_escalate():
    text = (
        "kind: subflow\nsteps:\n  a:\n    instructions: x\n"
        "    escalate: {workflow: main}\n"
    )
    with pytest.raises(model.WorkflowError, match="may not 'escalate'"):
        model.parse(text)


def test_inputs_are_validated_at_parse():
    with pytest.raises(model.WorkflowError, match="must be lower-case"):
        model.parse("kind: subflow\ninputs: {Base: null}\nsteps: {a: {instructions: x}}")
    with pytest.raises(model.WorkflowError, match="unknown key"):
        model.parse("kind: subflow\ninputs: {b: {typo: 1}}\nsteps: {a: {instructions: x}}")
    with pytest.raises(model.WorkflowError, match="both required and defaulted"):
        model.parse(
            "kind: subflow\ninputs: {b: {required: true, default: x}}\n"
            "steps: {a: {instructions: x}}"
        )


def test_resolve_inputs_refuses_unknown_and_missing_and_fills_defaults():
    w = model.parse(SUB)
    assert model.resolve_inputs(w.inputs, {"base": "auto"}) == {"base": "auto", "paths": ""}
    assert model.resolve_inputs(w.inputs, {"base": 7, "paths": "a b"}) == {"base": "7", "paths": "a b"}
    with pytest.raises(model.WorkflowError, match="required input 'base'"):
        model.resolve_inputs(w.inputs, {})
    with pytest.raises(model.WorkflowError, match="unknown input"):
        model.resolve_inputs(w.inputs, {"base": "x", "nope": "y"})


# --------------------------------------------------------------------------- #
# the slot
# --------------------------------------------------------------------------- #
def test_run_names_are_spelled_like_scopes_and_main_is_reserved():
    assert state_mod.normalize_run(None) is None
    assert state_mod.normalize_run("") is None
    assert state_mod.normalize_run("main") is None
    assert state_mod.normalize_run("side-1") == "side-1"
    for bad in ("..", "a/b", "a b", "."):
        with pytest.raises(state_mod.StateError):
            state_mod.normalize_run(bad)
    assert not state_mod.valid_run_name("main")


def test_scope_dir_places_a_sub_run_inside_its_scope(proj):
    main = state_mod.scope_dir(str(proj), "s1")
    sub = state_mod.scope_dir(str(proj), "s1", run="side")
    assert sub == main / state_mod.SUB_DIR / "side"
    # the ambient run does the same, and MAIN_RUN forces the main slot
    token = state_mod.push_run("side")
    try:
        assert state_mod.scope_dir(str(proj), "s1") == sub
        assert state_mod.scope_dir(str(proj), "s1", run=state_mod.MAIN_RUN) == main
    finally:
        state_mod.pop_run(token)
    assert state_mod.current_run() is None


# --------------------------------------------------------------------------- #
# starting
# --------------------------------------------------------------------------- #
def test_a_sub_run_needs_an_active_main_run(proj):
    with pytest.raises(CflowError, match="needs an active main run"):
        engine.start("side", inputs={"base": "x"}, run="side")
    assert not (_slot(proj, "side") / "state.json").exists()


def test_a_sub_run_starts_in_its_own_slot_and_both_journals_say_so(proj):
    main = engine.start("main")
    sub = engine.start("side", inputs={"base": "auto"}, run="side")
    assert sub["sub"] == "side"
    assert sub["parent_run"] == main["run"]
    assert sub["inputs"] == {"base": "auto", "paths": ""}
    assert sub["step_id"] == "derive"
    assert (_slot(proj, "side") / "state.json").is_file()
    assert (_slot(proj, "side") / "workflow.yaml").is_file()
    # the main run is untouched, and knows about its side track
    st = engine.status()
    assert st["run"] == main["run"] and st["step_id"] == "work"
    assert "sub" not in st
    assert st["subs"] == [
        {"sub": "side", "run": sub["run"], "workflow": "side", "status": "running", "step_id": "derive"}
    ]
    main_events = [e["event"] for e in state_mod.read_journal(str(proj), "s1")]
    assert "sub_started" in main_events
    started = next(e for e in state_mod.read_journal(str(proj), "s1") if e["event"] == "sub_started")
    assert started == {
        **started,
        "run": main["run"], "sub": "side", "sub_run": sub["run"], "workflow": "side",
    }
    token = state_mod.push_run("side")
    try:
        own = state_mod.read_journal(str(proj), "s1")
    finally:
        state_mod.pop_run(token)
    assert [e["event"] for e in own if e["event"] != "step_delivered"] == ["started"]
    assert own[0]["sub"] == "side" and own[0]["inputs"] == {"base": "auto", "paths": ""}


def test_only_a_kind_subflow_definition_may_be_a_sub_run(proj):
    engine.start("main")
    with pytest.raises(CflowError, match="is not a 'kind: subflow' definition"):
        engine.start("main", run="again")
    assert not (_slot(proj, "again") / "state.json").exists()


def test_a_sub_definition_may_run_as_the_main_run(proj):
    payload = engine.start("side", inputs={"base": "x"})
    assert "sub" not in payload
    assert payload["inputs"] == {"base": "x", "paths": ""}
    assert (_slot(proj) / "state.json").is_file()


def test_inputs_are_refused_where_they_do_not_belong(proj):
    with pytest.raises(CflowError, match="takes no inputs"):
        engine.start("main", inputs={"base": "x"})
    engine.start("main")
    with pytest.raises(model.WorkflowError, match="required input 'base'"):
        engine.start("side", run="side")
    with pytest.raises(model.WorkflowError, match="unknown input"):
        engine.start("side", inputs={"base": "x", "zzz": 1}, run="side")
    assert not (_slot(proj, "side") / "state.json").exists()


def test_the_scope_holds_at_most_max_subflows(proj):
    engine.start("main")
    for i in range(engine.MAX_SUBFLOWS):
        engine.start("side", inputs={"base": "x"}, run=f"s{i}")
    with pytest.raises(CflowError, match=f"limit is {engine.MAX_SUBFLOWS}"):
        engine.start("side", inputs={"base": "x"}, run="one-more")
    assert state_mod.sub_runs(str(proj)) == [f"s{i}" for i in range(engine.MAX_SUBFLOWS)]


def test_the_same_sub_name_is_one_slot(proj):
    engine.start("main")
    first = engine.start("side", inputs={"base": "x"}, run="side")
    with pytest.raises(CflowError, match="already active"):
        engine.start("side", inputs={"base": "x"}, run="side")
    assert engine.status(run="side")["run"] == first["run"]


# --------------------------------------------------------------------------- #
# independence
# --------------------------------------------------------------------------- #
def test_two_sub_runs_and_the_main_run_move_independently(proj):
    main = engine.start("main")
    a = engine.start("side", inputs={"base": "a"}, run="a")
    b = engine.start("side", inputs={"base": "b"}, run="b")
    engine.report("derived", run="a")
    moved = engine.next_step(run="a")
    assert moved["sub"] == "a" and moved["step_id"] == "inspect"
    assert engine.status(run="b")["step_id"] == "derive"
    assert engine.status()["step_id"] == "work"
    assert engine.status(run="a")["inputs"] == {"base": "a", "paths": ""}
    assert engine.status(run="b")["inputs"] == {"base": "b", "paths": ""}
    # finishing a sub run frees nothing of the main run, and the main
    # journal records the end
    engine.report("inspected", run="a")
    done = engine.next_step(run="a")
    assert done["status"] == "done" and done["sub"] == "a"
    assert engine.status()["run"] == main["run"]
    ended = [e for e in state_mod.read_journal(str(proj), "s1") if e["event"] == "sub_ended"]
    assert ended == [{**ended[0], "sub": "a", "sub_run": a["run"], "status": "done"}]
    assert b["run"] == engine.status(run="b")["run"]
    listed = {s["sub"]: s["status"] for s in engine.status()["subs"]}
    assert listed == {"a": "done", "b": "running"}


def test_verify_of_a_sub_run_reads_its_inputs_from_the_environment(proj):
    engine.start("main")
    engine.start("envcheck", inputs={"base": "abc123"}, run="env")
    engine.report("probing", run="env")
    payload = engine.next_step(run="env")
    assert payload["status"] == "done", payload
    engine.start("envcheck", inputs={"base": "wrong"}, run="env2")
    engine.report("probing", run="env2")
    payload = engine.next_step(run="env2")
    assert payload["status"] == "verify_failed"
    assert payload["exit_code"] == 3


def test_probe_env_carries_inputs_and_nothing_else_changes(monkeypatch):
    env = engine.probe_env("s1", inputs={"base": "x", "paths": "a b"})
    assert env[state_mod.SESSION_ENV] == "s1"
    assert env["CFLOW_IN_BASE"] == "x" and env["CFLOW_IN_PATHS"] == "a b"
    assert "CFLOW_IN_BASE" not in engine.probe_env("s1")


# --------------------------------------------------------------------------- #
# the end of the main run
# --------------------------------------------------------------------------- #
def test_the_main_run_finishing_ends_its_sub_runs(proj):
    main = engine.start("main")
    a = engine.start("side", inputs={"base": "a"}, run="a")
    engine.report("w")
    engine.next_step()
    engine.report("r")
    done = engine.next_step()
    assert done["status"] == "done"
    assert engine.status(run="a")["status"] == "aborted"
    token = state_mod.push_run("a")
    try:
        own = state_mod.read_journal(str(proj), "s1")
    finally:
        state_mod.pop_run(token)
    aborted = next(e for e in own if e["event"] == "aborted")
    assert aborted["reason"] == "main run ended"
    ended = [e for e in state_mod.read_journal(str(proj), "s1") if e["event"] == "sub_ended"]
    assert ended == [{**ended[0], "run": main["run"], "sub": "a", "sub_run": a["run"], "status": "aborted"}]


def test_aborting_the_main_run_ends_its_sub_runs(proj):
    engine.start("main")
    engine.start("side", inputs={"base": "a"}, run="a")
    engine.abort()
    assert engine.status(run="a")["status"] == "aborted"
    # and a sub run already finished is left as it was
    ended = [e for e in state_mod.read_journal(str(proj), "s1") if e["event"] == "sub_ended"]
    assert len(ended) == 1


def test_archiving_the_main_run_retires_its_sub_runs(proj):
    engine.start("main")
    engine.start("side", inputs={"base": "a"}, run="a")
    engine.start("side", inputs={"base": "b"}, run="b")
    engine.archive()
    assert state_mod.sub_runs(str(proj)) == []
    for name in ("a", "b"):
        archive = _slot(proj, name) / "archive"
        assert archive.is_dir() and len(list(archive.iterdir())) == 1
        assert not (_slot(proj, name) / "state.json").exists()
    assert engine.status()["status"] == "idle"
    # a fresh main run starts without last round's side tracks
    engine.start("main")
    assert "subs" not in engine.status()


def test_a_forced_start_of_the_main_run_retires_sub_runs_too(proj):
    engine.start("main")
    engine.start("side", inputs={"base": "a"}, run="a")
    engine.start("main", force=True)
    assert state_mod.sub_runs(str(proj)) == []
    assert (_slot(proj, "a") / "archive").is_dir()


def test_a_sub_run_may_be_abandoned_and_archived_on_its_own(proj):
    engine.start("main")
    engine.start("side", inputs={"base": "a"}, run="a")
    engine.abort(run="a")
    ended = [e for e in state_mod.read_journal(str(proj), "s1") if e["event"] == "sub_ended"]
    assert ended and ended[0]["status"] == "aborted"
    engine.archive(run="a")
    assert state_mod.sub_runs(str(proj)) == []
    assert engine.status(run="a")["status"] == "idle"
    assert engine.status()["step_id"] == "work"


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_the_registry_keeps_sub_runs_out_of_known_runs(proj):
    engine.start("main")
    engine.start("side", inputs={"base": "a"}, run="a")
    cwd = state_mod.resolve_cwd(str(proj))
    assert state_mod.known_runs() == [(cwd, "s1")]
    assert state_mod.known_sub_runs() == [(cwd, "s1", "a")]
    engine.archive(run="a")
    assert state_mod.known_sub_runs() == []
    assert state_mod.known_runs() == [(cwd, "s1")]


# --------------------------------------------------------------------------- #
# MCP: run / sub / inputs, and the per-run fence
# --------------------------------------------------------------------------- #
def test_every_run_addressed_tool_takes_run_and_start_takes_sub_and_inputs():
    by_name = {t["name"]: t for t in mcp.TOOLS}
    for name in mcp._RUN_TOOLS:
        assert by_name[name]["inputSchema"]["properties"]["run"]["type"] == "string"
    assert by_name["start"]["inputSchema"]["properties"]["sub"]["type"] == "string"
    assert by_name["start"]["inputSchema"]["properties"]["inputs"]["type"] == "object"
    for name in ("asks", "answer", "request_child_goto"):
        assert "run" not in by_name[name]["inputSchema"].get("properties", {})


def test_mcp_routes_by_run(proj):
    main = mcp.call_tool("start", {"workflow": "main"})
    sub = mcp.call_tool("start", {"workflow": "side", "sub": "a", "inputs": {"base": "x"}})
    assert sub["sub"] == "a"
    assert mcp._seen_run == main["run"]
    assert mcp._seen_subs == {"a": sub["run"]}
    mcp.call_tool("report", {"summary": "derived", "run": "a"})
    moved = mcp.call_tool("next", {"run": "a"})
    assert moved["step_id"] == "inspect" and moved["sub"] == "a"
    assert mcp.call_tool("status", {})["step_id"] == "work"
    assert mcp.call_tool("status", {"run": "a"})["step_id"] == "inspect"
    with pytest.raises(CflowError, match="must be an object"):
        mcp.call_tool("start", {"workflow": "side", "sub": "b", "inputs": "no"})


def test_the_fence_is_per_run(proj):
    mcp.call_tool("start", {"workflow": "main"})
    mcp.call_tool("start", {"workflow": "side", "sub": "a", "inputs": {"base": "x"}})
    # the sub run is replaced under the agent: its own fence trips, the main's does not
    engine.archive(run="a")
    engine.start("side", inputs={"base": "y"}, run="a")
    with pytest.raises(CflowError, match="sub run 'a' you were driving"):
        mcp.call_tool("report", {"summary": "x", "run": "a"})
    mcp.call_tool("report", {"summary": "main still fine"})
    # re-reading the sub run re-arms its fence
    mcp.call_tool("status", {"run": "a"})
    mcp.call_tool("report", {"summary": "now fine", "run": "a"})
    # an emptied sub slot disarms only that fence
    engine.archive(run="a")
    assert mcp.call_tool("status", {"run": "a"})["status"] == "idle"
    assert "a" not in mcp._seen_subs
    assert mcp._seen_run is not None


# --------------------------------------------------------------------------- #
# CLI: --run and sub-done
# --------------------------------------------------------------------------- #
def _ns(**kw):
    base = {"session": None, "run": None, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_sub_done_answers_with_exit_codes(proj, capsys):
    engine.start("main")
    assert cli_cflow._cmd_sub_done(_ns(name="a")) == 2
    engine.start("side", inputs={"base": "x"}, run="a")
    assert cli_cflow._cmd_sub_done(_ns(name="a")) == 1
    engine.report("d", run="a")
    engine.next_step(run="a")
    engine.report("i", run="a")
    engine.next_step(run="a")
    assert cli_cflow._cmd_sub_done(_ns(name="a")) == 0
    out = capsys.readouterr().out
    assert "no such sub run" in out and "done" in out
    engine.archive(run="a")
    assert cli_cflow._cmd_sub_done(_ns(name="a")) == 2
    # a name that cannot be a slot is answered like a missing one: exit 2, said
    assert cli_cflow._cmd_sub_done(_ns(name="../x")) == 2
    assert "invalid cflow sub run name" in capsys.readouterr().out


def test_cli_status_names_the_sub_run_and_lists_them(proj, capsys):
    engine.start("main")
    engine.start("side", inputs={"base": "x"}, run="a")
    assert cli_cflow._cmd_status(_ns()) == 0
    out = capsys.readouterr().out
    assert "sub:      a  side  running (step derive)  (--run a)" in out
    assert "sub run:" not in out
    assert cli_cflow._cmd_status(_ns(run="a")) == 0
    out = capsys.readouterr().out
    assert "sub run:  a" in out
    assert "step:     derive" in out
    assert cli_cflow._cmd_status(_ns(run="a", json=True)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["sub"] == "a" and doc["inputs"] == {"base": "x", "paths": ""}


def test_a_bad_run_name_leaves_no_scope_behind(proj):
    """A refused run name must not leave the pushed scope in place: the
    engine's ops push scope then run, and a raise between the two once
    leaked the scope into every later call of the process."""
    engine.start("main")
    with pytest.raises(state_mod.StateError):
        engine.status(scope="other", run="../x")
    assert state_mod.current_scope() == "s1"
    assert state_mod.current_run() is None
    assert engine.current_run_id(scope="other", run="../x") is None
    assert engine.status()["step_id"] == "work"


def test_cli_run_flag_is_registered_beside_session():
    parser = argparse.ArgumentParser()
    cli_cflow.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["cflow", "status", "--run", "a"])
    assert args.run == "a"
    args = parser.parse_args(["cflow", "sub-done", "a"])
    assert args.name == "a" and args.func is cli_cflow._cmd_sub_done
    assert not hasattr(parser.parse_args(["cflow", "ls"]), "run")


def test_the_main_run_is_what_every_unqualified_call_is_about(proj):
    """No sub run is ever selected by the environment: the session env names
    the scope, and only an explicit run argument selects a side track."""
    engine.start("main")
    engine.start("side", inputs={"base": "x"}, run="a")
    assert os.environ[state_mod.SESSION_ENV] == "s1"
    assert state_mod.current_run() is None
    assert engine.status()["workflow"] == "main"
    assert engine.current_run_id() == engine.status()["run"]
    assert engine.current_run_id(run="a") == engine.status(run="a")["run"]
    assert engine.current_run_id(run="nope") is None
