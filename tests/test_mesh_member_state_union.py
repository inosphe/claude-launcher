"""``?state=`` takes several roster words at once, comma-joined.

The mesh page's member filter is the session rail's multi-state selection
(claunch-qzhea): any number of lifecycle chips on at once. One request has
to answer that selection, so the roster routes read ``running,hold,paused``
as the union of the three. A single word, ``current`` and ``all`` keep the
meaning they had.
"""

from __future__ import annotations

import asyncio
import sys
import time

from claude_launcher import store
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import (
    MeshManager,
    member_in_state,
    member_state_words,
    valid_member_state,
)

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def test_a_comma_joined_state_is_the_union_of_its_words():
    assert member_state_words(" running, ,hold ") == ("running", "hold")
    assert member_in_state("hold", "running,hold")
    assert member_in_state("running", "running,hold")
    assert not member_in_state("killed", "running,hold")
    # The presets keep their meaning inside a union.
    assert member_in_state("delayed", "killed,current")
    assert member_in_state("archived", "running,all")


def test_single_words_keep_their_meaning():
    assert member_in_state("killed", "killed")
    assert not member_in_state("killed", "current")
    assert member_in_state("hold", "current")
    assert member_in_state("archived", "all")


def test_a_state_is_valid_only_when_every_word_is_known():
    assert valid_member_state("all")
    assert valid_member_state("running,hold,delayed,paused,remote")
    assert not valid_member_state("running,nope")
    assert not valid_member_state("")
    assert not valid_member_state(",")


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


def test_the_roster_routes_answer_a_union(home, tmp_path):
    """One live member and one killed: each word alone, the union of both,
    and a union with an unknown word (400), on the roster and the ledger."""
    _register_py_harness()
    proj = tmp_path / "proj"
    proj.mkdir()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name in ("live", "gone"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(proj)))
                await mm.join("team", name, handle=name)
            mgr.kill("gone")
            for _ in range(200):
                if mgr.get("gone").exited:
                    break
                await asyncio.sleep(0.05)

            async def handles(state):
                doc = await (await client.get(
                    f"/api/mesh/team?state={state}", headers=BEARER)).json()
                return {m["handle"] for m in doc["members"]}, doc["member_counts"]

            assert (await handles("running"))[0] == {"live"}
            assert (await handles("killed"))[0] == {"gone"}
            both, counts = await handles("running,killed")
            assert both == {"live", "gone"}
            # The counts stay over every member, whatever was asked for.
            assert counts["all"] == 2 and counts["killed"] == 1

            bad = await client.get("/api/mesh/team?state=running,nope", headers=BEARER)
            assert bad.status == 400

            owed = await client.get(
                "/api/mesh/team/owed?state=running,killed", headers=BEARER)
            assert owed.status == 200
            bad = await client.get("/api/mesh/team/owed?state=nope,", headers=BEARER)
            assert bad.status == 400

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
