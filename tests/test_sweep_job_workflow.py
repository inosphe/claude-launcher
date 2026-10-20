"""`sweep-job-v2`: the session that watches one batch sweep (claunch-yfkt2)."""
from __future__ import annotations

from pathlib import Path

from claude_launcher.cflow2 import frontend

WORKFLOWS = Path(__file__).resolve().parents[1] / "src" / "claude_launcher" / "workflows" / "v2"


def chart():
    return frontend.compile_file(WORKFLOWS / "sweep-job-v2.yaml")


def test_the_watch_workflow_compiles_with_a_timer_that_nudges():
    nodes = set(chart().nodes)
    assert {"attend", "watch", "sweep-wait", "nudge", "result"} <= nodes


def test_the_workflow_is_one_shot():
    text = (WORKFLOWS / "sweep-job-v2.yaml").read_text(encoding="utf-8")
    top = [line for line in text.splitlines() if line and not line.startswith((" ", "#"))]
    assert not any(line.startswith(("recur:", "escalate:")) for line in top)


def test_the_leader_and_the_worker_state_the_split():
    leader = (WORKFLOWS / "improv-leader-v2.yaml").read_text(encoding="utf-8")
    worker = (WORKFLOWS / "improv-worker-v2.yaml").read_text(encoding="utf-8")
    assert "sweep-job-v2" in leader and "delayed_sweep_batch_size" in leader
    assert "머지 5건 단위 배치" in worker
