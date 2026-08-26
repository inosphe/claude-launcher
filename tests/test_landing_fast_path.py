"""A clean merge request does not spend the session above it a turn.

The landing decision used to be asked of the parent every single round. The
journal says what that bought: across this repository's runs, every landing
decision ever recorded chose ``request`` and none chose ``hold`` (18
``select_confirmed`` before the delegation existed, 2 ``ask_answered`` after
it). Twenty-two questions, twenty-two identical answers.

What it cost was not nothing:

* a turn of the parent's context per worker -- in ``ask-19ed7b`` the leader
  re-read ``master``/tip/``rev-list``/``merge-tree``/``diff --stat`` to
  answer, which is exactly the reading its own ``integrate-preflight`` does
  a step later;
* a smaller integration batch. The leader's own preflight report
  (2026-08-26T03:30:32Z) wrote it down: one candidate's landing was stuck at
  a human gate and "cannot enter the batch yet", and shipping the two
  together "saves one rebase and one sweep". A stalled landing turns one
  swept batch into two -- the precise waste the leader's five-minute
  integration window exists to prevent;
* a live session per blocked worker, since a worker holds its slot until the
  merge lands.

And it was asked too early to know its own answer. ``landing`` sits *before*
``rebase`` (which is where divergence and conflict are actually measured) and
before ``peer-review``. Everything its prompt claimed to weigh is re-decided
downstream with better information.

So the fast path: when the machine checks hold, the worker chooses
``request`` itself. When one breaks, ``landing-review`` -- the old step,
delegation intact -- asks the parent. What this module pins is that the
escape hatch cannot be the lazy one: ``hold`` is the option that skips the
rebase and the evidence bundle, and no agent can reach it alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import model, state as state_mod

PROJECT_OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"


def _bundled(name: str) -> Path:
    return Path(dict(state_mod.bundled_workflows())[name])


#: Every copy of the landing pair, and where its ``request`` goes. A worker
#: requests through ``rebase``; a middle worker hands its stack over through
#: ``handoff``. The project layer is a regenerated copy of the bundled
#: worker and drifts by hand-edit, so it is checked as its own layer.
LAYERS = {
    "worker/bundled": (lambda: _bundled("improv-worker"), "rebase"),
    "worker/project": (lambda: PROJECT_OVERRIDES / "improv-worker.yaml", "rebase"),
    "mid/bundled": (lambda: _bundled("improv-mid"), "handoff"),
}


@pytest.fixture(params=sorted(LAYERS))
def layer(request):
    where, request_next = LAYERS[request.param]
    path = where()
    wf = model.parse(path.read_text(encoding="utf-8"), default_name=path.stem)
    return request.param, wf, request_next


def test_a_clean_request_asks_nobody(layer):
    """The fast path is the agent's own, and it leads where it always led."""
    label, wf, request_next = layer
    sel = wf.steps["landing"].select
    assert sel.chooser == "agent", (
        f"{label}: landing went back to asking somebody on every round"
    )
    assert sel.delegate is None
    assert sel.options["request"].next == request_next


def test_the_agent_cannot_reach_hold_on_its_own(layer):
    """The one option that skips work stays with the session above.

    This is the objection the delegation was built on, and it survives: the
    chooser and the beneficiary must not be the same party. ``request`` is
    *more* work (rebase, evidence bundle, a request to answer for), so an
    agent choosing it is not taking a shortcut. ``hold`` freezes the branch
    and ends the round -- that one is still somebody else's word.
    """
    label, wf, _ = layer
    fast = wf.steps["landing"].select
    assert set(fast.options) == {"request", "escalate"}, (
        f"{label}: the fast path must offer exactly request/escalate; "
        f"got {sorted(fast.options)}"
    )
    assert fast.options["escalate"].next == "landing-review"
    assert "hold" in wf.steps["landing-review"].select.options


def test_the_escalation_still_carries_the_full_delegation(layer):
    """Moving the question must not have loosened it.

    Same candidates, same fall to a human, same clock as the landing this
    replaced -- only the frequency changed.
    """
    label, wf, request_next = layer
    review = wf.steps["landing-review"].select
    assert review.chooser == "delegate", f"{label}: landing-review stopped delegating"
    assert review.delegate.otherwise == model.OTHERWISE_HUMAN
    assert review.delegate.timeout == 1800
    assert all(c.scope == model.SCOPE_ANCESTOR for c in review.delegate.candidates)
    assert review.options["request"].next == request_next
    assert review.options["hold"].next == "wrapup"


def test_the_checks_are_commands_not_adjectives(layer):
    """A "no particular issue" that a machine settles, not a mood.

    A checklist written as "looks fine / seems clean" is decided by whoever
    is in a hurry. Each item names the command whose output decides it, so
    the reason the agent files -- and the parent later reads -- carries
    outputs rather than impressions.
    """
    label, wf, _ = layer
    # Prompts are wrapped prose: an anchor may straddle a newline, so the
    # phrase is what is pinned, not the author's line breaks.
    prompt = " ".join(wf.steps["landing"].select.prompt.split())
    for anchor in (
        "git status --porcelain",   # 1. working tree
        # 2. the green was measured on THIS tip. The round measures at
        # ``review`` and commits after it, so "the numbers are green" and
        # "the numbers describe what I am offering" are different claims --
        # a nested worker's commit step merges its children in between.
        # Comparing the measured commit to HEAD by equality would fail every
        # normal round; comparing their file lists is the check that holds.
        "git diff --name-only",
        "git merge-tree",           # 3. preview merge onto the target
        "CHANGES REQUESTED",        # 4. open brake markers
        "BLOCKED",
        "reason에 적는다",           # the outputs are journaled, not summarised
    ):
        assert anchor in prompt, f"{label}: fast-path check lost its {anchor!r}"
    # And the two failure modes it must name: don't guess your way to a
    # request, and don't escalate out of politeness.
    assert "모르는 것을 request로 밀지 않는다" in prompt
    assert "리더의 턴" in prompt or "상위 세션의 턴" in prompt


def test_the_escalated_question_says_why_it_arrived(layer):
    """The parent's first move is reading what broke, not re-judging the branch.

    The value of a gate that only opens on exceptions is lost if the
    responder answers it the way it answered the every-round version. The
    prompt has to send it to the reason the agent filed.
    """
    label, wf, _ = layer
    prompt = wf.steps["landing-review"].select.prompt
    assert "기계 확인" in prompt, f"{label}: landing-review lost the 'why you'"
    assert "claunch cflow journal -t" in prompt, (
        f"{label}: landing-review must point at the reason the agent journaled"
    )


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(lambda: _bundled("improv-leader"), id="leader/bundled"),
        pytest.param(
            lambda: PROJECT_OVERRIDES / "improv-leader.yaml", id="leader/project"
        ),
    ],
)
def test_the_leader_knows_a_landing_question_is_now_an_exception(path):
    """Both ends of the protocol have to agree, or the change is half-made.

    The leader's standby bullet described landing decisions as routine
    inflow. If it keeps saying that, a leader reads the arrival as normal,
    answers ``request`` from the request text, and the filter is decoration.
    """
    wf = model.parse(path().read_text(encoding="utf-8"), default_name="improv-leader")
    standby = wf.steps["standby"].instructions
    assert "landing-review" in standby, (
        "the leader must know which step now asks it, and that landing does not"
    )
    assert "예외" in standby
    assert "claunch cflow journal -t" in standby, (
        "the leader is told to read what broke before answering"
    )
    # ...and told not to redo the preflight's measuring in the answer, which
    # is where the duplicated turn came from (ask-19ed7b).
    assert "integrate-preflight의 일" in standby
