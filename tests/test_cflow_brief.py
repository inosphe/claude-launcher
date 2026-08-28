"""The briefing secretary: `brief`, a sibling of `poll` with a briefing step.

`brief` declares the same two-tier cadence machinery `poll` does (step
`timer:` + `recur: {auto: true}`, daemon-started rounds) instead of
extending it — the packaged workflows must each load standalone, without
layer resolution, so an overlay is not an option. These tests pin the
workflow itself (parse, bundled presence, instructions), the cadence parity
with `poll` (a drift between the two would silently change briefing
periodicity), and one end-to-end round on the seeded global layer.
"""

from __future__ import annotations

import pytest

from claude_launcher import install as install_mod
from claude_launcher.cflow import engine
from claude_launcher.cflow import model, mcp, state as state_mod

BRIEF = "brief"
POLL = "poll"


@pytest.fixture
def seeded(home, tmp_path, monkeypatch):
    """A project whose global layer carries the full bundled set."""
    lines = install_mod.install_into_user()
    assert any(line.startswith("workflow ->") for line in lines)
    d = tmp_path / "proj"
    d.mkdir()
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    mcp._seen_run = None
    return d


def _bundled(name: str) -> "model.Workflow":
    bundled = dict(state_mod.bundled_workflows())
    assert name in bundled
    return model.load(bundled[name])  # standalone — no layer resolution


def test_brief_is_a_bundled_workflow():
    assert BRIEF in dict(state_mod.bundled_workflows())


def test_brief_is_self_contained_and_parses():
    wf = _bundled(BRIEF)
    assert wf.name == BRIEF
    assert wf.recur and wf.recur_auto
    wait = wf.steps["wait"]
    assert wait.timer is not None
    assert wait.timer.then == "poll"
    assert wait.timer.after == "end"
    assert wait.next is None  # `next: end` is normalized to termination
    poll = wf.steps["poll"]
    assert poll.next == "wait"
    assert "브리핑" in poll.instructions
    assert "brief-last.json" in poll.instructions
    assert poll.done_when and "last_seen" in poll.done_when
    assert not wf.warnings and not wf.deprecations


def test_brief_cadence_stays_in_step_with_poll():
    """The periodicity is declared twice, so it is measured twice."""
    brief_wait = _bundled(BRIEF).steps["wait"]
    poll_wait = _bundled(POLL).steps["wait"]
    assert brief_wait.timer == poll_wait.timer
    assert brief_wait.next == poll_wait.next


def test_a_brief_round_runs_the_machinery(seeded):
    """start -> briefing step -> timed wait: end to end on the global
    layer, exactly as a running secretary would start."""
    payload = engine.start(BRIEF, context='{"mesh": "mesh-0826", "note": "smoke"}')
    assert (payload["status"], payload["step_id"]) == ("step", "poll")
    engine.report("새 메시지 3 (ask 1) — 판정 없음")
    wait = engine.next_step()
    assert wait["status"] == "waiting_timer"
    assert wait["then"] == "poll"
    assert wait["max"] == 22
    assert wait["after"] == "end"


def _instructions_of(wf: "model.Workflow", step: str) -> str:
    """Instructions folded to one line — block scalars wrap mid-sentence."""
    return " ".join(wf.steps[step].instructions.split())


def test_brief_poll_only_reads_channels_and_never_sends():
    """The briefing secretary is a watch, not a correspondent: the poll
    step reads mesh history/owed and the board, and reports to the user.
    No step of the workflow may instruct sending to another session —
    without the user's explicit request it is a silent monitor only."""
    wf = _bundled(BRIEF)
    poll = _instructions_of(wf, "poll")
    for read in ("mesh history", "mesh owed", "beads comments"):
        assert read in poll
    assert "사용자에게" in poll  # reports to the user, not to other sessions
    whole = " ".join(
        [wf.description or ""] + [_instructions_of(wf, s) for s in wf.steps]
    )
    assert "mesh send" not in whole


def test_brief_description_declares_the_watch_only_contract():
    """claunch-hrql: the pin that guards the property itself. brief may
    only watch — the description says so in its own words, so a future
    change that adds sending has to strike the declaration first."""
    wf = _bundled(BRIEF)
    desc = " ".join(wf.description.split())
    assert "메시지를 보내지 않는다" in desc
    assert "다른 세션에 아무것도 보내지 않고" in desc
