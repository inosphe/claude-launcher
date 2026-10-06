"""Procedure block params and ``splice`` (claunch-yq80p.11, docs 2.2 block params).

A block is a tree passed to a call (``with: {p: <tree>}``) and placed in the
procedure body with ``splice: p``. It is compiled in the call site's scope;
these tests pin each hygiene rule: task names, loop targets, ``$result``,
``$params`` and foreach items, ``return``, error positions, recursion -- and
the one thing that is not lexical: an outcome travels up the structure it was
spliced into, so the procedure's own try catches it."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import check, frontend, ir, runtime

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


def C(src):
    # a test's own text is indented; GUARD (appended after "\n" + ) is not
    head, sep, tail = src.partition("\n" + "\nprocedures:\n  guarded:")
    text = textwrap.dedent(head) + (sep.lstrip("\n") + tail if sep else "")
    return frontend.compile_text("cflow: 2\nname: t\n" + text, source="flow.yaml")


def err(src):
    with pytest.raises(frontend.FrontendError) as exc:
        C(src)
    return str(exc.value)


class Drv:
    def __init__(self, tmp_path, src, params=None):
        self.cwd = str(tmp_path)
        self.st = runtime.start(C(src), cwd=self.cwd, scope="t", params=params, caps={},
                                now=T0)

    def kw(self):
        return {"cwd": self.cwd, "scope": "t", "now": T0}

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks", [])]

    def report(self, label, outcome="ok", value=None):
        iid = next(t["instance"] for t in self.st["tasks"] if t["label"] == label)
        self.st = runtime.report(iid, outcome, value, **self.kw())
        return self.st

    def prompt(self, label):
        return next(t["prompt"] for t in self.st["tasks"] if t["label"] == label)


# A procedure with the skeleton A of the issue: try the block; a failure asks
# whether to try again.
GUARD = """
procedures:
  guarded:
    params:
      attempt: {type: block, required: true}
      note: {type: str, default: inner-note}
    returns: {ok: {}, gave_up: {}}
    nodes:
      recover: {prompt: "recover ${params.note}", returns: {again: {}, stop: {}}}
    body:
      id: tries
      loop:
        max: 3
        while: "true"
        do:
          try:
            seq:
              - splice: attempt
              - return: ok
          catch:
            fail:
              seq: [recover]
              on:
                again: {continue: tries}
                stop: {return: gave_up}
"""


def test_block_tasks_are_the_call_sites(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          work: {prompt: "work ${params.mode}"}
          after: {prompt: after}
        params: {mode: {default: outer}}
        body:
          - call: guarded
            with: {attempt: work, note: n1}
          - after
    """ + "\n" + GUARD)
    assert d.tasks() == ["work"]
    # $params in the block is the run's, not the procedure's
    assert d.prompt("work") == "work outer"
    d.report("work", "fail")
    assert d.tasks() == ["recover"] and d.prompt("recover") == "recover n1"
    d.report("recover", "again")
    assert d.tasks() == ["work"]
    d.report("work")
    assert d.tasks() == ["after"]


def test_node_ids_carry_the_call_prefix_and_the_call_sites_line():
    chart = C("""
        nodes:
          work: {prompt: W}
        body:
          - id: g
            call: guarded
            with: {attempt: work}
    """ + "\n" + GUARD)
    work = chart.by_label("work")
    assert work.id.startswith("g/")
    assert work.src.startswith("flow.yaml:")
    splice = [n for n in chart.nodes.values() if n.get("splice")]
    assert len(splice) == 1 and splice[0].get("splice") == {"block": "attempt", "call": "g"}
    assert [str(f) for f in check.check(chart)] == []


def test_an_unknown_task_in_a_block_is_reported_at_the_call_site():
    msg = err("""
        nodes:
          work: {prompt: W}
        body:
          call: guarded
          with: {attempt: [work, recover]}
    """ + "\n" + GUARD)
    # recover is the procedure's task: not visible to the call site's block
    assert "unknown task 'recover'" in msg and "work" in msg


def test_continue_in_a_block_targets_the_call_sites_loop(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          step: {prompt: S, returns: {ok: {}, skip: {}}}
          tail: {prompt: T}
          done: {prompt: D}
        procedures:
          wrap:
            params: {inner: {type: block}}
            nodes:
              pre: {prompt: P}
            body:
              id: own
              loop:
                max: 5
                while: "true"
                do: [pre, {splice: inner}, {break: own}]
        body:
          - id: outer
            loop:
              max: 2
              while: "true"
              do:
                - call: wrap
                  with:
                    inner:
                      seq: [step]
                      on: {skip: {continue: null}}
                - tail
                - {break: null}
          - done
    """)
    assert d.tasks() == ["pre"]
    d.report("pre")
    d.report("step", "skip")
    # continue left the procedure's own loop and re-entered the outer one
    assert d.tasks() == ["pre"]
    assert len([e for e in runtime.journal(cwd=d.cwd, scope="t")
                if e["event"] == "entered" and e.get("node") == "outer"]) == 1
    d.report("pre")
    d.report("step")
    assert d.tasks() == ["tail"]
    d.report("tail")
    assert d.tasks() == ["done"]


def test_a_block_without_an_enclosing_loop_at_the_call_site_cannot_continue():
    msg = err("""
        nodes:
          step: {prompt: S}
        procedures:
          wrap:
            params: {inner: {type: block}}
            body:
              loop: {max: 2, while: "true", do: {splice: inner}}
        body:
          call: wrap
          with: {inner: [step, {continue: null}]}
    """)
    assert "continue outside any loop" in msg


def test_return_in_a_block_ends_the_call_site_not_the_procedure(tmp_path):
    src = """
        returns: {early: {why: str}}
        nodes:
          step: {prompt: S}
          never: {prompt: N}
        procedures:
          wrap:
            params: {inner: {type: block}}
            returns: {ok: {}}
            nodes:
              post: {prompt: P}
            body: [{splice: inner}, post]
        body:
          - call: wrap
            with: {inner: [step, {return: early, value: {why: "$result.step.outcome"}}]}
          - never
    """
    chart = C(src)
    ret = next(n for n in chart.nodes.values()
               if n.type == ir.T_CONTROL and n.get("op") == ir.CTL_RETURN)
    assert ret.get("target") == ir.RETURN_RUN
    d = Drv(tmp_path, src)
    d.report("step")
    assert d.st["status"] == "done" and d.st["outcome"] == "early"
    assert d.st["value"] == {"why": "ok"}


def test_a_procedure_calling_a_procedure_returns_to_its_own_call(tmp_path):
    """A block written inside procedure outer and spliced into inner: its
    return ends outer's call (outer's scope), not inner's and not the run."""
    d = Drv(tmp_path, """
        nodes:
          last: {prompt: L}
        procedures:
          inner:
            params: {blk: {type: block}}
            nodes:
              in-post: {prompt: IP}
            body: [{splice: blk}, in-post]
          outer:
            returns: {ok: {}, cut: {}}
            nodes:
              o1: {prompt: O1}
              o-post: {prompt: OP}
            body:
              - call: inner
                with: {blk: [o1, {return: cut}]}
              - o-post
        body:
          - call: outer
            on: {cut: last}
    """)
    assert d.tasks() == ["o1"]
    d.report("o1")
    assert d.tasks() == ["last"]


def test_outcomes_travel_through_the_procedures_structure(tmp_path):
    """Not lexical: a block's fail is an outcome at the splice position, so the
    procedure's try catches it (the point of the skeleton)."""
    d = Drv(tmp_path, """
        nodes:
          work: {prompt: W}
        body:
          call: guarded
          with: {attempt: work}
    """ + "\n" + GUARD)
    d.report("work", "fail")
    assert d.tasks() == ["recover"]
    d.report("recover", "stop")
    # the procedure returned gave_up; the run does not declare it, so it fails
    assert d.st["status"] == "failed" and d.st["outcome"] == "gave_up"


def test_a_foreach_item_at_the_call_site_is_visible_in_the_block(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          one: {prompt: "item ${item}"}
        procedures:
          shadow:
            params: {blk: {type: block}}
            body:
              foreach:
                in: "['inner']"
                as: item
                do: {splice: blk}
        body:
          foreach:
            in: "['outer']"
            as: item
            do:
              call: shadow
              with: {blk: one}
    """)
    assert d.tasks() == ["one"] and d.prompt("one") == "item outer"


def test_block_param_errors():
    assert "missing block parameter 'attempt'" in err("""
        nodes:
          work: {prompt: W}
        body: {call: guarded}
    """ + "\n" + GUARD)
    assert "not a block param" in err("""
        nodes:
          w: {prompt: W}
        procedures:
          p:
            params: {x: {type: str}}
            body: {splice: x}
        body: {call: p, with: {x: w}}
    """)
    assert "type block is a procedure param" in err("""
        params: {b: {type: block}}
        nodes:
          w: {prompt: W}
        body: w
    """)
    assert "unknown parameter" in err("""
        nodes:
          w: {prompt: W}
        body: {call: guarded, with: {attempt: w, nope: 1}}
    """ + "\n" + GUARD)


def test_a_block_may_not_call_the_procedure_it_is_spliced_into():
    msg = err("""
        nodes:
          w: {prompt: W}
        procedures:
          p:
            params: {b: {type: block}}
            body: {splice: b}
        body:
          call: p
          with: {b: {call: p, with: {b: w}}}
    """)
    assert "recursive call: p -> p" in msg


def test_a_default_block_is_the_procedures_own_text(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          w: {prompt: W}
        procedures:
          p:
            params: {b: {type: block, default: own}}
            nodes:
              own: {prompt: own}
            body: {splice: b}
        body: [{call: p}, {call: p, with: {b: w}}]
    """)
    assert d.tasks() == ["own"]
    d.report("own")
    assert d.tasks() == ["w"]


def test_a_block_is_passed_on_through_a_second_call(tmp_path):
    d = Drv(tmp_path, """
        params: {who: {default: run}}
        nodes:
          w: {prompt: "w ${params.who}"}
        procedures:
          inner:
            params: {b: {type: block}, who: {type: str, default: inner}}
            body: {splice: b}
          outer:
            params: {b: {type: block}, who: {type: str, default: outer}}
            body:
              call: inner
              with: {b: {splice: b}}
        body:
          call: outer
          with: {b: w}
    """)
    assert d.tasks() == ["w"] and d.prompt("w") == "w run"


# -- {lit:} / {expr:} --------------------------------------------------------------


def test_lit_and_expr_forms(tmp_path):
    chart = C("""
        nodes:
          w: {prompt: W}
        procedures:
          p:
            params: {a: {type: str}, b: {type: str}, c: {type: str}}
            nodes:
              show: {prompt: "${params.a}|${params.b}|${params.c}"}
            body: show
        body:
          call: p
          with:
            a: {lit: "cost $5"}
            b: {expr: "'xy'"}
            c: "$params.zz"
    """)
    scope = next(n for n in chart.nodes.values() if n.type == ir.T_SCOPE)
    assert scope.get("with_")["a"] == {"lit": "cost $5"}
    assert scope.get("with_")["b"] == {"expr": "'xy'"}
    d = runtime.start(chart, cwd=str(tmp_path), scope="t", params={"zz": "z"}, caps={}, now=T0)
    assert d["tasks"][0]["prompt"] == "cost $5|xy|z"


# -- optional return fields ---------------------------------------------------------


def test_a_return_fills_left_out_optional_fields_with_null(tmp_path):
    d = Drv(tmp_path, """
        returns: {ok: {how: str, remote: "str?", repo: "str?"}}
        nodes:
          w: {prompt: W}
        procedures:
          p:
            returns: {ok: {how: str, base: "str?", pr: "int?"}}
            body: {return: ok, value: {how: local}}
        body:
          - call: p
            bind: got
          - w
          - {return: ok, value: {how: "$data.got.how", repo: r}}
    """)
    assert d.tasks() == ["w"]
    assert runtime.RunStore(d.cwd, "t").load_state()["data"]["got"] == {
        "how": "local", "base": None, "pr": None}
    d.report("w")
    assert d.st["value"] == {"how": "local", "remote": None, "repo": "r"}


# -- call cases (docs 2.10) -------------------------------------------------------

PICK = """
procedures:
  pick:
    params:
      choices: {type: list, required: true}
      topic: {type: str, default: ""}
    returns:
      ok: {decision: str}
      reframe: {reason: str}
    nodes:
      decide: {prompt: "pick one of ${params.choices}", returns: {ok: {decision: str}, reframe: {reason: str}}}
    body:
      waterfall:
        - decide
        - {return: ok, value: {decision: "$prev.decision"}}
      on:
        reframe: {return: reframe, value: {reason: "$result.decide.value.reason"}}
"""


def _cases(body):
    return textwrap.dedent(body) + "\n" + PICK


def test_cases_build_the_choices_and_branch(tmp_path):
    src = _cases("""
        nodes:
          go-local: {prompt: L}
          go-pr: {prompt: P}
          after: {prompt: A}
        body:
          - call: pick
            with: {topic: medium}
            bind: chosen
            cases:
              local: go-local
              pr: [go-pr]
              none: []
          - after
    """)
    d = Drv(tmp_path, src)
    assert "['local', 'pr', 'none']" in d.prompt("decide") or '["local", "pr", "none"]' in d.prompt("decide")
    d.report("decide", "ok", {"decision": "pr"})
    assert d.tasks() == ["go-pr"]
    assert runtime.RunStore(d.cwd, "t").load_state()["data"]["chosen"]["decision"] == "pr"
    d.report("go-pr")
    assert d.tasks() == ["after"]


def test_a_choice_with_an_empty_tree_does_nothing(tmp_path):
    d = Drv(tmp_path, _cases("""
        nodes:
          x: {prompt: X}
          after: {prompt: A}
        body:
          - call: pick
            cases: {go: x, skip: []}
          - after
    """))
    d.report("decide", "ok", {"decision": "skip"})
    assert d.tasks() == ["after"]


def test_on_stays_the_outcome_handler(tmp_path):
    d = Drv(tmp_path, _cases("""
        nodes:
          x: {prompt: X}
          again: {prompt: AG}
        body:
          call: pick
          cases: {go: x}
          on: {reframe: again}
    """))
    d.report("decide", "reframe", {"reason": "wrong frame"})
    assert d.tasks() == ["again"]


def test_cases_trees_compile_in_the_call_sites_scope(tmp_path):
    d = Drv(tmp_path, _cases("""
        nodes:
          x: {prompt: X}
          done: {prompt: D}
        body:
          - id: rounds
            loop:
              max: 3
              while: "true"
              do:
                call: pick
                cases:
                  more: {continue: rounds}
                  stop: {break: rounds}
          - done
    """))
    d.report("decide", "ok", {"decision": "more"})
    assert d.tasks() == ["decide"]
    d.report("decide", "ok", {"decision": "stop"})
    assert d.tasks() == ["done"]


def test_cases_errors():
    base = """
        nodes:
          x: {prompt: X}
        body:
          call: pick
    """
    assert "with.choices and cases together" in err(_cases(base + """
          with: {choices: [a]}
          cases: {a: x}
    """))
    assert "every choice needs a tree" in err(_cases(base + """
          cases: {a: x, b: null}
    """))
    assert "duplicate choice" in err(_cases(base + """
          cases: {a: x, A: x}
    """))
    assert "declares a 'choices' param" in err(textwrap.dedent("""
        nodes:
          x: {prompt: X}
        procedures:
          plain:
            params: {topic: {type: str}}
            body: x
        body:
          call: plain
          cases: {a: x}
    """))
