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
