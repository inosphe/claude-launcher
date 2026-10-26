"""daemon/basetemp_reaper.py: an ended session's basetemps go, and leave a row.

claunch-4zc51.2. Every case runs against a tmp root and a tmp history file;
nothing here touches the real ``C:/t``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import basetemp_reaper as br


def _run_tree(root: Path, name: str, *, workers: int = 2, summary: dict | None = None) -> Path:
    path = root / name
    for i in range(workers):
        gw = path / f"popen-gw{i}" / "test_x0"
        gw.mkdir(parents=True)
        (gw / "out.txt").write_text("x" * 10, encoding="utf-8")
    path.mkdir(parents=True, exist_ok=True)
    if summary is not None:
        (path / br.RUN_SUMMARY).write_text(json.dumps(summary), encoding="utf-8")
    return path


def _reaper(
    tmp_path: Path,
    sessions: list,
    foreign: set = frozenset(),
    grants: set = frozenset(),
) -> br.BasetempReaper:
    return br.BasetempReaper(
        lambda: list(sessions),
        root=tmp_path / "t",
        history=tmp_path / "daemon" / br.HISTORY_NAME,
        background=False,
        foreign=lambda: set(foreign),
        grants=lambda: set(grants),
    )


def _aged(path: Path, hours: float) -> Path:
    """Set ``path``'s mtime (and its entries') ``hours`` into the past."""
    when = time.time() - hours * 3600.0
    for root, dirs, names in os.walk(path):
        for name in [*dirs, *names]:
            os.utime(os.path.join(root, name), (when, when))
    os.utime(path, (when, when))
    return path


def _rows(tmp_path: Path) -> list:
    text = (tmp_path / "daemon" / br.HISTORY_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _session(name: str) -> SimpleNamespace:
    return SimpleNamespace(sdef=SimpleNamespace(name=name))


# ---- naming ---------------------------------------------------------------- #

@pytest.mark.parametrize("name, session, expected", [
    ("s995c1011010955", "s995", True),
    ("s995w", "s995", True),
    ("s995w2", "s995", True),           # a numbered sweep generation
    ("s995-iso", "s995", True),
    ("s995", "s995", True),             # the bare name: the general test directory
    ("s995-", "s995", False),
    ("s995c101101095", "s995", False),  # nine digits: not changed_tests' stamp
    ("s995ww", "s995", False),
    ("s995c1011010955", "s99", False),  # s99 is a prefix of s995, not its owner
    ("s99c1011010955", "s995", False),
])
def test_matches_only_the_shapes(name, session, expected):
    assert br.matches(name, session) is expected


@pytest.mark.parametrize("name, session, expected", [
    ("s995w", "s995", True),
    ("s995w2", "s995", True),
    # A sweep run from a differently-named worktree: sweep.py names the
    # basetemp after $CLAUNCH_SESSION, which is that worktree's session.
    ("s995-07cd9bfd7e2cbe0dfc551b74w", "s995", True),
    ("s995-07cd9bfd7e2cbe0dfc551b74w3", "s995", True),
    ("s995-egr6yt1014051949", "s995", True),   # the stamped form
    ("s995-iso", "s995", False),        # a basetemp pinned by hand
    ("s995-ctrlj-treeiso", "s995", False),
    ("s995", "s995", False),            # the general test directory
    ("s995-", "s995", False),
    ("s995ww", "s995", False),
    ("s99-07cd9bfd7e2cbe0dfc551b74w", "s995", False),
])
def test_sweep_generation_is_the_swept_shapes_only(name, session, expected):
    assert br.sweep_generation(name, session) is expected


def test_is_sweep_tree_wants_the_xdist_workers(tmp_path):
    # The name is only the gate: a hand-pinned basetemp can end in "w" and a
    # serial run leaves no popen-gw*, so the content has to decide
    # (claunch-9gpqk: s677-narrow, s90-ipo2q-preview are not sweeps).
    xdist = _run_tree(tmp_path / "t", "s1w", workers=2)
    serial = _run_tree(tmp_path / "t", "s1-notasweepw", workers=0)
    assert br.is_sweep_tree(xdist) is True
    assert br.is_sweep_tree(serial) is False
    assert br.is_sweep_tree(tmp_path / "missing") is False


def test_owner_is_the_longest_matching_name():
    names = ["s995", "s995-rev2", "s99"]
    assert br.owner("s995-rev2c1011010955", names) == "s995-rev2"
    assert br.owner("s995-iso", names) == "s995"
    assert br.owner("s995c1011010955", names) == "s995"
    assert br.owner("s99w", names) == "s99"
    assert br.owner("s995", names) == "s995"
    assert br.owner("unrelated", names) is None
    assert br.owner("s995-rev2c1011010955", set(names)) == "s995-rev2"


def test_owner_by_prefix_lookup_agrees_with_scanning_every_session():
    # claunch-2r7vj replaced the scan over every session with a lookup of the
    # name's prefixes; the answer has to be the one the scan gave.
    def scanned(name, sessions):
        best = None
        for session in sessions:
            if br.matches(name, session) and (best is None or len(session) > len(best)):
                best = session
        return best

    sessions = ["s9", "s99", "s995", "s995-rev2", "s29", "s29-egr6y", "sweep", ""]
    dirs = [
        "s995-rev2c1011010955", "s995-iso", "s99w", "s99w15", "s9-x", "s9",
        "s29-07cd9bfd7e2cbe0dfc551b74w", "s29-egr6yt1014051949", "s29w",
        "s995x", "s9950", "sweepw", "dfv2-a", "s1162tree3", "", "s",
    ]
    for name in dirs:
        assert br.owner(name, sessions) == scanned(name, sessions), name
        assert br.owner(name, set(sessions)) == scanned(name, sessions), name


# ---- the exit hook ---------------------------------------------------------- #

def test_exit_removes_the_sessions_basetemps_and_leaves_a_row(tmp_path):
    root = tmp_path / "t"
    summary = {"tree": "abc123", "exit_code": 0, "counts": {"passed": 3}, "workers": 2}
    _run_tree(root, "s995c1011010955", workers=2, summary=summary)
    _run_tree(root, "s995w", workers=4)
    _run_tree(root, "s995-iso", workers=0)

    _reaper(tmp_path, [("s995", True)]).session_exited(_session("s995"))

    assert list(root.iterdir()) == []
    rows = {Path(r["path"]).name: r for r in _rows(tmp_path)}
    assert set(rows) == {"s995c1011010955", "s995w", "s995-iso"}
    row = rows["s995c1011010955"]
    assert row["session"] == "s995"
    assert row["outcome"] == "removed"
    assert row["workers"] == 2
    assert row["files"] == 3
    assert row["run"] == summary
    assert "started_at" in row and "ended_at" in row and "reaped_at" in row
    assert rows["s995w"]["workers"] == 4


def test_exit_removes_the_general_test_directory_too(tmp_path):
    """``<root>/<session>`` ($CLAUNCH_TEST_DIR) is reaped with the basetemps.

    That is the kill/archive cleanup: whatever a session wrote there
    (browser profiles, playwright reports) goes when the session ends,
    without the session remembering to clean it.
    """
    root = tmp_path / "t"
    general = root / "s995"
    (general / "browser-profile").mkdir(parents=True)
    (general / "browser-profile" / "prefs.js").write_text("x", encoding="utf-8")
    keep = _run_tree(root, "s995c1011010955")

    _reaper(tmp_path, [("s995", True)]).session_exited(_session("s995"))

    assert not general.exists()
    assert not keep.exists()
    names = {Path(r["path"]).name for r in _rows(tmp_path)}
    assert names == {"s995", "s995c1011010955"}


def test_exit_leaves_other_sessions_with_nested_names_alone(tmp_path):
    root = tmp_path / "t"
    _run_tree(root, "s995c1011010955")
    keep = [
        _run_tree(root, "s99c1011010955"),        # shorter name, different session
        _run_tree(root, "s9951c1011010955"),      # longer name, different session
        _run_tree(root, "s995-rev2c1011010955"),  # a live session named s995-rev2
        _run_tree(root, "s995-rev2w"),
    ]
    sessions = [("s995", True), ("s99", False), ("s9951", False), ("s995-rev2", False)]

    _reaper(tmp_path, sessions).session_exited(_session("s995"))

    assert sorted(root.iterdir()) == sorted(keep)
    assert [Path(r["path"]).name for r in _rows(tmp_path)] == ["s995c1011010955"]


def test_files_outside_the_root_are_never_touched(tmp_path):
    # sweep.py's "<session>w" can coincide with a leader's sweep worktree
    # name (claunch-sweep-basetemp-collision-77x); only the root is looked at.
    elsewhere = tmp_path / "worktrees" / "s995w"
    elsewhere.mkdir(parents=True)
    (tmp_path / "t").mkdir()

    _reaper(tmp_path, [("s995", True)]).session_exited(_session("s995"))

    assert elsewhere.is_dir()


def test_a_directory_that_cannot_be_removed_is_skipped_and_recorded(tmp_path, monkeypatch):
    root = tmp_path / "t"
    locked = _run_tree(root, "s995c1011010955")
    _run_tree(root, "s995c1011020000")
    real_rmtree = br.shutil.rmtree

    def rmtree(path, **kw):
        if Path(path) == locked:
            handler = kw.get("onexc") or kw.get("onerror")
            handler(os.unlink, str(locked / "popen-gw0"), PermissionError())
            return
        real_rmtree(path, **kw)

    monkeypatch.setattr(br.shutil, "rmtree", rmtree)
    _reaper(tmp_path, [("s995", True)]).session_exited(_session("s995"))

    rows = {Path(r["path"]).name: r for r in _rows(tmp_path)}
    assert rows["s995c1011010955"]["outcome"] == "locked"
    assert rows["s995c1011010955"]["locked"]
    assert rows["s995c1011020000"]["outcome"] == "removed"
    assert locked.is_dir()
    # The retry added the write bit; it must not have taken read and search
    # away -- a 0200 directory is one no later rmtree can list (claunch-ersj2).
    mode = (locked / "popen-gw0").stat().st_mode
    assert mode & stat.S_IRUSR and mode & stat.S_IXUSR and mode & stat.S_IWUSR
    shutil.rmtree(root)


def test_read_only_files_are_removed(tmp_path):
    # git writes .git/objects read-only; Windows' os.unlink refuses those
    # until the bit is cleared (claunch-4zc51.3: 1129 such files kept two
    # deployed-daemon basetemps "locked" on every pass).
    root = tmp_path / "t"
    tree = _run_tree(root, "s995c1011010955")
    obj = tree / "popen-gw0" / "test_x0" / "repo" / ".git" / "objects" / "03"
    obj.mkdir(parents=True)
    for name in ("e0bec8", "1d0413"):
        (obj / name).write_bytes(b"blob")
        os.chmod(obj / name, stat.S_IREAD)

    _reaper(tmp_path, [("s995", True)]).session_exited(_session("s995"))

    assert not tree.exists()
    [row] = _rows(tmp_path)
    assert row["outcome"] == "removed"
    assert "locked" not in row


def test_an_unlistable_0200_tree_is_repaired_and_removed(tmp_path):
    # claunch-zbu54: a basetemp written before the claunch-ersj2 fix is 0200,
    # so rmtree fails on os.scandir before removing anything. The removal
    # retry never reached it: 9,135 of 10,033 history rows on 2026-10-22 were
    # "locked", the same trees over and over, and their inodes were never
    # freed. The walk needs read and search back, not the write bit.
    root = tmp_path / "t"
    tree = _run_tree(root, "s995c1011010955")
    for path in (tree / "popen-gw0", tree):
        os.chmod(path, 0o200)

    _reaper(tmp_path, [("s995", True)]).session_exited(_session("s995"))

    assert not tree.exists()
    [row] = _rows(tmp_path)
    assert row["outcome"] == "removed"
    assert "locked" not in row


@pytest.mark.parametrize("func", [os.open, os.scandir, os.lstat])
def test_a_failed_walk_call_is_recorded_not_retried(tmp_path, monkeypatch, func):
    # POSIX rmtree reports os.open/os.scandir/os.lstat failures too; calling
    # os.open(path) again raises TypeError, which must not escape the reap
    # (s995-rev3, CHANGES REQUESTED @ aa27b127).
    root = tmp_path / "t"
    stuck = _run_tree(root, "s995c1011010955")
    entry = str(stuck / "popen-gw0")

    def rmtree(path, **kw):
        handler = kw.get("onexc") or kw.get("onerror")
        handler(func, entry, PermissionError())

    monkeypatch.setattr(br.shutil, "rmtree", rmtree)
    _reaper(tmp_path, [("s995", True)]).session_exited(_session("s995"))

    [row] = _rows(tmp_path)
    assert row["outcome"] == "locked"
    assert row["locked"] == [Path(entry).as_posix()]
    assert stuck.is_dir()


# ---- the boot pass ---------------------------------------------------------- #

def test_boot_reaps_ended_sessions_and_spares_running_ones(tmp_path):
    root = tmp_path / "t"
    _run_tree(root, "s1c1011010955")
    alive = _run_tree(root, "s2c1011010955")
    sessions = [("s1", True), ("s2", False)]

    _reaper(tmp_path, sessions).reap_ended()

    assert list(root.iterdir()) == [alive]
    assert [r["session"] for r in _rows(tmp_path)] == ["s1"]


def test_boot_lists_the_root_once_for_every_ended_session(tmp_path, monkeypatch):
    # claunch-2r7vj: a listing per ended session made the boot pass
    # (names x entries x sessions); one listing serves them all.
    root = tmp_path / "t"
    for name in ("s1", "s2", "s3", "s30"):
        _run_tree(root, f"{name}c1011010955")
    alive = _run_tree(root, "s4c1011010955")
    sessions = [("s1", True), ("s2", True), ("s3", True), ("s30", True), ("s4", False)]
    listed = []
    real = Path.iterdir

    def counting(self):
        if self == root:
            listed.append(self)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", counting)
    _reaper(tmp_path, sessions).reap_ended()

    assert len(listed) == 1
    assert list(root.iterdir()) == [alive]
    by_session = {r["session"]: Path(r["path"]).name for r in _rows(tmp_path)}
    assert by_session == {
        "s1": "s1c1011010955", "s2": "s2c1011010955",
        "s3": "s3c1011010955", "s30": "s30c1011010955",
    }


def test_registry_view_treats_held_and_os_ended_sessions_as_running():
    def rec(name, exited, **extra):
        return SimpleNamespace(sdef=SimpleNamespace(name=name), exited=exited, **extra)

    manager = SimpleNamespace(list=lambda: [
        rec("gone", True),
        rec("live", False),
        rec("held", True, held_at="2026-10-11T00:00:00+00:00"),
        rec("oslogoff", True, ended_by_os=True),
    ])
    assert br.registry_view(manager)() == [
        ("gone", True), ("live", False), ("held", False), ("oslogoff", False),
    ]


def test_a_daemon_shutdown_runs_no_exit_hooks(tmp_path):
    # The manager's guard is what keeps a restart from deleting anything:
    # shutting_down short-circuits the funnel the reaper hangs off.
    from claude_launcher.daemon.manager import SessionManager

    called = []
    fake = SimpleNamespace(shutting_down=True, exit_hooks=[called.append])
    SessionManager._session_exited(fake, _session("s995"))
    assert called == []


# ---- the periodic pass (claunch-ssroc) --------------------------------------- #
# Boot and the exit funnel only see a session end, so a leader that sweeps
# between handoffs used to keep every generation until the next handoff.

def test_the_periodic_pass_keeps_the_newest_generation_and_ages_out_the_rest(tmp_path):
    root = tmp_path / "t"
    newest = _aged(_run_tree(root, "s29w"), 0)
    old = _aged(_run_tree(root, "s29w5"), 7)
    old2 = _aged(_run_tree(root, "s29w4"), 9)
    fresh = _aged(_run_tree(root, "s29w2"), 1)     # not oldest, but still young

    rows = _reaper(tmp_path, [("s29", False)]).reap_live()

    assert newest.is_dir()
    assert fresh.is_dir()
    assert not old.exists() and not old2.exists()
    assert {Path(r["path"]).name for r in rows} == {"s29w5", "s29w4"}
    assert {r["reason"] for r in rows} == {"sweep-generation"}
    assert {r["session"] for r in rows} == {"s29"}
    assert {r["outcome"] for r in rows} == {"removed"}
    assert {Path(r["path"]).name for r in _rows(tmp_path)} == {"s29w5", "s29w4"}


def test_the_periodic_pass_reaps_a_worktree_sweeps_generartions(tmp_path):
    # claunch-9gpqk: sweep.py names the basetemp after $CLAUNCH_SESSION, and
    # a leader's sweep runs from a differently-named worktree, so the tree is
    # "<session>-<label>w". Once that worktree session is gone from the
    # registry, owner() collapses the name to the leader -- alive, so neither
    # the generation branch nor the orphan branch used to reach it (339 such
    # trees under s29 on 2026-10-22).
    root = tmp_path / "t"
    newest = _aged(_run_tree(root, "s29-ca5e348ea55ca759c74e5529w"), 0)
    old = _aged(_run_tree(root, "s29-0127c4f7c5d6cbd8e1cc25b0w"), 24 * 9)
    old2 = _aged(_run_tree(root, "s29-07cd9bfd7e2cbe0dfc551b74w"), 24 * 7)
    young = _aged(_run_tree(root, "s29-ff00ff00ff00ff00ff00ff00w"), 2)
    stamped = _aged(_run_tree(root, "s29-egr6yt1014051949"), 24 * 8)
    # Name says sweep, content says a hand-pinned serial basetemp: kept.
    serial_w = _aged(_run_tree(root, "s29-ipo2q-previeww", workers=0), 24 * 9)
    # Not a sweep shape at all: kept.
    pinned = _aged(_run_tree(root, "s29-ctrlj-treeiso"), 24 * 9)

    rows = _reaper(tmp_path, [("s29", False)]).reap_live()

    assert newest.is_dir()
    assert young.is_dir()
    assert serial_w.is_dir()
    assert pinned.is_dir()                # a basetemp pinned by hand
    assert not old.exists() and not old2.exists() and not stamped.exists()
    assert {Path(r["path"]).name for r in rows} == {
        "s29-0127c4f7c5d6cbd8e1cc25b0w", "s29-07cd9bfd7e2cbe0dfc551b74w",
        "s29-egr6yt1014051949"}


def test_the_periodic_pass_keeps_a_generation_a_live_grant_is_using(tmp_path):
    # The session holds the measurement window: its run is writing into
    # <session>w right now, so no generation of it goes.
    root = tmp_path / "t"
    held = _aged(_run_tree(root, "s29w"), 9)
    other = _aged(_run_tree(root, "s29w4"), 9)

    rows = _reaper(tmp_path, [("s29", False)], grants={"s29"}).reap_live()

    assert held.is_dir() and other.is_dir()
    assert rows == []


def test_the_periodic_pass_leaves_ended_foreign_and_other_names_alone(tmp_path):
    root = tmp_path / "t"
    ended = _aged(_run_tree(root, "s1w"), 9)
    foreign = _aged(_run_tree(root, "s2w"), 9)
    nested = _aged(_run_tree(root, "s995-rev2w"), 9)
    mine = _aged(_run_tree(root, "s3w9"), 9)
    keep_newest = _aged(_run_tree(root, "s3w"), 0)
    sessions = [("s1", True), ("s2", False), ("s995", False),
                ("s995-rev2", False), ("s3", False)]

    alien = _aged(_run_tree(root, "s7w"), 24 * 9)   # only s7 (elsewhere) knows it

    rows = _reaper(tmp_path, sessions, foreign={"s2", "s7"}).reap_live()

    assert ended.is_dir()                 # the exit funnel's business
    assert foreign.is_dir()               # live under another instance
    assert alien.is_dir()                 # orphan-shaped, but a foreign live name
    assert nested.is_dir()                # another live session's
    assert keep_newest.is_dir()
    assert not mine.exists()
    assert [Path(r["path"]).name for r in rows] == ["s3w9"]


def test_the_periodic_pass_reaps_only_old_orphan_names(tmp_path):
    root = tmp_path / "t"
    stale = _aged(_run_tree(root, "sweepw"), 24 * 8)
    young = _aged(_run_tree(root, "dfv2-abc"), 24 * 3)
    owned = _aged(_run_tree(root, "s1c1011010955"), 24 * 8)

    rows = _reaper(tmp_path, [("s1", False)]).reap_live()

    assert not stale.exists()
    assert young.is_dir()                 # younger than basetemp_orphan_days
    assert owned.is_dir()                 # a known name, still running
    assert [(Path(r["path"]).name, r["orphan"]) for r in rows] == [("sweepw", True)]
    assert rows[0]["session"] is None


def test_the_periodic_pass_reads_its_thresholds_from_the_config(tmp_path):
    from claude_launcher import store

    root = tmp_path / "t"
    young = _aged(_run_tree(root, "s1w"), 3)
    _aged(_run_tree(root, "s1w2"), 4)
    store.set_daemon_field("basetemp_sweep_hours", 0)
    store.set_daemon_field("basetemp_orphan_days", 0)

    rows = _reaper(tmp_path, [("s1", False)]).reap_live()

    assert young.is_dir()
    assert rows == []
    assert br.settings() == (0.0, 0.0)


def test_the_clock_runs_the_pass_off_the_event_loop_and_stops(tmp_path):
    root = tmp_path / "t"
    old = _aged(_run_tree(root, "s1w2"), 9)
    _aged(_run_tree(root, "s1w"), 0)
    reaper = _reaper(tmp_path, [("s1", False)])

    async def scenario():
        clock = br.BasetempClock(reaper, poll=0.01)
        clock.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if clock.last is not None:
                break
        await clock.shutdown()
        return clock.last

    rows = asyncio.run(scenario())

    assert not old.exists()
    assert [Path(r["path"]).name for r in rows] == ["s1w2"]


# ---- the history file -------------------------------------------------------- #

def test_history_drops_rows_past_retention(tmp_path):
    history = tmp_path / "daemon" / br.HISTORY_NAME
    history.parent.mkdir(parents=True)
    old = {"session": "s0", "path": "x", "reaped_at": "2000-01-01T00:00:00+00:00"}
    history.write_text(json.dumps(old) + "\n", encoding="utf-8")
    _run_tree(tmp_path / "t", "s1c1011010955")

    _reaper(tmp_path, [("s1", True)]).reap_ended()

    assert [r["session"] for r in _rows(tmp_path)] == ["s1"], (
        history.read_text(encoding="utf-8"), sorted(p.name for p in history.parent.iterdir()))


def test_history_write_survives_a_reader_holding_the_file(tmp_path):
    # A scanner or indexer holding the history file for a moment made
    # os.replace answer WinError 5 and the row was never written (claunch-xqk1w).
    history = tmp_path / "daemon" / br.HISTORY_NAME
    history.parent.mkdir(parents=True)
    history.write_text("", encoding="utf-8")
    _run_tree(tmp_path / "t", "s1c1011010955")

    def release_soon(handle):
        time.sleep(0.1)
        handle.close()

    with open(history, "rb") as handle:  # no FILE_SHARE_DELETE, as Python's open()
        threading.Thread(target=release_soon, args=(handle,)).start()
        _reaper(tmp_path, [("s1", True)]).reap_ended()

    assert [r["session"] for r in _rows(tmp_path)] == ["s1"]


def test_the_suite_never_reaps_the_real_root(tmp_path):
    # conftest points the default root into tmp_path: tests that boot the
    # real daemon (test_restart_gate, test_daemon_restart, ...) reach
    # reap_ended() over registries of made-up names.
    assert br.default_root() == tmp_path / ".basetemp-root"
    assert br.BasetempReaper(lambda: []).root == tmp_path / ".basetemp-root"


# ---- other daemon instances -------------------------------------------------- #
# Names are per instance; C:/t is per machine (s995-rev2, CHANGES REQUESTED @
# 7fadbd8c: local2 and migtest exist on this host and hand out their own sN).

def test_a_name_live_in_another_instance_is_kept(tmp_path):
    root = tmp_path / "t"
    theirs = _run_tree(root, "s1c1011010955")
    _run_tree(root, "s2c1011010955")
    sessions = [("s1", True), ("s2", True)]

    _reaper(tmp_path, sessions, foreign={"s1"}).reap_ended()

    assert list(root.iterdir()) == [theirs]
    assert [r["session"] for r in _rows(tmp_path)] == ["s2"]


def _record(name: str, **extra) -> dict:
    return {"def": {"name": name}, "was_running": False, **extra}


def test_foreign_live_reads_every_other_instances_registry(home, monkeypatch):
    import sqlite3

    # This daemon is the named instance "mine"; the default and "local2"
    # are the others. One keeps a sessions.db, the other only the legacy
    # sessions.json an older instance still has.
    monkeypatch.setenv("CLAUNCH_DAEMON", "mine")
    mine = home / "daemons" / "mine"
    mine.mkdir(parents=True)
    (mine / "sessions.json").write_text(
        json.dumps([_record("s9", was_running=True)]), encoding="utf-8")

    default = home / "daemon"
    default.mkdir()
    conn = sqlite3.connect(default / "sessions.db")
    conn.execute("CREATE TABLE sessions (name TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT)")
    for rec in (_record("s0", was_running=True), _record("s3"),
                _record("s4", held_at="2026-10-11T00:00:00+00:00")):
        conn.execute("INSERT INTO sessions (name, data) VALUES (?, ?)",
                     (rec["def"]["name"], json.dumps(rec)))
    conn.commit()
    conn.close()

    local2 = home / "daemons" / "local2"
    local2.mkdir(parents=True)
    (local2 / "sessions.json").write_text(
        json.dumps([_record("s1", was_running=True), _record("s2")]), encoding="utf-8")

    assert br.foreign_live() == {"s0", "s4", "s1"}


def test_foreign_live_survives_unreadable_registries(home, monkeypatch):
    monkeypatch.setenv("CLAUNCH_DAEMON", "mine")
    broken = home / "daemons" / "broken"
    broken.mkdir(parents=True)
    (broken / "sessions.json").write_text("{not json", encoding="utf-8")
    (home / "daemon").mkdir()
    (home / "daemon" / "sessions.db").write_bytes(b"not a database")

    assert br.foreign_live() == set()
