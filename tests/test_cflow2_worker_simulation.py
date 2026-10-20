"""improv-worker-v2: the ``simulation`` start input (claunch-qrh0a).

off (default) leaves every round as it was. advisory and required run the
sim-run procedure once per full round, after the landing medium and before
coding (sim-check, user decision qa-d0605072). required also holds the
landing procedure until the simulation has nothing open (sim-gate, user
decision qa-008521b4).

The guards under test are the real ones: the ``sim-precheck`` and
``sim-gate`` blocks are lifted from worker-round into a small body whose
neighbours (work, land) are stubs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import claude_launcher
from claude_launcher.cflow2 import frontend, inputs, layers, runtime

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"

CLEAR = {"sim_id": "sim-1", "run_seq": 1, "assessment": "consistent",
         "findings": "clear", "open": {}, "limits": []}
GAPS = {**CLEAR, "assessment": "gaps", "findings": "gaps",
        "open": {"questions": 2, "proposals": 0, "ambiguous": 0}}
UNAVAILABLE = {"assessment": "unavailable", "findings": "unavailable",
               "limits": ["no-profile"]}


def _doc():
    return yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)


def _find(obj, block_id):
    if isinstance(obj, dict):
        if obj.get("id") == block_id:
            return obj
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            hit = _find(v, block_id)
            if hit is not None:
                return hit
    return None


def _run(tmp_path, monkeypatch, values, mode="full"):
    """sim-precheck -> work -> batch{try: [sim-gate, land], catch rework}."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = _doc()
    rnd = doc["procedures"]["worker-round"]
    pre, gate = _find(rnd, "sim-precheck"), _find(rnd, "sim-gate")
    doc["procedures"] = {}
    doc.pop("spec", None)
    doc.pop("sides", None)
    doc["params"]["mode"] = {"type": "str", "default": mode}
    doc["nodes"] = {k: doc["nodes"][k] for k in ("sim-check", "sim-recheck")}
    doc["nodes"]["work"] = {"prompt": "work"}
    doc["nodes"]["land"] = {"prompt": "land"}
    doc["body"] = [pre, "work", {"id": "batch", "loop": {
        "while": "true", "max": 3,
        "do": {"try": [gate, "land", {"break": "batch"}],
               "catch": {"rework": {"continue": "batch"}}}}}]
    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    chart = inputs.apply(chart, values)
    kw = {"cwd": str(tmp_path), "scope": "sim", "now": 1.0}
    return runtime.start(chart, caps={}, **kw), kw


def _one(st, label):
    [task] = st["tasks"]
    assert task["label"] == label, (task["label"], label)
    return task["instance"]


def _report(st, kw, label, outcome="ok", value=None):
    return runtime.report(_one(st, label), outcome, value or {}, **kw)


# -- declaration ------------------------------------------------------------


def test_simulation_is_an_off_by_default_enum_a_person_can_change():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    decl = doc["inputs"]["simulation"]
    assert decl["type"] == "enum"
    # "off" must stay a string: unquoted it is YAML 1.1's boolean false
    assert decl["choices"] == ["off", "advisory", "required"]
    assert decl["default"] == "off"
    assert decl["set_by"] == ["user"]
    assert not decl.get("required")


def test_both_tasks_may_call_only_sim_run_and_are_not_sims_deny():
    nodes = _doc()["nodes"]
    for name in ("sim-check", "sim-recheck"):
        assert nodes[name]["calls"] == ["sim-run"], name
        assert nodes[name].get("sims") != "deny", name
    assert set(nodes["sim-recheck"]["returns"]) == {"ok", "rework"}


def test_precheck_follows_the_medium_and_gate_precedes_land_inside_the_rework_try():
    rnd = _doc()["procedures"]["worker-round"]
    full = _find(rnd, "medium")
    seq = next(s for s in _walk_lists(rnd) if full in s)
    assert seq[seq.index(full) + 1]["id"] == "sim-precheck"
    batch = _find(rnd, "batch")["loop"]["do"]
    ids = [b.get("id") if isinstance(b, dict) else b for b in batch["try"]]
    assert ids.index("sim-gate") + 1 == ids.index("land")
    assert batch["catch"]["rework"] == {"continue": "batch"}


def _walk_lists(obj):
    if isinstance(obj, list):
        yield obj
        for v in obj:
            yield from _walk_lists(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_lists(v)


# -- off ----------------------------------------------------------------------


@pytest.mark.parametrize("values", [{}, {"simulation": "off"}])
def test_off_keeps_todays_round(tmp_path, monkeypatch, values):
    st, kw = _run(tmp_path, monkeypatch, values)
    st = _report(st, kw, "work")
    st = _report(st, kw, "land")
    assert st["status"] == "done"


# -- advisory -------------------------------------------------------------------


def test_advisory_runs_the_check_and_lands_with_gaps_open(tmp_path, monkeypatch):
    st, kw = _run(tmp_path, monkeypatch, {"simulation": "advisory"})
    st = _report(st, kw, "sim-check", value=GAPS)
    assert st["data"]["sim"]["findings"] == "gaps"
    st = _report(st, kw, "work")
    st = _report(st, kw, "land")
    assert st["status"] == "done"


def test_an_unavailable_check_does_not_stop_an_advisory_round(tmp_path, monkeypatch):
    st, kw = _run(tmp_path, monkeypatch, {"simulation": "advisory"})
    st = _report(st, kw, "sim-check", value=UNAVAILABLE)
    st = _report(st, kw, "work")
    st = _report(st, kw, "land")
    assert st["status"] == "done"


# -- required -------------------------------------------------------------------


def test_required_and_clear_lands_without_a_recheck(tmp_path, monkeypatch):
    st, kw = _run(tmp_path, monkeypatch, {"simulation": "required"})
    st = _report(st, kw, "sim-check", value=CLEAR)
    st = _report(st, kw, "work")
    st = _report(st, kw, "land")
    assert st["status"] == "done"


@pytest.mark.parametrize("first", [GAPS, UNAVAILABLE])
def test_required_holds_land_until_the_recheck_is_clear(tmp_path, monkeypatch, first):
    st, kw = _run(tmp_path, monkeypatch, {"simulation": "required"})
    st = _report(st, kw, "sim-check", value=first)
    st = _report(st, kw, "work")
    # an ok that is still not clear does not open the gate
    st = _report(st, kw, "sim-recheck", value={**GAPS, "run_seq": 2})
    st = _report(st, kw, "sim-recheck", value={**CLEAR, "run_seq": 3})
    assert st["data"]["sim"]["run_seq"] == 3
    st = _report(st, kw, "land")
    assert st["status"] == "done"


def test_required_rework_goes_back_through_the_batch_catch(tmp_path, monkeypatch):
    st, kw = _run(tmp_path, monkeypatch, {"simulation": "required"})
    st = _report(st, kw, "sim-check", value=GAPS)
    st = _report(st, kw, "work")
    st = _report(st, kw, "sim-recheck", "rework", {"reason": "rule R2 not implemented"})
    # the catch continued the batch: the gate stands again before land
    st = _report(st, kw, "sim-recheck", value=CLEAR)
    st = _report(st, kw, "land")
    assert st["status"] == "done"


def test_the_gate_does_not_hold_a_stack_only_round(tmp_path, monkeypatch):
    st, kw = _run(tmp_path, monkeypatch, {"simulation": "required"}, mode="stack-only")
    st = _report(st, kw, "sim-check", value=GAPS)
    st = _report(st, kw, "work")
    st = _report(st, kw, "land")
    assert st["status"] == "done"
