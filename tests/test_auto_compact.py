"""A claude session's auto-compact switch and threshold (claunch-5iq22).

Both are two keys of claude's env — ``DISABLE_AUTO_COMPACT`` and
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` — held in the session definition's own
env, which is the spawn env's last layer. These tests pin each way in:

- the create API takes them as ``env`` and the spawned claude sees them over
  the profile's ``auto_compact_at``;
- a spawn inherits the parent's answer and may replace it with
  ``auto_compact``, ungated by the spawn policy, claude children only;
- :meth:`SessionManager.auto_compact` and its route restart a running
  session with the answer changed, refuse a no-op or a bad value before
  stopping anything, and the answer survives a daemon restart;
- the context gauge draws no compaction tick for a session that does not
  compact automatically.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import auto_compact, lineage, profile as profile_mod
from claude_launcher import spawn, store
from claude_launcher.daemon import ctxsize, harness as harness_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshManager

OFF = auto_compact.AUTO_COMPACT_OFF_ENV
WINDOW = auto_compact.COMPACT_WINDOW_ENV
BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _fake_claude_build_command(sdef, *, restoring=False, opening=""):
    """Stand in for the claude launch: a real child process, no real claude."""
    return [sys.executable, "-u", "-c", CHILD], {"CLAUNCH_SESSION": sdef.name}, sdef.cwd


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _serve(mgr, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# --------------------------------------------------------------------------- #
# the env translation
# --------------------------------------------------------------------------- #
def test_with_auto_compact_writes_and_removes_only_its_keys():
    base = {"A": "1", WINDOW: "100000"}
    off = auto_compact.with_auto_compact(base, enabled=False, window=None)
    assert off == {"A": "1", OFF: "1"}
    pinned = auto_compact.with_auto_compact(off, enabled=True, window=200000)
    assert pinned == {"A": "1", WINDOW: "200000"}
    # DISABLE_COMPACT would keep an enable from holding, so it goes with it
    again = auto_compact.with_auto_compact(
        {"DISABLE_COMPACT": "1"}, enabled=True, window=None
    )
    assert again == {}
    assert base == {"A": "1", WINDOW: "100000"}  # not mutated


def test_the_switch_reads_claudes_truthy_spellings():
    assert auto_compact.auto_compact_off({OFF: "true"})
    assert auto_compact.auto_compact_off({"DISABLE_COMPACT": "1"})
    assert not auto_compact.auto_compact_off({OFF: "0"})
    assert not auto_compact.auto_compact_off({})


@pytest.mark.parametrize("value", [
    None, {}, {"enabled": "no"}, {"enabled": True, "window": 0},
    {"enabled": True, "window": -1}, {"enabled": True, "window": "100"},
    {"enabled": True, "window": True}, {"enabled": True, "window": 1.5},
])
def test_parse_refuses_what_claude_could_not_act_on(value):
    with pytest.raises(ValueError):
        auto_compact.parse(value)


def test_parse_takes_the_pair():
    assert auto_compact.parse({"enabled": False}) == (False, None)
    assert auto_compact.parse({"enabled": True, "window": 5}) == (True, 5)


# --------------------------------------------------------------------------- #
# the launch: the session's env wins over the profile's threshold
# --------------------------------------------------------------------------- #
def test_the_spawned_claude_sees_the_sessions_answer(home, tmp_path):
    p = profile_mod.create("work")
    from claude_launcher import settings

    settings.set_env(p, {WINDOW: "900000"})
    sdef = harness_mod.normalize(SessionDef(
        name="x", profile="work", cwd=str(tmp_path),
        env={OFF: "1", WINDOW: "150000"},
    ))
    _, env, _ = harness_mod.build_command(sdef)
    assert env[OFF] == "1"
    assert env[WINDOW] == "150000"


# --------------------------------------------------------------------------- #
# the gauge
# --------------------------------------------------------------------------- #
def test_no_tick_is_drawn_for_a_session_that_does_not_compact(home, monkeypatch):
    monkeypatch.setenv(WINDOW, "111000")
    assert ctxsize.compact_window_of(SessionDef(
        name="s", harness="claude", env={OFF: "1", WINDOW: "250000"})) is None
    assert ctxsize.compact_window_of(SessionDef(
        name="s2", harness="claude", env={WINDOW: "250000"})) == 250_000


# --------------------------------------------------------------------------- #
# a spawn
# --------------------------------------------------------------------------- #
CLAUDE_PARENT = {
    "name": "lead", "harness": "claude", "profile": "talk", "cwd": "/tmp/p",
    "args": [], "env": {OFF: "1", "A": "1"}, "effort": None,
}


def test_a_child_inherits_the_parents_answer(home):
    profile_mod.create("talk")
    child = spawn.check(spawn.SpawnPolicy.load(), {}, parent=CLAUDE_PARENT,
                        depth=0, children=0)
    assert child["env"] == {OFF: "1", "A": "1"}


def test_a_child_may_answer_for_itself_under_the_shipped_policy(home):
    """Ungated like null_token: the shipped policy keeps env shut, and this
    still goes through — it grants the child no credential, tool or argv."""
    profile_mod.create("talk")
    policy = spawn.SpawnPolicy.load()
    assert not policy.allow_env
    child = spawn.check(
        policy, {"auto_compact": {"enabled": True, "window": 120000}},
        parent=CLAUDE_PARENT, depth=0, children=0,
    )
    assert child["env"] == {"A": "1", WINDOW: "120000"}


def test_a_non_claude_child_is_refused_the_switch(home):
    store.update(lambda doc: doc.setdefault("harnesses", {}).update(
        {"py": {"command": "python"}}))
    work = profile_mod.create("work")
    lineage.set_harness(work, "py")
    parent = {**CLAUDE_PARENT, "harness": "py", "profile": "work"}
    with pytest.raises(ValueError, match="claude's switch"):
        spawn.check(spawn.SpawnPolicy.load(),
                    {"auto_compact": {"enabled": False}},
                    parent=parent, depth=0, children=0)


def test_a_malformed_spawn_answer_is_refused(home):
    profile_mod.create("talk")
    with pytest.raises(ValueError):
        spawn.check(spawn.SpawnPolicy.load(),
                    {"auto_compact": {"enabled": True, "window": 0}},
                    parent=CLAUDE_PARENT, depth=0, children=0)


# --------------------------------------------------------------------------- #
# a running session: restart with the answer changed
# --------------------------------------------------------------------------- #
def test_auto_compact_restart_switches_and_pins(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(
            name="s1", harness="claude", profile="p1", cwd=str(tmp_path),
            env={"KEEP": "1"},
        ))
        off = await mgr.auto_compact("s1", False, None)
        assert off.sdef.env == {"KEEP": "1", OFF: "1"}
        assert not off.exited  # relaunched, not just redefined
        assert off.sdef.conversation_id == session.sdef.conversation_id
        pinned = await mgr.auto_compact("s1", True, 300000)
        assert pinned.sdef.env == {"KEEP": "1", WINDOW: "300000"}
        back = await mgr.auto_compact("s1", True, None)
        assert back.sdef.env == {"KEEP": "1"}
        await mgr.shutdown_all()

    asyncio.run(run())


def test_auto_compact_refusals_touch_nothing(home, tmp_path, monkeypatch):
    store.update(lambda doc: doc.update(
        {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}))
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        s1 = mgr.create(SessionDef(
            name="s1", harness="claude", profile="p1", cwd=str(tmp_path)))
        other = mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
        with pytest.raises(ManagerError, match="claude's switch"):
            await mgr.auto_compact("s2", False, None)
        with pytest.raises(ManagerError, match="already runs"):
            await mgr.auto_compact("s1", True, None)  # already the default
        with pytest.raises(ManagerError, match="positive token count"):
            await mgr.auto_compact("s1", True, 0)
        assert not s1.exited and not other.exited
        await mgr.shutdown_all()

    asyncio.run(run())


def test_auto_compact_survives_a_daemon_restart(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(
            name="s1", harness="claude", profile="p1", cwd=str(tmp_path)))
        await mgr.auto_compact("s1", False, None)
        await mgr.shutdown_all()

        restarted = _manager()
        assert not restarted.restore_all()
        assert restarted.get("s1").sdef.env == {OFF: "1"}
        assert not restarted.get("s1").exited
        await restarted.shutdown_all()

    asyncio.run(run())


def test_api_auto_compact(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(
                name="s1", harness="claude", profile="p1", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/s1/auto-compact",
                json={"enabled": True, "window": 200000}, headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["env"] == {WINDOW: "200000"}
            for body in ({"enabled": True, "window": 200000},   # no-op
                         {"enabled": "no"}, {}, {"enabled": True, "window": -1}):
                resp = await client.post(
                    "/api/sessions/s1/auto-compact", json=body, headers=BEARER)
                assert resp.status == 400, body
            assert not mgr.get("s1").exited
            resp = await client.post(
                "/api/sessions/s1/auto-compact",
                json={"enabled": False, "window": None}, headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["env"] == {OFF: "1"}
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
