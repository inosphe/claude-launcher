"""The index behind the test-debris cleanup (claunch-zrpa4).

On 2026-10-22 ``/`` hit 100% inode use with 500 GB of bytes still free, and
the daemon's sqlite writes started failing with ``Errno 28``
(claunch-zbu54). The trees that filled it are under one root, and the
question the cleanup needs answered before anything is deleted is *whose*
each tree is: a live session's, an ended session's the reaper has not taken
yet, or nobody's.

What these pin is that answer and the batch order that follows from it:

* ownership uses ``basetemp_reaper.owner``, so this index cannot disagree
  with the exit reaper about who owns a directory;
* a held session reads as live -- its program has ended, the session has
  not, and its trees must not be in any removal batch;
* a mode ``0200`` tree goes to the repair batch, never to a delete batch:
  nothing can remove it until the search bit is back (claunch-zbu54);
* an entry no registry names is an orphan -- no session's exit will ever
  claim it, so the reaper never will.

The registries are real sqlite files in ``tmp_path`` because that is what the
tool reads; no daemon is started.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "test_debris_index.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_debris_index_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


index = _load()


def _registry(directory: Path, records) -> Path:
    """Write a ``sessions.db`` the way the daemon does: one JSON row per session."""
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / "sessions.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (data TEXT)")
    for record in records:
        conn.execute("INSERT INTO sessions VALUES (?)", (json.dumps(record),))
    conn.commit()
    conn.close()
    return directory


def _session(name, *, running=False, held=False):
    return {"def": {"name": name}, "was_running": running, "held_at": 1.0 if held else None}


# ---- registry ------------------------------------------------------------ #


def test_session_states_reads_live_and_ended(tmp_path):
    directory = _registry(tmp_path / "daemon", [
        _session("s1", running=True),
        _session("s2"),
    ])
    assert index.session_states([directory]) == {"s1": index.LIVE, "s2": index.ENDED}


def test_a_held_session_reads_as_live(tmp_path):
    """Held means the program ended and the session did not (basetemp_reaper)."""
    directory = _registry(tmp_path / "daemon", [_session("s1", held=True)])
    assert index.session_states([directory]) == {"s1": index.LIVE}


def test_states_merge_across_instances_and_live_wins(tmp_path):
    """A name adopted into another instance is recorded twice; only live matters."""
    one = _registry(tmp_path / "daemon", [_session("s1")])
    two = _registry(tmp_path / "daemons" / "other", [_session("s1", running=True)])
    assert index.session_states([one, two]) == {"s1": index.LIVE}
    assert index.session_states([two, one]) == {"s1": index.LIVE}


def test_a_missing_registry_is_not_an_error(tmp_path):
    assert index.session_states([tmp_path / "nope"]) == {}


def _legacy_instance(directory: Path, records) -> Path:
    """An instance that predates ``sessions.db`` and still keeps a JSON file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sessions.json").write_text(json.dumps(records), encoding="utf-8")
    return directory


def test_a_legacy_sessions_json_instance_is_read_too(tmp_path, monkeypatch):
    """An instance that never migrated still names its sessions.

    Reproduced by s1450-rev (claunch-zrpa4 review): with only the database
    read, a live session recorded here fell through to ``orphan`` and its
    tree was batched for deletion. ``basetemp_reaper.foreign_live`` calls the
    same state live, so the two disagreed about the same name.
    """
    base = tmp_path / "home"
    _legacy_instance(base / "daemons" / "old", [_session("s77", running=True)])
    monkeypatch.setattr(index.reaper.paths, "base_home", lambda: base)
    assert index.session_states() == {"s77": index.LIVE}


def test_the_registry_is_looked_for_under_the_instance_base(tmp_path, monkeypatch):
    """``CLAUNCH_INSTANCE_BASE`` moves the registries, not the launcher home.

    Reading only ``config.launcher_home()`` here returned no sessions at all,
    and every entry became an orphan.
    """
    launcher_home = tmp_path / "launcher"
    instance_base = tmp_path / "instances"
    launcher_home.mkdir()
    _registry(instance_base / "daemon", [_session("s88", running=True)])
    monkeypatch.setattr(index.reaper.paths, "base_home", lambda: instance_base)
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(launcher_home))
    assert index.session_states() == {"s88": index.LIVE}


# ---- classification ------------------------------------------------------ #


def test_orphan_is_an_entry_no_registry_names():
    states = {"s29": index.LIVE}
    assert index.classify("s1162tree", states) == index.ORPHAN
    assert index.classify("dfv2-20261021-190339", states) == index.ORPHAN


def test_owner_shape_decides_by_longest_matching_name():
    """``s29w2`` belongs to ``s29``: it has the ``<session>-<anything>`` shape."""
    states = {"s29": index.LIVE, "s29w2": index.ENDED}
    assert index.classify("s29w2", states) == index.ENDED
    assert index.classify("s29c1021200000", states) == index.LIVE


# ---- the plan ------------------------------------------------------------ #


def _tree(root: Path, name: str, *, mode=0o755) -> Path:
    path = root / name
    path.mkdir()
    (path / "a.txt").write_text("x")
    if mode != 0o755:
        os.chmod(path, mode)
    return path


def test_plan_batches_by_state_and_never_touches_a_live_tree(tmp_path):
    root = tmp_path / "basetemp"
    root.mkdir()
    _tree(root, "s1c1021200000")
    _tree(root, "s2c1021000000")
    _tree(root, "s3c1019000000")
    _tree(root, "orphan-tree")
    states = {"s1": index.LIVE, "s2": index.ENDED, "s3": index.ENDED}
    rows = index.build(root, states, do_measure=False)
    batches = {b["step"]: b for b in index.plan(rows, stale_days=1.0)}
    assert batches["orphaned"]["count"] == 1
    assert batches["orphaned"]["paths"] == [(root / "orphan-tree").as_posix()]
    # Every ended tree is older than a day only if its mtime says so; the two
    # just written are fresh, so only the orphan leaves the "live" set alone.
    names = {Path(p).name for step in ("orphaned", "stale") for p in batches[step]["paths"]}
    assert "s1c1021200000" not in names


def test_an_unreadable_tree_goes_to_repair_and_not_to_a_delete_batch(tmp_path):
    """mode 0200 cannot be listed, so nothing else can remove it (claunch-zbu54)."""
    root = tmp_path / "basetemp"
    root.mkdir()
    locked = _tree(root, "s9c1012000000", mode=0o200)
    rows = index.build(root, {"s9": index.ENDED}, do_measure=False)
    batches = {b["step"]: b for b in index.plan(rows, stale_days=0.0)}
    assert batches["repair"]["paths"] == [locked.as_posix()]
    assert batches["stale"]["count"] == 0
    os.chmod(locked, 0o755)


def test_a_stale_ended_tree_is_batched_by_age(tmp_path):
    root = tmp_path / "basetemp"
    root.mkdir()
    old = _tree(root, "s4c1012000000")
    past = 1_000_000.0
    os.utime(old, (past, past))
    rows = index.build(root, {"s4": index.ENDED}, do_measure=False)
    batches = {b["step"]: b for b in index.plan(rows, stale_days=1.0)}
    assert batches["stale"]["paths"] == [old.as_posix()]


# ---- the entry point ----------------------------------------------------- #


def test_main_returns_two_when_the_root_cannot_be_listed(tmp_path, capsys):
    code = index.main(["--root", str(tmp_path / "missing")])
    assert code == 2
    assert "root not listable" in capsys.readouterr().err


def test_main_json_carries_the_entries_and_the_batches(tmp_path, capsys):
    root = tmp_path / "basetemp"
    root.mkdir()
    _tree(root, "nobody-owns-this")
    code = index.main(["--root", str(root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["root"] == root.as_posix()
    assert [row["name"] for row in payload["entries"]] == ["nobody-owns-this"]
    assert payload["entries"][0]["state"] == index.ORPHAN
    assert payload["batches"][1]["step"] == "orphaned"
