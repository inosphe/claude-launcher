"""Leader faults: which stopped leaders raise the dashboard's fatal card.

The reading is three facts that already exist elsewhere -- the mesh role
(``Member.roles``), the session's category (``session_category``) and the
harness fault (``Session.fault``) -- so what is tested here is the join, not
any of the three: a leader that is running or held and faulted is an alert, a
leader that was paused, killed or archived is not (the project stopped
waiting when a person stopped it), and a fault anywhere else is somebody
else's problem.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import api, leader_fault
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

FAULT = {
    "harness": "claude", "kind": "usage_limit", "label": "usage limit",
    "action": "wait for the reset or raise the plan",
    "resolved_by": "account", "message": "You've hit your weekly limit",
    "code": "", "reset": "resets Oct 26, 9am",
    "since": "2026-10-27T04:00:00+00:00", "last_seen": "2026-10-27T04:10:00+00:00",
    "until": "2026-10-28T00:00:00+00:00", "strikes": 1, "active": True,
    "source": "screen",
}


class _Def:
    def __init__(self, name):
        self.name = name


class _Session:
    """Only the fields the two readings touch; the route never asks more."""

    def __init__(self, name, *, exited=False, held=False, paused=False,
                 archived=False, failed=False, delayed=False, view=None):
        self.sdef = _Def(name)
        self.exited = exited
        self.held_at = "2026-10-27T04:00:00+00:00" if held else None
        self.paused_at = "2026-10-27T04:00:00+00:00" if paused else None
        self.archived_at = "2026-10-27T04:00:00+00:00" if archived else None
        self.failed_at = "2026-10-27T04:00:00+00:00" if failed else None
        self.delayed_at = "2026-10-27T04:00:00+00:00" if delayed else None
        self._view = view

    def fault(self):
        return self._view


class _Member:
    def __init__(self, handle, session, roles):
        self.handle = handle
        self.session = session
        self.roles = list(roles)


class _Mesh:
    def __init__(self, name, members):
        self.name = name
        self.members = {m.handle: m for m in members}


class _Meshes:
    def __init__(self, *meshes):
        self._meshes = list(meshes)

    def list(self):
        return list(self._meshes)


class _Manager:
    def __init__(self, *sessions):
        self._sessions = {s.sdef.name: s for s in sessions}

    def get(self, name):
        return self._sessions[name]

    def list(self):
        return list(self._sessions.values())


def _alerts(*sessions, members=None):
    meshes = _Meshes(_Mesh("claunch", members if members is not None else [
        _Member("s29", "s29", ["leader"]),
    ]))
    return leader_fault.alerts(_Manager(*sessions), meshes)


def test_a_running_leader_with_an_active_fault_is_an_alert():
    rows = _alerts(_Session("s29", view=FAULT))
    assert len(rows) == 1
    row = rows[0]
    assert row["session"] == "s29"
    assert row["handle"] == "s29"
    assert row["meshes"] == ["claunch"]
    assert row["category"] == "running"
    assert row["fault"]["kind"] == "usage_limit"
    assert row["fault"]["resolved_by"] == "account"


def test_a_held_leader_is_alerted_too():
    """Hold is a kept conversation whose work is still owed: the project is
    still waiting on it, which is the whole reason to raise the card."""
    rows = _alerts(_Session("s29", exited=True, held=True, view=FAULT))
    assert [r["category"] for r in rows] == ["hold"]


@pytest.mark.parametrize("stopped", [
    {"exited": True, "paused": True},
    {"exited": True},                       # killed
    {"exited": True, "failed": True},
    {"exited": True, "held": True, "delayed": True},
    {"archived": True},
])
def test_a_leader_stopped_on_purpose_raises_nothing(stopped):
    """Paused, killed, failed, delayed and archived sessions are not the
    project standing still -- a person already stopped them."""
    assert _alerts(_Session("s29", view=FAULT, **stopped)) == []


def test_a_subrole_leader_counts():
    meshes = _Meshes(_Mesh("claunch", [_Member("s29", "s29", ["worker", "leader"])]))
    rows = leader_fault.alerts(_Manager(_Session("s29", view=FAULT)), meshes)
    assert len(rows) == 1


def test_a_worker_fault_is_not_a_leader_alert():
    meshes = _Meshes(_Mesh("claunch", [_Member("s7", "s7", ["worker"])]))
    assert leader_fault.alerts(_Manager(_Session("s7", view=FAULT)), meshes) == []


def test_an_expired_fault_is_not_an_alert():
    expired = {**FAULT, "active": False}
    assert _alerts(_Session("s29", view=expired)) == []


def test_a_session_that_never_faulted_is_not_an_alert():
    assert _alerts(_Session("s29", view=None)) == []


def test_a_member_on_another_machine_is_not_read_here():
    """A fault is read off this machine's screen; a mirrored member whose
    session this daemon does not know contributes nothing."""
    assert _alerts() == []


def test_one_row_per_session_across_meshes():
    """Two meshes routing through the same leader make one card, naming both."""
    meshes = _Meshes(
        _Mesh("claunch", [_Member("s29", "s29", ["leader"])]),
        _Mesh("other", [_Member("lead", "s29", ["leader"])]),
    )
    rows = leader_fault.alerts(_Manager(_Session("s29", view=FAULT)), meshes)
    assert len(rows) == 1
    assert rows[0]["meshes"] == ["claunch", "other"]


def test_an_unreadable_roster_answers_nothing_rather_than_failing():
    class _Broken:
        def list(self):
            raise OSError("roster unreadable")

    assert leader_fault.alerts(_Manager(_Session("s29", view=FAULT)), _Broken()) == []


# ------------------------------------------------------------------------ route

def _client(mgr, scenario, mesh=None):
    async def run():
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mesh)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(run())


@pytest.fixture
def mgr(home):
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def test_the_route_publishes_the_alerts(mgr):
    mgr._sessions["s29"] = _Session("s29", view=FAULT)
    mesh = _Meshes(_Mesh("claunch", [_Member("s29", "s29", ["leader"])]))

    async def scenario(client):
        return await (await client.get("/api/leader-faults", headers=BEARER)).json()

    body = _client(mgr, scenario, mesh=mesh)
    assert body["count"] == 1
    assert body["alerts"][0]["session"] == "s29"
    assert body["alerts"][0]["fault"]["kind"] == "usage_limit"


def test_the_route_is_empty_when_nothing_is_wrong(mgr):
    mgr._sessions["s29"] = _Session("s29", view=None)
    mesh = _Meshes(_Mesh("claunch", [_Member("s29", "s29", ["leader"])]))

    async def scenario(client):
        return await (await client.get("/api/leader-faults", headers=BEARER)).json()

    body = _client(mgr, scenario, mesh=mesh)
    assert body == {"alerts": [], "count": 0}


def test_the_alert_fields_stay_inside_the_published_ones():
    """The view carries ``source`` and other internals; the card reads a fixed
    list, so a later field cannot reach the page by accident."""
    rows = _alerts(_Session("s29", view=FAULT))
    assert "source" not in rows[0]["fault"]
    assert set(rows[0]["fault"]) <= set(leader_fault.FAULT_FIELDS)
