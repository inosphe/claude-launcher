"""The durable session store: round-trip, the empty-snapshot guard, migration.

These lock down the three properties that ``sessions.json`` did not have and
that the loss on 2026-09-07 turned on. A write is a transaction (no torn read);
an empty snapshot never clears a non-empty table unless a clear asked for it;
and the legacy file is imported at most once for the life of the database.
"""

from __future__ import annotations

import json

from claude_launcher.daemon import db


def _entry(name, **fields):
    entry = {"def": {"name": name}}
    entry.update(fields)
    return entry


def test_round_trip_preserves_records_and_order(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    entries = [_entry("s1", was_running=True), _entry("s2", paused_at="t")]
    store.save(entries)

    loaded = store.load_all()
    assert [e["def"]["name"] for e in loaded] == ["s1", "s2"]
    assert loaded[0]["was_running"] is True
    assert loaded[1]["paused_at"] == "t"


def test_save_upserts_in_place(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1", exit_code=None)])
    store.save([_entry("s1", exit_code=0)])

    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0]["exit_code"] == 0


def test_prune_drops_forgotten_records(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1"), _entry("s2")])
    store.save([_entry("s1")])  # s2 forgotten

    assert [e["def"]["name"] for e in store.load_all()] == ["s1"]


def test_empty_snapshot_does_not_clear_a_full_table(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1"), _entry("s2")])

    # A persist that finds the set unexpectedly empty is a bug, not a clear.
    store.save([], prune=True)

    assert store.count() == 2


def test_explicit_clear_empties_the_table(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1"), _entry("s2")])

    store.save([], prune=True, allow_empty=True)

    assert store.count() == 0


def test_prune_off_keeps_records_a_partial_write_omits(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1"), _entry("s2")])

    # Restore fills the set one session at a time; each persist must not prune.
    store.save([_entry("s1")], prune=False)

    assert {e["def"]["name"] for e in store.load_all()} == {"s1", "s2"}


def test_delete_removes_named_rows(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1"), _entry("s2")])
    store.delete(["s1"])

    assert [e["def"]["name"] for e in store.load_all()] == ["s2"]


def test_migrate_imports_a_legacy_file_once(tmp_path):
    legacy = tmp_path / "sessions.json"
    legacy.write_text(
        json.dumps([_entry("s1"), _entry("s2")]), encoding="utf-8"
    )
    store = db.SessionStore(tmp_path / "sessions.db")

    assert store.migrate_from_json(legacy) == 2
    assert {e["def"]["name"] for e in store.load_all()} == {"s1", "s2"}

    # The marker, not the file's absence, is what stops a second read: even
    # after a genuine clear, the legacy file is never imported again.
    store.save([], prune=True, allow_empty=True)
    assert store.migrate_from_json(legacy) == 0
    assert store.count() == 0


def test_migrate_skips_when_table_already_has_rows(tmp_path):
    legacy = tmp_path / "sessions.json"
    legacy.write_text(json.dumps([_entry("old")]), encoding="utf-8")
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("live")])

    assert store.migrate_from_json(legacy) == 0
    assert {e["def"]["name"] for e in store.load_all()} == {"live"}


def test_migrate_tolerates_a_missing_or_torn_file(tmp_path):
    store = db.SessionStore(tmp_path / "sessions.db")
    assert store.migrate_from_json(tmp_path / "nope.json") == 0

    torn = tmp_path / "torn.json"
    torn.write_text("[{\"def\": {\"na", encoding="utf-8")
    store2 = db.SessionStore(tmp_path / "sessions2.db")
    assert store2.migrate_from_json(torn) == 0


# -- only the rows that changed are written (claunch-7l7b7) ------------- #


def _traced(store, monkeypatch):
    """Record the upserts ``store`` issues."""
    upserts = []
    connect = store._connect

    def traced():
        conn = connect()
        conn.set_trace_callback(
            lambda sql: upserts.append(sql)
            if sql.lstrip().upper().startswith("INSERT INTO SESSIONS") else None
        )
        return conn

    monkeypatch.setattr(store, "_connect", traced)
    return upserts


def test_save_writes_only_the_rows_that_changed(tmp_path, monkeypatch):
    store = db.SessionStore(tmp_path / "sessions.db")
    upserts = _traced(store, monkeypatch)
    entries = [_entry(f"s{i}", exit_code=None) for i in range(20)]
    store.save(entries)
    assert len(upserts) == 20

    upserts.clear()
    store.save(entries)
    assert upserts == []

    entries[3] = _entry("s3", exit_code=0)
    store.save(entries)
    assert len(upserts) == 1
    assert {e["def"]["name"]: e["exit_code"] for e in store.load_all()}["s3"] == 0


def test_another_writer_makes_the_next_save_write_every_row(tmp_path):
    """What this store remembers writing is no evidence once someone else
    has written: an overlapping daemon's row must be overwritten, not kept."""
    path = tmp_path / "sessions.db"
    mine, other = db.SessionStore(path), db.SessionStore(path)
    entries = [_entry("s1", exit_code=None), _entry("s2", exit_code=None)]
    mine.save(entries)
    other.save([_entry("s1", exit_code=9)], prune=False)

    mine.save(entries)
    assert {e["def"]["name"]: e["exit_code"] for e in mine.load_all()} == {
        "s1": None, "s2": None,
    }


def test_a_delete_from_another_store_is_written_back(tmp_path):
    path = tmp_path / "sessions.db"
    mine, other = db.SessionStore(path), db.SessionStore(path)
    entries = [_entry("s1"), _entry("s2")]
    mine.save(entries)
    other.delete(["s1"])

    mine.save(entries)
    assert {e["def"]["name"] for e in mine.load_all()} == {"s1", "s2"}


def test_a_failed_save_forgets_what_was_written(tmp_path, monkeypatch):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1", exit_code=None)])
    bad = _entry("s2", exit_code=None)
    bad["unserialisable"] = object()
    try:
        store.save([_entry("s1", exit_code=0), bad])
    except TypeError:
        pass
    else:
        raise AssertionError("expected the save to raise")
    # Rolled back: s1 is still the old row on disk.
    assert store.load_all()[0]["exit_code"] is None

    upserts = _traced(store, monkeypatch)
    store.save([_entry("s1", exit_code=0)])
    assert len(upserts) == 1
    assert store.load_all()[0]["exit_code"] == 0


def test_unchanged_saves_still_prune_and_still_guard(tmp_path, caplog):
    store = db.SessionStore(tmp_path / "sessions.db")
    store.save([_entry("s1"), _entry("s2")])
    store.save([_entry("s1")])  # s1 unchanged, s2 gone
    assert [e["def"]["name"] for e in store.load_all()] == ["s1"]

    store.save([])  # nobody asked for an empty registry
    assert [e["def"]["name"] for e in store.load_all()] == ["s1"]
    assert "refusing to clear" in caplog.text

    store.save([_entry("s2")], prune=False)  # a restore still filling the set
    assert {e["def"]["name"] for e in store.load_all()} == {"s1", "s2"}


def test_saves_from_two_threads_leave_the_last_state_on_disk(tmp_path):
    """The manager saves from the loop and from a worker thread; whatever
    interleaving, a later save of the current state must reach the disk."""
    import threading

    store = db.SessionStore(tmp_path / "sessions.db")
    names = [f"s{i}" for i in range(30)]
    barrier = threading.Barrier(2)

    def writer(code):
        barrier.wait()
        for _ in range(10):
            store.save([_entry(n, exit_code=code) for n in names])

    threads = [threading.Thread(target=writer, args=(c,)) for c in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.save([_entry(n, exit_code=3) for n in names])
    assert {e["exit_code"] for e in store.load_all()} == {3}
