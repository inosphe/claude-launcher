"""Toggling ``--dangerously-skip-permissions`` on a session: an args edit,
a restart — nothing carried.

The flag lives in the definition's args, so switching it is the same shape
as a reborrow: :meth:`SessionManager.skip_permissions` validates before
anything is stopped, restarts the session through
:meth:`SessionManager.redefine`, and rolls the record back if the relaunch
fails. These tests pin the toggle both ways, the refusals, the rollback,
that a session's other args are never touched, and that the answer outlives
the daemon that was told it.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import profile as profile_mod
from claude_launcher import store
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}

FLAG = "--dangerously-skip-permissions"


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _fake_claude_build_command(sdef, *, restoring=False, opening=""):
    """Stand in for the claude launch: a real child process, no real claude."""
    return [sys.executable, "-u", "-c", CHILD], {"CLAUNCH_SESSION": sdef.name}, sdef.cwd


async def _serve(mgr, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_skip_permissions_toggles_both_ways(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )
        skipping = await mgr.skip_permissions("s1", True)
        assert FLAG in skipping.sdef.args
        assert not skipping.exited  # relaunched, not just redefined
        assert skipping.sdef.conversation_id == session.sdef.conversation_id
        asking = await mgr.skip_permissions("s1", False)
        assert FLAG not in asking.sdef.args
        assert not asking.exited
        await mgr.shutdown_all()

    asyncio.run(run())


def test_skip_permissions_keeps_the_sessions_other_args(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(
                name="s1", harness="claude", profile="p1",
                cwd=str(tmp_path), args=("--model", "sonnet"),
            )
        )
        skipping = await mgr.skip_permissions("s1", True)
        assert skipping.sdef.args == ("--model", "sonnet", FLAG)
        asking = await mgr.skip_permissions("s1", False)
        assert asking.sdef.args == ("--model", "sonnet")
        await mgr.shutdown_all()

    asyncio.run(run())


def test_codex_skip_permissions_toggles_declared_arg_group(home, tmp_path, monkeypatch):
    from claude_launcher import lineage

    p = profile_mod.create("cx")
    lineage.set_harness(p, "codex")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="cx1", profile="cx", cwd=str(tmp_path),
                              args=("--sandbox", "danger-full-access")))
        skipping = await mgr.skip_permissions("cx1", True)
        assert skipping.sdef.args == (
            "--sandbox", "danger-full-access", "--approval-mode", "full-auto"
        )
        asking = await mgr.skip_permissions("cx1", False)
        assert asking.sdef.args == ("--sandbox", "danger-full-access")
        await mgr.shutdown_all()

    asyncio.run(run())


def test_skip_permissions_refusals_touch_nothing(home, tmp_path, monkeypatch):
    _register_py_harness()
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )
        other = mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
        with pytest.raises(ManagerError, match="does not declare"):
            await mgr.skip_permissions("s2", True)
        with pytest.raises(ManagerError, match="not skipping"):
            await mgr.skip_permissions("s1", False)  # already asking
        skipping = await mgr.skip_permissions("s1", True)
        with pytest.raises(ManagerError, match="already skips"):
            await mgr.skip_permissions("s1", True)
        # only the deliberate toggle cost a session its process: the other
        # session is untouched, and the relaunched one survived the refusal
        assert not other.exited and not skipping.exited
        await mgr.shutdown_all()

    asyncio.run(run())


def test_skip_permissions_rolls_back_when_the_relaunch_fails(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )

        def refuse(sdef, *, restoring=False, opening=""):
            raise harness_mod.HarnessError("no relaunch today")

        monkeypatch.setattr(harness_mod, "build_command", refuse)
        with pytest.raises(harness_mod.HarnessError):
            await mgr.skip_permissions("s1", True)
        kept = mgr.get("s1")
        assert FLAG not in kept.sdef.args  # the record keeps its old args
        assert kept.exited  # it was stopped; the record says so honestly

    asyncio.run(run())


def test_skip_permissions_survives_a_daemon_restart(home, tmp_path, monkeypatch):
    """Both answers are part of the definition, so both must be persisted:
    a restart restores the session still skipping the asks, and — once the
    toggle is turned back — still asking. The removal counts as much as the
    addition; a restart that dropped the flag would silently put the
    questions back."""
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )
        skipping = await mgr.skip_permissions("s1", True)
        assert skipping.sdef.args == (FLAG,)
        await mgr.shutdown_all()

        # A fresh manager reads the persisted definitions off disk, the way a
        # restarted daemon does.
        restarted = _manager()
        assert not restarted.restore_all()
        still_skipping = restarted.get("s1")
        assert still_skipping.sdef.args == (FLAG,)
        assert not still_skipping.exited  # restored, not merely remembered

        asking = await restarted.skip_permissions("s1", False)
        assert asking.sdef.args == ()
        await restarted.shutdown_all()

        again = _manager()
        assert not again.restore_all()
        assert again.get("s1").sdef.args == ()

    asyncio.run(run())


def test_api_skip_permissions(home, tmp_path, monkeypatch):
    _register_py_harness()
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(
                SessionDef(
                    name="s1", harness="claude", profile="p1", cwd=str(tmp_path)
                )
            )
            resp = await client.post(
                "/api/sessions/s1/skip-permissions",
                json={"skip": True},
                headers=BEARER,
            )
            assert resp.status == 200
            assert FLAG in (await resp.json())["args"]
            assert FLAG in mgr.get("s1").sdef.args
            # a no-op and a bad body are 400s; the session never went down
            # for either
            for body in ({"skip": True}, {"skip": "yes"}, {}):
                resp = await client.post(
                    "/api/sessions/s1/skip-permissions", json=body, headers=BEARER
                )
                assert resp.status == 400
            assert not mgr.get("s1").exited
            resp = await client.post(
                "/api/sessions/s1/skip-permissions",
                json={"skip": False},
                headers=BEARER,
            )
            assert resp.status == 200
            assert FLAG not in (await resp.json())["args"]
            resp = await client.post(
                "/api/sessions/s2/skip-permissions",
                json={"skip": True},
                headers=BEARER,
            )
            assert resp.status == 400  # no such session / wrong harness
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
