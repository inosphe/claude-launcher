"""Crash boundaries, damaged projections and explicit recovery of a run."""
import json
import os
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import pytest

from claude_launcher.cflow2 import checkpoint, frontend, ir, runtime, store


@pytest.fixture
def run_store(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = frontend.compile_text("cflow: 2\nname: durable\nnodes:\n  work: {prompt: work}\n"
                                  "body: [work]\n")
    runtime.start(chart, cwd=str(tmp_path), scope="durable", caps={}, now=1000)
    return store.RunStore(str(tmp_path), "durable")


def test_state_and_events_survive_destroyed_json(run_store):
    st = run_store
    before = st.load_state()
    events = st.journal()
    st.state_path.write_bytes(b"\0" * 123101)
    st.chart_path.write_bytes(b"\0" * 10)
    st.journal_path.write_bytes(b'{"partial":')
    assert st.load_state() == before
    assert st.load_chart().name == "durable"
    assert st.journal() == events
    assert st.recovery()["differences"] == ["state.json", "chart.json", "journal.jsonl"]


def test_commit_failure_rolls_back_state_and_events(run_store, monkeypatch):
    st = run_store
    old = st.load_state()
    old_events = st.journal()
    state = dict(old, test_marker="new")
    original = checkpoint.digest

    def fail_on_state(text):
        if '"test_marker"' in text:
            raise OSError("disk full")
        return original(text)

    monkeypatch.setattr(checkpoint, "digest", fail_on_state)
    with st.lock(), pytest.raises(store.StoreError, match="disk full"):
        st.commit(state, [{"event": "must_rollback"}])
    assert st.load_state() == old
    assert st.journal() == old_events


def test_projection_failure_does_not_lose_committed_operation(run_store, monkeypatch):
    st = run_store
    state = dict(st.load_state(), test_marker="committed")

    def fail(*args):
        raise OSError("export denied")

    monkeypatch.setattr(store, "_atomic_write", fail)
    with st.lock():
        st.commit(state, [{"event": "committed"}])
    assert st.load_state()["test_marker"] == "committed"
    assert st.journal()[-1]["event"] == "committed"
    assert "state.json" in st.recovery()["differences"]


def test_corrupt_checkpoint_never_falls_back_to_json(run_store):
    st = run_store
    assert json.loads(st.state_path.read_text(encoding="utf-8"))["run_id"]
    st.checkpoint_path.write_bytes(b"\0" * st.checkpoint_path.stat().st_size)
    with pytest.raises(store.StoreError, match="checkpoint"):
        st.load_state()
    with pytest.raises(store.StoreError, match="checkpoint"):
        st.recovery(apply=True, expected=1)


def test_recovery_preserves_corrupt_bytes_and_halts(run_store):
    st = run_store
    st.state_path.write_bytes(b"\0" * 123101)
    st.write_receipt("d1", {"outcome": "ok"})
    st.receipt_path("d1").write_bytes(b"broken")
    preview = st.recovery()
    assert st.state_path.read_bytes() == b"\0" * 123101
    result = st.recovery(apply=True, expected=preview["revision"])
    evidence = Path(result["evidence"])
    assert (evidence / "state.json").read_bytes() == b"\0" * 123101
    assert (evidence / "receipts" / "d1.json").read_bytes() == b"broken"
    assert json.loads(st.receipt_path("d1").read_text())["outcome"] == "ok"
    assert st.recovery()["differences"] == []
    status = runtime.status(cwd=str(st.cwd), scope=st.scope)
    assert status["halted"] == ["storage-recovery"]
    with pytest.raises(runtime.FlowError, match="halted"):
        runtime.report("work", "ok", cwd=str(st.cwd), scope=st.scope)
    runtime.release_halt("storage-recovery", cwd=str(st.cwd), scope=st.scope)
    runtime.report("work", "ok", cwd=str(st.cwd), scope=st.scope)
    assert st.load_state()["status"] == "done"


def test_recovery_rejects_stale_revision(run_store):
    st = run_store
    revision = st.recovery()["revision"]
    with st.lock():
        st.save_state(dict(st.load_state(), changed=True))
    with pytest.raises(store.StoreError, match="changed"):
        st.recovery(apply=True, expected=revision)
    assert not (st.dir / "recovery").exists()


def test_legacy_migration_is_strict_and_keeps_history(run_store):
    st = run_store
    old = st.load_state()
    events = st.journal()
    st.checkpoint_path.unlink()
    with st.lock():
        st.commit(dict(old, migrated=True), [{"event": "migration"}])
    assert st.load_state()["migrated"] is True
    assert st.journal()[:-1] == events
    assert st.journal()[-1]["event"] == "migration"


def test_legacy_corruption_is_not_imported(run_store):
    st = run_store
    old = st.load_state()
    st.checkpoint_path.unlink()
    st.state_path.write_bytes(b"\0" * 10)
    with pytest.raises(store.StoreError, match="corrupt"):
        st.save_state(old)
    with pytest.raises(store.StoreError, match="no durable checkpoint"):
        st.recovery()
    assert not st.checkpoint_path.exists()


def test_bad_legacy_journal_refuses_migration(run_store):
    st = run_store
    st.checkpoint_path.unlink()
    st.journal_path.write_bytes(b'{"event":')
    with pytest.raises(store.StoreError, match="cannot commit"):
        st.save_state(st.load_state())
    assert not st.checkpoint_path.exists()


@pytest.mark.parametrize("boundary", ["in_transaction", "after_commit"])
def test_process_death_at_transaction_boundary(run_store, boundary):
    st = run_store
    before = st.load_state()
    program = r'''
import os, sys
from pathlib import Path
from claude_launcher.cflow2 import checkpoint
p = Path(sys.argv[1])
snap = checkpoint.read(p)
if sys.argv[2] == "in_transaction":
    with checkpoint.connect(p) as db:
        db.execute("PRAGMA cache_size=1")
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE snapshot SET state=? WHERE id=1", ('partial' * 100000,))
        db.execute("INSERT INTO events(body,sha) VALUES ('partial','bad')")
        os._exit(73)
else:
    import json
    state = json.loads(snap['state'])
    state['crash_marker'] = True
    checkpoint.commit(p, checkpoint.encode(state), snap['chart'],
                      [{'event': 'crash_committed'}], legacy=[], receipts={})
    os._exit(73)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(store.__file__).parents[2]))
    child = subprocess.run([sys.executable, "-c", program, str(st.checkpoint_path), boundary],
                           env=env, capture_output=True, timeout=30)
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    if boundary == "in_transaction":
        assert st.load_state() == before
        assert all(e.get("event") != "crash_committed" for e in st.journal())
    else:
        assert st.load_state()["crash_marker"] is True
        assert st.journal()[-1]["event"] == "crash_committed"


def test_archive_includes_checkpoint_and_no_old_generation_leaks(run_store):
    st = run_store
    with st.lock():
        target = st.archive()
    assert (target / checkpoint.FILE).is_file()
    assert not st.exists()


def test_checkpoint_content_checksum_detects_valid_json_tampering(run_store):
    st = run_store
    with checkpoint.connect(st.checkpoint_path) as db:
        db.execute("UPDATE snapshot SET state='{}' WHERE id=1")
    with pytest.raises(store.StoreError, match="checksum"):
        st.load_state()


def test_recovery_prevents_daemon_dispatch_and_trigger_claims(run_store, monkeypatch):
    from claude_launcher.daemon.cflow2_clock import Cflow2Clock

    st = run_store
    state = st.load_state()
    state["triggers"] = [{"id": "pending", "do": "briefing"}]
    with st.lock():
        st.save_state(state)
    st.recovery(apply=True, expected=st.recovery()["revision"])
    assert runtime.claim_triggers(str(st.cwd), st.scope) == []
    clock = Cflow2Clock(None, threaded=False)
    calls = []
    monkeypatch.setattr(clock, "_claim", lambda *a: calls.append("claim"))
    monkeypatch.setattr(runtime, "wait_tick", lambda *a, **k: calls.append("wait"))
    clock.tick_slot(str(st.cwd), st.scope, 1001, [])
    assert calls == []


def test_fsync_failure_does_not_publish_a_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}')

    def fail(fd):
        raise OSError("flush failed")

    monkeypatch.setattr(store.os, "fsync", fail)
    with pytest.raises(OSError, match="flush failed"):
        store._atomic_write(path, '{"new": true}')
    assert json.loads(path.read_text()) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_checkpoint_connections_enable_durable_sync(run_store):
    with checkpoint.connect(run_store.checkpoint_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert db.execute("PRAGMA synchronous").fetchone() == (3,)
        assert db.execute("PRAGMA fullfsync").fetchone() == (1,)


def test_checkpoint_rejects_nonobject_state_with_matching_checksum(run_store):
    st = run_store
    with checkpoint.connect(st.checkpoint_path) as db:
        db.execute("UPDATE snapshot SET state=?,state_sha=? WHERE id=1",
                   ("[]", checkpoint.digest("[]")))
    with pytest.raises(store.StoreError, match="not an object"):
        st.load_state()


@pytest.mark.parametrize("boundary", ["before_schema", "after_schema",
                                     "before_publish", "after_publish"])
@pytest.mark.parametrize("failure", ["exception", "process_death"])
def test_first_import_failure_and_retry(run_store, boundary, failure):
    st = run_store
    old = st.load_state()
    events = st.journal()
    st.write_receipt("legacy", {"outcome": "ok"})
    st.checkpoint_path.unlink()  # a healthy legacy fixture
    files = [st.state_path, st.chart_path, st.journal_path, st.receipt_path("legacy")]
    original = {p: p.read_bytes() for p in files}
    program = r'''
import os, sys
from claude_launcher.cflow2 import checkpoint, store
st = store.RunStore(sys.argv[1], sys.argv[2])
boundary, failure = sys.argv[3:]
schema, publish = checkpoint._schema, checkpoint._publish
def stop():
    if failure == "process_death":
        os._exit(73)
    raise OSError("injected import failure")
def schema_boundary(conn):
    if boundary == "before_schema":
        stop()
    schema(conn)
    if boundary == "after_schema":
        stop()
def publish_boundary(tmp, dest):
    if boundary == "before_publish":
        stop()
    publish(tmp, dest)
    if boundary == "after_publish":
        stop()
checkpoint._schema, checkpoint._publish = schema_boundary, publish_boundary
try:
    # This isolated fixture has no concurrent writer; no abandoned slot lock.
    st.commit(dict(st.load_state(), imported=True), [{"event": "imported"}])
except store.StoreError:
    sys.exit(74)
sys.exit(99)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(store.__file__).parents[2]))
    child = subprocess.run([sys.executable, "-c", program, str(st.cwd), st.scope,
                            boundary, failure], env=env, capture_output=True, timeout=30)
    assert child.returncode == (73 if failure == "process_death" else 74), child.stderr
    assert {p: p.read_bytes() for p in files} == original
    if boundary == "after_publish":
        assert st.load_state()["imported"] is True
        assert st.journal()[-1]["event"] == "imported"
        assert "state.json" in st.recovery()["differences"]
        st.recovery(apply=True, expected=st.recovery()["revision"])
        assert store.recovery_held(st.load_state())
    else:
        assert not st.checkpoint_path.exists()
        assert st.load_state() == old
        assert st.journal() == events
        with st.lock():
            st.commit(dict(old, retried=True), [{"event": "retried"}])
        assert st.load_state()["retried"] is True
        assert st.journal()[:-1] == events
        assert st.journal()[-1]["event"] == "retried"
        assert st.recovery()["differences"] == []
    snap = checkpoint.read(st.checkpoint_path, full=True)
    assert json.loads(snap["receipts"]["legacy.json"]) == {"outcome": "ok"}


@pytest.mark.parametrize("boundary", ["fsync", "rename"])
def test_first_import_publication_io_failure_keeps_legacy(run_store, monkeypatch, boundary):
    st = run_store
    old, events = st.load_state(), st.journal()
    st.checkpoint_path.unlink()

    def fail(*args):
        raise OSError("publication failure")

    with monkeypatch.context() as patch:
        if boundary == "fsync":
            patch.setattr(checkpoint.os, "fsync", fail)
        else:
            patch.setattr(checkpoint.atomic, "replace", fail)
        with st.lock(), pytest.raises(store.StoreError, match="publication failure"):
            st.commit(dict(old, imported=True), [{"event": "imported"}])
    assert not st.checkpoint_path.exists()
    assert not list(st.dir.glob(checkpoint.FILE + ".*.tmp*"))
    assert st.load_state() == old
    assert st.journal() == events
    with st.lock():
        st.commit(dict(old, retried=True), [{"event": "retried"}])
    assert st.load_state()["retried"] is True
    assert st.recovery()["differences"] == []


def test_recovery_between_auto_round_check_and_start_keeps_hold(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    chart = frontend.compile_text(
        "cflow: 2\nname: recurring\nrecur: {auto: true, min_gap: 0}\n"
        "nodes:\n  work: {prompt: work}\nbody: [work]\n")
    cwd, scope = str(tmp_path), "durable"
    runtime.start(chart, cwd=cwd, scope=scope, caps={}, now=1000)
    runtime.report("work", "ok", cwd=cwd, scope=scope, now=1001)
    st = store.RunStore(cwd, scope)
    old_id = st.load_state()["run_id"]
    original = runtime._next_round_chart

    def recover_before_start(slot, state):
        result = original(slot, state)
        slot.recovery(apply=True, expected=slot.recovery()["revision"])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_next_round_chart", recover_before_start)
        assert runtime.start_next_round(cwd, scope, now=1002) is None
    assert st.load_state()["run_id"] == old_id
    assert store.recovery_held(st.load_state())
    assert st.archived() == []
    runtime.release_halt("storage-recovery", cwd=cwd, scope=scope)
    assert runtime.start_next_round(cwd, scope, now=1003)["round"] == 2
    assert st.load_state()["run_id"] != old_id


# -- the verified read cache (checkpoint.read_state/read_chart) -------------

def test_cached_state_read_sees_another_process_commit(run_store):
    st = run_store
    assert "other_marker" not in st.load_state()
    st.load_state()  # a second read of the same revision comes from the cache
    program = r'''
import json, sys
from pathlib import Path
from claude_launcher.cflow2 import checkpoint
p = Path(sys.argv[1])
snap = checkpoint.read(p)
state = json.loads(snap['state'])
state['other_marker'] = 1
checkpoint.commit(p, checkpoint.encode(state), snap['chart'], [], legacy=[], receipts={})
'''
    env = dict(os.environ, PYTHONPATH=str(Path(store.__file__).parents[2]))
    subprocess.run([sys.executable, "-c", program, str(st.checkpoint_path)],
                   env=env, check=True, timeout=30)
    assert st.load_state()["other_marker"] == 1


def test_cached_read_detects_tampering_that_keeps_the_revision(run_store):
    st = run_store
    st.load_state()
    st.load_chart()
    with checkpoint.connect(st.checkpoint_path) as db:
        db.execute("UPDATE snapshot SET state='{}' WHERE id=1")
    with pytest.raises(store.StoreError, match="checksum"):
        st.load_state()


def test_state_read_does_not_verify_chart_and_chart_read_does(run_store):
    st = run_store
    st.load_chart()
    with checkpoint.connect(st.checkpoint_path) as db:
        db.execute("UPDATE definition SET body=body||' ' WHERE id=1")
    assert st.load_state()["run_id"]
    with pytest.raises(store.StoreError, match="checksum"):
        st.load_chart()


def test_cached_state_is_a_new_object_per_read(run_store):
    st = run_store
    first = st.load_state()
    first["inst"].clear()
    first["scribble"] = True
    second = st.load_state()
    assert "scribble" not in second and second["inst"]


def test_cache_skips_verification_of_an_unchanged_revision(run_store, monkeypatch):
    st = run_store
    st.load_state()
    st.load_chart()
    calls = []
    original = checkpoint.digest
    monkeypatch.setattr(checkpoint, "digest", lambda text: calls.append(1) or original(text))
    for _ in range(3):
        st.load_state()
        st.load_chart()
    assert calls == []
    with st.lock():
        st.commit(dict(st.load_state(), marker=1), [])
    calls.clear()
    st.load_state()
    assert len(calls) == 1  # the new state only; the chart is verified when read


def _count_parses(monkeypatch) -> list:
    monkeypatch.setattr(checkpoint, "_charts", OrderedDict(), raising=False)
    calls = []
    original = ir.Chart.from_json
    monkeypatch.setattr(ir.Chart, "from_json",
                        classmethod(lambda cls, text: calls.append(1) or original(text)))
    return calls


def test_state_commit_reverifies_the_chart_without_parsing_it(run_store, monkeypatch):
    st = run_store
    parses = _count_parses(monkeypatch)
    first = st.load_chart()
    assert parses == [1]
    with st.lock():
        st.commit(dict(st.load_state(), marker=1), [])
    digests = []
    original = checkpoint.digest
    monkeypatch.setattr(checkpoint, "digest", lambda text: digests.append(1) or original(text))
    assert st.load_chart() is first
    assert parses == [1] and len(digests) == 1


def test_a_pass_over_more_slots_than_the_path_cache_keeps_the_chart(run_store, monkeypatch):
    # The clock reads every slot's state each pass; more slots than CACHE_MAX
    # pushed the live run's chart out before its next pass (2026-10-26).
    st = run_store
    parses = _count_parses(monkeypatch)
    st.load_chart()
    parses.clear()
    for n in range(checkpoint.CACHE_MAX * 3):
        checkpoint._remember(Path(f"other-{n}"), "state", (n,), "{}")
    st.load_chart()
    assert parses == []


def test_chart_tampered_after_a_state_commit_is_still_refused(run_store, monkeypatch):
    st = run_store
    _count_parses(monkeypatch)
    st.load_chart()
    with st.lock():
        st.commit(dict(st.load_state(), marker=1), [])
    with checkpoint.connect(st.checkpoint_path) as db:
        db.execute("UPDATE definition SET body=body||' ' WHERE id=1")
    with pytest.raises(store.StoreError, match="checksum"):
        st.load_chart()


def test_save_that_changes_only_the_updated_stamp_writes_nothing(run_store):
    st = run_store

    def revision():
        return checkpoint.read(st.checkpoint_path)["revision"]

    before, stamp = revision(), st.load_state()["updated"]
    with st.lock():
        r = runtime.Run.load(st, 5000)
        r.state["updated"] = "2099-01-01T00:00:00+00:00"
        r.save()
    assert revision() == before and r.state["updated"] == stamp
    assert st.load_state()["updated"] == stamp
    with st.lock():
        r = runtime.Run.load(st, 5000)
        r.state["marker"] = 1
        r.save()
    assert revision() == before + 1
    assert st.load_state()["updated"] == r.state["updated"] != "2099-01-01T00:00:00+00:00"
    with st.lock():
        r = runtime.Run.load(st, 5000)
        r.events.append({"event": "noted"})
        r.save()
    assert revision() == before + 2 and st.journal()[-1]["event"] == "noted"
