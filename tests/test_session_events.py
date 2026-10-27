"""Mechanical events survive restarts and share the observer timeline."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import observer, session_events
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession


def test_history_is_bounded_durable_and_isolated_by_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(session_events, "LIMIT", 3)
    session = SimpleNamespace(sdef=SimpleNamespace(name="s1"), created_at="first")
    events = session_events.Events(tmp_path)
    for action in ("pause", "resume", "kill", "archive"):
        events.record(session, action, action)
    restored = session_events.Events(tmp_path)
    assert [e["kind"] for e in restored.rows(session)] == ["resume", "kill", "archive"]
    assert restored.rows(session) == events.rows(session)
    session.created_at = "second"
    assert restored.rows(session) == []


def test_observer_merges_all_origins_by_instant_while_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(observer.paths, "daemon_dir", lambda: tmp_path)
    session = SimpleNamespace(sdef=SimpleNamespace(name="s1", cwd="/repo"),
                              created_at="first", exited=True, info=lambda: {})
    events = session_events.Events(tmp_path)
    manager = SimpleNamespace(list=lambda: [session], events=events)
    service = observer.Observer(manager, SimpleNamespace(meshes_for_session=lambda _: []))
    mechanical = events.record(session, "archive", "세션 보관")
    mechanical["at"] = "2026-09-18T09:30:00+09:00"
    service.data["sessions"]["s1"] = {"events": [
        {"id": "observed", "at": "2026-09-18T01:00:00Z", "text": "observed"}]}
    monkeypatch.setattr(service.reports, "rows", lambda _: [
        {"id": "direct", "at": "2026-09-18T00:00:00+00:00", "text": "direct", "state": "done"}])
    result = service.snapshot()
    assert not result["enabled"]
    assert [e["id"] for e in result["sessions"][0]["events"]] == ["direct", mechanical["id"], "observed"]
    assert mechanical["needs_action"] is False
    assert mechanical["origin"] == "daemon"


def test_control_events_only_follow_success_and_preserve_resume_history(home, monkeypatch):
    mgr = SessionManager(idle_threshold=1, scrollback=100, restore_default=True)
    old = DeadSession(SessionDef(name="s1", cwd="/repo"), paused_at="paused")
    mgr._sessions["s1"] = old
    monkeypatch.setattr(mgr, "persist", lambda: None)

    def fail(*args, **kwargs):
        raise RuntimeError("launch failed")

    monkeypatch.setattr(mgr, "create", fail)
    with pytest.raises(RuntimeError):
        mgr.respawn("s1")
    assert mgr.events.rows(old) == []
    assert mgr.get("s1") is old

    def create(sdef, **kwargs):
        new = DeadSession(sdef, created_at=kwargs["created_at"])
        mgr._sessions[sdef.name] = new
        return new

    monkeypatch.setattr(mgr, "create", create)
    resumed = mgr.respawn("s1")
    respawned = mgr.respawn("s1")
    mgr.archive("s1")
    mgr.archive("s1")
    mgr.kill("s1")
    mgr.pause("s1")
    assert [e["kind"] for e in mgr.events.rows(respawned)] == ["resume", "respawn", "archive"]
    assert mgr.events.rows(resumed) == mgr.events.rows(old)


def test_worktree_event_contains_old_and_new_location_only_after_success(home, tmp_path, monkeypatch):
    mgr = SessionManager(idle_threshold=1, scrollback=100, restore_default=True)
    old = DeadSession(SessionDef(name="s1", harness="codex", cwd="/old"))
    mgr._sessions["s1"] = old

    def fail(*args):
        raise RuntimeError("launch failed")

    monkeypatch.setattr(mgr, "_recreate", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(mgr.migrate("s1", str(tmp_path)))
    assert mgr.events.rows(old) == []
    monkeypatch.setattr(mgr, "_recreate", lambda name, session, definition:
                        DeadSession(definition, created_at=session.created_at))
    moved, carried = asyncio.run(mgr.migrate("s1", str(tmp_path)))
    event = mgr.events.rows(moved)[-1]
    assert event["kind"] == "worktree" and not carried
    assert event["details"] == {"previous": "/old", "current": str(tmp_path), "transcript_moved": False}


def test_history_write_failure_does_not_change_control_result(tmp_path, monkeypatch, caplog):
    events = session_events.Events(tmp_path)
    session = SimpleNamespace(sdef=SimpleNamespace(name="s1"), created_at="first")
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(session_events.atomic, "replace", fail)
    event = events.record(session, "kill", "세션 종료 요청")
    assert events.rows(session) == [event]
    assert "could not persist session event" in caplog.text


def _both_miss_the_cache_together(events, session, action_a, action_b):
    """Drive two ``record`` calls so both must miss the cache before either writes.

    The collision is the **cache miss**, so merely starting two threads does not
    reach it reliably: whichever finishes first populates the cache, and the
    second is then a plain ``list.append``, which the GIL already makes safe.
    The miss is held open at the one place both writers must pass before either
    can install a list -- the file read inside :meth:`Events.rows`.

    ``_path`` is that read's first step, and each ``record`` calls it twice
    (once through ``rows`` to read, once to name the file it writes). So the
    gate is per thread and only on its **first** call. Both threads wait there
    until both have arrived.

    With the lock this rendezvous is unreachable: the first writer holds
    ``_lock`` across the read *and* the write, so the second never reaches the
    read until the first is done. Its wait then times out and the barrier
    breaks, which is the pass case -- both rows end up in the file, because the
    two records were serialized. Without the lock both threads arrive, each
    reads the file, each gets its own list, and the second write drops the
    first writer's row -- which is the red case this test exists to show.
    """
    original = session_events.Events._path
    arrived = threading.Barrier(2, timeout=1.0)
    seen = {}
    failures = []

    def first_call_only(self, key):
        ident = threading.get_ident()
        seen[ident] = seen.get(ident, 0) + 1
        if seen[ident] == 1:
            try:
                arrived.wait()
            except threading.BrokenBarrierError:
                pass          # serialized by the lock -- see the docstring
        return original(self, key)

    def write(action):
        try:
            events.record(session, action, action)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
            failures.append(exc)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_events.Events, "_path", first_call_only)
        threads = [threading.Thread(target=write, args=(action,))
                   for action in (action_a, action_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads), "a writer never returned"
    return failures


def test_two_threads_recording_the_first_event_of_one_session_keep_both(
        tmp_path, monkeypatch):
    """The daemon records the same session from the loop and a worker thread.

    ``cflow2_exec.spawn_session``/``_spawn_fork`` report through
    ``spawn_events`` on ``pools.background``, and the loop records control
    events through ``manager``. For a session's **first** event both threads
    miss the cache, each reads the file into its own list, and whichever writes
    second writes a list the other's row is not in -- so one event survives.
    Once both share the cached list ``list.append`` is atomic under the GIL;
    the miss is the whole hazard (claunch-tn2wo).

    The lock must cover the miss, which is what this measures: replacing
    ``Events._lock`` with a no-op makes it read ``['fail']`` instead of
    ``['fail', 'kill']``, every run. Holding the lock across the write too
    guards a second hazard (the dump walking a list another thread appends to),
    and that half is not measured here -- guarding only the lookup still passes,
    so this test claims the miss and nothing more.
    """
    monkeypatch.setattr(session_events, "LIMIT", 200)
    session = SimpleNamespace(sdef=SimpleNamespace(name="s1"), created_at="first")
    events = session_events.Events(tmp_path)

    failures = _both_miss_the_cache_together(events, session, "kill", "fail")
    assert failures == [], f"record() raised: {failures}"

    # Both writers' rows are in the file, not just the one that wrote last.
    restored = session_events.Events(tmp_path)
    kinds = sorted(row["kind"] for row in restored.rows(session))
    assert kinds == ["fail", "kill"], (
        "one event was lost: the two writers both missed the cache and the "
        f"second write replaced the first writer's list, got {kinds}")
def test_kill_records_request_then_exit_and_noop_is_silent(home, monkeypatch):
    mgr = SessionManager(idle_threshold=1, scrollback=100, restore_default=True)
    session = SimpleNamespace(sdef=SessionDef(name="s1"), created_at="first",
                              exited=False, exit_code=0, kill=lambda **kwargs: None)
    mgr._sessions["s1"] = session
    monkeypatch.setattr(mgr, "persist", lambda: None)
    mgr.kill("s1", force=True)
    assert mgr.events.rows(session)[0]["details"] == {"force": True}
    session.exited = True
    mgr._session_exited(session)
    mgr.kill("s1")
    assert [e["kind"] for e in mgr.events.rows(session)] == ["kill", "exit"]
    mgr.shutting_down = True
    mgr._session_exited(session)
    assert len(mgr.events.rows(session)) == 2
