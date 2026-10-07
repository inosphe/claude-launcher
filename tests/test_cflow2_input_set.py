"""Writing a started run's empty start input (claunch-7d4nt, docs 2.6).

``set_by: [agent]`` lets the run's driver fill one declared input once,
while it is empty, through ``flow_input`` / ``claunch cflow2 input``
(``runtime.set_input``). No node writes ``$data.inputs``: a ``bind`` or
``data:`` key named ``inputs`` is a compile error.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap

import pytest

from claude_launcher.cflow2 import frontend, inputs, layers, mcp as cflow2_mcp, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

DEF = """
inputs:
  issue: {type: string, default: "", set_by: [agent]}
  rounds: {type: int, set_by: [agent]}
  mode: {type: enum, choices: [full, quick], set_by: agent}
  tags: {type: list, set_by: [agent]}
  goal: {type: text, default: ""}
  pr: {type: string, set_by: [agent]}
  no_pr: {type: string, set_by: [agent]}
input_constraints:
  - assert: "$inputs.pr == null or $inputs.no_pr == null"
    message: pr and no_pr exclude each other
nodes:
  first: {prompt: "first ${data.inputs.issue}"}
  second: {prompt: "second ${data.inputs.issue}"}
body: [first, second]
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


def _chart(text: str = DEF):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(text))


def _start(tmp_path, values=None, driver="drv"):
    return runtime.start(_chart(), cwd=str(tmp_path), scope="t", caps={}, driver=driver,
                         inputs=values or {}, now=T0)


def _set(tmp_path, name, value, session="drv"):
    return runtime.set_input(name, value, session=session, cwd=str(tmp_path), scope="t",
                             now=T0)


def _state(tmp_path):
    return RunStore(str(tmp_path), "t").load_state()


def _events(tmp_path, name):
    return [e for e in RunStore(str(tmp_path), "t").journal() if e["event"] == name]


def _prompt(payload, label):
    return next(t["prompt"] for t in payload["tasks"] if t["label"] == label)


# -- declaration -------------------------------------------------------------

def test_set_by_is_kept_in_the_schema():
    sch = _chart().meta["inputs"]
    assert sch["issue"]["set_by"] == ["agent"]
    assert sch["mode"]["set_by"] == ["agent"]  # a bare string is one setter
    assert sch["goal"]["set_by"] == []


@pytest.mark.parametrize("decl,msg", [
    ("{type: string, set_by: [person]}", "unknown set_by 'person'"),
    ("{type: string, set_by: 3}", "set_by must be a list"),
    ("{type: bool, set_by: [agent]}", "set_by applies to types"),
    ("{type: invariants, set_by: [agent]}", "set_by applies to types"),
    ("{type: acceptance, set_by: [agent]}", "set_by applies to types"),
])
def test_bad_set_by_is_a_compile_error(decl, msg):
    with pytest.raises(inputs.InputsError, match=msg):
        inputs.schema({"x": __import__("yaml").safe_load(decl)})


# -- writing -----------------------------------------------------------------

def test_an_empty_settable_input_is_filled(tmp_path):
    _start(tmp_path)
    out = _set(tmp_path, "issue", "claunch-abc")
    assert _state(tmp_path)["data"]["inputs"]["issue"] == "claunch-abc"
    assert out["inputs"]["issue"] == "claunch-abc"  # status shows it
    assert runtime.status(cwd=str(tmp_path), scope="t")["inputs"]["issue"] == "claunch-abc"
    # the active task's prompt and the next task's read the new value
    assert _prompt(out, "first") == "first claunch-abc"
    after = runtime.report("first", cwd=str(tmp_path), scope="t", now=T0)
    assert _prompt(after, "second") == "second claunch-abc"
    ev = _events(tmp_path, "input_set")
    assert len(ev) == 1
    assert {k: ev[0][k] for k in ("name", "value", "by", "was")} == {
        "name": "issue", "value": "claunch-abc", "by": "drv", "was": ""}


def test_values_are_coerced_like_a_start(tmp_path):
    _start(tmp_path)
    _set(tmp_path, "rounds", "4")
    _set(tmp_path, "tags", "a, b")
    data = _state(tmp_path)["data"]["inputs"]
    assert data["rounds"] == 4 and data["tags"] == ["a", "b"]
    ev = _events(tmp_path, "input_set")
    assert ev[0]["was"] is None and ev[1]["was"] == []


def test_a_filled_input_is_refused_with_its_value(tmp_path):
    _start(tmp_path, {"issue": "claunch-board"})  # e.g. the daemon's Board issue
    with pytest.raises(runtime.FlowError, match='already holds "claunch-board"'):
        _set(tmp_path, "issue", "claunch-other")
    _start_fresh = _state(tmp_path)["data"]["inputs"]["issue"]
    assert _start_fresh == "claunch-board"
    assert _events(tmp_path, "input_set") == []


def test_a_second_write_is_refused(tmp_path):
    _start(tmp_path)
    _set(tmp_path, "issue", "claunch-a")
    with pytest.raises(runtime.FlowError, match='already holds "claunch-a"'):
        _set(tmp_path, "issue", "claunch-b")


def test_an_input_without_set_by_is_refused(tmp_path):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="fixed when the run starts"):
        _set(tmp_path, "goal", "new goal")
    assert _state(tmp_path)["data"]["inputs"]["goal"] == ""


@pytest.mark.parametrize("name,value,msg", [
    ("rounds", "many", "is not an integer"),
    ("mode", "slow", "is not one of full, quick"),
    ("issue", "a\nb", "must be one line"),
    ("issue", "  ", "an empty value writes nothing"),
])
def test_type_and_enum_violations_are_refused(tmp_path, name, value, msg):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match=msg):
        _set(tmp_path, name, value)
    assert _events(tmp_path, "input_set") == []


def test_a_constraint_is_checked_with_the_new_value(tmp_path):
    _start(tmp_path, {"pr": "yes"})
    with pytest.raises(runtime.FlowError, match="pr and no_pr exclude each other"):
        _set(tmp_path, "no_pr", "yes")


@pytest.mark.parametrize("name", ["claim", "inputs", "inputs.issue", ""])
def test_nothing_outside_the_declared_inputs_is_written(tmp_path, name):
    _start(tmp_path)
    before = _state(tmp_path)["data"]
    with pytest.raises(runtime.FlowError, match="unknown input"):
        _set(tmp_path, name, "x")
    assert _state(tmp_path)["data"] == before


def test_only_the_driver_writes(tmp_path):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="driven by drv"):
        _set(tmp_path, "issue", "claunch-x", session="someone")
    assert _state(tmp_path)["data"]["inputs"]["issue"] == ""


def test_a_finished_run_is_refused(tmp_path):
    _start(tmp_path)
    runtime.report("first", cwd=str(tmp_path), scope="t", now=T0)
    runtime.report("second", cwd=str(tmp_path), scope="t", now=T0)
    with pytest.raises(runtime.FlowError, match="start a new one"):
        _set(tmp_path, "issue", "claunch-x")


# -- doors -------------------------------------------------------------------

def test_mcp_flow_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "t")
    runtime.start(_chart(), cwd=str(tmp_path), scope="t", caps={}, driver="t")
    out = cflow2_mcp.call_tool("flow_input", {"name": "issue", "value": "claunch-m"})
    assert out["inputs"]["issue"] == "claunch-m"
    assert _events(tmp_path, "input_set")[0]["by"] == "t"
    monkeypatch.setenv("CLAUNCH_SESSION", "someone")
    with pytest.raises(runtime.FlowError, match="driven by t"):
        cflow2_mcp.call_tool("flow_input", {"name": "rounds", "value": 2, "scope": "t"})
    assert "flow_input" in {t["name"] for t in cflow2_mcp.TOOLS}


def _cli(cwd, *args, session=None):
    env = dict(os.environ)
    env.pop("CLAUNCH_SESSION", None)
    if session:
        env["CLAUNCH_SESSION"] = session
    return subprocess.run([sys.executable, "-c", "import sys; from claude_launcher.cli import "
                           "main; sys.exit(main())", "cflow2", *args], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=60)


def test_cli_input(tmp_path):
    _start(tmp_path)
    p = _cli(tmp_path, "input", "issue", "claunch-c", "--scope", "t", session="drv")
    assert p.returncode == 0, p.stderr
    assert "claunch-c" in p.stdout
    p = _cli(tmp_path, "input", "tags", '["x", "y"]', "--json-value", "--scope", "t",
             session="drv")
    assert p.returncode == 0, p.stderr
    assert _state(tmp_path)["data"]["inputs"]["tags"] == ["x", "y"]
    p = _cli(tmp_path, "input", "rounds", "2", "--scope", "t", session="other")
    assert p.returncode == 1 and "driven by drv" in p.stderr
    p = _cli(tmp_path, "input", "issue", "claunch-d", "--scope", "t", session="drv")
    assert p.returncode == 1 and "already holds" in p.stderr


# -- no node writes $data.inputs -----------------------------------------------

def test_bind_inputs_is_a_compile_error():
    with pytest.raises(frontend.FrontendError, match="bind 'inputs' is reserved"):
        _chart("""
            nodes:
              a: {prompt: A}
            body: {id: x, bind: inputs, seq: [a]}
        """)


def test_data_key_inputs_is_a_compile_error():
    with pytest.raises(frontend.FrontendError, match="data key 'inputs' is reserved"):
        _chart("""
            data: {inputs: {issue: x}}
            nodes:
              a: {prompt: A}
            body: a
        """)


def test_no_packaged_definition_binds_into_inputs():
    found = [d for d in layers.list_definitions(None) if d.layer == "package"]
    assert "improv-worker-v2" in [d.name for d in found]
    for d in found:
        assert d.error is None, (d.name, d.error)  # the compiler refuses both forms
        name = d.name
        chart = layers.load(name)
        assert "inputs" not in (chart.data or {}), name
        bound = [n.id for n in chart.nodes.values() if n.bind == "inputs"]
        assert bound == [], (name, bound)


def test_improv_worker_names_the_tool_only_for_the_start_issue():
    chart = layers.load("improv-worker-v2")
    assert chart.meta["inputs"]["issue"]["set_by"] == ["agent"]
    door = re.compile(r"(?<![A-Za-z_])flow_input\b|cflow2 input\b")  # not cflow_inputs
    naming = [n.label or n.id for n in chart.nodes.values()
              if door.search(n.props.get("prompt") or "")]
    assert naming == ["intake-issue"]
    prompt = next(n.props["prompt"] for n in chart.nodes.values()
                  if (n.label or n.id) == "intake-issue")
    at = prompt.index("flow_input")
    window = prompt[max(0, at - 300): at + 400]
    assert "시작" in window and "비어 있고" in window
    assert "data.claim" not in window and "resolve-issue" not in window


# -- set_by with a default that is never empty (claunch-7d4nt.1) --------------

def _input_warnings(decls: str):
    from claude_launcher.cflow2 import check

    chart = _chart(f"inputs:\n{textwrap.indent(textwrap.dedent(decls), '  ')}"
                   "nodes:\n  a: {prompt: A}\nbody: a\n")
    return [(f.level, f.message) for f in check.check(chart) if f.code == "set-by-default"]


def test_set_by_with_a_filled_default_warns():
    got = _input_warnings("""
        rounds: {type: int, default: 3, set_by: [agent]}
        mode: {type: enum, choices: [full, quick], default: full, set_by: [agent]}
    """)
    assert [lvl for lvl, _ in got] == ["warning", "warning"]
    assert "'rounds'" in got[0][1] and "3" in got[0][1]
    assert "'mode'" in got[1][1]


def test_set_by_with_an_empty_default_or_none_is_quiet():
    assert _input_warnings("""
        issue: {type: string, default: "", set_by: [agent]}
        tags: {type: list, default: [], set_by: [agent]}
        n: {type: int, set_by: [agent]}
        fixed: {type: int, default: 3}
    """) == []
    from claude_launcher.cflow2 import check

    for d in layers.list_definitions(None):
        if d.layer == "package" and d.error is None:
            codes = [f.code for f in check.check(layers.load(d.name))]
            assert "set-by-default" not in codes, d.name


def test_set_by_on_a_required_input_warns():
    from claude_launcher.cflow2 import check

    chart = _chart("inputs:\n"
                   "  issue: {type: string, required: true, set_by: [agent]}\n"
                   "  goal: {type: text, required: true}\n"
                   "nodes:\n  a: {prompt: A}\nbody: a\n")
    got = [(f.level, f.code, f.message) for f in check.check(chart)
           if f.code.startswith("set-by-")]
    assert [(lvl, code) for lvl, code, _ in got] == [("warning", "set-by-required")]
    assert "'issue'" in got[0][2]
