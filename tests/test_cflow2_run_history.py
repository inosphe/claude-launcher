"""Read-only v2 history: saved run identity and explicit reload evidence."""
import json
import sqlite3

import pytest

from claude_launcher.cflow2 import runtime
from claude_launcher.cflow2.frontend import compile_text
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_web


def chart(recur=""):
    return compile_text("cflow: 2\nname: history-demo\n" + recur +
                        "nodes:\n  work: {prompt: do it}\nbody: work\n",
                        resolve_include=lambda _: None)


@pytest.mark.parametrize("recur,mode", [
    ("", None), ("recur: true\n", False),
    ("recur: {auto: true, min_gap: 0}\n", True),
])
def test_detail_uses_running_snapshot_and_one_run(home, tmp_path, monkeypatch, recur, mode):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    cwd = str(tmp_path)
    started = runtime.start(chart(recur), cwd=cwd, scope="owner", caps={})
    st = RunStore(cwd, "owner")
    # Same-run loop/forced movement observations are never extra history rows.
    st.append([{"event": "loop_iteration", "round": n} for n in range(250)] +
              [{"event": "forced_move", "node": "work"}])
    before = st.load_state()
    detail = cflow2_web.run_detail(cwd, "owner")
    snap = detail["chart"]["meta"].get("recur")
    assert (snap.get("auto") if snap else None) is mode
    rows = detail["history"]["runs"]
    assert len(rows) == 1 and rows[0]["run"] == started["run"]
    assert rows[0]["transition"]["event"] == "started"
    assert rows[0]["transition"]["previous"] is None
    assert rows[0]["transition"]["reloaded"] is None
    assert st.load_state() == before
    assert not st.archive_root().exists()


@pytest.mark.parametrize("reloaded", [True, False])
def test_auto_recur_history_keeps_early_event_and_previous(home, tmp_path, monkeypatch, reloaded):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    cwd = str(tmp_path)
    definition = chart("recur: {auto: true, min_gap: 0}\n")
    first = runtime.start(definition, cwd=cwd, scope="owner", caps={}, now=1000)
    runtime.report("work", cwd=cwd, scope="owner", now=1001)
    if reloaded:
        monkeypatch.setattr(runtime, "load_definition", lambda *a: definition)
    else:
        def missing(*a):
            raise runtime.FlowError("definition missing")
        monkeypatch.setattr(runtime, "load_definition", missing)
    nxt = runtime.start_next_round(cwd, "owner", now=1002)
    st = RunStore(cwd, "owner")
    st.append([{"event": "entered", "node": "standby-loop"} for _ in range(250)])
    assert all(e.get("event") != "round_auto_started" for e in st.journal(200))
    # History's start query does not call the full-journal/receipt reader.
    monkeypatch.setattr(st, "journal", lambda *a: pytest.fail("full journal read"))
    history = cflow2_web.run_history(st, st.load_state())
    current, old = history["runs"]
    assert current["run"] == nxt["run"] != old["run"] == first["run"]
    assert current["round"] == 2 and current["current"] is True
    assert current["transition"] == {
        "event": "round_auto_started", "at": st.start_events()[-1]["ts"],
        "previous": first["run"], "reloaded": reloaded,
    }
    assert old["status"] == "done" and old["finished"]
    assert old["current"] is False


def test_manual_recur_start_does_not_invent_reload_or_previous(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    cwd = str(tmp_path)
    definition = chart("recur: true\n")
    first = runtime.start(definition, cwd=cwd, scope="owner", caps={})
    runtime.report("work", cwd=cwd, scope="owner")
    assert runtime.start_next_round(cwd, "owner") is None
    second = runtime.start(definition, cwd=cwd, scope="owner", caps={})
    history = cflow2_web.run_detail(cwd, "owner")["history"]["runs"]
    assert [r["run"] for r in history] == [second["run"], first["run"]]
    assert history[0]["round"] == 2
    assert history[0]["transition"]["event"] == "started"
    assert history[0]["transition"]["reloaded"] is None
    assert history[0]["transition"]["previous"] is None


def test_legacy_missing_records_bounded_archives_and_no_context(tmp_path):
    st = RunStore(str(tmp_path), "owner")
    st.dir.mkdir(parents=True)
    state = {"run_id": "current", "round": 1, "status": "running",
             "data": {"secret": "do not expose"}}
    st.state_path.write_text(json.dumps(state))
    for i in range(13):
        path = st.archive_root() / f"20261013-{i:03}"
        path.mkdir(parents=True)
        (path / "state.json").write_text(json.dumps({**state, "run_id": f"old-{i}"}))
    # Most recent corrupt record is counted, never replaced by guesses.
    (path / "state.json").write_text("broken")
    history = cflow2_web.run_history(st, state)
    assert len(history["runs"]) == 9
    assert history["limit"] == 10 and history["truncated"] is True
    assert history["unavailable"] == 1
    assert history["runs"][0]["transition"] == {
        "event": None, "at": None, "previous": None, "reloaded": None}
    assert "secret" not in json.dumps(history)
    assert {r["run"] for r in history["runs"][1:]} == {f"old-{i}" for i in range(4, 12)}


def test_legacy_start_scan_is_bounded_and_ignores_partial_line(tmp_path):
    st = RunStore(str(tmp_path), "owner")
    st.dir.mkdir(parents=True)
    event = {"event": "started", "run": "legacy", "ts": "start"}
    st.journal_path.write_bytes(json.dumps(event).encode() + b"\n" + b"x" * (1024 * 1024))
    assert st.start_events() == [event]
    st.journal_path.write_bytes(b"x" * (1024 * 1024) + b"\n" +
                                b'{"event":"round_auto_started","reloaded":true}\n')
    assert st.start_events() == []


def test_checkpoint_evidence_wins_over_stale_json(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    runtime.start(chart(), cwd=str(tmp_path), scope="owner", caps={})
    st = RunStore(str(tmp_path), "owner")
    st.journal_path.write_text('{"event":"round_auto_started","reloaded":true}\n')
    row = cflow2_web.run_history(st, st.load_state())["runs"][0]
    assert row["transition"]["event"] == "started"
    assert row["transition"]["reloaded"] is None


def test_checkpoint_scan_limit_does_not_invent_late_reload(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    runtime.start(chart(), cwd=str(tmp_path), scope="owner", caps={})
    st = RunStore(str(tmp_path), "owner")
    st.append([{"event": "entered"} for _ in range(2050)] +
              [{"event": "round_auto_started", "reloaded": True}])
    row = cflow2_web.run_history(st, st.load_state())["runs"][0]
    assert row["transition"]["event"] == "started"
    assert row["transition"]["reloaded"] is None


def test_corrupt_checkpoint_start_stays_unknown_without_json_fallback(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    runtime.start(chart(), cwd=str(tmp_path), scope="owner", caps={})
    st = RunStore(str(tmp_path), "owner")
    with sqlite3.connect(st.checkpoint_path) as conn:
        conn.execute("UPDATE events SET sha='corrupt' WHERE id=1")
    row = cflow2_web.run_history(st, st.load_state())["runs"][0]
    assert row["transition"]["event"] is None
    assert row["transition"]["reloaded"] is None
