"""A failed or downgraded session creation reaches the person, once.

The two acceptance readings of claunch-is4o4:

- one failed creation makes one ``warning`` event, and
- the same target failing for the same reason is reported once.

The surface is the session event history the daemon already keeps
(``daemon/session_events.py``) plus the ``warn`` notice the person at that
session sees; ``daemon/spawn_events.py`` says why those two and no new one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import onboard, session_events, spawn_events
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshManager
from test_spawn_api import BEARER, _manager, _register_py_harness, _serve


@pytest.fixture(autouse=True)
def isolated_reporter():
    """The reporter is process-wide (``spawn_events.install``); a test that
    leaves its own installed would answer for the next one."""
    previous = spawn_events.installed()
    yield
    spawn_events.install(previous)


def _events(tmp_path):
    return session_events.Events(tmp_path)


def _session(name="s1"):
    return SimpleNamespace(sdef=SimpleNamespace(name=name), created_at="first",
                           notify=lambda *a, **kw: 0)


def test_one_failed_creation_is_one_warning_event(tmp_path):
    session = _session()
    events = _events(tmp_path)
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: 100.0)

    event = failures.report("s1", reason="session 's1' is in 2 meshes", site="api.spawn")

    assert [e["id"] for e in events.rows(session)] == [event["id"]]
    assert event["kind"] == "fail"
    assert event["details"]["level"] == "warning"
    assert event["details"]["reason"] == "session 's1' is in 2 meshes"
    assert event["details"]["site"] == "api.spawn"
    assert event["details"]["target"] == "s1"
    assert event["at"], "the event carries its own time"
    assert "session 's1' is in 2 meshes" in event["text"]


def test_repeated_failure_of_the_same_reason_is_reported_once(tmp_path):
    session = _session()
    events = _events(tmp_path)
    now = [100.0]
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: now[0])

    first = failures.report("s1", reason="refused: no such workflow", site="api.spawn")
    for _ in range(4):
        now[0] += 60.0
        assert failures.report("s1", reason="refused: no such workflow",
                               site="api.spawn") is None
    assert [e["id"] for e in events.rows(session)] == [first["id"]]

    # Past the gap the same failure is worth one line again, and it says how
    # many were folded into the report before it.
    now[0] += failures.gap + 1
    later = failures.report("s1", reason="refused: no such workflow", site="api.spawn")
    assert later is not None and later["details"]["repeats"] == 4
    assert "4회 더" in later["text"]

    # A different reason on the same target is its own report.
    other = failures.report("s1", reason="no such harness", site="api.spawn")
    assert other is not None and other["id"] != later["id"]


def test_the_route_and_its_http_caller_name_the_same_failure(tmp_path):
    """The route answers an error body; ``daemon_client`` shows its caller
    the same sentence behind ``POST <path>: ``. Both must key as one."""
    session = _session()
    events = _events(tmp_path)
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: 100.0)

    refused = failures.report("s1", reason="session 's1' is in 2 meshes (a, b)", site="api.spawn")
    assert refused is not None
    assert failures.report(
        "s1", reason="POST /api/sessions/s1/children: session 's1' is in 2 meshes (a, b)",
        site="cflow2.spawn") is None
    assert len(events.rows(session)) == 1


def test_notice_goes_to_the_session_a_person_is_looking_at(tmp_path):
    seen = []

    def viewer(name):
        return SimpleNamespace(
            sdef=SimpleNamespace(name=name), created_at="now",
            notify=lambda text, **kw: seen.append((name, text, kw)),
        )

    child, parent = viewer("child"), viewer("parent")
    failures = spawn_events.Failures(
        _events(tmp_path), lambda name: {"child": child, "parent": parent}[name],
        clock=lambda: 100.0)

    failures.report("child", kind="fallback", reason="a mesh was opened for it",
                    site="onboard.inherit_mesh", notify_on="parent")

    assert [name for name, _, _ in seen] == ["parent"], "the child has no viewer yet"
    _, text, kwargs = seen[0]
    assert "fallback" in text and kwargs["level"] == "warn"


def test_a_refused_child_spawn_lands_a_warning_on_the_parent(home, tmp_path, monkeypatch):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        parent = mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
        notices = []
        monkeypatch.setattr(parent, "notify", lambda text, **kw: notices.append(text))
        # The child's name is the parent's: stage refuses it, so the spawn
        # answers 409 -- a refused creation and nothing else.
        body = {"name": "parent", "task": "child work", "beads": False}
        try:
            first = await client.post("/api/sessions/parent/children", headers=BEARER, json=body)
            assert first.status == 409, await first.text()
            again = await client.post("/api/sessions/parent/children", headers=BEARER, json=body)
            assert again.status == 409, await again.text()
        finally:
            await client.close()
        return mgr, parent, notices

    mgr, parent, notices = asyncio.run(run())
    rows = [e for e in mgr.events.rows(parent) if e["kind"] == "fail"]
    assert len(rows) == 1, "one failure is one event; the repeat adds none"
    assert rows[0]["details"]["level"] == "warning"
    assert rows[0]["details"]["site"] == "api.spawn"
    assert rows[0]["details"]["status"] == 409
    assert "already exists" in rows[0]["details"]["reason"]
    assert len(notices) == 1 and "세션 생성 실패" in notices[0]


def test_opening_a_mesh_for_a_meshless_parent_is_reported(home, tmp_path, monkeypatch):
    """The fallback branch of ``inherit_mesh`` (claunch-is4o4 addendum): a
    parent in no mesh gets one opened for it and its children, and the user
    hears about the reshaped topology."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        parent = mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
        notices = []
        monkeypatch.setattr(parent, "notify", lambda text, **kw: notices.append(text))
        body = {}
        await onboard.inherit_mesh(body, parent="parent", mesh_mgr=mm, child="child")
        return mgr, parent, notices, body

    mgr, parent, notices, body = asyncio.run(run())
    assert body["mesh"] == "parent"
    rows = [e for e in mgr.events.rows(parent) if e["kind"] == "fallback"]
    assert len(rows) == 1
    assert rows[0]["details"]["mesh"] == "parent"
    assert rows[0]["details"]["child"] == "child"
    assert rows[0]["details"]["site"] == "onboard.inherit_mesh"
    assert len(notices) == 1 and "fallback" in notices[0]
