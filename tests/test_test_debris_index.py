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


def test_an_orphan_named_after_a_live_session_is_ambiguous():
    """``s29d`` has no shape in the reaper's list, but s29 is alive.

    Measured 2026-10-22: five such trees under basetemp were held by hand at
    batch A because deleting them on "no owner" could have destroyed a live
    session's working tree.
    """
    states = {"s29": index.LIVE, "s7": index.ENDED}
    assert index.classify("s29d", states) == index.AMBIGUOUS
    assert index.classify("s29om", states) == index.AMBIGUOUS
    # ``s29w2`` is a numbered sweep generation: reaper._SWEEP gives it to s29
    # (tests/test_basetemp_reaper.py: ("s995w2", "s995", True)), so it is s29's
    # and live, not ambiguous.
    assert index.classify("s29w2", states) == index.LIVE
    # An ended session's prefix is not protected: nothing is using it.
    assert index.classify("s7d", states) == index.ORPHAN
    assert index.classify("nothing-like-a-session", states) == index.ORPHAN


def test_an_ambiguous_orphan_never_reaches_a_removal_batch(tmp_path):
    root = tmp_path / "basetemp"
    root.mkdir()
    _tree(root, "s29d")
    states = {"s29": index.LIVE}
    rows = index.build(root, states, do_measure=False)
    batches = {b["step"]: b for b in index.plan(rows, stale_days=0.0)}
    assert rows[0]["state"] == index.AMBIGUOUS
    assert batches["orphaned"]["count"] == 0


def test_owner_shape_decides_by_longest_matching_name():
    """``s29w2`` belongs to ``s29`` (a sweep generation), unless a longer name is registered."""
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


def test_an_unreadable_live_or_ambiguous_tree_stays_out_of_every_batch(tmp_path):
    """State is decided before readability (s1450-rev, claunch-zrpa4 review).

    The other order put a mode-0200 live tree into ``repair``, whose first
    step is "chmod it, then delete it" — the guard undone one line later.
    """
    root = tmp_path / "basetemp"
    root.mkdir()
    live = _tree(root, "s1c1021200000", mode=0o200)
    ambiguous = _tree(root, "s29d", mode=0o200)
    states = {"s1": index.LIVE, "s29": index.LIVE}
    rows = index.build(root, states, do_measure=False)
    batches = {b["step"]: b for b in index.plan(rows, stale_days=0.0)}
    assert {r["state"] for r in rows} == {index.LIVE, index.AMBIGUOUS}
    for step in ("repair", "orphaned", "stale"):
        assert batches[step]["count"] == 0, step
    os.chmod(live, 0o755)
    os.chmod(ambiguous, 0o755)


def test_an_unreadable_orphan_is_repaired_first_then_removed(tmp_path):
    """Repair is the precondition of removal, not a removal batch of its own."""
    root = tmp_path / "basetemp"
    root.mkdir()
    orphan = _tree(root, "nobody-owns-this", mode=0o200)
    rows = index.build(root, {}, do_measure=False)
    batches = {b["step"]: b for b in index.plan(rows, stale_days=0.0)}
    assert batches["repair"]["paths"] == [orphan.as_posix()]
    os.chmod(orphan, 0o755)


def test_a_stale_ended_tree_is_batched_by_age(tmp_path):
    root = tmp_path / "basetemp"
    root.mkdir()
    old = _tree(root, "s4c1012000000")
    past = 1_000_000.0
    os.utime(old, (past, past))
    rows = index.build(root, {"s4": index.ENDED}, do_measure=False)
    batches = {b["step"]: b for b in index.plan(rows, stale_days=1.0)}
    assert batches["stale"]["paths"] == [old.as_posix()]


# ---- worktrees ----------------------------------------------------------- #

PORCELAIN = """\
worktree /repo
HEAD aaaa1111
branch refs/heads/master

worktree /home/me/.claude-launcher/daemon/sessions/s9/scratch/rev-x
HEAD bbbb2222
detached

worktree /home/me/.claude-launcher/daemon/sessions/s10/scratch/tree
HEAD cccc3333
branch refs/heads/feature
"""


def test_porcelain_is_read_into_one_row_per_worktree():
    rows = index.parse_worktrees(PORCELAIN)
    assert [r["path"] for r in rows][1].endswith("rev-x")
    assert rows[0]["branch"] == "master" and rows[0]["detached"] is False
    assert rows[1]["detached"] is True and rows[1]["branch"] == ""
    assert rows[2]["branch"] == "feature"


def test_a_scratch_path_names_its_session_and_anything_else_does_not():
    assert index.session_of("/x/daemon/sessions/s9/scratch/rev-x") == "s9"
    assert index.session_of("/x/.claude/worktrees/s9-20261022") is None
    assert index.session_of("/x/daemon/sessions/s9/nope") is None


@pytest.fixture
def repo_with_worktrees(tmp_path):
    """A repository, one worktree under a fake launcher home, one outside it."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a, cwd=repo: subprocess.run(["git", "-C", str(cwd), *a], check=True,
                                              capture_output=True, text=True)
    run("init", "-q", "-b", "master")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    run("add", "-A")
    run("commit", "-qm", "init")
    home = tmp_path / "home"
    inside = home / "daemon" / "sessions" / "s9" / "scratch" / "rev-x"
    inside.parent.mkdir(parents=True)
    run("worktree", "add", "--detach", str(inside), "HEAD")
    outside = tmp_path / "elsewhere"
    run("worktree", "add", "--detach", str(outside), "HEAD")
    return repo, home, inside, outside


def test_only_worktrees_under_the_launcher_home_are_indexed(repo_with_worktrees):
    repo, home, inside, outside = repo_with_worktrees
    states = {"s9": index.ENDED}
    rows = index.worktree_rows(repo, states, home, index.reachable_commits(repo))
    assert [r["path"] for r in rows] == [str(inside)]
    assert rows[0]["session"] == "s9" and rows[0]["removable"] is True


def test_a_live_session_keeps_its_worktree(repo_with_worktrees):
    repo, home, inside, _ = repo_with_worktrees
    rows = index.worktree_rows(repo, {"s9": index.LIVE}, home,
                               index.reachable_commits(repo))
    assert rows[0]["removable"] is False
    assert rows[0]["why"] == "live session"


def test_uncommitted_changes_keep_a_worktree(repo_with_worktrees):
    repo, home, inside, _ = repo_with_worktrees
    (inside / "dirty.txt").write_text("wip")
    rows = index.worktree_rows(repo, {"s9": index.ENDED}, home,
                               index.reachable_commits(repo))
    assert rows[0]["removable"] is False
    assert rows[0]["why"] == "uncommitted changes"


def test_a_session_in_no_registry_keeps_its_worktree(repo_with_worktrees):
    """``unknown`` is not ``ended``: nothing would ever revisit this tree.

    A basetemp orphan is safe to batch because no exit hook will rewrite it;
    a worktree is removed by hand, so "no registry names this session" is not
    the same evidence as "the session ended" (s1450-rev, claunch-zrpa4.1).
    """
    repo, home, inside, _ = repo_with_worktrees
    rows = index.worktree_rows(repo, {}, home, index.reachable_commits(repo))
    assert rows[0]["session"] == "s9"
    assert rows[0]["state"] == "unknown"
    assert rows[0]["removable"] is False
    assert rows[0]["why"] == "session in no registry"


def test_a_commit_no_ref_reaches_keeps_a_worktree(repo_with_worktrees):
    """The one case that cannot be recovered after removal."""
    repo, home, inside, _ = repo_with_worktrees
    rows = index.worktree_rows(repo, {"s9": index.ENDED}, home, set())
    assert rows[0]["removable"] is False
    assert rows[0]["why"] == "commit reachable from no ref"


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
