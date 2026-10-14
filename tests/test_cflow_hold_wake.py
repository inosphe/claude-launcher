"""A person acting on a held session's cflow run wakes it (claunch-2xyjw).

A held record (daemon/hold.py) is exited as far as the PTY goes, so every
cflow nudge used to skip it: the dashboard's buttons (``_scope_sessions``
answered only live sessions), the CLI's nudge (``responders.nudge`` skipped
``status == exited``, and ``/deliver`` refused an exited record), and the v2
CLI's select/move/report/abort/extend did not nudge at all. The action landed
on disk and the run sat until something else woke the session.
"""

from __future__ import annotations

import argparse
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import cli_cflow2
from claude_launcher.cflow import responders
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.session import DeadSession
from test_daemon_e2e import _manager, _register_py_harness, _wait_screen

HELD_AT = "2026-10-09T12:00:00+00:00"


def add_dead(mgr, name: str, cwd: str, *, held_at=None) -> DeadSession:
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd=cwd), exit_code=0, held_at=held_at,
    )
    mgr._sessions[name] = record
    return record


def _record_wakes(mgr) -> list:
    woken = []
    mgr.wake_soon = lambda name, **kw: woken.append((name, kw.get("by"),
                                                     kw.get("deliver"))) or True
    return woken


# --------------------------------------------------------------------------- #
# the dashboard's cflow actions
# --------------------------------------------------------------------------- #
def test_dashboard_nudge_wakes_a_held_driver_and_skips_a_killed_one(home, tmp_path):
    async def run():
        mgr = _manager()
        cwd = str(tmp_path)
        add_dead(mgr, "held", cwd, held_at=HELD_AT)
        add_dead(mgr, "killed", cwd)
        woken = _record_wakes(mgr)
        resolved = api_mod._session_cwd(mgr.get("held"))

        assert await api_mod._nudge_sessions(mgr, resolved, "held", "go on") == ["held"]
        assert woken == [("held", "cflow", "go on")]
        assert await api_mod._nudge_sessions(mgr, resolved, "killed", "go on") == []
        assert len(woken) == 1

        app = {"manager": mgr, "cflow_nudge_tasks": set()}
        assert api_mod._schedule_cflow_nudges(app, resolved, "held", "moved",
                                              force=True) == ["held"]
        assert woken[-1] == ("held", "cflow", "moved")
        assert api_mod._schedule_cflow_nudges(app, resolved, "killed", "moved") == []
        assert app["cflow_nudge_tasks"] == set()

        # The read-only views still list live sessions only.
        assert api_mod._scope_sessions(mgr, resolved, "held") == []

    asyncio.run(run())


def test_dashboard_cflow2_nudge_respawns_a_held_session_and_types_it(home, tmp_path):
    """End to end on the py harness: hold, press the run page's Nudge, and
    the respawned program reads the line."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mgr.resumable = lambda s: True
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer sekrit"}
        try:
            s = mgr.create(SessionDef(name="drv", harness="py", cwd=str(tmp_path)))
            await _wait_screen(s, "READY")
            mgr.hold("drv", reason="idle")
            await mgr.get("drv").wait_for("exited", timeout=10.0, threshold=0.5)
            assert mgr.get("drv").held_at

            response = await client.post(
                "/api/cflow2/nudge", headers=headers,
                json={"cwd": str(tmp_path), "scope": "drv"},
            )
            assert response.status == 200, await response.text()
            assert (await response.json())["nudge_scheduled_sessions"] == ["drv"]

            for _ in range(150):
                awake = mgr.get("drv")
                if not awake.exited:
                    break
                await asyncio.sleep(0.1)
            awake = mgr.get("drv")
            assert not awake.exited and awake.held_at is None
            await _wait_screen(awake, "continue your v2 workflow run")
        finally:
            for s in list(mgr.list()):
                await s.shutdown()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the CLI's door: /deliver and responders.nudge
# --------------------------------------------------------------------------- #
def test_deferred_deliver_wakes_a_held_session(home, tmp_path):
    async def run():
        mgr = _manager()
        add_dead(mgr, "held", str(tmp_path), held_at=HELD_AT)
        add_dead(mgr, "killed", str(tmp_path))
        woken = _record_wakes(mgr)
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer sekrit"}
        try:
            body = await (await client.post(
                "/api/sessions/held/deliver", headers=headers,
                json={"text": "nudge", "defer": True},
            )).json()
            assert body["queued"] is True and body["woken"] is True
            assert woken == [("held", "deliver", "nudge")]

            body = await (await client.post(
                "/api/sessions/killed/deliver", headers=headers,
                json={"text": "nudge", "defer": True},
            )).json()
            assert body["queued"] is False and "woken" not in body
            assert len(woken) == 1
        finally:
            await client.close()

    asyncio.run(run())


class _FakeClient:
    def __init__(self, sessions):
        self.sessions = sessions
        self.posts = []

    def get(self, path, timeout=None):
        assert path == "/api/sessions"
        return {"sessions": self.sessions}

    def post(self, path, body, timeout=None):
        self.posts.append((path, body))
        return {"ok": True, "queued": True}


def test_cli_nudge_targets_a_held_session_but_not_a_killed_one(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    client = _FakeClient([
        {"name": "held", "status": "exited", "held_at": HELD_AT, "cwd": cwd},
        {"name": "killed", "status": "exited", "held_at": None, "cwd": cwd},
    ])
    monkeypatch.setattr(responders.daemon_client, "connect", lambda: client)

    assert responders.nudge("held", "go", cwd=cwd) == ["held"]
    assert client.posts == [("/api/sessions/held/deliver", {"text": "go", "defer": True})]
    assert responders.nudge("killed", "go", cwd=cwd) == []
    assert len(client.posts) == 1


# --------------------------------------------------------------------------- #
# the v2 CLI's person commands nudge the driver
# --------------------------------------------------------------------------- #
def test_cflow2_cli_person_commands_nudge_the_driver(tmp_path, monkeypatch, capsys):
    from claude_launcher.cflow2 import asks, runtime

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_cflow2, "_refuse_own_session", lambda scope, what: None)
    monkeypatch.setattr(cli_cflow2, "_print_status", lambda payload, as_json: None)
    sent = []
    monkeypatch.setattr(asks, "nudge_driver",
                        lambda message, cwd=None, scope=None: sent.append((scope, message))
                        or ["drv"])
    monkeypatch.setattr(runtime, "select", lambda *a, **kw: {})
    monkeypatch.setattr(runtime, "abort", lambda *a, **kw: {})
    monkeypatch.setattr(runtime, "force_move", lambda *a, **kw: {"forced": {}})
    monkeypatch.setattr(runtime, "report", lambda *a, **kw: {})
    monkeypatch.setattr(runtime, "extend_loop", lambda *a, **kw: {})

    ns = argparse.Namespace
    cli_cflow2._cmd_select(ns(scope="drv", node=None, option="yes", reason=None))
    cli_cflow2._cmd_abort(ns(scope="drv", reason="r"))
    cli_cflow2._cmd_move(ns(scope="drv", node="n", reason=None, approve=False, deny=False))
    cli_cflow2._cmd_report(ns(scope="drv", node="n", outcome="ok", value=None, reason=None))
    cli_cflow2._cmd_extend(ns(scope="drv", node="n", by=1, stop=False, reset=False,
                              visits=False))

    assert [s for s, _ in sent] == ["drv"] * 5
    assert sent[0][1] == cli_cflow2.NUDGE_SELECTED.format(option="yes", note="")
    assert [m for _, m in sent[1:]] == [cli_cflow2.NUDGE_ABORTED, cli_cflow2.NUDGE_MOVED,
                                         cli_cflow2.NUDGE_REPORTED,
                                         cli_cflow2.NUDGE_EXTENDED]
    assert "nudge accepted for session(s): drv" in capsys.readouterr().out


def test_nudge_driver_reaches_the_runs_driver_and_never_raises(tmp_path):
    from claude_launcher.cflow2 import asks

    got = []
    assert asks.nudge_driver("m", cwd=str(tmp_path), scope="s9",
                             nudge_fn=lambda d, m, c: got.append((d, m, c)) or [d]) == ["s9"]
    assert got[0][:2] == ("s9", "m")

    def boom(*a):
        raise RuntimeError("no daemon")

    assert asks.nudge_driver("m", cwd=str(tmp_path), scope="s9", nudge_fn=boom) is None
