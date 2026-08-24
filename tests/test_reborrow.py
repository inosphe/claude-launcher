"""Reborrowing a session: swap the ``--borrow``, restart — nothing carried.

A managed session's borrow is part of its definition (reapplied on every
restore), so changing it on a live session is a restart, not an edit:
:meth:`SessionManager.reborrow` stops the session and relaunches it under the
definition with its borrow swapped, on the same stop-and-relaunch skeleton
(:meth:`SessionManager.redefine`) a migrate carries a transcript through.
These tests pin the validations that must fire *before* anything is stopped,
the relaunch, the rollback when it fails, and the API's own refusals.
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


# --------------------------------------------------------------------------- #
# the manager's restart
# --------------------------------------------------------------------------- #
def test_reborrow_relaunches_a_live_session_on_the_new_borrow(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    profile_mod.create("p2")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )
        relaunched = await mgr.reborrow("s1", "p2")
        assert relaunched.sdef.borrow == "p2"
        assert not relaunched.exited  # relaunched, not just redefined
        assert relaunched.sdef.conversation_id == session.sdef.conversation_id
        assert mgr.get("s1") is relaunched
        await mgr.shutdown_all()

    asyncio.run(run())


def test_reborrow_clears_the_borrow(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    profile_mod.create("p2")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(
                name="s1", harness="claude", profile="p1",
                cwd=str(tmp_path), borrow="p2",
            )
        )
        relaunched = await mgr.reborrow("s1", None)
        assert relaunched.sdef.borrow is None
        assert not relaunched.exited
        await mgr.shutdown_all()

    asyncio.run(run())


def test_reborrow_works_on_an_exited_session(home, tmp_path, monkeypatch):
    """An exited record has no process to stop — the reborrow is the respawn."""
    profile_mod.create("p1")
    profile_mod.create("p2")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )
        await session.shutdown()
        assert mgr.get("s1").exited
        relaunched = await mgr.reborrow("s1", "p2")
        assert relaunched.sdef.borrow == "p2"
        assert not relaunched.exited
        await mgr.shutdown_all()

    asyncio.run(run())


def test_reborrow_refusals_touch_nothing(home, tmp_path, monkeypatch):
    """Every refusal fires before the stop: the session never went down."""
    _register_py_harness()
    profile_mod.create("p1")
    profile_mod.create("p2")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )
        other = mgr.create(
            SessionDef(name="s2", harness="py", cwd=str(tmp_path))
        )
        nulled = mgr.create(
            SessionDef(
                name="s3", harness="claude", profile="p1",
                cwd=str(tmp_path), null_token=True,
            )
        )
        with pytest.raises(ManagerError, match="only applies to the claude"):
            await mgr.reborrow("s2", "p2")
        with pytest.raises(ManagerError, match="does not exist"):
            await mgr.reborrow("s1", "nosuch")
        with pytest.raises(ManagerError, match="own token"):
            await mgr.reborrow("s1", None)  # nothing to clear
        with pytest.raises(ManagerError, match="cannot be combined"):
            await mgr.reborrow("s3", "p2")
        with pytest.raises(ManagerError, match="no token"):
            await mgr.reborrow("s3", None)  # a --null session, honestly said
        # none of them went down for a refusal
        for s in (session, other, nulled):
            assert not s.exited
        await mgr.shutdown_all()

    asyncio.run(run())


def test_reborrow_noop_names_the_lender(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    profile_mod.create("p2")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        mgr.create(
            SessionDef(
                name="s1", harness="claude", profile="p1",
                cwd=str(tmp_path), borrow="p2",
            )
        )
        with pytest.raises(ManagerError, match="already borrows 'p2'"):
            await mgr.reborrow("s1", "p2")
        await mgr.shutdown_all()

    asyncio.run(run())


def test_reborrow_rolls_back_when_the_relaunch_fails(home, tmp_path, monkeypatch):
    """A failed relaunch leaves the world as it was: the record keeps its old
    borrow, stopped and honest about it."""
    profile_mod.create("p1")
    profile_mod.create("p2")
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
            await mgr.reborrow("s1", "p2")
        kept = mgr.get("s1")
        assert kept.sdef.borrow is None
        assert kept.exited  # it was stopped; the record says so honestly

    asyncio.run(run())


def test_redefine_refuses_a_noop(home, tmp_path, monkeypatch):
    """The skeleton's one refusal of its own: a restart that would change
    nothing must not cost the session its process."""
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(tmp_path))
        )
        with pytest.raises(ManagerError, match="nothing to restart"):
            await mgr.redefine("s1")
        with pytest.raises(ManagerError, match="nothing to restart"):
            await mgr.redefine("s1", borrow=None)
        assert mgr.get("s1") is session and not session.exited
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #
def test_api_reborrow_sets_and_clears(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    profile_mod.create("p2")
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
                "/api/sessions/s1/reborrow", json={"borrow": "p2"}, headers=BEARER
            )
            assert resp.status == 200
            assert (await resp.json())["borrow"] == "p2"
            assert mgr.get("s1").sdef.borrow == "p2"
            # "" clears, exactly like null
            resp = await client.post(
                "/api/sessions/s1/reborrow", json={"borrow": ""}, headers=BEARER
            )
            assert resp.status == 200
            assert (await resp.json())["borrow"] is None
            assert mgr.get("s1").sdef.borrow is None
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_api_reborrow_refusals(home, tmp_path, monkeypatch):
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            session = mgr.create(
                SessionDef(
                    name="s1", harness="claude", profile="p1", cwd=str(tmp_path)
                )
            )
            for body in ({}, {"borrow": 123}):
                resp = await client.post(
                    "/api/sessions/s1/reborrow", json=body, headers=BEARER
                )
                assert resp.status == 400
            # an unknown lender and a no-op are the manager's 400s, as they
            # always were — and the session never went down for any of them
            for body in ({"borrow": "nosuch"}, {"borrow": None}):
                resp = await client.post(
                    "/api/sessions/s1/reborrow", json=body, headers=BEARER
                )
                assert resp.status == 400
            assert not session.exited
            resp = await client.post(
                "/api/sessions/nosuch/reborrow", json={"borrow": "p1"},
                headers=BEARER,
            )
            assert resp.status == 400
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
