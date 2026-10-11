"""A session that holds a mesh role and drives no cflow run (claunch-val3l).

Two halves, and this file pins both.

The session side (.1): a session someone created by hand, holding a role whose
workflow exists, but started without a run, is not given one by the daemon. A
run begins only on its user's explicit yes, so the opening tells it to ask --
and how to start the run on a yes and record a no. An operator who already
said no at creation (``workflow: "-"``) has that refusal recorded instead, and
the session is told not to start one.

The leader side (.2): the refusal lives on the session record, survives a
restart, and is on what a mesh leader reads -- the roster (``claunch mesh
members``, the MCP ``members`` tool) and ``children`` -- so the leader can tell
a worker its user said no for from one whose run is missing, and stop asking it
for what only a run produces.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import cli_cflow, cli_mesh, lineage, profile, store, wizard
from claude_launcher.cflow import state as cflow_state
from claude_launcher.daemon import db, onboard, paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.session import DeadSession

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)
DECLINED = {"workflow": "worker-flow", "reason": "not for this one",
            "at": "2026-10-11T08:00:00+00:00", "by": "s1"}


# ---------------------------------------------------------------- the record

def test_the_refusal_rides_the_session_record():
    sdef = SessionDef(name="w", cflow_declined=dict(DECLINED))
    assert SessionDef.from_dict(sdef.to_dict()) == sdef


def test_a_session_nobody_declined_for_writes_no_key():
    sdef = SessionDef(name="w")
    assert "cflow_declined" not in sdef.to_dict()
    assert SessionDef.from_dict({"name": "old", "cwd": "/x"}).cflow_declined is None


def test_a_malformed_refusal_reads_as_none():
    for raw in ("yes", ["x"], {"workflow": "w"}):   # the last has no time
        assert SessionDef.from_dict({"name": "w", "cflow_declined": raw}).cflow_declined is None


@pytest.fixture
def mgr(home):
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _dead(mgr, name, cwd, parent=None):
    mgr._sessions[name] = DeadSession(
        SessionDef(name=name, harness="claude", cwd=str(cwd), parent=parent),
        exit_code=0,
    )


def test_recording_persists_and_clearing_withdraws(mgr, tmp_path):
    _dead(mgr, "w", tmp_path)
    mgr.set_cflow_declined("w", workflow="worker-flow", reason="  no run  ", by="w")

    record = mgr.get("w").sdef.cflow_declined
    assert record["workflow"] == "worker-flow"
    assert record["reason"] == "no run"
    assert record["by"] == "w" and record["at"]
    # on disk, which is the copy a restarting daemon reads
    stored = {e["def"]["name"]: e["def"]
              for e in db.SessionStore(paths.sessions_db()).load_all()}
    assert stored["w"]["cflow_declined"]["workflow"] == "worker-flow"

    mgr.clear_cflow_declined("w")
    assert mgr.get("w").sdef.cflow_declined is None


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


def test_the_endpoint_records_and_withdraws(mgr, tmp_path):
    _dead(mgr, "w", tmp_path)

    async def scenario(client):
        resp = await client.post("/api/sessions/w/cflow-declined", headers=BEARER,
                                 json={"workflow": "worker-flow", "reason": "no", "by": "w"})
        assert resp.status == 200
        recorded = (await resp.json())["cflow_declined"]
        bad = await client.post("/api/sessions/w/cflow-declined", headers=BEARER,
                                json={"reason": 7})
        assert bad.status == 400
        gone = await client.delete("/api/sessions/w/cflow-declined", headers=BEARER)
        assert gone.status == 200
        return recorded, (await gone.json()).get("cflow_declined")

    recorded, after = _client(mgr, scenario)
    assert recorded["workflow"] == "worker-flow" and recorded["reason"] == "no"
    assert after is None


def test_children_tells_the_leader_which_child_declined(mgr, tmp_path):
    _dead(mgr, "lead", tmp_path)
    _dead(mgr, "w1", tmp_path, parent="lead")
    _dead(mgr, "w2", tmp_path, parent="lead")
    mgr.set_cflow_declined("w1", workflow="worker-flow", reason="no", by="w1")

    async def scenario(client):
        resp = await client.get("/api/sessions/lead/children", headers=BEARER)
        assert resp.status == 200
        return {c["name"]: c for c in (await resp.json())["children"]}

    rows = _client(mgr, scenario)
    assert rows["w1"]["cflow_declined"]["workflow"] == "worker-flow"
    assert "cflow_declined" not in rows["w2"]


def test_mesh_members_prints_the_flag(monkeypatch, capsys):
    class Client:
        def get(self, path):
            return {"members": [
                {"handle": "w1", "session": "w1", "role": "worker",
                 "reachability": "idle", "cflow_declined": dict(DECLINED)},
                {"handle": "w2", "session": "w2", "role": "worker",
                 "reachability": "idle", "cflow_declined": None},
            ]}

    monkeypatch.setattr(cli_mesh.daemon_client, "ensure_running", lambda: Client())
    assert cli_mesh._cmd_members(argparse.Namespace(mesh="team")) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line}
    assert "no-cflow:declined" in lines["w1"]
    assert "no-cflow" not in lines["w2"]


def test_cflow_decline_needs_the_users_answer(monkeypatch, capsys):
    calls = []

    class Client:
        def post(self, path, body=None, **kw):
            calls.append((path, body))
            return {"cflow_declined": {**DECLINED, "workflow": body["workflow"]}}

        def delete(self, path, **kw):
            calls.append((path, None))
            return {}

    monkeypatch.setattr(cli_cflow.daemon_client, "ensure_running", lambda: Client())
    monkeypatch.setenv("CLAUNCH_SESSION", "w1")
    args = argparse.Namespace(session=None, workflow="worker-flow", reason="", clear=False)
    assert cli_cflow._cmd_decline(args) == 1      # no answer, nothing recorded
    assert calls == []

    args.reason = "the user said no"
    assert cli_cflow._cmd_decline(args) == 0
    assert calls[-1] == ("/api/sessions/w1/cflow-declined",
                         {"workflow": "worker-flow", "reason": "the user said no", "by": "w1"})

    assert cli_cflow._cmd_decline(argparse.Namespace(
        session=None, workflow=None, reason=None, clear=True)) == 0
    assert calls[-1] == ("/api/sessions/w1/cflow-declined", None)


# ------------------------------------------------------- the role's workflow

ENTRIES = [
    {"name": "audit", "default_role": "leader", "priority": 9},
    {"name": "worker-flow", "default_role": "worker", "priority": 1},
]


def test_the_role_selects_the_workflow_the_form_would_preselect():
    assert onboard.role_default_workflow(ENTRIES, "worker") == ("worker-flow", 1)
    assert onboard.role_default_workflow(ENTRIES, "reviewer") == ("", 0)
    assert onboard.role_default_workflow(ENTRIES, "") == ("", 0)
    # Settings' override decides first, '-' included (pick nothing)
    assert onboard.role_default_workflow(ENTRIES, "worker", {"worker": "audit"}) == ("audit", 1)
    assert onboard.role_default_workflow(ENTRIES, "worker", {"worker": "-"}) == ("", 0)


def test_the_offer_asks_first_and_names_both_answers():
    text = onboard.compose_cflow_offer(role="worker", workflow="worker-flow", version=1)
    assert "ask your user explicitly" in text
    assert "Never start it without their yes" in text
    assert "the cflow 'start' tool with workflow 'worker-flow'" in text
    assert "claunch cflow decline --workflow worker-flow" in text
    v2 = onboard.compose_cflow_offer(role="worker", workflow="v2:worker-flow", version=2)
    assert "the flow_start tool with workflow 'worker-flow'" in v2


# ------------------------------------------------------------ creation, end to end

def _declare_worker_flow(cwd) -> None:
    d = cwd / ".claunch" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "worker-flow.yaml").write_text(
        "name: worker-flow\n"
        "default_role: worker\n"
        "start: work\n"
        "steps:\n"
        "  work:\n"
        "    title: Work\n"
        "    instructions: do the one thing\n",
        encoding="utf-8",
    )


def _creating(tmp_path, monkeypatch, scenario):
    store.update(lambda doc: doc.update(
        {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}))
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        openings = {}
        launch = mgr.launch_async

        async def record(session, **kwargs):
            openings[session.sdef.name] = kwargs.get("opening") or ""
            return await launch(session, **kwargs)

        monkeypatch.setattr(mgr, "launch_async", record)
        try:
            await scenario(client, mgr, openings)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def _create(client, name, cwd, **fields):
    return client.post("/api/sessions", headers=BEARER, json={
        "name": name, "profile": "py", "cwd": str(cwd), "beads": False,
        "mesh": "team", **fields,
    })


def test_a_role_with_a_workflow_and_no_run_is_told_to_ask(home, tmp_path, monkeypatch):
    _declare_worker_flow(tmp_path)

    async def scenario(client, mgr, openings):
        resp = await _create(client, "w1", tmp_path, role="worker", task="fix it")
        assert resp.status == 201, await resp.text()
        body = await resp.json()
        assert body["cflow"] == {"offered": "worker-flow"}
        # nothing was started: the run waits on the user's yes
        assert "workflow" not in body
        assert "w1" not in cflow_state.scopes_in(str(tmp_path))
        assert mgr.get("w1").sdef.cflow_declined is None
        assert "# claunch cflow: no run" in openings["w1"]
        assert "ask your user explicitly" in openings["w1"]

    _creating(tmp_path, monkeypatch, scenario)


def test_the_operators_no_at_creation_is_recorded_not_asked(home, tmp_path, monkeypatch):
    _declare_worker_flow(tmp_path)

    async def scenario(client, mgr, openings):
        resp = await _create(client, "w2", tmp_path, role="worker", task="fix it",
                             workflow="-")
        assert resp.status == 201, await resp.text()
        body = await resp.json()
        assert body["cflow"] == {"declined": True, "workflow": "worker-flow"}
        record = mgr.get("w2").sdef.cflow_declined
        assert record["by"] == "operator" and record["workflow"] == "worker-flow"
        assert "w2" not in cflow_state.scopes_in(str(tmp_path))
        assert "the operator declined a workflow run" in openings["w2"]
        assert "ask your user explicitly" not in openings["w2"]

        # and the leader reads it off the roster, next to a member that has none
        resp = await _create(client, "w3", tmp_path, role="worker", task="fix it")
        assert resp.status == 201, await resp.text()
        resp = await client.get("/api/mesh/team", headers=BEARER)
        assert resp.status == 200
        rows = {m["session"]: m for m in (await resp.json())["members"]}
        assert rows["w2"]["cflow_declined"]["by"] == "operator"
        assert rows["w3"]["cflow_declined"] is None

    _creating(tmp_path, monkeypatch, scenario)


def test_a_named_workflow_still_starts_and_nothing_is_offered(home, tmp_path, monkeypatch):
    _declare_worker_flow(tmp_path)

    async def scenario(client, mgr, openings):
        resp = await _create(client, "w3", tmp_path, role="worker",
                             workflow="worker-flow", task="fix it")
        assert resp.status == 201, await resp.text()
        body = await resp.json()
        assert body["workflow"]["ok"] is True
        assert "cflow" not in body
        assert "# claunch cflow: no run" not in openings["w3"]

    _creating(tmp_path, monkeypatch, scenario)


def test_a_role_with_no_workflow_is_told_nothing(home, tmp_path, monkeypatch):
    _declare_worker_flow(tmp_path)

    async def scenario(client, mgr, openings):
        resp = await _create(client, "r1", tmp_path, role="reviewer", task="look")
        assert resp.status == 201, await resp.text()
        assert "cflow" not in await resp.json()
        assert "# claunch cflow: no run" not in openings["r1"]

    _creating(tmp_path, monkeypatch, scenario)


def test_a_no_with_nothing_to_refuse_records_nothing(home, tmp_path, monkeypatch):
    _declare_worker_flow(tmp_path)

    async def scenario(client, mgr, openings):
        resp = await _create(client, "r2", tmp_path, role="reviewer", task="look",
                             workflow="-")
        assert resp.status == 201, await resp.text()
        assert "cflow" not in await resp.json()
        assert mgr.get("r2").sdef.cflow_declined is None

    _creating(tmp_path, monkeypatch, scenario)
