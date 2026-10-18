"""The dashboard's notice store, shared by every browser (claunch-mfzlv).

A card read or put away in one browser goes down in the others on their next
poll. ``dismissed`` only goes one way; a ``read`` change made against an
older base than the stored read is refused and the stored record comes back
as a conflict. The records survive a daemon restart through their file.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import notice_sync, paths
from claude_launcher.daemon.notice_sync import NoticeSyncStore


def _store(tmp_path):
    return NoticeSyncStore(tmp_path / "notice-sync.json")


def _rec(rows, nid):
    return next(r for r in rows if r["id"] == nid)


def test_dismiss_reaches_another_browser(tmp_path):
    s = _store(tmp_path)
    first = s.sync({"epoch": "", "since": 0, "changes": []})
    out = s.sync({"epoch": first["epoch"], "since": 0,
                  "changes": [{"id": "qa:q1:open", "dismissed": True}]})
    other = s.sync({"epoch": first["epoch"], "since": first["seq"], "changes": []})
    rec = _rec(other["changes"], "qa:q1:open")
    assert rec["dismissed"] is True and rec["read"] is None
    assert out["seq"] == other["seq"] == 1


def test_dismissed_only_goes_one_way(tmp_path):
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "dismissed": True})])
    applied, conflicts = s.apply([notice_sync.clean_change({"id": "a", "dismissed": False})])
    assert applied == [] and conflicts == []
    assert s.since(0)[0]["dismissed"] is True


def test_same_change_twice_moves_nothing(tmp_path):
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "dismissed": True, "read": True})])
    seq = s.seq
    applied, conflicts = s.apply([notice_sync.clean_change({"id": "a", "dismissed": True, "read": True})])
    assert applied == [] and conflicts == [] and s.seq == seq


def test_read_against_older_base_is_a_conflict(tmp_path):
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "read": True})])
    seen = s.seq
    # Browser A, knowing the read above, marks it unread.
    s.apply([notice_sync.clean_change({"id": "a", "read": False, "base": seen})])
    # Browser B still at the first state marks it read again: refused.
    applied, conflicts = s.apply([notice_sync.clean_change({"id": "a", "read": True, "base": seen})])
    assert applied == []
    assert conflicts[0]["id"] == "a" and conflicts[0]["read"] is False
    assert s.since(0)[0]["read"] is False


def test_read_with_current_base_passes(tmp_path):
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "read": True})])
    applied, conflicts = s.apply([notice_sync.clean_change({"id": "a", "read": False, "base": s.seq})])
    assert conflicts == [] and applied[0]["read"] is False


def test_dismiss_after_read_is_seen_by_a_poll_at_that_seq(tmp_path):
    """A dismiss on a record whose read was the last write still moves seq:
    a browser polling from that seq must get it."""
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "read": True})])
    seen = s.seq
    s.apply([notice_sync.clean_change({"id": "a", "dismissed": True})])
    assert [r["id"] for r in s.since(seen)] == ["a"]


def test_a_conflict_does_not_block_the_dismiss_in_the_same_change(tmp_path):
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "read": False})])
    applied, conflicts = s.apply([notice_sync.clean_change({"id": "a", "read": True, "dismissed": True})])
    assert [r["id"] for r in applied] == ["a"] and [r["id"] for r in conflicts] == ["a"]
    rec = s.since(0)[0]
    assert rec["dismissed"] is True and rec["read"] is False


def test_records_survive_a_restart(tmp_path):
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "read": True, "dismissed": True})])
    again = _store(tmp_path)
    assert again.epoch == s.epoch and again.seq == s.seq
    assert again.since(0)[0]["read"] is True


def test_another_epoch_reads_from_the_start(tmp_path):
    s = _store(tmp_path)
    s.apply([notice_sync.clean_change({"id": "a", "dismissed": True})])
    out = s.sync({"epoch": "another-file", "since": 99, "changes": []})
    assert [r["id"] for r in out["changes"]] == ["a"]


def test_oldest_records_are_trimmed(tmp_path, monkeypatch):
    monkeypatch.setattr(notice_sync, "MAX_RECORDS", 3)
    s = _store(tmp_path)
    for i in range(5):
        s.apply([notice_sync.clean_change({"id": f"n{i}", "dismissed": True})])
    assert [r["id"] for r in s.since(0)] == ["n2", "n3", "n4"]


@pytest.mark.parametrize("bad", [
    {"changes": "x"},
    {"changes": [{"id": ""}]},
    {"changes": [{"id": "a", "read": "yes"}]},
    {"changes": [{"id": "a", "base": -1}]},
    {"changes": [{"id": "x" * (notice_sync.MAX_ID + 1)}]},
    {"since": "1"},
])
def test_malformed_bodies_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        _store(tmp_path).sync(bad)


def test_unreadable_file_starts_empty(tmp_path):
    (tmp_path / "notice-sync.json").write_text("{not json", encoding="utf-8")
    s = _store(tmp_path)
    assert s.since(0) == [] and s.epoch


def test_path_is_in_the_daemon_dir():
    assert paths.notice_sync_json().parent == paths.daemon_dir()


def test_route_answers_and_refuses(tmp_path):
    async def run():
        app = web.Application()
        notice_sync.install(app, _store(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/notices/sync", json={
                "epoch": "", "since": 0, "changes": [{"id": "thread:th-a:5", "dismissed": True}]})
            assert resp.status == 200
            doc = await resp.json()
            assert doc["seq"] == 1 and doc["changes"][0]["id"] == "thread:th-a:5"
            resp = await client.post("/api/notices/sync", json={"changes": [{"id": 3}]})
            assert resp.status == 400 and "id" in (await resp.json())["error"]
            resp = await client.post("/api/notices/sync", data="nope")
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(run())
    saved = json.loads((tmp_path / "notice-sync.json").read_text(encoding="utf-8"))
    assert saved["records"]["thread:th-a:5"]["dismissed"] is True
