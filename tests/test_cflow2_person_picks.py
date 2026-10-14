"""A choice a person takes for the driver is kept in flow_status with the
note they typed (claunch-e437w): the journal alone never reaches the agent."""

from __future__ import annotations

from claude_launcher.cflow2 import frontend, runtime

CHART = """cflow: 2
name: picks
nodes:
  a: {prompt: A}
  b: {prompt: B}
body:
  loop:
    while: "true"
    max: 10
    do:
      choose:
        prompt: which way?
        options:
          left: {then: a}
          right: {then: b}
"""


def _start(tmp_path):
    cwd = str(tmp_path)
    runtime.start(frontend.compile_text(CHART), cwd=cwd, scope="drv", caps={})
    return cwd


def _choice(cwd):
    return runtime.status(cwd=cwd, scope="drv")["choices"][0]["instance"]


def _finish_task(cwd):
    task = runtime.status(cwd=cwd, scope="drv")["tasks"][0]["instance"]
    runtime.report(task, cwd=cwd, scope="drv")


def test_a_persons_pick_and_note_show_in_status(tmp_path):
    cwd = _start(tmp_path)
    assert "person_picks" not in runtime.status(cwd=cwd, scope="drv")
    payload = runtime.select(_choice(cwd), "left", "edit the yaml in claunch",
                             caller=runtime.CALLER_USER, cwd=cwd, scope="drv")
    [pick] = payload["person_picks"]
    assert pick["option"] == "left" and pick["reason"] == "edit the yaml in claunch"
    assert pick["node"] and pick["instance"] and pick["at"]
    assert runtime.status(cwd=cwd, scope="drv")["person_picks"] == [pick]


def test_the_agents_own_pick_is_not_a_person_pick(tmp_path):
    cwd = _start(tmp_path)
    payload = runtime.select(_choice(cwd), "right", "mine", cwd=cwd, scope="drv")
    assert "person_picks" not in payload


def test_person_picks_keep_the_newest_few(tmp_path):
    cwd = _start(tmp_path)
    for i in range(runtime.PERSON_PICKS_KEPT + 2):
        runtime.select(_choice(cwd), "left", f"note {i}", caller=runtime.CALLER_USER,
                       cwd=cwd, scope="drv")
        _finish_task(cwd)
    picks = runtime.status(cwd=cwd, scope="drv")["person_picks"]
    assert [p["reason"] for p in picks] == [
        f"note {i}" for i in range(2, runtime.PERSON_PICKS_KEPT + 2)]


def test_pick_words_for_a_nudge():
    payload = {"person_picks": [{"option": "left", "label": "way", "reason": " go \n"}]}
    assert runtime.person_pick_words(payload, "left") == {
        "label": "way", "note": " -- their note: go"}
    # no note, or a payload that does not carry this pick: nothing to quote
    assert runtime.person_pick_words(
        {"person_picks": [{"option": "left", "label": "way", "reason": None}]},
        "left")["note"] == ""
    assert runtime.person_pick_words(payload, "right") == {
        "label": "the open choice", "note": ""}
    long = {"person_picks": [{"option": "left", "label": "way",
                              "reason": "x" * (runtime.NUDGE_NOTE_LIMIT + 50)}]}
    note = runtime.person_pick_words(long, "left")["note"]
    assert "x" * runtime.NUDGE_NOTE_LIMIT in note and "person_picks" in note
    assert "x" * (runtime.NUDGE_NOTE_LIMIT + 1) not in note
