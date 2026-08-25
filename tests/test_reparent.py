"""``reparent``: the tree edit ``spawn`` fixes at birth, made after the fact.

A lead whose workers crowded into one area wants a nested worker to own it —
collect their branches, request one integration — without killing and
respawning sessions that hold live conversations, worktrees and cflow runs.
The tree is one field on each definition (``SessionDef.parent``), so the move
is cheap; what this file pins is the guards around it, the route that exposes
it, the mesh edge the route opens, and the tool that sends the caller as the
actor.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon import paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshManager


# --------------------------------------------------------------------------- #
# the manager: guards around one field
# --------------------------------------------------------------------------- #
class _Fake:
    """Enough of a session for the tree walks and for ``persist``."""

    exit_code = None
    pid = None
    created_at = 0.0
    last_output_at = 0.0
    exited_at = None

    def __init__(self, name: str, parent, exited: bool = False):
        self.sdef = SessionDef(name=name, harness="py", parent=parent)
        self.exited = exited

    def status(self, threshold=None) -> str:
        # persist() asks what each session was doing, to record whether a
        # restart interrupted a turn here (see daemon/resume.py).
        return "exited" if self.exited else "idle"


def _tree() -> SessionManager:
    """lead -> (w1 -> w1a, w2), plus a root ``solo``."""
    mgr = SessionManager(idle_threshold=1.0, scrollback=10, restore_default=False)
    for name, parent in (
        ("lead", None), ("w1", "lead"), ("w2", "lead"), ("w1a", "w1"), ("solo", None),
    ):
        mgr._sessions[name] = _Fake(name, parent)
    return mgr


def test_a_move_takes_the_subtree_along_and_is_persisted(home):
    mgr = _tree()
    out = mgr.reparent("w1", "w2")
    assert out == {"session": "w1", "parent": "w2", "previous": "lead", "depth": 2}
    assert mgr.children("lead") == ["w2"]
    assert mgr.children("w2") == ["w1"]
    assert mgr.descendants("w2") == ["w1", "w1a"]
    assert mgr.depth("w1a") == 3  # one deeper than before, with its parent
    # the daemon's next restart reads the same tree
    saved = json.loads(paths.sessions_json().read_text(encoding="utf-8"))
    assert {e["def"]["name"]: e["def"]["parent"] for e in saved}["w1"] == "w2"


def test_a_cycle_is_refused(home):
    mgr = _tree()
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("lead", "w1a")  # under its own grandchild
    assert "cycle" in str(exc.value)
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("lead", "lead")
    assert "own parent" in str(exc.value)
    assert mgr.children("w1a") == []  # nothing moved


def test_an_exited_parent_is_refused(home):
    """A subtree hung from a dead session is a subtree nobody commands — the
    same reason an exited session is refused a spawn."""
    mgr = _tree()
    mgr._sessions["w2"] = _Fake("w2", "lead", exited=True)
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("w1", "w2")
    assert "has exited" in str(exc.value)
    assert mgr.get("w1").sdef.parent == "lead"


def test_the_depth_limit_holds_for_the_deepest_session_moved(home):
    """``spawn.max_depth`` is hard, and a move must not carry a subtree past
    it: the check is on the deepest session that would end up under the new
    parent, not on the one named."""
    mgr = _tree()
    mgr._sessions["s1"] = _Fake("s1", "solo")
    mgr._sessions["s2"] = _Fake("s2", "s1")  # solo -> s1 -> s2, height 2
    # default max_depth is 3 (root at 0): under w1a (depth 2) s2 would sit at 5
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("solo", "w1a")
    assert "5 level(s) deep" in str(exc.value)
    assert "spawn.max_depth" in str(exc.value)
    assert mgr.get("solo").sdef.parent is None
    # under lead (depth 0) s2 lands exactly on the limit — allowed
    mgr.reparent("solo", "lead")
    assert mgr.depth("s2") == 3


def test_authority_runs_down_the_tree_for_an_actor(home):
    """An agent hands over only what it spawned, and only to itself or to a
    session it spawned; an operator (no actor) is not scoped."""
    mgr = _tree()
    # a sibling cannot adopt a sibling
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("w2", "w1", actor="w1")
    assert "does not command 'w2'" in str(exc.value)
    # nobody can move itself
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("w2", "w1", actor="w2")
    assert "cannot move itself" in str(exc.value)
    # the lead may not push its worker under a stranger
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("w1", "solo", actor="lead")
    assert "does not command 'solo'" in str(exc.value)
    # nor adopt a stranger
    with pytest.raises(ManagerError) as exc:
        mgr.reparent("solo", "w1", actor="lead")
    assert "does not command 'solo'" in str(exc.value)
    # the lead hands its worker to a nested worker it spawned
    assert mgr.reparent("w2", "w1", actor="lead")["parent"] == "w1"
    # and pulls a grandchild back up under itself (parent == actor)
    assert mgr.reparent("w1a", "lead", actor="lead")["previous"] == "w1"
    assert mgr.children("lead") == ["w1", "w1a"]
    # an operator is not scoped at all
    assert mgr.reparent("solo", "w2")["depth"] == 3


# --------------------------------------------------------------------------- #
# the route: the move, the mesh edge it opens, the status it answers with
# --------------------------------------------------------------------------- #
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def _register_py_harness():
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


def _edges(info: dict) -> dict:
    return {
        frozenset((e["a"], e["b"])): bool(e.get("enabled"))
        for e in info.get("member_links") or []
    }


def test_the_route_moves_the_session_and_opens_the_parent_edge(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name, parent in (
                ("lead", None), ("mid", "lead"), ("w1", "lead"), ("w2", "lead"),
            ):
                mgr.create(
                    SessionDef(name=name, harness="py", cwd=str(tmp_path), parent=parent)
                )
                await mm.join("team", name, handle=name)
            # a spawn-shaped star: every worker wired to lead, none to mid
            before = _edges(await (await client.get("/api/mesh/team", headers=BEARER)).json())
            assert before.get(frozenset(("w1", "lead")))
            assert not before.get(frozenset(("w1", "mid")))

            resp = await client.post(
                "/api/sessions/w1/parent",
                json={"parent": "mid", "actor": "lead"},
                headers=BEARER,
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["session"] == "w1" and body["parent"] == "mid"
            assert body["previous"] == "lead" and body["depth"] == 2
            # the edge to the new parent is opened, the old one is kept
            assert body["connected"] == [{"mesh": "team", "a": "w1", "b": "mid"}]
            assert mgr.get("w1").sdef.parent == "mid"
            after = _edges(await (await client.get("/api/mesh/team", headers=BEARER)).json())
            assert after.get(frozenset(("w1", "mid")))
            assert after.get(frozenset(("w1", "lead")))
            # ...and the tree the children route reports follows
            kids = await (await client.get("/api/sessions/mid/children", headers=BEARER)).json()
            assert [c["name"] for c in kids["children"]] == ["w1"]

            # a repeat says the move, but opens nothing new
            resp = await client.post(
                "/api/sessions/w1/parent", json={"parent": "mid"}, headers=BEARER
            )
            assert resp.status == 200
            assert (await resp.json())["connected"] == []

            # a sibling asking is refused with the authority rule
            resp = await client.post(
                "/api/sessions/w2/parent",
                json={"parent": "mid", "actor": "w1"},
                headers=BEARER,
            )
            assert resp.status == 403
            assert "does not command" in (await resp.json())["error"]
            assert mgr.get("w2").sdef.parent == "lead"
            # an unknown session, a cycle, a missing parent
            resp = await client.post(
                "/api/sessions/nope/parent", json={"parent": "mid"}, headers=BEARER
            )
            assert resp.status == 404
            resp = await client.post(
                "/api/sessions/lead/parent", json={"parent": "w1"}, headers=BEARER
            )
            assert resp.status == 400
            assert "cycle" in (await resp.json())["error"]
            resp = await client.post("/api/sessions/w2/parent", json={}, headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the tool: the caller is the actor, always
# --------------------------------------------------------------------------- #
def test_the_tool_sends_the_caller_as_the_actor(home, monkeypatch):
    from claude_launcher import mesh_mcp

    posted = []

    class Client:
        def post(self, path, body, **kw):
            posted.append((path, body))
            return {"session": "w1", "parent": "mid", "previous": "lead", "depth": 2}

    monkeypatch.setattr(mesh_mcp.daemon_client, "connect", lambda: Client())
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")
    out = mesh_mcp.call_tool("reparent", {"session": "w1", "parent": "mid"})
    assert out["parent"] == "mid"
    # the route is the child's; the actor is this session — the tool cannot
    # express "move it as somebody else"
    assert posted == [("/api/sessions/w1/parent", {"parent": "mid", "actor": "lead"})]
    with pytest.raises(mesh_mcp.MeshMcpError):
        mesh_mcp.call_tool("reparent", {"session": "w1"})
    assert [t["name"] for t in mesh_mcp.TOOLS if t["name"] == "reparent"] == ["reparent"]
