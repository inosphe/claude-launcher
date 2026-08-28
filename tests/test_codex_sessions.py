from __future__ import annotations

import json

from claude_launcher.daemon import codex_sessions


def _rollout(root, day, session_id, cwd):
    path = root / "sessions" / day / f"rollout-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": str(cwd)},
    }) + "\n", encoding="utf-8")


def test_claim_new_uses_snapshot_and_cwd(tmp_path):
    profile_home = tmp_path / "codex"
    ours = tmp_path / "ours"
    other = tmp_path / "other"
    _rollout(profile_home, "2026/08/27", "old", ours)
    known = codex_sessions.snapshot(profile_home)

    _rollout(profile_home, "2026/08/27", "wrong-cwd", other)
    _rollout(profile_home, "2026/08/27", "ours", ours)

    assert codex_sessions.claim_new(profile_home, str(ours), known, timeout=0) == "ours"


def test_latest_supports_legacy_unpinned_definitions(tmp_path):
    profile_home = tmp_path / "codex"
    cwd = tmp_path / "work"
    _rollout(profile_home, "2026/08/26", "older", cwd)
    older = next((profile_home / "sessions").glob("**/*older.jsonl"))
    _rollout(profile_home, "2026/08/27", "newer", cwd)
    older.touch()

    # Write time, not directory/filename ordering, is Codex's definition of
    # the conversation most recently active in this cwd.
    assert codex_sessions.latest(profile_home, str(cwd)) == "older"


def test_find_returns_the_rollout_whose_metadata_owns_the_id(tmp_path):
    profile_home = tmp_path / "codex"
    cwd = tmp_path / "work"
    _rollout(profile_home, "2026/08/27", "wanted", cwd)
    expected = next((profile_home / "sessions").glob("**/*wanted.jsonl"))

    assert codex_sessions.find(profile_home, "wanted") == expected
    assert codex_sessions.find(profile_home, "missing") is None
