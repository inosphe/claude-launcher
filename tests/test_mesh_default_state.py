"""The mesh roster routes default to the current members (claunch-cj2th).

``member_links`` is every pair of the members sent, and ended members are
kept for good: on the claunch mesh (1559 members, 1350 killed or archived)
the old ``all`` default made ``GET /api/mesh`` 1,214,461 pairs and 67MB,
past the 5s budget of cflow's responder lookup. A caller that needs the
ended members asks for them; these tests pin both halves.
"""

from __future__ import annotations

import asyncio

from claude_launcher import cli_mesh, mesh_mcp, workspec_import
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import DEFAULT_MEMBER_STATE, MeshManager

from test_mesh import _register_py_harness, _wait_exited
from test_mesh_member_state_union import BEARER, _serve


def test_the_default_state_is_current():
    assert DEFAULT_MEMBER_STATE == "current"


def test_the_roster_routes_leave_out_ended_members_unless_asked(home, tmp_path):
    """Two live members and one killed: no ``state`` sends the live two and
    only their pair; ``state=all`` sends all three and all three pairs; the
    counts are over every member either way; the rail view is unchanged."""
    from claude_launcher.daemon.manager import SessionManager

    _register_py_harness()
    proj = tmp_path / "proj"
    proj.mkdir()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name in ("a", "b", "gone"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(proj)))
                await mm.join("team", name, handle=name)
            mgr.kill("gone")
            await _wait_exited(mgr.get("gone"))

            async def get(path):
                resp = await client.get(path, headers=BEARER)
                assert resp.status == 200, await resp.text()
                return await resp.json()

            def view(doc):
                pairs = {(e["a"], e["b"]) for e in doc["member_links"]}
                return {m["handle"] for m in doc["members"]}, pairs

            one = await get("/api/mesh/team")
            assert view(one) == ({"a", "b"}, {("a", "b")})
            assert one["member_state"] == "current"
            assert one["member_counts"]["all"] == 3
            assert one["member_counts"]["killed"] == 1

            every = await get("/api/mesh/team?state=all")
            assert view(every) == (
                {"a", "b", "gone"},
                {("a", "b"), ("a", "gone"), ("b", "gone")},
            )

            listed = await get("/api/mesh")
            (team,) = [m for m in listed["meshes"] if m["name"] == "team"]
            assert view(team) == view(one)
            assert team["member_counts"]["all"] == 3

            listed = await get("/api/mesh?state=all")
            (team,) = [m for m in listed["meshes"] if m["name"] == "team"]
            assert view(team) == view(every)

            listed = await get("/api/mesh?state=killed")
            (team,) = [m for m in listed["meshes"] if m["name"] == "team"]
            assert {m["handle"] for m in team["members"]} == {"gone"}

            bad = await client.get("/api/mesh?state=nope", headers=BEARER)
            assert bad.status == 400

            # The rail view never carried the roster filter.
            rail = await get("/api/mesh?view=rail&state=nope")
            assert [m["name"] for m in rail["meshes"]] == ["team"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


class _Client:
    """A daemon client that records the paths it was asked for."""

    def __init__(self, answer):
        self.answer = answer
        self.paths = []

    def get(self, path, **_kw):
        self.paths.append(path)
        return self.answer


ROSTER = {
    "members": [{
        "handle": "a", "session": "sa", "role": "worker", "roles": ["worker"],
        "reachability": "local", "subroles": [],
    }],
    "member_links": [],
    "member_counts": {"all": 5},
    "peers": [],
}


def _cli(monkeypatch, argv, answer=ROSTER):
    from claude_launcher import cli

    client = _Client(answer)
    monkeypatch.setattr(
        cli_mesh.daemon_client, "ensure_running", lambda *a, **k: client
    )
    assert cli.main(argv) == 0
    return client


def test_mesh_members_asks_for_the_state_it_is_given(home, monkeypatch, capsys):
    assert _cli(monkeypatch, ["mesh", "members", "m"]).paths == ["/api/mesh/m"]
    client = _cli(monkeypatch, ["mesh", "members", "m", "--state", "all"])
    assert client.paths == ["/api/mesh/m?state=all"]
    client = _cli(monkeypatch, ["mesh", "members", "m", "--state", "killed,archived"])
    assert client.paths == ["/api/mesh/m?state=killed%2Carchived"]


def test_mesh_members_says_where_the_ended_members_are(home, monkeypatch, capsys):
    empty = {**ROSTER, "members": [], "member_counts": {"all": 4}}
    _cli(monkeypatch, ["mesh", "members", "m"], empty)
    out = capsys.readouterr().out
    assert "no current members (4 in all; --state all lists them)" in out
    assert "no members yet" not in out

    _cli(monkeypatch, ["mesh", "members", "m"], {**empty, "member_counts": {"all": 0}})
    assert "has no members yet" in capsys.readouterr().out


def test_mesh_ls_counts_every_member(home, monkeypatch, capsys):
    """``members`` holds the current ones; the count is the whole roster."""
    answer = {"meshes": [{
        "name": "m", "project": "default", "members": [{"handle": "a"}],
        "member_counts": {"all": 5}, "messages": 0,
    }]}
    client = _Client(answer)
    monkeypatch.setattr(
        cli_mesh.daemon_client, "connect_with_diagnosis", lambda *a, **k: (client, None)
    )
    from claude_launcher import cli

    assert cli.main(["mesh", "ls"]) == 0
    assert "5 member(s)" in capsys.readouterr().out
    # A daemon that predates the counts sent every member in ``members``.
    assert cli_mesh._member_total({"members": [1, 2]}) == 2


def test_mesh_subroles_reads_ended_members_too(home, monkeypatch, capsys):
    client = _cli(monkeypatch, ["mesh", "subroles", "m", "a"])
    assert client.paths == ["/api/mesh/m?state=all"]


def test_the_mcp_members_tool_passes_its_state(monkeypatch):
    client = _Client(ROSTER)
    monkeypatch.setattr(mesh_mcp, "_client", lambda: client)
    mesh_mcp.call_tool("members", {"mesh": "m"})
    mesh_mcp.call_tool("members", {"mesh": "m", "state": "all"})
    assert client.paths == ["/api/mesh/m", "/api/mesh/m?state=all"]
    tool = next(t for t in mesh_mcp.TOOLS if t["name"] == "members")
    assert "state" in tool["inputSchema"]["properties"]


def test_the_spec_import_sees_ended_members(monkeypatch):
    """A handle an archived member holds is taken: the import reads them."""
    client = _Client(ROSTER)
    backend = workspec_import.DaemonBackend.__new__(workspec_import.DaemonBackend)
    backend.client = client
    backend.get_mesh("m")
    assert client.paths == ["/api/mesh/m?state=all"]
