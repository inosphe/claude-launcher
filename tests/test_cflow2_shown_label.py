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


def test_the_shown_label_reports_the_node_it_names(tmp_path):
    """Reported by the bare label, the first site's node is the one found
    (by_label returns the first match); the shown label names the other."""
    chart = _chart(TWO_SITES)
    node = chart.by_label("end-decision/pose")
    assert node is not None and node.id == "end-decision/pose"
    assert runtime.shown_label(chart, node) == "end-decision/pose"


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
