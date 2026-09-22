"""``improv-worker``: the branch cannot be cut before the location is judged.

A round used to reach ``branch-setup`` directly from six places -- the four
options of ``issue-check``, ``issue-auto``, ``issue-decision``'s ``none``,
``issue-adopt`` and ``issue-create``. Between ``intake`` and the branch,
nothing asked whether the directory about to receive it is the one this
round's work belongs to, and the reported symptom (claunch-vc5ma) is that
rounds do start in the wrong repository or in another session's worktree.
Where it surfaces is the integration request, which is four steps and a test
run later than where it could have.

Three steps were added in front of ``branch-setup``, and what this file pins
is the shape rather than the prose:

``workspace-check``   the measurement, chosen by the agent off an exit code
``workspace-ok``      the match branch, re-measured by ``verify``
``workspace-gate``    the mismatch branch, approved by a person

The property that makes it a gate and not a suggestion is reachability:
``branch-setup`` has exactly two ways in, and one of them is a human
approval. The property that keeps ``chooser: agent`` honest is that the other
one re-runs the same command on the way out, so choosing ``match`` when it is
false buys nothing -- the run stops at ``workspace-ok`` and the only way on is
``request_goto`` to the approval the agent was avoiding.

Both layers are checked. The packaged copy ships to every repository and
carries the prose; the project copy is where this repository's ``verify``
lives (``tools/sync_project_layer.py``). They have drifted before, by hand,
which is why the pair is asserted together rather than one standing for both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import model, state as state_mod

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / ".claunch" / "workflows" / "improv-worker.yaml"

#: The steps that reached ``branch-setup`` before the gate existed. Named
#: rather than derived: the point of the test is that this list now routes
#: through the gate, and a derivation from the current file would follow the
#: file wherever it went.
ISSUE_STEPS = (
    "issue-check", "issue-auto", "issue-decision", "issue-adopt", "issue-create",
)


def _bundled() -> Path:
    return Path(dict(state_mod.bundled_workflows())["improv-worker"])


@pytest.fixture(params=["bundled", "project"])
def layer(request):
    path = _bundled() if request.param == "bundled" else PROJECT
    return request.param, path, model.parse(path.read_text(encoding="utf-8"), default_name="improv-worker")


def _targets(step):
    """Every step id this step can move to."""
    out = []
    if step.next:
        out.append(step.next)
    if step.select:
        opts = step.select.options
        out += [o.next for o in (opts.values() if isinstance(opts, dict) else opts) if o.next]
    if step.ask is not None and step.ask.on_decline:
        out.append(step.ask.on_decline)
    return out


def test_the_three_steps_exist_in_both_layers(layer):
    _, path, wf = layer
    for step_id in ("workspace-check", "workspace-ok", "workspace-gate"):
        assert step_id in wf.steps, f"{path.name} lost {step_id}"


def test_branch_setup_has_exactly_two_ways_in(layer):
    """The reachability property, which is the whole gate.

    If any other step can reach ``branch-setup``, that route cuts a branch
    without the location having been judged -- and a route like that is
    exactly how the six edges behaved before this change.
    """
    _, path, wf = layer
    entrances = sorted(
        step_id for step_id, step in wf.steps.items() if "branch-setup" in _targets(step)
    )
    assert entrances == ["workspace-gate", "workspace-ok"], (
        f"{path.name}: branch-setup is reachable from {entrances}. Every route "
        f"into it must pass the location check -- one through the machine "
        f"verdict (workspace-ok), one through a person (workspace-gate)."
    )


@pytest.mark.parametrize("step_id", ISSUE_STEPS)
def test_the_issue_steps_route_through_the_check(layer, step_id):
    """The six edges that used to reach the branch now reach the measurement."""
    _, path, wf = layer
    assert "workspace-check" in _targets(wf.steps[step_id]), (
        f"{path.name}:{step_id} no longer routes through workspace-check"
    )


def test_the_check_is_the_agents_to_make_and_routes_both_ways(layer):
    """An exit code, read by the driver, with one branch each way.

    ``chooser: agent`` is right here because the answer is a command's exit
    code rather than a judgment -- and is only safe because of the two
    assertions below it.
    """
    _, path, wf = layer
    select = wf.steps["workspace-check"].select
    assert select is not None, f"{path.name}: workspace-check stopped being a select"
    assert select.chooser == "agent"
    assert select.delegate is None, "the location check is not delegated to another session"
    opts = select.options
    routes = {name: opt.next for name, opt in opts.items()}
    assert routes == {"match": "workspace-ok", "mismatch": "workspace-gate"}


def test_the_match_branch_is_re_measured_where_this_repository_arms_it(layer):
    """The answer to "what stops the agent choosing match?"

    In this repository the project layer grafts the command onto
    ``workspace-ok``, so a false ``match`` fails on the way out of that step.
    The packaged copy ships without it -- no other repository has
    ``tools/`` -- and says so in its own text, which is what the second half
    checks. A packaged copy that stopped saying it would leave the next
    reader with a ``chooser: agent`` whose backstop is invisible.
    """
    which, path, wf = layer
    step = wf.steps["workspace-ok"]
    if which == "project":
        assert step.verify is not None, f"{path.name}: workspace-ok is not armed"
        assert "tools/workspace_check.py" in step.verify.command
        assert step.verify.command.startswith("uv run --no-sync python "), (
            "the gate must reach this checkout; test_gates_run_this_checkout.py "
            "owns the full rule"
        )
    else:
        assert step.verify is None, (
            "the packaged copy must not name a tools/ script -- it ships to "
            "repositories that do not have one"
        )
        assert "verify" in step.instructions and "workspace_check.py" in step.instructions


def test_the_match_branch_names_the_way_out_of_a_failed_verify(layer):
    """A trap with no documented exit teaches the agent to fake its way past.

    ``workspace-ok`` has no back edge: a red verify leaves the run standing
    there. That is deliberate -- the alternative is a route the agent can take
    by itself, which is the gate again -- so the step has to say that the exit
    is ``request_goto`` to the approval, and say it where the agent reads it.
    """
    _, path, wf = layer
    text = wf.steps["workspace-ok"].instructions
    assert "request_goto" in text and "workspace-gate" in text, (
        f"{path.name}: workspace-ok does not tell the agent where a red gate goes"
    )


def test_the_mismatch_branch_is_a_person_and_nothing_opens_it_on_a_clock(layer):
    """No responder, no timeout.

    No role can answer "may this session work here": it is the intent of
    whoever put the session in that directory. A timeout would be worse than
    no gate, because the round it lets through is the exact round this was
    built to stop, and it would go through unattended.
    """
    _, path, wf = layer
    ask = wf.steps["workspace-gate"].ask
    assert ask is not None, f"{path.name}: workspace-gate stopped being a gate"
    assert ask.delegate.candidates == [], "a human gate has no responders in `from`"
    assert ask.delegate.otherwise == model.OTHERWISE_HUMAN
    assert ask.delegate.timeout is None, "this gate must not open by itself"


def test_the_gate_prompt_carries_both_costs_and_the_way_to_answer(layer):
    """What a person reads is the whole basis they have for answering.

    They did not watch the round: the prompt has to say what approving does,
    what holding costs (nothing yet -- no branch, no commits), and the command
    that settles it either way.
    """
    _, path, wf = layer
    prompt = wf.steps["workspace-gate"].ask.prompt
    assert "허가하면" in prompt and "보류하면" in prompt
    assert "claunch cflow abort" in prompt, "the third door -- retire the run -- is not named"


def test_the_approved_override_is_written_to_the_board(layer):
    """An approval that leaves no record is a branch nobody can explain later.

    The round proceeds in a directory the machine called wrong. Six steps on,
    at the integration request, the reason has to be readable from the issue
    rather than from a run journal nobody opens.
    """
    _, path, wf = layer
    step = wf.steps["workspace-gate"]
    assert "WORKSPACE OVERRIDE" in step.instructions
    assert "WORKSPACE OVERRIDE" in step.done_when
