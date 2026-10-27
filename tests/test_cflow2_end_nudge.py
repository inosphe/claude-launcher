"""The end questions raise themselves on the dashboard (claunch-q4cqy.4).

A worker's round-end question waits on a person: ``end-decision`` for five
minutes and then ``release-decision`` with no deadline at all. The dashboard
only learns about them through the driver's own ``flow_nudge``, and a driver
that reads "wait for the answer" and stops leaves the person with no card,
no notification row and no bell count -- which is how a round sat through its
five minutes unnoticed (thread th-2a7e90).

So the two call sites hand ``ask-user`` a ``nudge`` instruction that
``ask-pose`` renders into its own prompt, and every other ``ask-user`` call in
this workflow (coding gate, settle stall, self-improve) keeps the empty
default: this defect is about the end questions, and making every question a
card is the noise the issue rules out.

``auto-release`` is reached *after* the deadline has closed the ask, so the
run is owed to nobody there and ``flow_nudge`` is refused
(``daemon/flow_nudge.py``'s ``owed``). Its prompt says so and carries the
timeout fact into the release question's message instead.
"""

from __future__ import annotations

from claude_launcher.cflow2 import layers, runtime

END_SITES = ("settle/end-decision.call", "settle/release-decision.call")


def _chart():
    return layers.load("improv-worker-v2")


def _scopes(chart):
    return {n.id: n for n in chart.nodes.values()
            if n.type == "scope" and n.get("with_") is not None}


def test_the_end_questions_carry_a_nudge_instruction():
    scopes = _scopes(_chart())
    for site in END_SITES:
        assert site in scopes, f"{site} is not a call scope any more"
        raw = (scopes[site].get("with_") or {}).get("nudge")
        # a plain string is stored in the explicit literal form ({lit: ...})
        text = raw.get("lit") if isinstance(raw, dict) else raw
        assert isinstance(text, str) and text.strip(), f"{site} passes no nudge text"
        assert "flow_nudge" in text
        # the person has to be told how to answer, not just that something waits
        assert "claunch cflow2 approve" in text


def test_no_other_ask_user_call_site_nudges():
    """The defect is these two questions; the rest stay quiet."""
    scopes = _scopes(_chart())
    nudging = sorted(i for i, n in scopes.items() if "nudge" in (n.get("with_") or {}))
    assert nudging == sorted(END_SITES)


def test_ask_pose_renders_the_instruction_and_nothing_when_unset():
    chart = _chart()
    poses = [n for n in chart.nodes.values() if n.label == "ask-pose"]
    # one pose per call site and per re-ask
    assert any("end-decision" in n.id for n in poses)
    for n in poses:
        assert "${params.nudge}" in (n.get("prompt") or "")

    template = next(n for n in poses if n.id.endswith("end-decision.call/ask-pose")).get("prompt")
    silent = runtime.render(template, {"params": {"nudge": ""}})
    assert "flow_nudge" not in silent
    told = runtime.render(template, {"params": {"nudge": "call flow_nudge(message) once"}})
    assert "flow_nudge(message) once" in told


def test_auto_release_forbids_a_nudge_where_it_is_refused():
    chart = _chart()
    node = next(n for n in chart.nodes.values() if n.label == "auto-release")
    prompt = node.get("prompt") or ""
    # a nudge here is refused: the deadline closed the ask (runtime._cancel_children)
    assert "flow_nudge" in prompt
    assert "여기서 `flow_nudge`를 부르지 않는다" in prompt
    assert "기한 초과" in prompt
    # ...and the fact still reaches the person, through the next question
    assert "release-decision" in prompt
