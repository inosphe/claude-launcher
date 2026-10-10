"""daemon/basetemp_reaper.py: an ended session's basetemps go, and leave a row.

claunch-4zc51.2. Every case runs against a tmp root and a tmp history file;
nothing here touches the real ``C:/t``.
"""

from __future__ import annotations

import json
import os
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


def _reaper(tmp_path: Path, sessions: list) -> br.BasetempReaper:
    return br.BasetempReaper(
        lambda: list(sessions),
        root=tmp_path / "t",
        history=tmp_path / "daemon" / br.HISTORY_NAME,
        background=False,
    )


def _rows(tmp_path: Path) -> list:
    text = (tmp_path / "daemon" / br.HISTORY_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _session(name: str) -> SimpleNamespace:
    return SimpleNamespace(sdef=SimpleNamespace(name=name))


# ---- naming ---------------------------------------------------------------- #

@pytest.mark.parametrize("name, session, expected", [
    ("s995c1011010955", "s995", True),
    ("s995w", "s995", True),
    ("s995-iso", "s995", True),
    ("s995", "s995", False),            # the bare name is nobody's basetemp shape
    ("s995-", "s995", False),
    ("s995c101101095", "s995", False),  # nine digits: not changed_tests' stamp
    ("s995ww", "s995", False),
    ("s995c1011010955", "s99", False),  # s99 is a prefix of s995, not its owner
    ("s99c1011010955", "s995", False),
])
def test_matches_only_the_three_shapes(name, session, expected):
    assert br.matches(name, session) is expected


def test_owner_is_the_longest_matching_name():
    names = ["s995", "s995-rev2", "s99"]
    assert br.owner("s995-rev2c1011010955", names) == "s995-rev2"
    assert br.owner("s995-iso", names) == "s995"
    assert br.owner("s995c1011010955", names) == "s995"
    assert br.owner("s99w", names) == "s99"
    assert br.owner("unrelated", names) is None


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


# ---- the boot pass ---------------------------------------------------------- #

def test_boot_reaps_ended_sessions_and_spares_running_ones(tmp_path):
    root = tmp_path / "t"
    _run_tree(root, "s1c1011010955")
    alive = _run_tree(root, "s2c1011010955")
    sessions = [("s1", True), ("s2", False)]

    _reaper(tmp_path, sessions).reap_ended()

    assert list(root.iterdir()) == [alive]
    assert [r["session"] for r in _rows(tmp_path)] == ["s1"]


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


# ---- the history file -------------------------------------------------------- #

def test_history_drops_rows_past_retention(tmp_path):
    history = tmp_path / "daemon" / br.HISTORY_NAME
    history.parent.mkdir(parents=True)
    old = {"session": "s0", "path": "x", "reaped_at": "2000-01-01T00:00:00+00:00"}
    history.write_text(json.dumps(old) + "\n", encoding="utf-8")
    _run_tree(tmp_path / "t", "s1c1011010955")

    _reaper(tmp_path, [("s1", True)]).reap_ended()

    assert [r["session"] for r in _rows(tmp_path)] == ["s1"]


def test_the_suite_never_reaps_the_real_root(tmp_path):
    # conftest points the default root into tmp_path: tests that boot the
    # real daemon (test_restart_gate, test_daemon_restart, ...) reach
    # reap_ended() over registries of made-up names.
    assert br.default_root() == tmp_path / ".basetemp-root"
    assert br.BasetempReaper(lambda: []).root == tmp_path / ".basetemp-root"
