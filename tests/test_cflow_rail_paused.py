"""The rail annotates a paused session's row with its cflow run.

A paused record reads ``exited`` like any other stopped session, but it is one
a person still selects and resumes, and the rail draws it under *Paused*. The
two-second poll behind the rail is ``/api/cflow?view=rail``, and it used to
drop every run whose scope was not a *live* session — so a paused session's run
never reached its row, and the header chip beside it stayed blank. This pins
the widened rule: a paused (but not archived) session keeps its run on the
rail, while a plainly killed one does not.
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


def _add_dead(mgr, name, cwd, *, paused_at=None, archived_at=None):
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd=str(cwd)),
        exit_code=0,
        paused_at=paused_at,
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
            return {r["scope"] for r in runs}
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_paused_session_keeps_its_run_on_the_rail(proj):
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    _add_dead(mgr, "held", cwd, paused_at="2026-09-07T00:00:00+00:00")
    _add_dead(mgr, "killed", cwd)
    cflow_engine.start("linear", cwd=cwd, scope="held")
    cflow_engine.start("linear", cwd=cwd, scope="killed")

    scopes = _rail_scopes(mgr)

    # The paused session's run rides the rail; the killed one's does not.
    assert "held" in scopes
    assert "killed" not in scopes


def test_archived_session_is_dropped_like_a_killed_one(proj):
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    _add_dead(
        mgr, "gone", cwd,
        paused_at="2026-09-07T00:00:00+00:00",
        archived_at="2026-09-07T01:00:00+00:00",
    )
    cflow_engine.start("linear", cwd=cwd, scope="gone")

    scopes = _rail_scopes(mgr)

    # Archiving is terminal: an archived record earns no rail row, even though
    # it still carries the paused marker.
    assert "gone" not in scopes


def test_live_session_still_rides_the_rail(proj):
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cflow_engine.start("linear", cwd=cwd, scope="live")

    scopes = _rail_scopes(mgr, live=_FakeLive("live", cwd))

    assert "live" in scopes
