"""Two pre-flight gates in ``improv-worker-v2`` (claunch-101wi).

1. **feasibility** -- before the round's items (and so before the coding
   approval gate), the agent judges whether doing the work is justified.
   ``justified`` goes on with no person in the way; ``unjustified`` stands a
   human approve/decline gate (``feasibility-gate``). A decline ends the
   round with no code change: ``feasibility-hold`` leaves a ``HOLD:`` and the
   batch loop is left for wrapup.
2. **landing-channel** -- right before the request that ``await-landing``
   waits on, the agent checks that a mesh channel to the request's receiver
   exists. The missing channel used to surface only after the await loop
   (25 nudges, ten minutes apart) ran out. ``no-channel`` stands a human
   approve/decline gate (``landing-channel-gate``) that names the fix; a
   decline ends ``land`` as ``blocked`` before ``integration-request``, so
   the board is not moved to ``in_review``.

The runtime tests run the real nodes from the canon with their surroundings
stubbed, the pattern ``tests/test_cflow2_improv_worker.py`` uses for
``items-pass`` and the coding gate.
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
    return {"cwd": str(tmp_path), "scope": "gates"}


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #
def test_feasibility_opens_the_batch_before_the_items():
    """Before items-pass, so before the item's coding-approval-check."""
    steps = _doc()["procedures"]["worker-round"]["body"][1]["loop"]["do"]["try"]
    ids = [s.get("id") if isinstance(s, dict) else s for s in steps]
    assert ids.index("feasibility") + 1 == ids.index("items-pass")


def test_landing_channel_stands_right_before_the_request():
    submit = _find(_doc()["procedures"]["land"]["body"], "submit")
    seq = submit["loop"]["do"]
    ids = [s.get("id") if isinstance(s, dict) else s for s in seq]
    assert ids.index("landing-channel") + 1 == ids.index("integration-request")
    assert ids.index("integration-request") + 1 == ids.index("await")


@pytest.mark.parametrize("label,decline", [("feasibility-gate", "infeasible"),
                                           ("landing-channel-gate", "no-channel-declined")])
def test_both_gates_are_human_approvals_with_no_timeout(label, decline):
    ask = _doc()["nodes"][label]["ask"]
    assert ask["otherwise"] == "human"
    assert "from" not in ask and "timeout" not in ask and "options" not in ask
    assert ask["urgent"] is True
    assert ask["on_decline"] == decline


@pytest.mark.parametrize("label,options", [("feasibility-check", ["justified", "unjustified"]),
                                           ("landing-channel", ["connected", "no-channel"])])
def test_the_self_judgement_is_the_agents_with_a_reason(label, options):
    chart = layers.load("improv-worker-v2")
    [node] = [n for n in chart.nodes.values() if n.label == label]
    assert node.type == "choose" and node.get("chooser") == "agent"
    assert node.get("require_reason") is True
    assert [o["name"] for o in node.get("options")] == options


def test_the_channel_check_names_its_commands():
    prompt = " ".join(_find(_doc()["procedures"]["land"]["body"],
                            "landing-channel")["choose"]["prompt"].split())
    for anchor in ("claunch gate landing-target --json", "claunch mesh members",
                   "connected pairs"):
        assert anchor in prompt
    gate = " ".join(_doc()["nodes"]["landing-channel-gate"]["ask"]["prompt"].split())
    assert "claunch mesh connect" in gate and "claunch mesh wire-requests" in gate


def test_the_channel_marker_is_not_a_brake_marker():
    """landing's fourth check greps BLOCKED / HOLD / CHANGES REQUESTED."""
    prompt = _doc()["nodes"]["landing-channel-gate"]["prompt"]
    marker = next(line.strip() for line in prompt.splitlines()
                  if line.strip().startswith("LANDING CHANNEL:"))
    for brake in ("BLOCKED", "HOLD", "CHANGES REQUESTED"):
        assert brake not in marker


# --------------------------------------------------------------------------- #
# feasibility at runtime
# --------------------------------------------------------------------------- #
def _feasibility_run(kw, rework=False):
    doc = _doc()
    feas = _find(doc["procedures"]["worker-round"]["body"], "feasibility")
    flow = {
        "cflow": 2, "name": "feasibility-gate-run",
        "data": {"land_out": {"rework": True} if rework else {},
                 "claim": {"issue": "X-1", "how": "claimed"}},
        "nodes": {
            "feasibility-gate": doc["nodes"]["feasibility-gate"],
            "feasibility-hold": doc["nodes"]["feasibility-hold"],
            "items": {"prompt": "items"},
            "wrapup": {"prompt": "wrapup"},
        },
        "procedures": {
            "worker-round": {
                "params": {"mode": {"type": "str", "default": "full"}},
                "body": [{"id": "batch", "loop": {"while": "true", "max": 3, "do": {
                    "try": [feas, "items", {"break": "batch"}],
                    "catch": {"held": {"break": "batch"}}}}},
                    "wrapup"],
            },
        },
        "body": [{"call": "worker-round"}],
    }
    chart = frontend.compile_text(json.dumps(flow, ensure_ascii=False))
    return runtime.start(chart, caps={}, **kw)


def test_justified_goes_on_without_a_person(kw):
    st = _feasibility_run(kw)
    assert _labels(st, "choices") == ["feasibility-check"]
    with pytest.raises(runtime.FlowError, match="requires a reason"):
        runtime.select(st["choices"][0]["instance"], "justified", **kw)
    st = runtime.select(st["choices"][0]["instance"], "justified",
                        reason="1-5 all true", **kw)
    assert _labels(st) == ["items"] and not st.get("asks")


def test_unjustified_then_approved_records_and_goes_on(kw):
    st = _feasibility_run(kw)
    st = runtime.select(st["choices"][0]["instance"], "unjustified",
                        reason="2: already on master", **kw)
    assert not st["tasks"]
    [ask] = st["asks"]
    assert ask["label"].endswith("feasibility-gate")
    assert ask["urgent"] is True and ask["resolution"] == "human"
    assert "X-1" in ask["prompt"]
    with pytest.raises(runtime.FlowError):
        runtime.approve(None, caller="agent", **kw)
    # time alone does not release it
    assert runtime.status(**kw, now=time.time() + 10 ** 6)["asks"]
    runtime.approve(None, reason="go anyway", **kw)
    st = runtime.status(**kw)
    assert _labels(st) == ["feasibility-gate"]
    st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    assert _labels(st) == ["items"]


def test_unjustified_then_declined_holds_and_skips_the_items(kw):
    st = _feasibility_run(kw)
    st = runtime.select(st["choices"][0]["instance"], "unjustified",
                        reason="4: needs a daemon restart", **kw)
    runtime.approve(None, decline=True, reason="not worth it", **kw)
    st = runtime.status(**kw)
    assert _labels(st) == ["feasibility-hold"]
    st = runtime.report(st["tasks"][0]["instance"], "ok", {"reason": "not worth it"}, **kw)
    assert _labels(st) == ["wrapup"]


def test_a_rework_pass_does_not_judge_again(kw):
    st = _feasibility_run(kw, rework=True)
    assert not st["choices"] and _labels(st) == ["items"]


# --------------------------------------------------------------------------- #
# landing-channel at runtime
# --------------------------------------------------------------------------- #
def _channel_run(kw):
    doc = _doc()
    channel = _find(doc["procedures"]["land"]["body"], "landing-channel")
    flow = {
        "cflow": 2, "name": "landing-channel-run",
        "inputs": {"relation": {"type": "string", "default": "independent"}},
        "nodes": {
            "landing-channel-gate": doc["nodes"]["landing-channel-gate"],
            "integration-request": {"prompt": "integration-request"},
            "after-landed": {"prompt": "after-landed"},
            "after-blocked": {"prompt": "after-blocked"},
        },
        "procedures": {
            "land": {
                "returns": {"landed": {"merge": "str"}, "blocked": {"reason": "str"}},
                "body": [channel, "integration-request",
                         {"return": "landed", "value": {"merge": "abc"}}],
            },
        },
        "body": [{"try": [{"call": "land"}],
                  "catch": {"landed": "after-landed", "blocked": "after-blocked"}}],
    }
    chart = frontend.compile_text(json.dumps(flow, ensure_ascii=False))
    return runtime.start(chart, caps={}, **kw)


def test_connected_goes_to_the_request(kw):
    st = _channel_run(kw)
    assert _labels(st, "choices") == ["landing-channel"]
    st = runtime.select(st["choices"][0]["instance"], "connected",
                        reason="s29 <-> s1 in mesh claunch", **kw)
    assert _labels(st) == ["integration-request"] and not st.get("asks")


def test_no_channel_then_approved_goes_to_the_request(kw):
    st = _channel_run(kw)
    st = runtime.select(st["choices"][0]["instance"], "no-channel",
                        reason="no edge to s29", **kw)
    [ask] = st["asks"]
    assert ask["label"].endswith("landing-channel-gate")
    assert ask["urgent"] is True and ask["resolution"] == "human"
    runtime.approve(None, reason="connected them", **kw)
    st = runtime.status(**kw)
    assert _labels(st) == ["landing-channel-gate"]
    st = runtime.report(st["tasks"][0]["instance"], "ok",
                        {"receiver": "s29", "channel": "connected"}, **kw)
    assert _labels(st) == ["integration-request"]


def test_no_channel_then_declined_blocks_before_the_request(kw):
    st = _channel_run(kw)
    st = runtime.select(st["choices"][0]["instance"], "no-channel",
                        reason="no edge to s29", **kw)
    runtime.approve(None, decline=True, reason="leave it", **kw)
    st = runtime.status(**kw)
    assert _labels(st) == ["after-blocked"]
