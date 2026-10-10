"""The label a reader is shown for a node inlined from a ``call``
(claunch-tm5ki).

A procedure called from several sites puts several nodes under one label;
improv-worker-v2's four ask-user calls all read ``ask-pose``. Such a label is
shown with its call site in front. A label nobody shares, and a site with
no id, are shown as they were.
"""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import frontend, ir, runtime

T0 = 1_000_000.0

PROC = """
procedures:
  confirm:
    params:
      topic: {type: str, required: true}
    nodes:
      pose: {prompt: "confirm ${params.topic}"}
    body: pose
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


def _chart(body: str) -> ir.Chart:
    return frontend.compile_text(
        "cflow: 2\nname: t\n" + textwrap.dedent(body) + PROC, source="flow.yaml")


def _start(tmp_path, body):
    return runtime.start(_chart(body), cwd=str(tmp_path), scope="t", caps={}, now=T0)


def _labels(st):
    return [t["label"] for t in st.get("tasks") or []]


TWO_SITES = """
nodes:
  after: {prompt: after}
body:
  seq:
    - id: start-decision
      call: confirm
      with: {topic: start}
    - id: end-decision
      call: confirm
      with: {topic: end}
    - after
"""


def test_a_shared_label_is_shown_with_its_call_site(tmp_path):
    st = _start(tmp_path, TWO_SITES)
    assert _labels(st) == ["start-decision/pose"]
    st = runtime.report("start-decision/pose", cwd=str(tmp_path), scope="t", now=T0)
    assert _labels(st) == ["end-decision/pose"]
    assert st["tasks"][0]["prompt"] == "confirm end"


def test_a_shown_label_is_accepted_as_a_reference(tmp_path):
    """What status shows is what an agent copies into flow_report."""
    st = _start(tmp_path, BOUND_SITES)
    assert _labels(st) == ["start-decision/pose"]
    st = runtime.report("start-decision/pose", cwd=str(tmp_path), scope="t", now=T0)
    assert _labels(st) == ["end-decision/pose"]


#: Two bound calls of one procedure.
BOUND_SITES = """
nodes:
  after: {prompt: after}
body:
  seq:
    - id: start-decision
      call: confirm
      bind: started
      with: {topic: start}
    - id: end-decision
      call: confirm
      bind: ended
      with: {topic: end}
    - after
"""


def test_the_packaged_worker_tells_its_end_question_apart():
    """The chart the issue was raised on. Its ask-user calls carry ``cases``,
    which wraps each in a generated scope (``settle/end-decision.call/...``),
    so the call site is named through that scope's segment."""
    from claude_launcher.cflow2 import layers

    chart = layers.load("improv-worker-v2")
    end = [n for n in chart.nodes.values()
           if n.label == "ask-pose" and "end-decision" in n.id]
    assert end
    shown = {runtime.shown_label(chart, n) for n in end}
    assert shown == {"settle/end-decision/ask-pose"}
    others = {runtime.shown_label(chart, n) for n in chart.nodes.values()
              if n.label == "ask-pose" and "end-decision" not in n.id}
    assert others and not shown & others


def test_a_label_nobody_shares_is_shown_as_it_is(tmp_path):
    st = _start(tmp_path, """
        nodes:
          after: {prompt: after}
        body:
          seq:
            - id: end-decision
              call: confirm
              with: {topic: end}
            - after
    """)
    assert _labels(st) == ["pose"]


def test_a_site_without_an_id_adds_nothing(tmp_path):
    st = _start(tmp_path, """
        nodes:
          after: {prompt: after}
        body:
          seq:
            - call: confirm
              with: {topic: start}
            - call: confirm
              with: {topic: end}
            - after
    """)
    assert _labels(st) == ["pose"]


def test_the_result_is_still_kept_under_the_label(tmp_path):
    """Only what a reader is shown changes; the procedure's own reads
    (result['pose']) and the node's own label stay as they were."""
    chart = _chart(TWO_SITES)
    node = chart.node("end-decision/pose")
    assert node.label == "pose"
    assert runtime.node_key(node) == "pose"


def test_a_nested_site_shows_only_the_ids_on_its_path(tmp_path):
    """An outer call with no id contributes nothing; the inner sites' ids
    are what tell the two nodes apart."""
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent("""
        nodes:
          after: {prompt: after}
        procedures:
          confirm:
            params:
              topic: {type: str, required: true}
            nodes:
              pose: {prompt: "confirm ${params.topic}"}
            body: pose
          both:
            nodes:
              tail: {prompt: tail}
            body:
              seq:
                - id: start-decision
                  call: confirm
                  with: {topic: start}
                - id: end-decision
                  call: confirm
                  with: {topic: end}
                - tail
        body:
          seq:
            - call: both
            - after
    """), source="flow.yaml")
    st = runtime.start(chart, cwd=str(tmp_path), scope="t", caps={}, now=T0)
    assert _labels(st) == ["start-decision/pose"]
    # the node's id carries the generated outer path; the shown label is
    # still a reference flow_report accepts
    assert st["tasks"][0]["node"] != "start-decision/pose"
    st = runtime.report("start-decision/pose", cwd=str(tmp_path), scope="t", now=T0)
    assert _labels(st) == ["end-decision/pose"]


def test_a_node_id_reference_wins_over_a_shown_label(tmp_path):
    """Both active: one node's id and another node's shown label read the
    same; the id still names its node."""
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent("""
        nodes:
          after: {prompt: after}
        procedures:
          confirm:
            nodes:
              pose: {prompt: "confirm"}
            body: pose
          both:
            body:
              - id: start-decision
                call: confirm
        body:
          seq:
            - par:
                branches:
                  own: {id: start-decision, call: confirm}
                  nested: {call: both}
            - after
    """), source="flow.yaml")
    st = runtime.start(chart, cwd=str(tmp_path), scope="t", caps={}, now=T0)
    nodes = sorted(t["node"] for t in st["tasks"])
    assert "start-decision/pose" in nodes and len(nodes) == 2
    other = next(n for n in nodes if n != "start-decision/pose")
    assert runtime.shown_label(chart, chart.nodes[other]) == "start-decision/pose"
    st = runtime.report("start-decision/pose", cwd=str(tmp_path), scope="t", now=T0)
    assert [t["node"] for t in st["tasks"]] == [other]
