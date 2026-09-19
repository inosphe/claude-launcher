"""The scanner behind a pi session's ``/new`` re-pin.

pi names the file itself (``<timestamp>_<id>.jsonl``) and stamps its header
with the moment of the command, so a claim is "the new file in this directory
whose header says this cwd, written within seconds of *this* ``/new``". See
:mod:`claude_launcher.daemon.pi_sessions`.
"""

from __future__ import annotations

import json

from claude_launcher.daemon import pi_sessions


def _session_file(directory, stem, *, cwd, timestamp, first_line=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.jsonl"
    header = first_line if first_line is not None else json.dumps({
        "type": "session",
        "version": 3,
        "id": stem.rsplit("_", 1)[-1],
        "timestamp": timestamp,
        "cwd": str(cwd),
    })
    path.write_text(header + "\n", encoding="utf-8")
    return path


def test_snapshot_lists_the_stems_of_every_session_file(tmp_path):
    _session_file(tmp_path, "aaa", cwd=tmp_path, timestamp="2026-09-15T00:00:00Z")
    (tmp_path / "not-a-session.txt").write_text("x", encoding="utf-8")
    assert pi_sessions.snapshot(tmp_path) == {"aaa"}
    assert pi_sessions.snapshot(tmp_path / "missing") == set()


def test_claim_new_finds_the_file_the_command_just_created(tmp_path):
    _session_file(tmp_path, "2026-09-14T00-00-00-000Z_old", cwd=tmp_path,
                  timestamp="2026-09-14T00:00:00Z")
    new = _session_file(
        tmp_path, "2026-09-15T01-00-00-000Z_new", cwd=tmp_path,
        timestamp="2026-09-15T01:00:00.000Z",
    )
    known = pi_sessions.snapshot(tmp_path) - {new.stem}
    since = 1789000000.0  # within seconds of the header above's epoch
    from datetime import datetime, timezone

    since = datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    assert pi_sessions.claim_new(tmp_path, str(tmp_path), known, since=since) == new.stem


def test_claim_new_rejects_a_header_from_another_moment(tmp_path):
    # The other pi session in this directory ran /new an hour later; its file
    # must not be claimed for ours.
    new = _session_file(tmp_path, "new", cwd=tmp_path,
                        timestamp="2026-09-15T02:00:00Z")
    from datetime import datetime, timezone

    since = datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    assert pi_sessions.claim_new(tmp_path, str(tmp_path), set(), since=since) is None
    assert new.stem  # the file exists; the timestamp is what excludes it


def test_claim_new_rejects_a_file_for_another_cwd(tmp_path):
    _session_file(tmp_path, "new", cwd=tmp_path / "elsewhere",
                  timestamp="2026-09-15T01:00:00Z")
    from datetime import datetime, timezone

    since = datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    assert pi_sessions.claim_new(tmp_path, str(tmp_path), set(), since=since) is None


def test_claim_new_waits_out_an_unfinished_or_foreign_first_line(tmp_path):
    # pi still writing the header, and a non-session record first: neither is
    # a candidate, and neither is an error.
    (tmp_path / "partial.jsonl").write_text('{"type":"ses', encoding="utf-8")
    _session_file(tmp_path, "odd", cwd=tmp_path, timestamp="2026-09-15T01:00:00Z",
                  first_line='{"type":"message"}')
    from datetime import datetime, timezone

    since = datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    assert pi_sessions.claim_new(tmp_path, str(tmp_path), set(), since=since) is None


def test_claim_new_never_breaks_a_tie(tmp_path):
    # Two new files in one window (two sessions ran /new nearly together):
    # nothing is better than the wrong one — the caller keeps the claim
    # pending rather than pinning somebody else's conversation.
    from datetime import datetime, timezone

    since = datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    _session_file(tmp_path, "one", cwd=tmp_path, timestamp="2026-09-15T01:00:00Z")
    _session_file(tmp_path, "two", cwd=tmp_path, timestamp="2026-09-15T01:00:01Z")
    assert pi_sessions.claim_new(tmp_path, str(tmp_path), set(), since=since) is None


def test_claim_new_can_wait_for_the_file_to_appear(tmp_path):
    from datetime import datetime, timezone

    since = datetime.now(timezone.utc).timestamp()
    import threading, time

    def late():
        time.sleep(0.15)
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _session_file(tmp_path, "late", cwd=tmp_path, timestamp=stamp)

    threading.Thread(target=late, daemon=True).start()
    assert pi_sessions.claim_new(
        tmp_path, str(tmp_path), set(), since=since, timeout=2.0, poll=0.02
    ) == "late"
