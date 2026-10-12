"""Every registered session keeps its cflow run on its rail card.

Process state controls session actions, not whether its workflow is visible.
Unbound historical runs remain outside the lightweight rail response.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession

BEARER = {"Authorization": "Bearer sekrit"}

LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "linear.yaml").write_text(LINEAR, encoding="utf-8")
    return d


def _add_dead(mgr, name, cwd, *, paused_at=None, archived_at=None, held_at=None):
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd=str(cwd)),
        exit_code=0,
        paused_at=paused_at,
        held_at=held_at,
        archived_at=archived_at,
    )
    mgr._sessions[name] = record
    return record


class _FakeLive:
    """A live session for the rail's purposes — no PTY, no harness to spawn."""

    exited = False
    paused_at = None
    archived_at = None
    created_at = "2026-09-07T00:00:00+00:00"

    def __init__(self, name, cwd):
        self.sdef = SessionDef(name=name, harness="claude", cwd=str(cwd))

    def status(self, threshold=None):
        return "idle"


def _rail_scopes(mgr, *, live=None):
    """The scopes the rail poll would draw a run for.

    ``build_app`` builds a ``ShellPty`` that binds to the running loop, so it
    is constructed inside the scenario rather than at call time.
    """
    from aiohttp.test_utils import TestClient, TestServer

    if live is not None:
        mgr._sessions[live.sdef.name] = live

    async def scenario():
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/cflow?view=rail", headers=BEARER)
            runs = (await resp.json())["runs"]
            assert all(r["sessions"] == [r["scope"]] for r in runs)
            assert all(r["workflow"] == "linear" and r["step_id"] == "one" for r in runs)
            return {r["scope"] for r in runs}
        finally:
            await client.close()

    return asyncio.run(scenario())


@pytest.mark.parametrize("markers", [
    {},
    {"held_at": "2026-10-12T00:00:00+00:00"},
    {"paused_at": "2026-10-12T00:00:00+00:00"},
    {"archived_at": "2026-10-12T00:00:00+00:00"},
    {"paused_at": "2026-10-12T00:00:00+00:00",
     "archived_at": "2026-10-12T01:00:00+00:00"},
], ids=["killed", "hold", "paused", "archived", "archived-pause"])
def test_stopped_session_keeps_its_run_on_the_rail(proj, markers):
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    _add_dead(mgr, "stopped", cwd, **markers)
    cflow_engine.start("linear", cwd=cwd, scope="stopped")
    cflow_engine.start("linear", cwd=cwd, scope="unbound")
    _add_dead(mgr, "no-run", cwd)
    _add_dead(mgr, "no-cwd", "")

    assert _rail_scopes(mgr) == {"stopped"}


def test_live_session_still_rides_the_rail(proj):
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cflow_engine.start("linear", cwd=cwd, scope="live")

    scopes = _rail_scopes(mgr, live=_FakeLive("live", cwd))

    assert "live" in scopes
