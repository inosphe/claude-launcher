"""``GET /api/mesh/{mesh}/flows``: the roster, joined to what it is doing.

The mesh endpoint says who is in the room; the cflow endpoints say where each
run stands. The flow view needs both at once, per member, at 2s intervals —
and doing that join in the browser would be one request per agent per poll.
So the join happens here, and this is what it must get right: the right run
for the right member, one graph per workflow rather than one per agent, and
an honest answer for the members it cannot see into.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}

REVIEW = """\
name: review
start: plan
steps:
  plan:
    instructions: plan it
    next: build
  build:
    gate: "ready to build?"
    instructions: build it
    next: judge
  judge:
    select:
      prompt: good enough?
      chooser: user
      options:
        ship: {description: ship it, next: ship}
        again: {description: another pass, next: build}
  ship:
    instructions: ship it
"""


PACED = """name: paced
start: standby
steps:
  standby:
    select:
      prompt: what now?
      chooser: agent
      options:
        integrate: {description: merge the batch, next: sweep, interval: 300}
        hold: {description: keep waiting, next: standby}
  sweep:
    instructions: sweep it
"""


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _declare(cwd: Path) -> None:
    d = cwd / ".claunch" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.yaml").write_text(REVIEW, encoding="utf-8")


def test_every_member_carries_its_run_and_the_graph_is_shared(home, tmp_path):
    """Two agents on one workflow: two runs, two positions, one graph."""
    _register_py_harness()
    proj = tmp_path / "proj"
    proj.mkdir()
    _declare(proj)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name in ("lead", "w1"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(proj)))
                await mm.join("team", name, handle=name)

            cwd = str(proj.resolve())
            cflow_engine.start("review", cwd=cwd, scope="lead")
            cflow_engine.start("review", cwd=cwd, scope="w1")
            # w1 walks on one step, so the two are demonstrably not the same
            # run behind one payload.
            cflow_engine.report("planned", cwd=cwd, scope="w1")
            cflow_engine.next_step(cwd=cwd, scope="w1")

            resp = await client.get("/api/mesh/team/flows", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()

            assert set(body["flows"]) == {"lead", "w1"}
            assert body["flows"]["lead"]["step_id"] == "plan"
            assert body["flows"]["lead"]["session"] == "lead"
            # w1 walked into the gated step, which is the state the view is
            # loudest about: something is waiting on a human.
            assert body["flows"]["w1"]["step_id"] == "build"
            assert body["flows"]["w1"]["status"] == "waiting_approval"
            assert body["flows"]["w1"]["gate"] == "ready to build?"

            # one graph for the pair, and the members point at it by key
            assert len(body["workflows"]) == 1
            key = body["flows"]["lead"]["key"]
            assert body["flows"]["w1"]["key"] == key
            wf = body["workflows"][key]
            assert wf["start"] == "plan"
            assert {s["id"] for s in wf["steps"]} == {"plan", "build", "judge", "ship"}

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_member_with_no_run_says_so_rather_than_looking_unstarted(home, tmp_path):
    """Absence has to be legible: an empty track and a missing run would draw
    the same, and only one of them means 'has not got going yet'."""
    _register_py_harness()
    proj = tmp_path / "proj"
    proj.mkdir()
    _declare(proj)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(proj)))
            await mm.join("team", "lead", handle="lead")

            resp = await client.get("/api/mesh/team/flows", headers=BEARER)
            body = await resp.json()
            assert body["flows"]["lead"]["status"] == "idle"
            assert "key" not in body["flows"]["lead"]
            assert body["workflows"] == {}

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_stopped_session_still_carries_the_run_it_stopped_in(home, tmp_path):
    """A run outlives the agent driving it: the state is on disk and the
    session is resumable. Reading "its session exited" as "no run" empties the
    card of a run with real work in it — and leaves no way to the run page,
    which is where that work can be read."""
    _register_py_harness()
    proj = tmp_path / "proj"
    proj.mkdir()
    _declare(proj)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            session = mgr.create(SessionDef(name="lead", harness="py", cwd=str(proj)))
            await mm.join("team", "lead", handle="lead")
            cwd = str(proj.resolve())
            cflow_engine.start("review", cwd=cwd, scope="lead")

            mgr.kill("lead")
            await session.wait_for("exited", timeout=10.0, threshold=0.5)

            resp = await client.get("/api/mesh/team/flows", headers=BEARER)
            body = await resp.json()
            flow = body["flows"]["lead"]
            assert flow["stopped"] is True          # the fact the card leads with
            assert flow["sessions"] == []           # nothing to nudge
            assert flow["step_id"] == "plan"        # ...where it got to
            assert flow["cwd"] == cwd               # ...and the way to its run page
            assert body["workflows"][flow["key"]]["start"] == "plan"  # track drawn

            # Deregistered (removing the exited record) is the other
            # absence: no session, no directory, nothing to show.
            mgr.remove("lead")
            resp = await client.get("/api/mesh/team/flows", headers=BEARER)
            body = await resp.json()
            assert body["flows"]["lead"] == {"session": "lead", "status": "no_session"}

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_paced_option_carries_its_interval_into_the_graph(home, tmp_path):
    """`interval` is what makes a branch paced, and the flow view *draws* the
    graph. Served without it, the drawing has no way to know: a branch the run
    may take once every five minutes renders exactly like one it may take at
    will, and the only place the pacing surfaces is the run page's prose,
    which speaks solely while a choice is actually held.

    So the field travels with the option. Its absence on the others is part of
    the contract too — the drawing dashes on truthiness, and a `0` or a `""`
    smuggled in for "none" would dash every branch in the workflow.
    """
    _register_py_harness()
    proj = tmp_path / "proj"
    proj.mkdir()
    d = proj / ".claunch" / "workflows"
    d.mkdir(parents=True)
    (d / "paced.yaml").write_text(PACED, encoding="utf-8")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(proj)))
            await mm.join("team", "lead", handle="lead")
            cwd = str(proj.resolve())
            cflow_engine.start("paced", cwd=cwd, scope="lead")

            resp = await client.get("/api/mesh/team/flows", headers=BEARER)
            body = await resp.json()
            wf = body["workflows"][body["flows"]["lead"]["key"]]
            standby = next(s for s in wf["steps"] if s["id"] == "standby")
            opts = {o["name"]: o for o in standby["select"]["options"]}

            assert opts["integrate"]["interval"] == 300
            # Declared on one branch only, and null — not 0 — on the other.
            assert opts["hold"]["interval"] is None

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
