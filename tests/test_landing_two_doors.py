"""improv-worker's landing: delegated to the session above, open to the user.

The landing decision moved from a human gate to the session above it because
that session — not the person — knows the state of the integration queue, and
because every round standing a person up cost more than the decision was
worth. What that move quietly took away was the user's own way in: the run
went out to an agent and the terminal it was started from could neither see
the question nor answer it.

Both are true at once now, and this pins the pair. The delegation is intact
(the chooser still names the sessions above, still falls to a human, still
times out), and the workflow says in its own text that a person may answer
the same question at any time and that their answer wins. The mechanism is
tested in ``test_cflow.py``; what is pinned here is that the workflow tells
the two parties who read it — the responder, through the prompt it is
delivered, and the next editor, through the comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import model, state as state_mod

PROJECT_OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"


def _bundled(name: str) -> Path:
    return Path(dict(state_mod.bundled_workflows())[name])


#: Both copies of the file, because the project layer is a regenerated copy
#: of the packaged one and a hand-edit to either is how they last drifted.
LAYERS = {
    "bundled": lambda: _bundled("improv-worker"),
    "project": lambda: PROJECT_OVERRIDES / "improv-worker.yaml",
}


@pytest.fixture(params=sorted(LAYERS))
def landing(request):
    path = LAYERS[request.param]()
    text = path.read_text(encoding="utf-8")
    step = model.parse(text, default_name="improv-worker").steps["landing"]
    return step, text


def test_the_landing_is_still_delegated_upward(landing):
    """The door is added beside the delegation, not in place of it."""
    step, _ = landing
    delegate = step.select.delegate
    assert delegate is not None, "landing stopped delegating"
    assert [c.role for c in delegate.candidates] == ["worker", "leader"]
    assert all(c.scope == "ancestor" for c in delegate.candidates)
    assert delegate.otherwise == model.OTHERWISE_HUMAN
    assert delegate.timeout == 1800


def test_the_prompt_tells_the_responder_a_person_may_answer_too(landing):
    """The responder is the party that can be overridden, so it is the party
    that has to know the rule before it spends a turn on the question."""
    step, _ = landing
    prompt = step.select.prompt
    assert "문이 둘" in prompt
    assert "사용자 지침이 이긴다" in prompt


def test_the_comment_names_the_press_and_who_wins(landing):
    """The next person to edit this step reads the comment, not the engine.

    Naming the command matters as much as naming the rule: "the user can
    answer" without ``claunch cflow select`` is how the door stayed shut
    while being documented as open.
    """
    _, text = landing
    assert "claunch cflow select request|hold" in text
    assert "사용자의 답이 이긴다" in text
    # ...and that it is a door, not a gate: nothing here re-introduces the
    # per-round stand-up the delegation was made to remove.
    assert "문이지 게이트가 아니다" in text
