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


def test_the_watch_ends_at_a_cap_without_waiting_for_a_person():
    """claunch-1rsgm: a job that never gets a verdict must not hold the session.

    ``loop.max`` is no cap: a loop that reaches it waits for a person
    (``loop_limit``). The cap is the ``$visits.nudge`` guard on the wait's
    timeout, and it routes to ``timed-out`` and out of the loop."""
    import re
    import yaml

    doc = yaml.safe_load((WORKFLOWS / "sweep-job-v2.yaml").read_text(encoding="utf-8"))
    loop = doc["body"]["seq"][1]["loop"]
    wait = loop["do"]["seq"][0]
    on = wait.get("on", wait.get(True))  # PyYAML reads the bare key `on` as True
    branches = on["timeout"]["cond"]
    cap = int(re.search(r">= (\d+)", branches[0]["if"]).group(1)) + 1
    assert branches[0]["then"] == {"seq": ["timed-out", {"break": "watch"}]}
    assert on["undecidable"] == on["timeout"]
    assert cap * wait["wait"]["seconds"] <= 3 * 3600, "the watcher outlives a sweep by hours"
    assert loop["max"] > cap, "the loop would reach its own max before the cap and wait for a person"
    assert "timed-out" in chart().nodes
