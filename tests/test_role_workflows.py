"""Settings' per-role auto-pick workflow (``daemon.role_workflows``).

The store keeps it, ``/api/role-workflows`` edits it from the Settings page,
and ``/api/cflow/workflows`` serves it beside the list the session pickers
rank -- the web form (``newflow_check.js``) and the CLI wizard
(``test_wizard.py``) apply it from there.
"""

from __future__ import annotations

import time

from claude_launcher import store
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}


def test_the_default_is_no_overrides():
    assert store.DAEMON_DEFAULTS["role_workflows"] == {}


def test_set_and_clear_one_role(home):
    assert store.role_workflows() == {}
    store.set_role_workflow("Worker", "improv-worker-v2")
    store.set_role_workflow("leader", "-")
    assert store.role_workflows() == {"worker": "improv-worker-v2", "leader": "-"}
    store.set_role_workflow("worker", "")
    assert store.role_workflows() == {"leader": "-"}
    store.set_role_workflow("leader", None)
    assert store.role_workflows() == {}
    # an emptied block leaves no daemon key behind
    assert "role_workflows" not in (store.load().get("daemon") or {})


def test_a_malformed_block_reads_as_no_overrides(home):
    store.set_daemon_field("role_workflows", ["worker"])
    assert store.role_workflows() == {}
    store.set_daemon_field("role_workflows", {"worker": 7, "": "x", "pm": " p "})
    assert store.role_workflows() == {"pm": "p"}


def test_the_api_edits_and_serves_the_overrides(home):
    import asyncio

    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                        mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/role-workflows", headers=BEARER)
            assert (await resp.json()) == {"role_workflows": {}}

            resp = await client.put(
                "/api/role-workflows", headers=BEARER,
                json={"role": "worker", "workflow": "improv-worker-v2"})
            assert resp.status == 200
            assert (await resp.json())["role_workflows"] == {
                "worker": "improv-worker-v2"}

            resp = await client.get("/api/cflow/workflows", headers=BEARER)
            body = await resp.json()
            assert body["role_workflows"] == {"worker": "improv-worker-v2"}
            assert isinstance(body["workflows"], list)

            for bad in ({"role": "", "workflow": "x"},
                        {"role": "Wor ker", "workflow": "x"},
                        {"role": "worker", "workflow": 3},
                        {"role": "worker", "workflow": "two words"}):
                resp = await client.put("/api/role-workflows", headers=BEARER,
                                        json=bad)
                assert resp.status == 400, bad

            resp = await client.put("/api/role-workflows", headers=BEARER,
                                    json={"role": "worker", "workflow": None})
            assert (await resp.json())["role_workflows"] == {}
        finally:
            await client.close()

    asyncio.run(scenario())
