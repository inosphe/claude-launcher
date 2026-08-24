"""What a person is handed at the moment a run stops and asks them something.

A cflow gate exists because the run does not get to decide that one. The
decision then travels to a human over two narrow channels — the payload's
``how_to_unblock``, which tells the agent what to write, and ``claunch cflow
status``, which is what the person reads at the moment they confirm — and
both used to carry almost nothing. ``how_to_unblock`` said "present your
recommendation and reasoning"; the CLI printed the agent's one-line proposal
and ``select <request, hold>``, never the question the workflow asked nor
what either option does. The reliable result was a request to ratify a
decision already made, which is a rubber stamp wearing the shape of a gate.

These tests hold the two channels to the brief: what is being decided, what
it rests on, what each answer costs *including holding off*, the
recommendation and what would overturn it, the weak part, and how to answer.
They are string assertions because the feature is text — the clause list in
:data:`BRIEF` is deliberately short and each entry is named for the element
it guards, so a failure says which one went missing instead of diffing a
paragraph.
"""

from __future__ import annotations

import pytest

from claude_launcher import cli
from claude_launcher.cflow import engine, install, state as state_mod

# --------------------------------------------------------------------------- #
# workflows
# --------------------------------------------------------------------------- #

#: A user-chooser branch whose prompt runs to more than one line — which is
#: the normal shape, since the question a person is asked rarely fits in one.
USER_BRANCH = """
steps:
  landing:
    select:
      prompt: |
        The branch is committed and the suite is green.
        Ask the leader to integrate now, or freeze the branch and close?
      chooser: user
      options:
        request: {description: "ask the leader to integrate now", next: after}
        hold:    {description: "freeze the branch, integrate later", next: after}
  after:
    instructions: continue
"""

AGENT_BRANCH = """
steps:
  triage:
    select:
      prompt: does the diff touch the parser?
      chooser: agent
      options:
        yes: {description: "run the grammar suite too", next: after}
        no:  {description: "the default suite is enough", next: after}
  after:
    instructions: continue
"""

GATED = """
steps:
  ship:
    gate: this publishes to the registry
    instructions: ship it
"""

LOOP = """
max_visits: 2
steps:
  impl:
    instructions: implement or rework
    next: review
  review:
    select:
      prompt: good enough?
      chooser: agent
      options:
        again: {description: "loop back", next: impl}
        done:  {description: "finish", next: end}
"""


@pytest.fixture
def project(home, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    monkeypatch.chdir(proj)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    return proj


def _write(proj, name, text):
    (proj / ".claunch" / "workflows" / f"{name}.yaml").write_text(
        text, encoding="utf-8"
    )


def _held_at_selection(proj):
    """A run parked on a user-chooser branch, the agent's proposal filed."""
    _write(proj, "branchy", USER_BRANCH)
    engine.start("branchy")
    return engine.select("request", "the suite is green at 887/887", by="agent")


# --------------------------------------------------------------------------- #
# the payload: what the agent is told to write
# --------------------------------------------------------------------------- #

#: The clauses the brief exists to force, each named for what it guards. A
#: failure here should read as "the gate stopped asking for X", not as a
#: diff of two paragraphs.
BRIEF = {
    "it is asked, not announced": "not an instruction",
    "evidence you can check": "concrete evidence",
    "what each answer costs": "what each answer costs",
    "what waiting costs": "piles up",
    "reversibility": "can be undone",
    "a recommendation": "recommendation",
    "what would overturn it": "overturn",
    "the weakest part of the case": "weakest part",
    "the answer is theirs": "theirs to take",
}


def _unblock_at_a_selection(proj):
    return _held_at_selection(proj)["how_to_unblock"]


def _unblock_at_a_gate(proj):
    _write(proj, "gated", GATED)
    return engine.start("gated")["how_to_unblock"]


def _unblock_at_the_loop_guard(proj):
    _write(proj, "loop", LOOP)
    engine.start("loop")
    engine.report("round 1")
    engine.next_step()
    engine.select("again", "not yet", by="agent")  # impl visit 2
    engine.report("round 2")
    engine.next_step()
    payload = engine.select("again", "still not yet", by="agent")  # over limit
    assert payload["reason"] == "loop_limit"
    return payload["how_to_unblock"]


GATES = {
    "a selection waiting on a person": _unblock_at_a_selection,
    "a declared approval gate": _unblock_at_a_gate,
    "the loop guard": _unblock_at_the_loop_guard,
}


@pytest.mark.parametrize("where", sorted(GATES))
@pytest.mark.parametrize("element", sorted(BRIEF))
def test_every_human_gate_asks_for_the_whole_brief(project, where, element):
    """One brief, not three — a person at the loop guard is owed what a
    person at a landing selection is owed. The three used to differ only in
    which single thing they asked for ("your recommendation", "your work so
    far", "why the loop keeps repeating"), which is how each of them ended
    up carrying a different fraction of the case."""
    text = GATES[where](project)
    assert BRIEF[element] in text, (
        f"{where} stopped asking for {element}: {text!r}"
    )


def test_the_brief_counts_holding_off_as_an_answer(project):
    """The clause a real gate found missing.

    The specimen: a run stopped on "the live server is serving pre-merge
    code — restart now?". Courteous, clear, and unanswerable — what the
    reader is weighing is whether a restart cuts the sessions attached right
    now, whether it can be undone, and what keeps accruing if they leave it
    sitting. The first two the brief already asked for. The third it did
    not, and a gate's cost is mostly paid by the time it spends open, so
    ``holding off`` is named explicitly rather than left to "each option".
    """
    text = _unblock_at_a_selection(project)
    assert "holding off included" in text
    assert "piles up while the run waits" in text


#: The lead each gate keeps in front of the shared closing. A directory
#: holds one run, so these are checked a gate at a time rather than in one
#: test that would have to start three.
LEADS = {
    "a selection waiting on a person": "which option you recommend",
    "a declared approval gate": "what entering this step will do",
    "the loop guard": "what another pass would do differently",
}


@pytest.mark.parametrize("where", sorted(LEADS))
def test_each_gate_still_says_what_its_own_decision_is(project, where):
    """The shared brief is a closing, not a replacement: the sentence before
    it still has to say what *this* gate is about, or every stop reads the
    same and the reader learns nothing from the difference."""
    assert LEADS[where] in GATES[where](project)


def test_the_brief_does_not_reach_a_decision_that_is_not_the_users(project):
    """An agent-chooser branch is the run's own call. Handing it the brief
    would be telling it to go ask somebody — there is nobody to ask, and the
    step's ``note`` already says to decide and move on."""
    _write(project, "triaged", AGENT_BRANCH)
    payload = engine.start("triaged")
    assert payload["status"] == "select" and payload["chooser"] == "agent"
    assert "how_to_unblock" not in payload
    assert "theirs to take" not in payload.get("note", "")


# --------------------------------------------------------------------------- #
# the CLI: what the person reads at the moment they confirm
# --------------------------------------------------------------------------- #


def _status_output(capsys):
    assert cli.main(["cflow", "status"]) == 0
    return capsys.readouterr().out


def test_status_shows_the_question_and_not_only_the_option_names(
    project, capsys
):
    """``select <request, hold>`` is not a decision. The prompt was in the
    payload the whole time; the CLI simply never printed it, so a person
    confirming from a shell had to reconstruct the question from whatever
    the agent happened to have said in its terminal."""
    _held_at_selection(project)
    out = _status_output(capsys)
    assert "Ask the leader to integrate now, or freeze the branch" in out


def test_status_keeps_every_line_of_a_multiline_prompt(project, capsys):
    """Prompts are paragraphs. Printing the first line only would drop the
    half that usually says what the choice costs — here, the second line is
    the question itself."""
    _held_at_selection(project)
    out = _status_output(capsys)
    assert "The branch is committed and the suite is green." in out
    assert "Ask the leader to integrate now" in out


def test_status_shows_what_each_option_does(project, capsys):
    """The workflow wrote a description per option precisely so the chooser
    would not have to guess from the name."""
    _held_at_selection(project)
    out = _status_output(capsys)
    assert "ask the leader to integrate now" in out
    assert "freeze the branch, integrate later" in out


def test_status_carries_the_agents_reasoning_and_marks_it_as_a_proposal(
    project, capsys
):
    """The recommendation is the thing being checked, so it is shown with
    its reasoning — and shown as a recommendation. A person reading
    ``confirm: claunch cflow select <...>`` under a proposal can reasonably
    read the run as waiting for a rubber stamp; it is not."""
    _held_at_selection(project)
    out = _status_output(capsys)
    assert "the suite is green at 887/887" in out
    assert "recommendation only" in out


def test_status_shows_the_question_even_when_the_agent_decides(
    project, capsys
):
    """A human watching a run they do not drive still gets told what is
    being decided and by whom — the branch is not theirs, the visibility
    is."""
    _write(project, "triaged", AGENT_BRANCH)
    engine.start("triaged")
    out = _status_output(capsys)
    assert "does the diff touch the parser?" in out
    assert "run the grammar suite too" in out
    assert "agent decides this one" in out


def test_status_prints_the_gate_it_is_held_on(project, capsys):
    _write(project, "gated", GATED)
    engine.start("gated")
    out = _status_output(capsys)
    assert "this publishes to the registry" in out
    assert "claunch cflow approve" in out


# --------------------------------------------------------------------------- #
# the skill text: the same rule, for the agent that still has its briefing
# --------------------------------------------------------------------------- #


def _flat(text: str) -> str:
    """Prose wrapped to a column, as one line — every phrase below spans a
    line break somewhere in the file, and none of them is about layout."""
    return " ".join(text.split()).lower()


@pytest.mark.parametrize("element", sorted(BRIEF))
def test_the_skill_text_carries_the_same_brief(element):
    """Two copies of one rule, deliberately: ``how_to_unblock`` is the copy
    that survives a compacted context (it is re-delivered on every poll),
    and this is the copy an agent reads while it still has its briefing.
    They must not drift into two different standards."""
    assert BRIEF[element] in _flat(install.SKILL_MD), (
        f"the /cflow skill text stopped asking for {element}"
    )


def test_the_skill_text_keeps_its_three_human_gate_branches_in_order():
    """The brief was added as a section, not a reorganisation.

    Sessions install this text and are held to it mid-run, so the dispatch
    an agent already knows has to keep its shape: three human-facing
    branches, in the order they were, each now pointing at the brief instead
    of carrying its own one-line instruction.
    """
    text = _flat(install.SKILL_MD)
    order = [
        "- `select` with `chooser: user`, or `waiting_selection`",
        "- `waiting_approval` — a human gate",
        "- `waiting_approval` with `reason: declined`",
        "## asking a person to decide",
    ]
    at = [text.index(fragment) for fragment in order]
    assert at == sorted(at), "the human-gate branches moved or were merged"
    for fragment in order[:3]:
        assert text.count(fragment) == 1


def test_the_skill_text_points_every_human_branch_at_the_brief():
    """Each branch names the brief rather than restating a piece of it —
    three restatements is how they drifted apart the first time."""
    assert _flat(install.SKILL_MD).count("decision brief below") == 3
