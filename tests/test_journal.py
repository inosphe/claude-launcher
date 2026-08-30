from claude_launcher import journal


def test_append_and_read_support_generic_events_and_filters(tmp_path):
    path = tmp_path / "run.jsonl"
    journal.append(path, "started", {"run": "r1", "source": "worker"}, at="t1")
    journal.append(path, "decision_made", {
        "run": "r1", "decision": "skip", "reason": "docs only"
    }, at="t2")
    journal.append(path, "started", {"run": "r2"}, at="t3")

    assert [e["event"] for e in journal.read(path)] == [
        "started", "decision_made", "started"
    ]
    assert journal.read(path, run_id="r1", events=["decision_made"])[0]["reason"] == "docs only"


def test_read_skips_malformed_and_non_object_lines(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text('{"event":"ok"}\nnot json\n[]\n', encoding="utf-8")
    assert journal.read(path) == [{"event": "ok"}]
