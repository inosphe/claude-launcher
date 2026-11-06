"""already-merged: a round whose commits are already in the target lands
without a person.

claunch-yzwut.2 (user instruction, thread th-e4c394). When a round's commits
reached the integration target by another route (a person merged the PR,
another branch carried them), ``land``'s ``landing`` sees delta 0 and
escalates, and the question ends in front of a person -- who picks
``merge-by-person`` or ``hold`` every time. ``already-merged`` sits right
before ``landing`` and lets an agent decide that one case: the driver, or its
parent worker through ``flow_select`` on the child's scope. Every other gate
stays a person's.

The drive below runs the real ``already-merged`` node from the packaged file
inside the smallest ``land`` that has its shape. ``confirm-landed`` (a daemon
probe in the real file) and ``landing`` (whose own behaviour other modules
pin) are stand-ins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

import claude_launcher
from claude_launcher.cflow2 import frontend, layers, runtime

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"


def _doc() -> Dict[str, Any]:
    return yaml.safe_load(V2.read_text(encoding="utf-8"))


def _land_body(doc: Dict[str, Any]) -> List[Any]:
    return doc["procedures"]["land"]["body"]


def _node(body: List[Any], node_id: str) -> Dict[str, Any]:
    for item in body:
        if isinstance(item, dict) and item.get("id") == node_id:
            return item
    raise AssertionError(f"land lost {node_id}")


def _stub(title: str, returns: Dict[str, Any]) -> Dict[str, Any]:
    return {"title": title, "prompt": title, "done_when": title,
            "returns": returns, "effects": []}


def _chart(tmp_path):
    doc = _doc()
    gate = _node(_land_body(doc), "already-merged")
    nodes = {
        "branch-setup": _stub("branch setup", {"ok": {"base_hash": "str"}}),
        "landed": _stub("landed", {"ok": {"tip": "str", "merge": "str", "target": "str"}}),
        "landing-stand-in": _stub("landing", {"ok": {}}),
    }
    flow = {
        "cflow": 2, "name": "already-merged-drive",
        "data": {"landing": {"medium": "pr", "remote": "origin", "repo": "h/o/r",
                             "base": "master", "pr_url": None, "pr_number": None}},
        "procedures": {
            "confirm-landed": {"body": ["landed"]},
            "land": {
                "returns": doc["procedures"]["land"]["returns"],
                "body": [gate, "landing-stand-in",
                         {"return": "blocked", "value": {"reason": "went on to landing"}}],
            },
        },
        "nodes": nodes,
        "body": ["branch-setup", {"id": "land-call", "call": "land", "bind": "land_out"}],
    }
    return frontend.compile_text(yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
                                 resolve_include=layers.resolver(str(tmp_path)))


def _drive(tmp_path, monkeypatch, option: str, *, parent: bool = False):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "m", "now": 1.0}
    st = runtime.start(_chart(tmp_path), caps={}, driver="w1",
                       driver_parent="p1" if parent else "", **kw)
    path: List[str] = []
    for _ in range(20):
        if st.get("choices"):
            choice = st["choices"][0]
            path.append(choice["label"])
            if parent:
                st = runtime.select(choice["instance"], option, "measured", by="p1",
                                    caller=runtime.CALLER_PARENT, **kw)
            else:
                st = runtime.select(choice["instance"], option, "measured", **kw)
            continue
        if not st.get("tasks"):
            break
        task = st["tasks"][0]
        label = task["label"].rsplit("/", 1)[-1]
        path.append(label)
        value: Dict[str, Any] = {}
        if label == "branch-setup":
            value = {"base_hash": "b0"}
        elif label == "landed":
            value = {"tip": "b1", "merge": "m1", "target": "origin/master"}
        st = runtime.report(task["instance"], "ok", value, **kw)
    return path, runtime.status(**kw)


def test_an_already_merged_round_lands_without_any_person_question(tmp_path, monkeypatch):
    path, st = _drive(tmp_path, monkeypatch, "landed")
    assert path == ["branch-setup", "already-merged", "landed"], path
    # a non-ok outcome at the top ends the run "failed" and keeps the outcome
    assert st["status"] in ("done", "failed"), st
    assert st["outcome"] == "landed"
    assert st["value"]["merge"] == "m1"
    assert not st.get("asks")


def test_a_not_merged_round_goes_on_to_the_landing_it_had(tmp_path, monkeypatch):
    path, st = _drive(tmp_path, monkeypatch, "not-landed")
    assert path == ["branch-setup", "already-merged", "landing-stand-in"], path
    assert st["outcome"] == "blocked"
    assert st["value"]["reason"] == "went on to landing"


def test_the_parent_worker_may_pick_it(tmp_path, monkeypatch):
    path, st = _drive(tmp_path, monkeypatch, "landed", parent=True)
    assert path == ["branch-setup", "already-merged", "landed"], path
    assert st["outcome"] == "landed"


def test_the_gate_is_an_agent_choice_with_two_answers():
    gate = _node(_land_body(_doc()), "already-merged")["choose"]
    assert gate.get("chooser", "agent") == "agent"
    assert gate["require_reason"] is True
    assert set(gate["options"]) == {"landed", "not-landed"}


def test_it_stands_right_before_landing():
    ids = [item.get("id") for item in _land_body(_doc()) if isinstance(item, dict)]
    assert ids.index("already-merged") + 1 == ids.index("landing"), ids


def test_landed_goes_through_the_same_machine_check_as_already_landed():
    doc = _doc()
    gate = _node(_land_body(doc), "already-merged")["choose"]["options"]["landed"]["then"]
    assert gate[0] == {"call": "confirm-landed"}
    assert gate[1]["return"] == "landed"
    probe = doc["procedures"]["confirm-landed"]["body"]["par"]["branches"]["probe"]
    assert "claunch gate landed-check" in probe["wait"]["probe"]


@pytest.mark.parametrize("needle", [
    # the same check confirm-landed's probe makes
    "claunch gate landed-check --target",
    # a round with no commits of its own is an analysis round, not a landing
    "${result['branch-setup'].value.base_hash}..HEAD",
    # the target is merge-ready's
    "merge-ready",
    # a parent may answer
    "flow_select",
])
def test_the_prompt_names_how_to_judge(needle):
    prompt = _node(_land_body(_doc()), "already-merged")["choose"]["prompt"]
    assert needle in prompt


def test_the_person_gates_stay_a_persons():
    doc = _doc()
    nodes = doc["nodes"]
    assert nodes["landing-review"]["ask"]["otherwise"] == "human"
    assert nodes["landing-review-leader"]["ask"]["otherwise"] == "human"
    topic = doc["procedures"]["landing-target-gate"]
    assert "merge-by-person" in yaml.safe_dump(topic, allow_unicode=True)
