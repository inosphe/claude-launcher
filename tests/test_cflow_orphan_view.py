"""The Flows page's Orphans tab reads the daemon's own judgment.

A run whose driving session has exited is already reported: the run event
clock types "nobody is driving" into the session that oversees it. That
notification names a CLI command and lands in an agent's terminal, so the
person who actually decides what to do about the run had no list to read.
``/api/cflow`` now marks those runs, and the dashboard lists them.

What these tests hold is that the mark and the notification come from ONE
rule (``cflow_clock.run_orphaned``). A tab that listed a different set from
the fyi that sent the reader to it would be worse than no tab: two rules
under one name, disagreeing quietly.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import cflow_clock
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


class _FakeLive:
    """A live session for the endpoint's purposes — no PTY, no harness."""

    exited = False
    paused_at = None
    archived_at = None
    created_at = "2026-09-22T00:00:00+00:00"

    def __init__(self, name, cwd):
        self.sdef = SessionDef(name=name, harness="claude", cwd=str(cwd))

    def status(self, threshold=None):
        return "idle"


def _add_dead(mgr, name, cwd):
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd=str(cwd)), exit_code=0
    )
    mgr._sessions[name] = record
    return record


def _runs(mgr, query=""):
    """The endpoint's answer, keyed by scope.

    ``build_app`` builds a ``ShellPty`` bound to the running loop, so it is
    constructed inside the scenario rather than at call time.
    """
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/cflow{query}", headers=BEARER)
            return {r["scope"]: r for r in (await resp.json())["runs"]}
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_a_dead_session_leaves_its_run_marked(proj):
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    _add_dead(mgr, "gone", cwd)
    mgr._sessions["alive"] = _FakeLive("alive", cwd)
    cflow_engine.start("linear", cwd=cwd, scope="gone")
    cflow_engine.start("linear", cwd=cwd, scope="alive")

    runs = _runs(mgr)

    assert runs["gone"].get("orphaned") is True
    # A run with a driver is not listed there, and says so by omission rather
    # than by a false — the tab filters on the key being truthy.
    assert not runs["alive"].get("orphaned")


def test_a_finished_run_is_not_orphaned_by_its_session_ending(proj):
    """Done is done: nothing is left for a driver to do."""
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    _add_dead(mgr, "gone", cwd)
    cflow_engine.start("linear", cwd=cwd, scope="gone")
    for summary in ("did one", "did two"):
        cflow_engine.report(summary, cwd=cwd, scope="gone")
        cflow_engine.next_step(cwd=cwd, scope="gone")

    runs = _runs(mgr)

    assert runs["gone"]["status"] == "done"
    assert not runs["gone"].get("orphaned")


def test_a_run_no_session_ever_drove_is_not_orphaned(proj):
    """A CLI run has no session to lose, and nobody could resume it."""
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cflow_engine.start("linear", cwd=cwd, scope="standalone")

    runs = _runs(mgr)

    assert not runs["standalone"].get("orphaned")


def test_the_rail_poll_does_not_pay_for_the_mark(proj):
    """``?view=rail`` is the two-second poll behind every session row. Its
    keys are already cut down to sessions that are alive, so the answer would
    be False for every one of them — the lookup is skipped rather than made.
    """
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    _add_dead(mgr, "gone", cwd)
    cflow_engine.start("linear", cwd=cwd, scope="gone")

    assert "gone" not in _runs(mgr, "?view=rail")


def test_the_tab_and_the_notification_list_the_same_run(proj):
    """The endpoint's mark and the clock's fyi come from one predicate."""
    cwd = str(proj)
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    _add_dead(mgr, "gone", cwd)
    mgr._sessions["alive"] = _FakeLive("alive", cwd)
    cflow_engine.start("linear", cwd=cwd, scope="gone")
    cflow_engine.start("linear", cwd=cwd, scope="alive")

    marked = {scope for scope, run in _runs(mgr).items() if run.get("orphaned")}

    events = cflow_clock.RunEventClock(mgr).scan()
    reported = {e["scope"] for e in events if e["kind"] == "orphaned"}

    assert marked == reported == {"gone"}
    assert "nobody is driving" in next(
        e["block"] for e in events if e["kind"] == "orphaned"
    )
