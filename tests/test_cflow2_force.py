"""A person's override of a v2 run (docs/cflow-v2.md 2.11): ``force_target``
marks, the forced move that rebuilds the position, a forced task outcome,
and archiving a finished run."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import frontend, ir, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

TASKS = """
nodes:
  a: {prompt: A, force_target: true}
  b: {prompt: B}
  c: {prompt: C, force_target: true}
  plain: {prompt: P}
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


def compile_src(src):
    head = TASKS if src.startswith(TASKS) else ""
    return frontend.compile_text("cflow: 2\nname: t\n" + head
                                 + textwrap.dedent(src[len(head):]))


class Drv:
    def __init__(self, tmp_path, src):
        self.cwd = str(tmp_path)
        self.scope = "t"
        self.st = runtime.start(compile_src(src), cwd=self.cwd, scope=self.scope,
                                caps={}, now=T0)

    def kw(self):
        return {"cwd": self.cwd, "scope": self.scope, "now": T0}

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks", [])]

    def do(self, *labels, outcome="ok"):
        for label in labels:
            hit = [t["instance"] for t in self.st["tasks"] if t["label"] == label]
            self.st = runtime.report(hit[0], outcome, None, **self.kw())
        return self.st

    def move(self, target, reason=None):
        self.st = runtime.force_move(target, reason, **self.kw())
        return self.st

    def events(self, name):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope)
                if e["event"] == name]

    def state(self):
        return RunStore(self.cwd, self.scope).load_state()


# -- the mark ------------------------------------------------------------------


def test_mark_compiles_onto_tasks_and_combinators():
    chart = compile_src(TASKS + """
        body:
          seq:
            - a
            - {id: tail, seq: [b], force_target: true}
    """)
    marked = sorted(n.label for n in chart.nodes.values() if n.get(ir.FORCE_TARGET))
    assert marked == ["a", "tail"]


def test_mark_must_be_a_boolean():
    with pytest.raises(frontend.FrontendError, match="force_target must be true or false"):
        compile_src("""
            nodes:
              a: {prompt: A, force_target: yes please}
            body: a
        """)


MARKED = "{id: m, seq: [plain], force_target: true}"


@pytest.mark.parametrize("body, why", [
    ("{par: {branches: {x: %s, y: b}}}" % MARKED, "par"),
    ("{foreach: {in: '$data.xs', do: %s}}" % MARKED, "foreach"),
    ("{try: b, catch: {fail: %s}}" % MARKED, "catch"),
    ("{seq: [b], on: {fail: %s}}" % MARKED, "'on' handler"),
    (MARKED, "root"),
    ("{seq: [plain, {seq: [b, %s], executor: session}]}" % MARKED, "delegated"),
])
def test_a_mapping_mark_a_move_cannot_rebuild_is_a_compile_error(body, why):
    with pytest.raises(frontend.FrontendError, match=why):
        compile_src(TASKS + "data: {xs: [1]}\nbody: " + body + "\n")


@pytest.mark.parametrize("body, why", [
    ("{par: {branches: {x: a, y: b}}}", "par"),
    ("{foreach: {in: '$data.xs', do: a}}", "foreach"),
    ("{try: b, catch: {fail: a}}", "catch"),
    ("{seq: [b], on: {fail: a}}", "'on' handler"),
])
def test_a_task_mark_is_dropped_where_a_move_cannot_rebuild_it(body, why):
    chart = compile_src(TASKS + "data: {xs: [1]}\nbody: {seq: [c, " + body + "]}\n")
    assert any(why in w and "force_target of task 'a'" in w for w in chart.warnings)
    assert not any(n.get(ir.FORCE_TARGET) for n in chart.nodes.values() if n.label == "a")


def test_a_task_mark_stays_on_its_other_uses():
    chart = compile_src(TASKS + "body: {try: {seq: [a, b]}, catch: {fail: a}}\n")
    uses = [bool(n.get(ir.FORCE_TARGET)) for n in chart.nodes.values() if n.label == "a"]
    assert sorted(uses) == [False, True]


def test_the_compiled_mark_is_checked_again_on_load():
    chart = compile_src(TASKS + "body: {seq: [b, {try: plain, catch: {fail: b}}]}\n")
    catch_b = [n for n in chart.nodes.values() if n.label == "b" and n.parent != chart.root]
    catch_b[0].props[ir.FORCE_TARGET] = True  # a hand-edited snapshot
    with pytest.raises(ir.IRError, match="catch"):
        ir.validate(chart)


# -- the move ------------------------------------------------------------------


def test_move_forward_and_back_within_a_seq(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [a, b, c]}\n")
    assert d.tasks() == ["a"]
    d.move("c", "skip b")
    assert d.tasks() == ["c"]
    ev = d.events("forced")[-1]
    assert ev["node"] == "c" and ev["by"] == "user" and ev["reason"] == "skip b"
    assert ev["left"] == ["a"]
    d.move("a")
    assert d.tasks() == ["a"]
    d.do("a")
    assert d.tasks() == ["b"]  # the seq goes on from where the move seated it
    d.do("b", "c")
    assert d.st["status"] == "done"


def test_move_restarts_a_live_target(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [a, b]}\n")
    first = d.st["tasks"][0]["instance"]
    d.move("a")
    assert d.tasks() == ["a"] and d.st["tasks"][0]["instance"] != first
    assert d.state()["visits"]["a"] == 2


def test_move_rebuilds_try_loop_and_cond_and_keeps_the_iteration(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          try:
            loop:
              while: "true"
              max: 3
              do:
                seq:
                  - plain
                  - cond:
                      - {if: "false", then: c}
                      - {else: b}
          catch: {fail: b}
    """)
    assert d.tasks() == ["plain"]
    d.move("c")  # the cond's guard says no; a person's move does not ask it
    assert d.tasks() == ["c"]
    d.do("c")
    assert d.tasks() == ["plain"]  # the loop's body finished: iteration 2
    loops = [i for i in d.state()["inst"].values() if i.get("frame", {}).get("iter")]
    assert loops and loops[0]["frame"]["iter"] == 2


def test_move_enters_a_procedure_with_its_params(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          first: {prompt: F}
        procedures:
          p:
            params: {cmd: {default: "pytest"}}
            nodes:
              run: {prompt: "run ${params.cmd}", force_target: true}
            body: {seq: [run]}
        body:
          seq:
            - first
            - {call: p, with: {cmd: "uv run"}}
    """)
    assert d.tasks() == ["first"]
    target = [n.id for n in RunStore(d.cwd, d.scope).load_chart().nodes.values()
              if n.get(ir.FORCE_TARGET)][0]
    d.move(target)
    assert d.st["tasks"][0]["prompt"] == "run uv run"


def test_move_cancels_what_was_open(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          seq:
            - choose:
                chooser: user
                options: {x: {then: b}}
            - a
    """)
    assert d.st["choices"]
    d.move("a")
    assert not d.st["choices"] and d.tasks() == ["a"]
    assert any(e.get("why") == "forced" for e in d.events("left"))


def test_move_refuses_an_unmarked_or_unknown_node(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [a, plain]}\n")
    with pytest.raises(runtime.FlowError, match="not a force target"):
        d.move("plain")
    with pytest.raises(runtime.FlowError, match="no node 'zzz'"):
        d.move("zzz")


def test_move_refuses_a_finished_run(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [a]}\n")
    d.do("a")
    with pytest.raises(runtime.FlowError, match="done"):
        d.move("a")


# -- a person's report and the archive ---------------------------------------


def test_a_person_forces_a_tasks_outcome(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          try: {seq: [plain, a]}
          catch: {fail: b}
    """)
    inst = d.st["tasks"][0]["instance"]
    d.st = runtime.report(inst, "fail", {"reason": "stuck"}, "forced from the web",
                          by="user", **d.kw())
    assert d.tasks() == ["b"]
    ev = d.events("reported")[-1]
    assert ev["by"] == "user" and ev["outcome"] == "fail"


def test_archive_clears_a_finished_slot_and_refuses_a_live_one(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [a]}\n")
    with pytest.raises(runtime.FlowError, match="abort it"):
        runtime.archive(cwd=d.cwd, scope=d.scope)
    runtime.abort("enough", **d.kw())
    out = runtime.archive(cwd=d.cwd, scope=d.scope)
    assert out["status"] == "idle"
    assert not RunStore(d.cwd, d.scope).exists()


def test_the_packaged_worker_moves_between_its_marked_nodes(tmp_path):
    """improv-worker-v2 marks round, item, land and wrapup (docs 2.11); each
    is reachable from wherever the run stands, through its procedures."""
    from claude_launcher.cflow2 import layers

    chart = layers.load("improv-worker-v2")
    marked = sorted(n.label for n in chart.nodes.values() if n.get(ir.FORCE_TARGET))
    assert marked == ["item", "land", "round", "wrapup"]
    cwd, scope = str(tmp_path), "w"
    st = runtime.start(chart, cwd=cwd, scope=scope, caps={}, now=T0)
    assert [t["label"] for t in st["tasks"]] == ["intake"]
    kw = {"cwd": cwd, "scope": scope, "now": T0}
    st = runtime.force_move("item", "redo the work", **kw)
    assert [t["label"] for t in st["tasks"]] == ["work"]
    st = runtime.force_move("wrapup", **kw)
    assert [t["label"] for t in st["tasks"]] == ["wrapup"]
    st = runtime.force_move("land", **kw)
    assert st["status"] in ("running", "waiting_approval") and st["active"]
    assert [n.rsplit("/", 1)[-1] for n in st["forced"]["left"]] == ["wrapup"]
    st = runtime.force_move("round", **kw)
    assert [t["label"] for t in st["tasks"]] == ["intake"]
    assert len([e for e in runtime.journal(cwd=cwd, scope=scope) if e["event"] == "forced"]) == 4


def test_a_forced_report_is_held_to_the_tasks_returns(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          t: {prompt: T, returns: {ok: {n: int}, blocked: {}}}
        body: {seq: [t]}
    """)
    inst = d.st["tasks"][0]["instance"]
    for outcome, value, why in [("nope", None, "unknown outcome"),
                                ("ok", {"n": "not-int"}, "n"),
                                ("ok", {}, "n")]:
        with pytest.raises(runtime.FlowError, match=why):
            runtime.report(inst, outcome, value, "forced", by="user", **d.kw())
    assert not d.events("reported")
    d.st = runtime.report(inst, "ok", {"n": 3}, "forced", by="user", **d.kw())
    assert d.st["status"] == "done"


def test_a_move_cancels_an_open_delegation(tmp_path):
    cwd, scope = str(tmp_path), "t"
    chart = compile_src(TASKS + "body: {seq: [{id: sub, seq: [b], executor: [subagent]}, a]}\n")
    st = runtime.start(chart, cwd=cwd, scope=scope,
                       caps={"subagent.spawn": True, "mcp.claunch": True}, now=T0)
    assert [g["status"] for g in st["delegations"]] == ["pending"]
    st = runtime.force_move("a", "skip the sub-agent", cwd=cwd, scope=scope, now=T0)
    assert [t["label"] for t in st["tasks"]] == ["a"]
    assert [g["status"] for g in st["delegations"]] == ["cancelled"]
    forced = [e for e in runtime.journal(cwd=cwd, scope=scope) if e["event"] == "forced"]
    assert forced and forced[0]["by"] == "user"
