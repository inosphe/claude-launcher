"""The pre-coding consultation stage in ``improv-worker-v2`` (claunch-xpnae).

s6383 carried a user's question through the round's items and into the coding
approval gate; the person rejected that gate and forced the run to wrapup.
The round now judges the request right after the issue is claimed:
``consult-check`` is the agent's own gate, and ``consult`` stands no person in
the way — it opens ``pre-coding-consult-hold``, which answers the question,
creates no branch, changes nothing in the repository, and keeps the task open
until the user's next message. A work instruction reported from the hold
continues the round into the branch setup and the coding gate.

The runtime tests run the real node from the canon with its surroundings
stubbed, the pattern ``tests/test_worker_v2_feasibility_channel_gates.py``
uses for ``feasibility`` and the landing channel.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

import claude_launcher
from claude_launcher.cflow2 import frontend, layers, runtime

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"


def _doc() -> dict:
    return yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)


def _find(tree: Any, step_id: str) -> Any:
    """The mapping under ``tree`` whose ``id`` is ``step_id`` (depth first)."""
    if isinstance(tree, dict):
        if tree.get("id") == step_id:
            return tree
        tree = list(tree.values())
    if isinstance(tree, list):
        for sub in tree:
            got = _find(sub, step_id)
            if got is not None:
                return got
    return None


def _labels(st, key="tasks"):
    return [x["label"].split("/")[-1] for x in st[key]]


@pytest.fixture
def kw(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    return {"cwd": str(tmp_path), "scope": "consult"}


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #
def test_the_consult_stage_stands_after_the_claim_and_before_the_branch():
    """After resolve-issue (the issue is known) and before workspace, branch
    setup, items and land — so a consultation creates no branch and no code."""
    body = _doc()["procedures"]["worker-round"]["body"]
    steps = body[0]["cond"][1]["else"]
    ids = [s.get("id") if isinstance(s, dict) else s for s in steps]
    assert ids.index("pre-coding-consult") == ids.index("resolve") + 1
    assert ids.index("pre-coding-consult") < ids.index("workspace")
    assert ids.index("pre-coding-consult") < ids.index("branch-setup")
    # the batch (feasibility, items, land) is the round's next statement
    assert body[1]["id"] == "batch"
    batch = [s.get("id") if isinstance(s, dict) else s for s in body[1]["loop"]["do"]["try"]]
    assert "feasibility" in batch and "items-pass" in batch and "land" in batch


def test_the_judgement_is_the_agents_with_a_reason():
    chart = layers.load("improv-worker-v2")
    [node] = [n for n in chart.nodes.values() if n.label == "consult-check"]
    assert node.type == "choose" and node.get("chooser") == "agent"
    assert node.get("require_reason") is True
    assert [o["name"] for o in node.get("options")] == ["work", "consult"]


def test_consult_stands_no_person_in_the_way():
    """A consult is a hold, not a question: no ask anywhere in the branch."""
    doc = _doc()
    for name in ("consult-check", "pre-coding-consult-hold"):
        assert "ask" not in doc["nodes"].get(name, {})
    options = _find(doc["procedures"]["worker-round"]["body"], "consult-check")["choose"]["options"]
    assert options["consult"]["then"] == ["pre-coding-consult-hold"]


def test_the_hold_task_moves_nothing_in_the_repository():
    hold = _doc()["nodes"]["pre-coding-consult-hold"]
    chart = layers.load("improv-worker-v2")
    [node] = [n for n in chart.nodes.values() if n.label == "pre-coding-consult-hold"]
    assert node.type == "task"
    effects = hold.get("effects") or []
    assert "fs.write" not in effects
    assert not [e for e in effects if e.startswith("git")]
    assert list(hold["returns"]["ok"]) == ["work"]


def test_the_judgement_names_its_evidence_and_the_hold():
    prompt = " ".join(_find(_doc()["procedures"]["worker-round"]["body"],
                            "consult-check")["choose"]["prompt"].split())
    for anchor in ("claunch beads show", "claunch beads comments", "판단이 서지 않으면"):
        assert anchor in prompt
    assert "**consult**" in prompt and "**work**" in prompt
    hold = " ".join(_doc()["nodes"]["pre-coding-consult-hold"]["prompt"].split())
    # the hold itself: finish the turn without reporting, wake on the next message
    assert "보고하지 않고 턴을 끝낸다" in hold
    assert "사용자의 다음 말이 이 작업을 다시 깨운다" in hold
    # nothing in the repository moves while it waits
    assert "코드·테스트·문서·브랜치는 이 단계에서 바꾸지 않았으므로" in hold
    # the clock's re-nudge is not a reason to answer again
    assert "사용자의 새 말이 없으면 답을 다시 쓰지 않고" in hold
    # the release path continues into the round's coding steps
    assert "코딩 승인 게이트로 이어진다" in hold


# --------------------------------------------------------------------------- #
# the stage at runtime
# --------------------------------------------------------------------------- #
def _consult_run(kw, mode="full"):
    doc = _doc()
    stage = _find(doc["procedures"]["worker-round"]["body"], "pre-coding-consult")
    flow = {
        "cflow": 2, "name": "pre-coding-consult-run",
        "data": {"claim": {"issue": "X-1", "how": "claimed"}},
        "nodes": {
            "pre-coding-consult-hold": doc["nodes"]["pre-coding-consult-hold"],
            "after": {"prompt": "after"},
        },
        "procedures": {
            "worker-round": {
                "params": {"mode": {"type": "str", "default": mode}},
                "body": [[stage, "after"]],
            },
        },
        "body": [{"call": "worker-round"}],
    }
    chart = frontend.compile_text(json.dumps(flow, ensure_ascii=False))
    return runtime.start(chart, caps={}, **kw)


def test_work_goes_on_without_a_person(kw):
    st = _consult_run(kw)
    assert _labels(st, "choices") == ["consult-check"]
    with pytest.raises(runtime.FlowError, match="requires a reason"):
        runtime.select(st["choices"][0]["instance"], "work", **kw)
    st = runtime.select(st["choices"][0]["instance"], "work",
                        reason="사용자가 파일 수정을 지시했다", **kw)
    assert _labels(st) == ["after"] and not st.get("asks")


def test_consult_holds_without_asking_and_without_the_next_step(kw):
    st = _consult_run(kw)
    st = runtime.select(st["choices"][0]["instance"], "consult",
                        reason="질문 형태이고 바꿀 대상이 없다", **kw)
    assert _labels(st) == ["pre-coding-consult-hold"]
    assert not st.get("asks") and not st.get("choices")
    # no timeout releases the hold: it stays until the person speaks again
    later = runtime.status(**{**kw, "now": time.time() + 10 ** 6})
    assert _labels(later) == ["pre-coding-consult-hold"] and not later.get("asks")


def test_a_work_instruction_from_the_hold_continues_the_round(kw):
    st = _consult_run(kw)
    st = runtime.select(st["choices"][0]["instance"], "consult",
                        reason="확인 요청", **kw)
    st = runtime.report(st["tasks"][0]["instance"], "ok",
                        {"work": "X를 Y로 바꾼다"}, **kw)
    assert _labels(st) == ["after"] and not st.get("asks")


@pytest.mark.parametrize("mode", ["land-only", "stack-only"])
def test_a_round_without_coding_does_not_judge(kw, mode):
    st = _consult_run(kw, mode=mode)
    assert not st["choices"] and _labels(st) == ["after"]
