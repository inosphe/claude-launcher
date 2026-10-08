"""A Codex session's YOLO and Sandbox switches, rewritten on a running
session (claunch-w186v).

Codex has approvals and the sandbox as two options, and its packaged default
(``--dangerously-bypass-approvals-and-sandbox``) answers both. These tests
pin:

- the packaged skip group is argv codex 0.160 accepts (``-a never``; the
  old ``--approval-mode full-auto`` is refused there as an unexpected
  argument), and a session still carrying the old group is read the way
  the daemon launches it (the old group is no mode group there) and
  repaired by the next write;
- :func:`codex_mode.runtime_args` writes all four states from any starting
  argv, keeping the session's other args;
- :meth:`SessionManager.runtime_mode` and its route restart the session
  with both switches written, refuse a harness without the sandbox pair and
  a no-op before stopping anything, and the answer survives a daemon
  restart.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import codex_mode, harnesses, lineage
from claude_launcher import profile as profile_mod
from claude_launcher import store
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}
BYPASS = ["--dangerously-bypass-approvals-and-sandbox"]
ON = ["--sandbox", "workspace-write"]
OFF = ["--sandbox", "danger-full-access"]

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _fake_build_command(sdef, *, restoring=False, opening=""):
    """Stand in for the codex launch: a real child process, no real codex."""
    return [sys.executable, "-u", "-c", CHILD], {"CLAUNCH_SESSION": sdef.name}, sdef.cwd


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _codex_profile(name="cx"):
    p = profile_mod.create(name)
    lineage.set_harness(p, "codex")
    return p


def _caps(home_):
    return harnesses.registry()["codex"].to_dict()


# --------------------------------------------------------------------------- #
# the rule
# --------------------------------------------------------------------------- #
def test_the_packaged_default_reads_yolo_without_sandbox(home):
    caps = _caps(home)
    assert caps["skip_permissions_args"] == ["-a", "never"]
    assert codex_mode.runtime_state([], caps) == (True, False)


@pytest.mark.parametrize("yolo,sandbox,want", [
    (True, False, [*BYPASS, "--x"]),
    (True, True, ["-a", "never", *ON, "--x"]),
    (False, False, [*OFF, "--x"]),
    (False, True, [*ON, "--x"]),
])
def test_each_state_is_written_and_read_back(home, yolo, sandbox, want):
    caps = _caps(home)
    got = codex_mode.runtime_args([*BYPASS, "--x"], caps, yolo=yolo, sandbox=sandbox)
    assert got == want
    assert codex_mode.runtime_state(got, caps) == (yolo, sandbox)


def test_a_session_carrying_the_refused_group_is_repaired(home):
    caps = _caps(home)
    # alone — the shape /skip-permissions appended to empty args: the
    # command builder keeps the bypass default beside it, and so does the
    # reading, which is what the panel and the no-op check go by (s129-rev)
    alone = ["--approval-mode", "full-auto"]
    assert codex_mode.runtime_state(alone, caps) == (True, False)
    assert codex_mode.runtime_args(alone, caps, yolo=True, sandbox=True) == [
        "-a", "never", *ON
    ]
    beside = ["--approval-mode", "full-auto", *ON]
    assert codex_mode.runtime_state(beside, caps) == (False, True)
    assert codex_mode.runtime_args(beside, caps, yolo=True, sandbox=True) == [
        "-a", "never", *ON
    ]


def test_the_sandbox_button_repairs_a_session_carrying_the_refused_group(
    home, tmp_path, monkeypatch
):
    _codex_profile()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="cx1", profile="cx", cwd=str(tmp_path),
                              args=("--approval-mode", "full-auto")))
        fixed = await mgr.runtime_mode("cx1", True, True)  # not a no-op
        assert fixed.sdef.args == ("-a", "never", *ON)
        await mgr.shutdown_all()

    asyncio.run(run())


def test_only_a_harness_with_the_sandbox_pair_declares_the_switch(home):
    reg = harnesses.registry()
    assert codex_mode.declares_runtime(reg["codex"].to_dict())
    assert not codex_mode.declares_runtime(reg["claude"].to_dict())


# --------------------------------------------------------------------------- #
# a running session
# --------------------------------------------------------------------------- #
def test_runtime_mode_turns_the_sandbox_on_and_off(home, tmp_path, monkeypatch):
    _codex_profile()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="cx1", profile="cx", cwd=str(tmp_path),
                              args=("--x",)))
        on = await mgr.runtime_mode("cx1", True, True)
        assert on.sdef.args == ("-a", "never", *ON, "--x")
        assert not on.exited  # relaunched, not just redefined
        off = await mgr.runtime_mode("cx1", True, False)
        assert off.sdef.args == (*BYPASS, "--x")
        asking = await mgr.runtime_mode("cx1", False, True)
        assert asking.sdef.args == (*ON, "--x")
        await mgr.shutdown_all()

    asyncio.run(run())


def test_runtime_mode_refusals_touch_nothing(home, tmp_path, monkeypatch):
    _codex_profile()
    profile_mod.create("p1")
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        cx = mgr.create(SessionDef(name="cx1", profile="cx", cwd=str(tmp_path)))
        cl = mgr.create(SessionDef(name="cl1", harness="claude", profile="p1",
                                   cwd=str(tmp_path)))
        with pytest.raises(ManagerError, match="declares no sandbox switch"):
            await mgr.runtime_mode("cl1", True, True)
        with pytest.raises(ManagerError, match="already runs"):
            await mgr.runtime_mode("cx1", True, False)  # the packaged default
        assert not cx.exited and not cl.exited
        assert cx.sdef.args == ()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_runtime_mode_survives_a_daemon_restart(home, tmp_path, monkeypatch):
    _codex_profile()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="cx1", profile="cx", cwd=str(tmp_path)))
        await mgr.runtime_mode("cx1", True, True)
        await mgr.shutdown_all()

        restarted = _manager()
        assert not restarted.restore_all()
        assert restarted.get("cx1").sdef.args == ("-a", "never", *ON)
        await restarted.shutdown_all()

    asyncio.run(run())


def test_api_runtime_mode(home, tmp_path, monkeypatch):
    _codex_profile()
    monkeypatch.setattr(harness_mod, "build_command", _fake_build_command)

    async def run():
        from aiohttp.test_utils import TestClient, TestServer

        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                        mesh=MeshManager(mgr, root=tmp_path / "mesh"))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="cx1", profile="cx", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/cx1/runtime-mode",
                                     json={"yolo": True, "sandbox": True},
                                     headers=BEARER)
            assert resp.status == 200
            assert (await resp.json())["args"] == ["-a", "never", *ON]
            for body in ({"yolo": True, "sandbox": True},  # no-op
                         {"sandbox": False}, {"yolo": "yes", "sandbox": False}):
                resp = await client.post("/api/sessions/cx1/runtime-mode",
                                         json=body, headers=BEARER)
                assert resp.status == 400, body
            assert not mgr.get("cx1").exited
            resp = await client.post("/api/sessions/cx1/runtime-mode",
                                     json={"yolo": True, "sandbox": False})
            assert resp.status == 401  # operator only
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
