"""cflow v2 delegate prompts: the brief a ``session`` child gets
(docs/cflow-v2.md 6.4, 7). The ``headless`` executor was removed
(claunch-xjbrh)."""

from __future__ import annotations

from claude_launcher.cflow2 import executors


def test_prompt_always_has_the_fenced_json_instruction():
    brief = {"delegation": "d1", "node": "t", "tasks": [{"node": "t", "prompt": "do it"}],
             "returns": {"ok": {"hits": "int"}}, "rules": [{"id": "r", "text": "be nice"}]}
    text = executors.build_prompt(brief, errors=["missing field 'hits'"], readonly=True)
    assert "```json" in text and "do it" in text and "be nice" in text
    assert "READ-ONLY" in text and "missing field 'hits'" in text


def test_a_nested_session_delegation_is_told_to_drive_its_run():
    brief = {"delegation": "d1", "node": "n", "tasks": [], "returns": {},
             "nested": {"cwd": "/w", "scope": "s1~d1"},
             "submit": "drive the nested run under scope 's1~d1' with flow_*"}
    text = executors.session_task(brief, cwd="/w", scope="s1", receipt_path="/w/r.json")
    assert "scope 's1~d1'" in text
    assert "flow_delegate" not in text


def test_a_session_child_is_told_whose_run_it_works_for():
    """The child's own $CLAUNCH_SESSION is not the run's: a round report it
    writes under its own name lands outside the parent's Reports block."""
    brief = {"delegation": "d1", "node": "n", "tasks": [], "returns": {"ok": {}}}
    for mcp in (True, False):
        text = executors.session_task(brief, cwd="/w", scope="s1",
                                      receipt_path="/w/r.json", mcp=mcp)
        assert "run of session 's1' in /w" in text
        assert "--session s1" in text
