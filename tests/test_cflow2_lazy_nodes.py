"""A chart read back from JSON builds its nodes on lookup (ir.LazyNodes).

The daemon's clock re-reads every live run's chart on each pass and looks up
only the active instances' nodes; building all ~800 nodes of improv-worker-v2
on every read was most of the cost (2026-10-26).
"""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import frontend, ir, runtime

CHART = """
cflow: 2
name: lazy
procedures:
  confirm:
    nodes:
      pose: {prompt: confirm}
    body: pose
nodes:
  a: {prompt: a}
  b: {prompt: b}
body:
  seq:
    - id: first
      call: confirm
    - id: second
      call: confirm
    - a
    - b
"""


@pytest.fixture
def compiled() -> ir.Chart:
    return frontend.compile_text(textwrap.dedent(CHART), source="flow.yaml")


@pytest.fixture
def built(monkeypatch) -> list:
    calls: list = []
    original = ir.Node.from_dict.__func__
    monkeypatch.setattr(ir.Node, "from_dict",
                        classmethod(lambda cls, raw: calls.append(raw["id"]) or original(cls, raw)))
    return calls


def test_a_read_builds_no_node_until_one_is_looked_up(compiled, built):
    chart = ir.Chart.from_json(compiled.to_json())
    assert built == []
    nid = next(iter(compiled.nodes))
    assert chart.node(nid) == compiled.node(nid)
    assert chart.node(nid) is chart.node(nid)
    assert built == [nid]


def test_a_read_chart_equals_the_compiled_one(compiled):
    chart = ir.Chart.from_json(compiled.to_json())
    assert list(chart.nodes) == list(compiled.nodes)
    assert len(chart.nodes) == len(compiled.nodes)
    assert chart == compiled
    assert chart.to_json() == compiled.to_json()


def test_labels_do_not_build_nodes(compiled, built):
    chart = ir.Chart.from_json(compiled.to_json())
    assert list(chart.labels()) == list(compiled.labels())
    assert built == []


def test_a_shared_label_is_shown_from_a_read_chart_without_building_the_rest(compiled, built):
    pose = next(n for n in compiled.nodes.values() if n.label == "pose")
    expected = runtime.shown_label(compiled, pose)
    assert expected != "pose"  # two call sites share it
    text = compiled.to_json()
    built.clear()
    chart = ir.Chart.from_json(text)
    assert runtime.shown_label(chart, chart.node(pose.id)) == expected
    assert len(built) < len(chart.nodes)


def test_assignment_and_deletion_keep_the_order_of_a_dict(compiled):
    chart = ir.Chart.from_json(compiled.to_json())
    ids = list(compiled.nodes)
    replaced = ir.Node(id=ids[0], type=ir.T_TASK, label="replaced")
    chart.nodes[ids[0]] = replaced
    chart.nodes["extra"] = ir.Node(id="extra", type=ir.T_TASK)
    del chart.nodes[ids[1]]
    assert list(chart.nodes) == [ids[0]] + ids[2:] + ["extra"]
    assert chart.node(ids[0]) is replaced
    assert ids[1] not in chart.nodes
    assert next(chart.labels()) == "replaced"


@pytest.mark.parametrize("damage, error", [
    (lambda n: n.pop("id"), KeyError),
    (lambda n: n.pop("type"), KeyError),
])
def test_a_node_without_its_shape_still_fails_the_read(compiled, damage, error):
    raw = compiled.to_dict()
    damage(next(iter(raw["nodes"].values())))
    with pytest.raises(error):
        ir.Chart.from_dict(raw)


def test_a_node_that_is_not_a_mapping_fails_the_read(compiled):
    raw = compiled.to_dict()
    raw["nodes"][next(iter(raw["nodes"]))] = ["not", "a", "node"]
    with pytest.raises(ir.IRError):
        ir.Chart.from_dict(raw)
