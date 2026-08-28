"""Removing a record in the middle of the tree: what happens to what is under it.

Before this, dropping a session's record left its children naming a session
that no longer existed. ``SessionManager.ancestors`` stops at the first name it
cannot resolve, so the whole subtree quietly became a set of roots — the
grandchildren kept running, held their conversations and worktrees, and the
grandparent that used to command them stopped doing so with nothing reporting
it. Two answers are pinned here: promote them one level (the default), or drop
their records along with it, which is refused while any of them is still
running.
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
# the manager
# --------------------------------------------------------------------------- #
class _Fake:
    """Enough of a session for the tree walks and for ``persist``."""

    exit_code = None
    pid = None
    created_at = 0.0
    last_output_at = 0.0
    last_visited_at = None
    last_input_at = None
    exited_at = None

    def __init__(self, name: str, parent, exited: bool = False):
        self.sdef = SessionDef(name=name, harness="py", parent=parent)
        self.exited = exited

    def status(self, threshold=None) -> str:
        return "exited" if self.exited else "idle"

    def delivery_held(self) -> bool:
        return False


def _tree(**exited) -> SessionManager:
    """lead -> mid -> (kid, kid2); plus a root ``solo``.

    ``exited=dict(mid=True)`` marks records as ended — remove only ever acts
    on one of those.
    """
    mgr = SessionManager(idle_threshold=1.0, scrollback=10, restore_default=False)
    for name, parent in (
        ("lead", None), ("mid", "lead"), ("kid", "mid"), ("kid2", "mid"),
        ("solo", None),
    ):
        mgr._sessions[name] = _Fake(name, parent, exited=bool(exited.get(name)))
    return mgr


def test_removing_a_middle_record_promotes_its_children(home):
    mgr = _tree(mid=True)
    session, moved = mgr.remove("mid")
    assert session.sdef.name == "mid"
    assert moved == ["kid", "kid2"]           # oldest first, as children() orders
    assert mgr.children("lead") == ["kid", "kid2"]
    assert mgr.get("kid").sdef.parent == "lead"
    assert mgr.ancestors("kid") == ["lead"]   # not a root, which is the defect
    assert mgr.depth("kid") == 1
    assert mgr.commands("lead", "kid")        # the grandparent still commands it
    # the daemon's next restart reads the promoted edge, not the dangling one
    saved = json.loads(paths.sessions_json().read_text(encoding="utf-8"))
    assert {e["def"]["name"]: e["def"]["parent"] for e in saved}["kid"] == "lead"


def test_a_removed_root_leaves_its_children_as_roots(home):
    """Nothing to promote them to, so they become what they would have become
    anyway — but by decision rather than by a dangling name."""
    mgr = _tree(lead=True)
    mgr._sessions["mid"] = _Fake("mid", "lead")
    _, moved = mgr.remove("lead")
    assert moved == ["mid"]
    assert mgr.get("mid").sdef.parent is None
    assert mgr.depth("mid") == 0


def test_a_dangling_grandparent_is_not_written_back(home):
    """``mid``'s own parent is already gone: promoting to that name would put
    the children back in exactly the state this fixes."""
    mgr = _tree(mid=True)
    del mgr._sessions["lead"]
    mgr.remove("mid")
    assert mgr.get("kid").sdef.parent is None


def test_cascade_drops_the_whole_subtree(home):
    mgr = _tree(mid=True, kid=True, kid2=True)
    session, dropped = mgr.remove("mid", children="remove")
    assert session.sdef.name == "mid"
    assert sorted(dropped) == ["kid", "kid2"]
    assert sorted(mgr._sessions) == ["lead", "solo"]
    saved = json.loads(paths.sessions_json().read_text(encoding="utf-8"))
    assert sorted(e["def"]["name"] for e in saved) == ["lead", "solo"]


def test_cascade_is_refused_while_anything_under_it_runs(home):
    """A cascade must not become a mass end-of-session: the rule that guards
    the named record (kill it first) is applied to the whole subtree, and the
    refusal leaves everything in place."""
    mgr = _tree(mid=True, kid=True)  # kid2 still running
    with pytest.raises(ManagerError) as exc:
        mgr.remove("mid", children="remove")
    assert "kid2" in str(exc.value)
    assert "kill them first" in str(exc.value)
    assert sorted(mgr._sessions) == ["kid", "kid2", "lead", "mid", "solo"]


def test_a_running_session_is_still_refused_outright(home):
    mgr = _tree()
    with pytest.raises(ManagerError) as exc:
        mgr.remove("mid")
    assert "still running" in str(exc.value)
    assert mgr.get("kid").sdef.parent == "mid"  # nothing moved on the refusal


def test_an_unknown_children_policy_is_refused(home):
    mgr = _tree(mid=True)
    with pytest.raises(ManagerError) as exc:
        mgr.remove("mid", children="ignore")
    assert "unknown children policy" in str(exc.value)
    assert "mid" in mgr._sessions


def test_clear_passes_live_grandchildren_up_past_a_chain(home):
    """The bulk half has the same hole, and a chain of cleared records has to
    hand the survivor all the way up rather than one level at a time."""
    mgr = _tree(lead=False, mid=True)
    mgr._sessions["kid"] = _Fake("kid", "mid", exited=True)  # cleared too
    mgr._sessions["grand"] = _Fake("grand", "kid")           # still running
    assert sorted(mgr.clear()) == ["kid", "mid"]
    assert mgr.get("grand").sdef.parent == "lead"
    assert mgr.get("kid2").sdef.parent == "lead"


# --------------------------------------------------------------------------- #
# the route
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


def test_the_route_promotes_and_opens_the_grandparent_edge(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name, parent in (("lead", None), ("mid", "lead"), ("kid", "mid")):
                mgr.create(
                    SessionDef(name=name, harness="py", cwd=str(tmp_path), parent=parent)
                )
                await mm.join("team", name, handle=name)
            # the spawn-shaped wiring: kid talks to mid, not to lead
            before = _edges(
                await (await client.get("/api/mesh/team", headers=BEARER)).json()
            )
            assert before.get(frozenset(("kid", "mid")))
            assert not before.get(frozenset(("kid", "lead")))

            mgr.kill("mid")
            resp = await client.delete("/api/sessions/mid?force=1", headers=BEARER)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["escalated"] == ["kid"]
            assert body["connected"] == [{"mesh": "team", "a": "kid", "b": "lead"}]
            assert mgr.get("kid").sdef.parent == "lead"
            # the child that was promoted can now reach the session it answers to
            after = _edges(
                await (await client.get("/api/mesh/team", headers=BEARER)).json()
            )
            assert after.get(frozenset(("kid", "lead")))
            kids = await (
                await client.get("/api/sessions/lead/children", headers=BEARER)
            ).json()
            assert [c["name"] for c in kids["children"]] == ["kid"]
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_the_route_cascades_only_when_asked_and_only_when_nothing_runs(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            for name, parent in (("lead", None), ("mid", "lead"), ("kid", "mid")):
                mgr.create(
                    SessionDef(name=name, harness="py", cwd=str(tmp_path), parent=parent)
                )
            mgr.kill("mid")

            # kid is still running: the cascade is refused and drops nothing
            resp = await client.delete(
                "/api/sessions/mid?children=remove", headers=BEARER
            )
            assert resp.status == 409
            assert "kid" in (await resp.json())["error"]
            assert mgr.get("mid").exited and mgr.get("kid")

            mgr.kill("kid")
            resp = await client.delete(
                "/api/sessions/mid?children=remove", headers=BEARER
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["removed_children"] == ["kid"]
            with pytest.raises(Exception):
                mgr.get("kid")
            assert mgr.children("lead") == []
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_cascade_asks_the_mesh_guard_of_every_record_it_would_drop(
    home, tmp_path
):
    """A grandchild's roster strands exactly as badly as the named session's,
    so the 409 has to name it — and nothing may be deleted before the check."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name, parent in (("lead", None), ("mid", "lead"), ("kid", "mid")):
                mgr.create(
                    SessionDef(name=name, harness="py", cwd=str(tmp_path), parent=parent)
                )
            await mm.join("team", "kid", handle="kid")  # only the grandchild
            mgr.kill("mid")
            mgr.kill("kid")

            resp = await client.delete(
                "/api/sessions/mid?children=remove", headers=BEARER
            )
            assert resp.status == 409
            assert "still a mesh member" in (await resp.json())["error"]
            assert mgr.get("kid") and mgr.get("mid")

            # force takes the held row off the roster first, then drops both
            resp = await client.delete(
                "/api/sessions/mid?children=remove&force=1", headers=BEARER
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["removed_children"] == ["kid"]
            assert mm.meshes_for_session("kid") == []
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())
