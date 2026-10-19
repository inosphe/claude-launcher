"""cflow2 run slots name their daemon (claunch-9jmdz): ``runs/<session>@<daemon>``.

Two daemons (the default one and a named instance) number their sessions
independently, so both can have an ``s15`` standing in one directory. Before
the qualifier they shared ``runs/s15``. These tests cover the slot names, the
clock's ownership, reading a slot not moved yet, and the start-time move.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import textwrap
import types

import pytest

from claude_launcher.cflow2 import frontend, runtime, slot_migration
from claude_launcher.cflow2 import store as cstore
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_clock, cflow2_slots, paths

T0 = 1_000_000.0

CHART = """
cflow: 2
name: t
nodes:
  a: {prompt: do a}
body: {seq: [a]}
"""


@pytest.fixture(autouse=True)
def _default_daemon(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_DAEMON", raising=False)


def _start(cwd, scope, monkeypatch, daemon=""):
    if daemon:
        monkeypatch.setenv("CLAUNCH_DAEMON", daemon)
    else:
        monkeypatch.delenv("CLAUNCH_DAEMON", raising=False)
    try:
        return runtime.start(frontend.compile_text(textwrap.dedent(CHART)), cwd=str(cwd),
                             scope=scope, caps={}, driver=scope, now=T0)
    finally:
        monkeypatch.delenv("CLAUNCH_DAEMON", raising=False)


def _legacy(cwd, scope):
    """Turn the qualified slot ``scope`` into one written before the
    qualifier: renamed to its bare name, no daemon recorded."""
    runs = cwd / ".cflow2" / "runs"
    old = runs / cstore.unqualified(scope)
    os.rename(runs / scope, old)
    st = RunStore.at(old)
    with st.lock():
        state = st.load_state()
        state.pop(cstore.DAEMON_KEY, None)
        state["scope"] = cstore.unqualified(scope)
        st.commit(state, [])
    return old


# -- names ---------------------------------------------------------------------


def test_default_daemon_id_is_reserved():
    assert paths.DEFAULT_INSTANCE_ID == "main"
    assert paths.instance_id("") == "main" and paths.instance_id("kimyori") == "kimyori"
    with pytest.raises(ValueError, match="default daemon's id"):
        paths.validate_instance("main")
    assert paths.validate_instance("default") == "default"  # still a named instance


def test_known_instances_skip_the_reserved_name(tmp_path):
    root = paths.base_home() / "daemons"
    for name in ("main", "kimyori"):
        (root / name).mkdir(parents=True)
    assert "main" not in paths.known_instances()
    assert "kimyori" in paths.known_instances()


def test_scope_helpers(monkeypatch):
    assert cstore.normalize_scope("s15") == "s15@main"
    assert cstore.normalize_scope("s15+stack~d2") == "s15@main+stack~d2"
    assert cstore.normalize_scope("s15@kimyori+stack") == "s15@kimyori+stack"
    assert cstore.normalize_scope("default") == "default"
    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    assert cstore.normalize_scope("s15") == "s15@kimyori"
    monkeypatch.setenv("CLAUNCH_SESSION", "s15")
    assert cstore.normalize_scope(None) == "s15@kimyori"
    assert cstore.session_of("s15@main+stack~d2") == "s15"
    assert cstore.daemon_of("s15@main~d1") == "main" and cstore.daemon_of("s15") is None
    assert cstore.unqualified("s15@main+stack~d2") == "s15+stack~d2"
    assert cstore.main_scope("s15@main+stack~d2") == "s15@main"
    assert cstore.side_of("s15@main+stack~d2") == "stack"
    for bad in ("s15@", "@x", "s15+st@ack", "s1@a@b", "s1~d1@x"):
        with pytest.raises(cstore.StoreError):
            cstore.normalize_scope(bad)


def test_own_name(monkeypatch):
    assert cstore.own_name("s15@main") == "s15"
    assert cstore.own_name("s15@main+stack") == "s15+stack"
    assert cstore.own_name("s15@kimyori") is None
    assert cstore.own_name("s15") == "s15"  # not moved yet: read as before
    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    assert cstore.own_name("s15@kimyori") == "s15" and cstore.own_name("s15@main") is None


# -- two daemons, one directory ------------------------------------------------------


def test_same_session_name_on_two_daemons_gets_two_slots(tmp_path, monkeypatch):
    a = _start(tmp_path, "s15", monkeypatch)
    b = _start(tmp_path, "s15", monkeypatch, daemon="kimyori")
    assert a["scope"] == "s15@main" and b["scope"] == "s15@kimyori"
    assert a["run"] != b["run"]
    runs = tmp_path / ".cflow2" / "runs"
    assert sorted(p.name for p in runs.iterdir()) == ["s15@kimyori", "s15@main"]
    assert cstore.list_scopes(str(tmp_path)) == ["s15@kimyori", "s15@main"]
    assert RunStore(str(tmp_path), "s15").load_state()["run_id"] == a["run"]
    assert RunStore(str(tmp_path), "s15").load_state()[cstore.DAEMON_KEY] == "main"
    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    assert RunStore(str(tmp_path), "s15").load_state()["run_id"] == b["run"]


class _Mgr:
    def __init__(self, **sessions):
        self.sessions = sessions

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]

    def list(self):
        return list(self.sessions.values())


def _session(name, cwd, **kw):
    fields = {"exited": False, "held_at": None, "paused_at": None}
    fields.update(kw)
    return types.SimpleNamespace(sdef=types.SimpleNamespace(name=name, cwd=str(cwd)), **fields)


def test_clock_owns_only_its_daemons_slots(tmp_path):
    clock = cflow2_clock.Cflow2Clock(_Mgr(s15=_session("s15", tmp_path)), heartbeat=False,
                                     threaded=False, probe_runner=None)
    assert clock.owns("s15@main") and clock.owns("s15@main+stack~d1")
    assert not clock.owns("s15@kimyori") and not clock.owns("s15@kimyori+stack")
    assert clock.owns("s15")  # not moved yet: decided by the name, as before
    assert not clock.owns("s16@main")


def test_a_held_record_of_the_same_name_does_not_take_another_daemons_slot(tmp_path):
    # s328-rev2 on PR #56: a held (retired) s15 here made the other daemon's
    # s15 slot this daemon's
    held = _session("s15", tmp_path, exited=True, held_at=1.0)
    clock = cflow2_clock.Cflow2Clock(_Mgr(s15=held), heartbeat=False, threaded=False,
                                     probe_runner=None)
    assert clock.owns("s15@main")
    assert not clock.owns("s15@kimyori")


def test_clock_scan_ticks_only_its_own_slot(tmp_path, monkeypatch):
    _start(tmp_path, "s15", monkeypatch)
    _start(tmp_path, "s15", monkeypatch, daemon="kimyori")
    clock = cflow2_clock.Cflow2Clock(_Mgr(s15=_session("s15", tmp_path)), heartbeat=False,
                                     threaded=False, probe_runner=None)
    ticked = []
    monkeypatch.setattr(clock, "tick_slot", lambda cwd, scope, now, notices: ticked.append(scope))
    clock.scan(T0)
    assert ticked == ["s15@main"]


# -- slots written before the qualifier ------------------------------------------------


def test_a_slot_not_moved_is_read_in_place(tmp_path, monkeypatch):
    a = _start(tmp_path, "s15", monkeypatch)
    old = _legacy(tmp_path, "s15@main")
    st = RunStore(str(tmp_path), "s15")
    assert st.legacy and st.dir == old and st.scope == "s15@main"
    assert st.load_state()["run_id"] == a["run"]
    assert cstore.list_scopes(str(tmp_path)) == ["s15@main"]


def test_a_slot_not_moved_that_records_another_daemon_is_not_read(tmp_path, monkeypatch):
    _start(tmp_path, "s15", monkeypatch, daemon="kimyori")
    runs = tmp_path / ".cflow2" / "runs"
    os.rename(runs / "s15@kimyori", runs / "s15")  # the daemon is still recorded
    st = RunStore(str(tmp_path), "s15")
    assert not st.legacy and not st.exists()
    assert cstore.list_scopes(str(tmp_path)) == ["s15@kimyori"]


# -- the move ------------------------------------------------------------------


def _sessions(**daemons):
    return {d: dict(names) for d, names in daemons.items()}


def test_attribution():
    started = {"started": "2026-10-10T00:00:00+00:00"}
    one = _sessions(main={"s15": ("2026-10-09T00:00:00+00:00", None)})
    assert slot_migration.attribute("s15", dict(started), one)[0] == "main"
    assert slot_migration.attribute("s15", dict(started, daemon="kimyori"), one)[0] == "kimyori"
    both = _sessions(main={"s15": ("2026-10-09T00:00:00+00:00", "2026-10-09T12:00:00+00:00")},
                     kimyori={"s15": ("2026-10-09T18:00:00+00:00", None)})
    assert slot_migration.attribute("s15+stack", dict(started), both)[0] == "kimyori"
    alive = _sessions(main={"s15": ("2026-10-09T00:00:00+00:00", None)},
                      kimyori={"s15": ("2026-10-09T18:00:00+00:00", None)})
    owner, why = slot_migration.attribute("s15", dict(started), alive)
    assert owner is None and "all alive" in why
    assert slot_migration.attribute("s15", dict(started), _sessions(main={}))[0] is None


def test_requalify_rewrites_every_scope_the_run_keeps():
    state = {"scope": "s15+stack", "parent": {"cwd": "/x", "scope": "s15", "delegation": "d1"},
             "delegations": {"d2": {"nested": {"cwd": "/x", "scope": "s15+stack~d2"}},
                             "d3": {"status": "done"}},
             "side": {"name": "stack", "main": "s15", "main_run": "r"}}
    out = slot_migration.requalify(state, "main")
    assert out["scope"] == "s15@main+stack" and out[cstore.DAEMON_KEY] == "main"
    assert out["parent"]["scope"] == "s15@main"
    assert out["delegations"]["d2"]["nested"]["scope"] == "s15@main+stack~d2"
    assert out["delegations"]["d3"] == {"status": "done"}
    assert out["side"]["main"] == "s15@main"


def test_migrate_moves_own_slots_once_and_leaves_a_link(tmp_path, monkeypatch):
    a = _start(tmp_path, "s15", monkeypatch)
    _legacy(tmp_path, "s15@main")
    b = _start(tmp_path, "s16", monkeypatch, daemon="kimyori")
    _legacy(tmp_path, "s16@kimyori")
    _start(tmp_path, "s17", monkeypatch)
    _legacy(tmp_path, "s17@main")
    sessions = _sessions(main={"s15": (None, None), "s17": (None, None)},
                         kimyori={"s16": (None, None), "s17": (None, None)})
    plan = slot_migration.migrate([str(tmp_path)], "main", sessions)
    assert [(e.slot, e.action) for e in plan.entries] == [
        ("s15", "move"), ("s16", "other"), ("s17", "leave")]
    runs = tmp_path / ".cflow2" / "runs"
    assert (runs / "s15").is_dir() and not (runs / "s15").is_symlink()  # a plan moves nothing

    done = slot_migration.migrate([str(tmp_path)], "main", sessions, apply=True)
    assert [(e.slot, e.action, e.target) for e in done.of(slot_migration.MOVE)] == [
        ("s15", "move", "s15@main")]
    if os.name != "nt" or (runs / "s15").is_symlink():  # move() only warns when Windows withholds the privilege
        assert (runs / "s15").is_symlink() and os.readlink(runs / "s15") == "s15@main"
    st = RunStore(str(tmp_path), "s15")
    assert not st.legacy and st.dir == runs / "s15@main"
    state = st.load_state()
    assert state["run_id"] == a["run"] and state["scope"] == "s15@main"
    assert state[cstore.DAEMON_KEY] == "main"
    assert st.journal()[-1]["event"] == slot_migration.EVENT
    # the link is not a slot of its own; an unmoved slot that recorded no
    # daemon is listed as this one's (s16), as every slot was before
    assert cstore.list_scopes(str(tmp_path)) == ["s15@main", "s16@main", "s17@main"]

    again = slot_migration.migrate([str(tmp_path)], "main", sessions, apply=True)
    assert [(e.slot, e.action) for e in again.entries] == [("s16", "other"), ("s17", "leave")]
    # the other daemon moves its own
    slot_migration.migrate([str(tmp_path)], "kimyori", sessions, apply=True)
    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    assert RunStore(str(tmp_path), "s16").load_state()["run_id"] == b["run"]
    assert not RunStore(str(tmp_path), "s16").legacy


def test_migrate_leaves_a_slot_whose_target_exists(tmp_path, monkeypatch):
    _start(tmp_path, "s15", monkeypatch)
    _legacy(tmp_path, "s15@main")
    runs = tmp_path / ".cflow2" / "runs"
    shutil.copytree(runs / "s15", runs / "s15@main")  # a slot already at the new name
    assert (runs / "s15@main").is_dir() and (runs / "s15").is_dir()
    done = slot_migration.migrate([str(tmp_path)], "main", _sessions(main={"s15": (None, None)}),
                                  apply=True)
    (entry,) = done.entries
    assert entry.action == "leave" and "already exists" in entry.why
    assert (runs / "s15").is_dir() and not (runs / "s15").is_symlink()


# -- at daemon start ---------------------------------------------------------------


def _write_sessions(db, **sessions):
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (name TEXT PRIMARY KEY, data TEXT NOT NULL, "
                 "updated_at TEXT)")
    for name, (created, exited) in sessions.items():
        conn.execute("INSERT INTO sessions VALUES (?, ?, NULL)",
                     (name, json.dumps({"created_at": created, "exited_at": exited})))
    conn.commit()
    conn.close()


def test_run_at_start_moves_once_and_clears_an_empty_reserved_instance(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _start(proj, "s15", monkeypatch)
    _legacy(proj, "s15@main")
    _write_sessions(cflow2_slots.instance_dir("") / "sessions.db", s15=(None, None))
    reserved = cflow2_slots.instance_dir("main")
    _write_sessions(reserved / "sessions.db")
    kept = cflow2_slots.instance_dir("default")
    kept.mkdir(parents=True)
    mgr = _Mgr(s15=_session("s15", proj))

    doc = cflow2_slots.run_at_start(mgr)
    assert [m["slot"] for m in doc["moved"]] == ["s15"] and doc["daemon"] == "main"
    assert doc["reserved"] == "removed" and not reserved.exists() and kept.is_dir()
    assert json.loads(cflow2_slots.marker_path().read_text(encoding="utf-8"))["version"] == 1
    assert (proj / ".cflow2" / "runs" / "s15@main").is_dir()
    assert cflow2_slots.run_at_start(mgr) is None  # once


def test_a_reserved_instance_with_sessions_is_kept(tmp_path):
    reserved = cflow2_slots.instance_dir("main")
    _write_sessions(reserved / "sessions.db", s1=("2026-10-01T00:00:00+00:00", None))
    assert cflow2_slots.clear_reserved().startswith("kept") and reserved.is_dir()


def test_slots_cli_is_read_only(tmp_path, monkeypatch, capsys):
    from claude_launcher import cli_cflow2
    import argparse

    _start(tmp_path, "s15", monkeypatch)
    _legacy(tmp_path, "s15@main")
    _write_sessions(cflow2_slots.instance_dir("") / "sessions.db", s15=(None, None))
    rc = cli_cflow2._cmd_slots(argparse.Namespace(cwd=str(tmp_path), json=True))
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert [m["target"] for m in doc["moved"]] == ["s15@main"]
    runs = tmp_path / ".cflow2" / "runs"
    assert (runs / "s15").is_dir() and not (runs / "s15").is_symlink()
    assert not (runs / "s15@main").exists()


def test_flow_start_takes_the_session_name_as_its_own_slot(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import mcp as cflow2_mcp

    (tmp_path / ".claunch" / "workflows" / "v2").mkdir(parents=True)
    (tmp_path / ".claunch" / "workflows" / "v2" / "t.yaml").write_text(textwrap.dedent(CHART),
                                                              encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "s15")
    out = cflow2_mcp.call_tool("flow_start", {"workflow": "t", "scope": "s15"})
    assert out["scope"] == "s15@main"
    with pytest.raises(runtime.FlowError, match="another's"):
        cflow2_mcp.call_tool("flow_start", {"workflow": "t", "scope": "s15@kimyori"})


def test_holders_are_this_daemons_runs_only(tmp_path, monkeypatch):
    chart = CHART + textwrap.dedent("""
        collections:
          landing:
            kind: queue
            key: issue
            item: {issue: str}
        """)
    for daemon in ("", "kimyori"):
        if daemon:
            monkeypatch.setenv("CLAUNCH_DAEMON", daemon)
        runtime.start(frontend.compile_text(textwrap.dedent(chart)), cwd=str(tmp_path),
                      scope="s15", caps={}, driver="s15", now=T0)
        monkeypatch.delenv("CLAUNCH_DAEMON", raising=False)
    assert [s for _, s in runtime.holders("landing", "s15", [str(tmp_path)])] == ["s15@main"]
    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    assert [s for _, s in runtime.holders("landing", "s15", [str(tmp_path)])] == ["s15@kimyori"]
