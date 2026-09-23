"""The run page's door to a run's state: ``POST /api/cflow/state``.

The web face of ``claunch cflow set`` (claunch-w5i81): the dashboard token is
the person's channel, so a write goes in as ``by="user"`` through the same
engine call, and the workflow's ``editable:`` declaration still decides who
may write what -- an agent-only path is refused here as it is on the CLI.
``GET /api/cflow/run`` is what the page draws from, so it is checked to carry
the ``state`` and ``landing_queue`` fields the blocks read.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}

FLOW = """
name: stated
landing_queue: true
editable:
  notes:
    type: text
    describe: what the person asks of this run
  fast:
    type: bool
    by: [user, agent]
  auto:
    type: bool
    by: [agent]
steps:
  work:
    instructions: do the work
    next: review
  review:
    instructions: have it reviewed
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "stated.yaml").write_text(FLOW, encoding="utf-8")
    return d


def _run(proj, scenario):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cwd = str(proj)
    cflow_engine.start("stated", cwd=cwd, scope="w1")

    async def main():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await scenario(client, cwd)
        finally:
            await client.close()

    asyncio.run(main())


def test_the_run_payload_carries_state_and_the_landing_queue(proj):
    async def scenario(client, cwd):
        resp = await client.get(
            f"/api/cflow/run?cwd={cwd}&scope=w1", headers=BEARER
        )
        body = await resp.json()
        assert resp.status == 200, body
        run = body["run"]  # the engine payload the page's blocks read
        assert [e["path"] for e in run["state"]] == ["notes", "fast", "auto"]
        assert {e["path"]: e["by"] for e in run["state"]}["auto"] == ["agent"]
        assert run["landing_queue"] == []

    _run(proj, scenario)


def test_a_person_writes_a_declared_path_as_user(proj):
    async def scenario(client, cwd):
        resp = await client.post(
            "/api/cflow/state", headers=BEARER,
            json={"cwd": cwd, "scope": "w1", "path": "notes", "value": "keep it small"},
        )
        body = await resp.json()
        assert resp.status == 200, body
        assert (body["path"], body["value"], body["by"], body["changed"]) == (
            "notes", "keep it small", "user", True,
        )
        # The write is the engine's own: the run's payload reads it back,
        # with the writer the page shows beside it.
        entry = {e["path"]: e for e in cflow_engine.status(cwd, scope="w1")["state"]}["notes"]
        assert (entry["value"], entry["set_by"]) == ("keep it small", "user")

        resp = await client.post(
            "/api/cflow/state", headers=BEARER,
            json={"cwd": cwd, "scope": "w1", "path": "fast", "value": True},
        )
        body = await resp.json()
        assert resp.status == 200, body
        assert body["value"] is True

    _run(proj, scenario)


def test_an_agent_only_path_and_an_undeclared_one_are_refused(proj):
    async def scenario(client, cwd):
        for path, words in (("auto", "written by agent only"),
                            ("nope", "not writable in this run")):
            resp = await client.post(
                "/api/cflow/state", headers=BEARER,
                json={"cwd": cwd, "scope": "w1", "path": path, "value": True},
            )
            body = await resp.json()
            assert resp.status >= 400, (path, body)
            assert words in body.get("error", ""), (path, body)
        values = {e["path"]: e["value"] for e in cflow_engine.status(cwd, scope="w1")["state"]}
        assert values["auto"] is False

    _run(proj, scenario)


def test_a_write_names_its_path_and_value(proj):
    async def scenario(client, cwd):
        for payload, words in (({"value": "x"}, "'path' required"),
                               ({"path": "notes"}, "'value' required")):
            resp = await client.post(
                "/api/cflow/state", headers=BEARER,
                json={"cwd": cwd, "scope": "w1", **payload},
            )
            body = await resp.json()
            assert resp.status == 400, body
            assert words in body["error"], body

    _run(proj, scenario)


def test_the_door_needs_the_dashboard_token(proj):
    async def scenario(client, cwd):
        resp = await client.post(
            "/api/cflow/state",
            json={"cwd": cwd, "scope": "w1", "path": "notes", "value": "x"},
        )
        assert resp.status == 401

    _run(proj, scenario)
