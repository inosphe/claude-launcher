"""``claunch basetemp prune``: a dry run by default, ``--apply`` leaves rows.

claunch-2hnyf. Every case runs against the home fixture's tmp registry and
the suite's tmp basetemp root (conftest); nothing here touches the real
``C:/t`` or ``~/.claude-launcher``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from claude_launcher import cli
from claude_launcher.daemon import basetemp_reaper as br


def _tree(name: str, *, age_days: float = 5.0) -> Path:
    root = br.default_root()
    path = root / name
    (path / "popen-gw0" / "test_x0").mkdir(parents=True)
    (path / "popen-gw0" / "test_x0" / "out.txt").write_text("x" * 8, encoding="utf-8")
    stamp = time.time() - age_days * 86400.0
    for target in [path, *path.iterdir()]:
        os.utime(target, (stamp, stamp))
    return path


def _registry(home: Path, records: list) -> None:
    daemon = home / "daemon"
    daemon.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(daemon / "sessions.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(name TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT)"
    )
    for record in records:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (name, data) VALUES (?, ?)",
            (record["def"]["name"], json.dumps(record)),
        )
    conn.commit()
    conn.close()


def _records() -> list:
    return [
        {"def": {"name": "s1"}, "was_running": False},                       # ended
        {"def": {"name": "s2"}, "was_running": True},                        # running
        {"def": {"name": "s3"}, "was_running": False,
         "held_at": "2026-10-11T00:00:00+00:00"},                            # held
    ]


def _history(home: Path) -> list:
    path = home / "daemon" / br.HISTORY_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_prune_is_a_dry_run_by_default(home, capsys):
    _registry(home, _records())
    ended = _tree("s1c1011010955")
    running = _tree("s2c1011010955")
    held = _tree("s3c1011010955")
    abandoned = _tree("s42c1011010955")

    assert cli.main(["basetemp", "prune"]) == 0

    out = capsys.readouterr().out
    assert "dry run" in out
    assert ended.is_dir() and running.is_dir() and held.is_dir() and abandoned.is_dir()
    assert _history(home) == []


def test_apply_removes_ended_and_abandoned_and_keeps_live_and_held(home, capsys):
    _registry(home, _records())
    ended = _tree("s1c1011010955")
    running = _tree("s2c1011010955")
    held = _tree("s3c1011010955")
    abandoned = _tree("s42c1011010955")
    fresh = _tree("s43c1011010955", age_days=0.0)

    assert cli.main(["basetemp", "prune", "--apply"]) == 0

    assert not ended.exists() and not abandoned.exists()
    assert running.is_dir() and held.is_dir() and fresh.is_dir()
    rows = {Path(row["path"]).name: row for row in _history(home)}
    assert set(rows) == {"s1c1011010955", "s42c1011010955"}
    assert rows["s42c1011010955"]["session"] == "s42"
    assert all(row["outcome"] == "removed" for row in rows.values())
    assert rows["s1c1011010955"]["workers"] == 1


def test_older_than_zero_includes_fresh_abandoned_trees(home):
    _registry(home, _records())
    fresh = _tree("s42c1011010955", age_days=0.0)

    assert cli.main(["basetemp", "prune", "--older-than", "0d", "--apply"]) == 0

    assert not fresh.exists()


def test_prune_leaves_directories_that_are_not_session_shaped(home):
    _registry(home, _records())
    other = _tree("notes-2026")

    assert cli.main(["basetemp", "prune", "--older-than", "0", "--apply"]) == 0

    assert other.is_dir()


def test_prune_survives_a_missing_registry(home):
    ended = _tree("s1c1011010955")

    assert cli.main(["basetemp", "prune", "--apply"]) == 0

    assert not ended.exists()
    [row] = _history(home)
    assert row["session"] == "s1"
