"""Saved run identity, trustworthy merge reports and bounded metric reads."""
import json
import sqlite3

import pytest

from claude_launcher.cflow2 import checkpoint, runtime
from claude_launcher.cflow2.frontend import compile_text
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_metrics as metrics


def definition(merges=True):
    task = ("{prompt: merge, effects: [git.merge], returns: {ok: {merges: list, repo: str}}}"
            if merges else "{prompt: work}")
    return compile_text(f"cflow: 2\nname: generic-demo\nnodes:\n  work: {task}\nbody: work\n",
                        resolve_include=lambda _: None)


def saved(st, rid, start="2026-10-13T10:00:00Z", end="2026-10-13T11:00:00Z",
          status="done", events=(), merges=True):
    st.dir.mkdir(parents=True, exist_ok=True)
    st.state_path.write_text(json.dumps({"run_id": rid, "started": start,
        "finished": end, "status": status, "workflow": "generic-demo"}))
    st.chart_path.write_text(definition(merges).to_json())
    st.journal_path.write_text("\n".join(json.dumps(e) for e in
        [{"event": "started", "run": rid}, *events]) + "\n")


def report(sha="a", **kw):
    return {"event": "reported", "node": "work", "outcome": "ok",
            "ts": "2026-10-13T10:30:00Z", "value": {"repo": "/repo",
            "merges": [{"issue": "demo", "tip": "b" * 40, "merge_sha": sha * 40}]}, **kw}


def test_completed_runs_not_loops_and_generic_merges(tmp_path):
    st = RunStore(str(tmp_path), "s")
    saved(st, "current", events=[report(), report(), *[{"event": "loop_iteration"}] * 20])
    old = RunStore(str(tmp_path), "s"); old.dir = st.archive_root() / "001"
    saved(old, "old", start="2026-10-13T09:00:00Z", end="2026-10-13T10:00:00Z",
          events=[report(ts="2026-10-13T09:30:00Z")])
    data = metrics.observations(st, now="2026-10-13T12:00:00Z")
    assert len(data["runs"]) == 2
    assert sum(bool(r["completed_at"]) for r in data["runs"]) == 2
    assert sum(len(r["merges"]) for r in data["runs"]) == 1
    assert data["runs"][1]["merges"][0]["at"] == "2026-10-13T09:30:00Z"
    assert data["runs"][0]["duration_s"] == 3600
    assert data["unavailable"] == data["journal_incomplete"] == 0


def test_no_merge_contract_and_duplicate_run_id(tmp_path):
    st = RunStore(str(tmp_path), "s"); saved(st, "same", merges=False, events=[report()])
    old = RunStore(str(tmp_path), "s"); old.dir = st.archive_root() / "001"
    saved(old, "same")
    data = metrics.observations(st, now="2026-10-13T12:00:00Z")
    assert len(data["runs"]) == 1
    assert data["runs"][0]["merges"] == []
    assert data["runs"][0]["merge_source"] is False


@pytest.mark.parametrize("event", [report(value={"repo": "/repo", "merges": [{}]}),
    report(ts="2026-10-13T15:00:00Z"), report(value={"merges": []})])
def test_invalid_merge_evidence_marks_coverage_unknown(tmp_path, event):
    st = RunStore(str(tmp_path), "s"); saved(st, "current", events=[event])
    row = metrics.observations(st, now="2026-10-13T12:00:00Z")["runs"][0]
    assert not row["merge_complete"] and not row["merges"]


def test_legacy_missing_truncated_mismatched_and_invalid_times(tmp_path, monkeypatch):
    st = RunStore(str(tmp_path), "s")
    saved(st, "current", events=[{"event": "entered", "text": "x" * 70000}, report()])
    assert not metrics.observations(st)["runs"][0]["merge_complete"]
    st.journal_path.write_text('{"event":"started","run":"new-slot"}\n')
    assert not st.metrics_snapshot()["events"]
    st.journal_path.unlink()
    assert not st.metrics_snapshot()["complete"]
    saved(st, "bad", start=None, end=None)
    assert metrics.observations(st)["unavailable"] == 1
    assert metrics.observations(st)["runs"][0]["completed_at"] is None
    old = RunStore(str(tmp_path), "s"); old.dir = st.archive_root() / "001"
    old.dir.mkdir(parents=True)
    old.state_path.write_text("broken")
    assert metrics.observations(st)["unavailable"] == 2


def test_run_limit_current_and_newest_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "RUN_LIMIT", 3)
    st = RunStore(str(tmp_path), "s"); saved(st, "current")
    for i in range(5):
        old = RunStore(str(tmp_path), "s"); old.dir = st.archive_root() / str(i)
        saved(old, f"old-{i}")
    data = metrics.observations(st)
    assert data["truncated"] and len(data["runs"]) == 3
    assert {r["run"] for r in data["runs"]} == {"current", "old-3", "old-4"}


def test_checkpoint_authority_consistency_caps_and_no_full_read(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    runtime.start(definition(False), cwd=str(tmp_path), scope="s", caps={})
    st = RunStore(str(tmp_path), "s")
    st.append([{"event": "loop_iteration"} for _ in range(30)])
    snap = st.metrics_snapshot(limit=8)
    assert len(snap["events"]) == 8 and not snap["complete"]
    st.state_path.write_text("broken projection")
    st.journal_path.write_text("broken projection")
    monkeypatch.setattr(st, "journal", lambda *a: pytest.fail("full journal"))
    data = metrics.observations(st)
    assert data["unavailable"] == 0 and len(data["runs"]) == 1
    assert data["runs"][0]["completed_at"] is None
    with sqlite3.connect(st.checkpoint_path) as conn:
        conn.execute("UPDATE events SET sha='bad' WHERE id=1")
    assert metrics.observations(st)["unavailable"] == 1


def test_live_and_failed_duration_and_empty(tmp_path):
    st = RunStore(str(tmp_path), "s")
    saved(st, "live", status="running", end=None)
    row = metrics.observations(st, now="2026-10-13T12:00:00Z")["runs"][0]
    assert row["ended"] == "2026-10-13T12:00:00Z" and row["completed_at"] is None
    saved(st, "failed", status="failed")
    row = metrics.observations(st)["runs"][0]
    assert row["duration_s"] == 3600 and row["completed_at"] is None
    st.state_path.unlink()
    assert metrics.observations(st)["runs"] == []


@pytest.mark.parametrize("broken", ["old_ir", "invalid_chart"])
def test_archived_checkpoint_ir_failure_isolated_without_json_fallback(tmp_path, broken):
    st = RunStore(str(tmp_path), "s"); saved(st, "current", merges=False)
    old = RunStore(str(tmp_path), "s"); old.dir = st.archive_root() / "001"
    saved(old, "old", merges=False)
    # Checksum-valid but unreadable IR must not use the valid JSON projection.
    chart = json.loads(old.chart_path.read_text())
    if broken == "old_ir":
        chart["ir_version"] = 0
    else:
        chart.pop("root")
    checkpoint.commit(old.checkpoint_path, old.state_path.read_text(), json.dumps(chart),
                      [], legacy=[], receipts={})
    data = metrics.observations(st, now="2026-10-13T12:00:00Z")
    assert data["unavailable"] == 1
    assert [r["run"] for r in data["runs"]] == ["current"]


def aged(st, seconds=60):
    """Back-date the slot files past the racy window so the cache may keep them."""
    import os
    import time
    then = time.time() - seconds
    for path in (st.checkpoint_path, st.state_path, st.chart_path, st.journal_path):
        if path.exists():
            os.utime(path, (then, then))


def counting(monkeypatch):
    reads = []
    real = metrics._read_facts
    monkeypatch.setattr(metrics, "_read_facts", lambda s: (reads.append(s.dir), real(s))[1])
    return reads


def test_saved_runs_read_once_until_their_files_change(tmp_path, monkeypatch):
    # claunch-02w8s: the run page re-read every archive on each 5 s poll.
    st = RunStore(str(tmp_path), "s"); saved(st, "current", events=[report()])
    old = RunStore(str(tmp_path), "s"); old.dir = st.archive_root() / "001"
    saved(old, "old", start="2026-10-13T09:00:00Z", end="2026-10-13T10:00:00Z",
          events=[report(sha="c", ts="2026-10-13T09:30:00Z")])
    aged(st); aged(old)
    reads = counting(monkeypatch)
    first = metrics.observations(st, now="2026-10-13T12:00:00Z")
    assert len(reads) == 2
    again = metrics.observations(st, now="2026-10-13T12:00:00Z")
    assert len(reads) == 2 and again == first
    # Relative to the observation time, not cached with the facts.
    later = metrics.observations(st, now="2026-10-13T09:45:00Z")
    assert later["unavailable"] == 2 and len(reads) == 2
    saved(old, "old", start="2026-10-13T09:00:00Z", end="2026-10-13T10:00:00Z")
    aged(old, 30)
    data = metrics.observations(st, now="2026-10-13T12:00:00Z")
    assert len(reads) == 3 and reads[-1] == old.dir
    assert [len(r["merges"]) for r in data["runs"]] == [1, 0]


def test_fresh_files_and_unreadable_runs(tmp_path, monkeypatch):
    st = RunStore(str(tmp_path), "s"); saved(st, "current")
    reads = counting(monkeypatch)
    metrics.observations(st); metrics.observations(st)
    assert len(reads) == 2, "a file written within the racy window is not cached"
    old = RunStore(str(tmp_path), "s"); old.dir = st.archive_root() / "001"
    old.dir.mkdir(parents=True)
    old.state_path.write_text("broken")
    aged(st); aged(old)
    reads.clear()
    assert metrics.observations(st)["unavailable"] == 1
    assert metrics.observations(st)["unavailable"] == 1
    assert len(reads) == 2, "an unreadable archive is remembered as unreadable"
    # No exception object is cached: re-raising one per poll grew its
    # traceback (and the frames it holds) by one entry each time.
    for _ in range(20):
        metrics.observations(st)
    assert not any(isinstance(v, BaseException) for _, v in metrics._facts_cache.values())
