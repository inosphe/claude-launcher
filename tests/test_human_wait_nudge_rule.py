"""claunch-8myyt: a run that waits on a person is told to flow_nudge, in the
leader workflow and in every v2 reminder form."""
from __future__ import annotations

from pathlib import Path

import yaml

from claude_launcher.daemon import cflow_clock

YAML = (Path(cflow_clock.__file__).resolve().parents[1]
        / "workflows" / "v2" / "improv-leader-v2.yaml")

PAYLOAD = {"scope": "s", "workflow": "w", "run": "r", "status": "running",
           "tasks": [{"instance": "integrate@18", "label": "integrate",
                      "prompt": "merge", "done_when": "merged"}],
           "choices": []}


def test_leader_yaml_carries_the_rule():
    rule = yaml.safe_load(YAML.read_text(encoding="utf-8"))["rules"]["human-wait-nudge"]
    assert "flow_nudge" in rule["text"]
    assert rule["deliver"] == "all"
    assert f"{cflow_clock.HUMAN_WAIT_NUDGE_INTERVAL // 60}분" in rule["text"]
    assert "상한 없이" in rule["text"]
    assert "emergency_nudge" in rule["text"] and "no person right now" in rule["text"]
    assert "r484" in rule["text"] and "r485" in rule["text"]


def test_reminder_forms_both_carry_the_nudge_line():
    first = cflow_clock.v2_reminder_block(PAYLOAD, 600)
    short = cflow_clock.v2_reminder_block(PAYLOAD, 600, stalled_for=1200)
    assert "flow_nudge" in first
    assert "flow_nudge" in short
    assert "emergency_nudge" in first and "emergency_nudge" in short
    minutes = f"every {cflow_clock.HUMAN_WAIT_NUDGE_INTERVAL // 60} minutes"
    assert minutes in first and minutes in short
    assert cflow_clock.HUMAN_WAIT_NUDGE_INTERVAL == 1200
    assert "task integrate" not in short  # the short form still quotes no prompt
