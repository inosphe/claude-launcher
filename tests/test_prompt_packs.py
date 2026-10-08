"""Prompt packs: shipped, installed per workspace, chosen at creation.

The module half (install/status/remove, the role mapping, the default list)
and the HTTP half: the Settings card's routes, and a create or spawn whose
opening carries the ticked packs plus the ones its roles require.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import lineage, profile, prompt_packs, store, workspaces
from claude_launcher.daemon import harness as harness_mod, rebrief
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


# --------------------------------------------------------------------------- #
# module
# --------------------------------------------------------------------------- #
def test_the_catalog_ships_the_packs_and_common_is_the_default():
    ids = [p.id for p in prompt_packs.catalog()]
    assert {"beads", "child-agents", "common", "unreal-operator"} <= set(ids)
    # Every pack the default role mapping names ships, so a role's guidance
    # never depends on a workspace having installed it.
    for packs in prompt_packs.DEFAULT_ROLE_PACKS.values():
        assert set(packs) <= set(ids)
    assert [p.id for p in prompt_packs.catalog() if p.default] == ["common"]
    for pack in prompt_packs.catalog():
        assert pack.title and pack.body and not pack.body.startswith("---")


def test_install_status_edit_and_remove(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    pack = prompt_packs.shipped("beads")
    assert prompt_packs.status(ws, pack) == prompt_packs.MISSING
    assert prompt_packs.install(ws, "beads") == prompt_packs.MISSING
    copy = ws / ".claunch" / "prompts" / "beads.md"
    assert copy.read_bytes() == pack.raw.encode("utf-8")
    assert prompt_packs.status(ws, pack) == prompt_packs.CURRENT

    # An untouched copy of an older package text is stale, and refreshes.
    copy.write_text("old text\n", encoding="utf-8")
    record = prompt_packs._record(ws)
    record["beads"] = prompt_packs._sha("old text\n")
    prompt_packs._write_record(ws, record)
    assert prompt_packs.status(ws, pack) == prompt_packs.STALE
    assert prompt_packs.install(ws, "beads") == prompt_packs.STALE

    # An edited copy is the workspace's own decision.
    copy.write_text(pack.raw + "\nlocal rule\n", encoding="utf-8")
    assert prompt_packs.status(ws, pack) == prompt_packs.EDITED
    with pytest.raises(prompt_packs.PackError, match="needs force"):
        prompt_packs.install(ws, "beads")
    with pytest.raises(prompt_packs.PackError, match="needs force"):
        prompt_packs.remove(ws, "beads")
    assert prompt_packs.pack_copy(ws, "beads").body.endswith("local rule")
    assert prompt_packs.remove(ws, "beads", force=True) == prompt_packs.EDITED
    assert not copy.exists()
    assert prompt_packs._record(ws) == {}


def test_a_local_pack_is_offered_and_needs_force_to_remove(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claunch" / "prompts").mkdir(parents=True)
    (ws / ".claunch" / "prompts" / "house.md").write_text(
        "---\ntitle: House\n---\nour rules\n", encoding="utf-8"
    )
    rows = {r["id"]: r for r in prompt_packs.workspace_rows(ws)}
    assert rows["house"]["status"] == prompt_packs.LOCAL
    assert rows["beads"]["status"] == prompt_packs.MISSING
    with pytest.raises(prompt_packs.PackError, match="needs force"):
        prompt_packs.remove(ws, "house")
    assert prompt_packs.remove(ws, "house", force=True) == prompt_packs.LOCAL


def test_opening_block_names_the_id_rebrief_uses():
    block = prompt_packs.opening([{"id": "beads", "text": "rule one"}])
    assert "# claunch prompt pack: beads" in block
    assert f"text id: {rebrief.block_digest('rule one')}" in block
    assert block.endswith("rule one")


def test_role_packs_default_save_and_clear(home):
    assert prompt_packs.role_packs() == prompt_packs.DEFAULT_ROLE_PACKS
    assert prompt_packs.role_packs()["unreal"] == ["unreal-operator"]
    assert prompt_packs.role_packs()["worker"] == ["role-worker"]
    saved = prompt_packs.set_role_packs({"Worker": "beads, common", "pm": []})
    assert saved == {"worker": ["beads", "common"]}
    assert prompt_packs.required_for(["worker", "unreal"]) == {
        "beads": ["worker"], "common": ["worker"],
    }
    # Clearing every row is kept as cleared, not read back as the default.
    assert prompt_packs.set_role_packs({}) == {}
    assert prompt_packs.set_role_packs(None) == prompt_packs.DEFAULT_ROLE_PACKS
    with pytest.raises(prompt_packs.PackError):
        prompt_packs.set_role_packs({"unreal": ["Bad Id"]})


def test_default_packs_follow_the_headers_until_saved(home):
    assert prompt_packs.default_packs() == ["common"]
    assert prompt_packs.set_default_packs(["beads", "common"]) == ["beads", "common"]
    assert prompt_packs.set_default_packs([]) == []
    assert prompt_packs.set_default_packs(None) == ["common"]


def test_always_packs_are_empty_until_saved_and_kept_apart(home):
    assert prompt_packs.always_packs() == []
    assert prompt_packs.set_always_packs(["common", "common"]) == ["common"]
    # A list of its own: saving it leaves the default-ticked list alone.
    assert prompt_packs.default_packs() == ["common"]
    assert prompt_packs.set_default_packs([]) == []
    assert prompt_packs.always_packs() == ["common"]
    with pytest.raises(prompt_packs.PackError):
        prompt_packs.set_always_packs(["Bad Id"])
    assert prompt_packs.always_packs() == ["common"]
    assert prompt_packs.set_always_packs(None) == []
    assert prompt_packs.default_packs() == []


def test_choose_adds_required_packs_and_names_the_role(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    prompt_packs.install(ws, "common")
    mapping = {"unreal": ["unreal-operator"]}
    # Required but not installed: the shipped copy goes in, marked as such.
    recs, required = prompt_packs.choose(
        ws, ["common"], ["worker", "unreal"], mapping=mapping
    )
    assert [r["id"] for r in recs] == ["common", "unreal-operator"]
    assert "source" not in recs[0]
    assert recs[1]["source"] == prompt_packs.PACKAGE
    assert recs[1]["text"] == prompt_packs.shipped("unreal-operator").body
    assert required == {"unreal-operator": ["unreal"]}
    # ...also with no workspace at all, and when the form sent it as a tick.
    recs, _ = prompt_packs.choose(
        None, ["unreal-operator"], ["unreal"], mapping=mapping
    )
    assert recs == [{
        "id": "unreal-operator", "source": prompt_packs.PACKAGE,
        "text": prompt_packs.shipped("unreal-operator").body,
        "required_by": ["unreal"], "ticked": True,
    }]

    # An installed copy wins, edits included.
    prompt_packs.install(ws, "unreal-operator")
    copy = ws / ".claunch" / "prompts" / "unreal-operator.md"
    copy.write_text(copy.read_text(encoding="utf-8") + "\nhouse rule\n",
                    encoding="utf-8")
    recs, _ = prompt_packs.choose(
        ws, ["common"], ["worker", "unreal"], mapping=mapping
    )
    assert recs[1]["text"].endswith("house rule") and "source" not in recs[1]

    # A ticked pack needs no install either, whatever the role: claunch's
    # copy goes in. One that exists nowhere is refused, a required one with
    # the role named.
    recs, required = prompt_packs.choose(ws, ["beads"], [], mapping=mapping)
    assert recs == [{
        "id": "beads", "source": prompt_packs.PACKAGE,
        "text": prompt_packs.shipped("beads").body, "ticked": True,
    }] and required == {}
    recs, _ = prompt_packs.choose(None, ["beads"], [])
    assert recs[0]["source"] == prompt_packs.PACKAGE
    with pytest.raises(prompt_packs.PackError, match="nor shipped with claunch"):
        prompt_packs.choose(ws, ["ghost"], [], mapping=mapping)
    with pytest.raises(prompt_packs.PackError, match="required by role ghost"):
        prompt_packs.choose(ws, [], ["ghost"], mapping={"ghost": ["no-such-pack"]})


def test_a_package_copy_says_so_in_the_opening():
    block = prompt_packs.opening([
        {"id": "role-worker", "text": "be a worker", "source": prompt_packs.PACKAGE},
        {"id": "common", "text": "house"},
    ])
    first, second = block.split("\n\n---\n")
    assert "# claunch prompt pack: role-worker -- shipped with claunch" in first
    assert "not installed in this workspace" in first
    assert "# claunch prompt pack: common -- installed in this workspace" in second
    assert "file: .claunch/prompts/common.md" in second
    sdef = SessionDef.from_dict({
        "name": "s",
        "prompt_packs": [{"id": "role-worker", "text": "t", "source": "package"}],
    })
    assert sdef.prompt_packs == ({"id": "role-worker", "text": "t", "source": "package"},)


def test_a_package_copy_says_why_it_went_in():
    # The roles that require it, else that it was ticked; a record from before
    # the reason was kept says neither.
    recs = prompt_packs.records(
        [prompt_packs.pack_copy(None, "role-worker"),
         prompt_packs.pack_copy(None, "unreal-operator"),
         prompt_packs.pack_copy(None, "beads")],
        required={"role-worker": ["worker", "lead"], "unreal-operator": ["unreal"]},
        asked=["unreal-operator", "beads"],
    )
    assert [(r.get("required_by"), r.get("ticked")) for r in recs] == [
        (["worker", "lead"], None), (["unreal"], True), (None, True),
    ]
    heads = [line for line in prompt_packs.opening(recs).splitlines()
             if line.startswith("# claunch prompt pack:")]
    assert heads[0].endswith(
        "role-worker -- shipped with claunch, required by roles worker, lead, "
        "not installed in this workspace")
    # Required wins over ticked: the role is why it cannot be unticked.
    assert heads[1].endswith(
        "unreal-operator -- shipped with claunch, required by role unreal, "
        "not installed in this workspace")
    assert heads[2].endswith(
        "beads -- shipped with claunch, ticked at session creation, "
        "not installed in this workspace")
    legacy = prompt_packs.opening([{"id": "beads", "text": "t", "source": "package"}])
    assert "beads -- shipped with claunch, not installed in this workspace" in legacy
    # A workspace copy's record is unchanged: no reason keys.
    ws_rec = prompt_packs.records(
        [prompt_packs.Pack(id="common", title="c", description="", body="b", raw="b")],
        required={"common": ["worker"]}, asked=["common"],
    )
    assert ws_rec == [{"id": "common", "text": "b"}]
    # The session definition keeps the reason across a restart record.
    sdef = SessionDef.from_dict({"name": "s", "prompt_packs": recs})
    assert [dict(r) for r in sdef.prompt_packs] == recs


def test_the_stance_command_prints_the_role_packs(home, tmp_path, monkeypatch, capsys):
    from claude_launcher import cli_mesh

    # Outside any workspace: claunch's copy, for the primary and a subrole.
    monkeypatch.chdir(tmp_path)
    cli_mesh._print_role_packs(["worker", "unreal", ""])
    out = capsys.readouterr().out
    assert ("# claunch prompt pack: role-worker -- shipped with claunch, "
            "required by role worker") in out
    assert prompt_packs.shipped("role-worker").body in out
    assert "# claunch prompt pack: unreal-operator -- shipped with claunch" in out

    # Inside a workspace that installed and edited one: its copy.
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")
    prompt_packs.install(ws, "role-worker")
    copy = ws / ".claunch" / "prompts" / "role-worker.md"
    copy.write_text(copy.read_text(encoding="utf-8") + "\nhouse rule\n",
                    encoding="utf-8")
    monkeypatch.chdir(ws)
    cli_mesh._print_role_packs(["worker"])
    out = capsys.readouterr().out
    assert "role-worker -- installed in this workspace" in out
    assert out.rstrip().endswith("house rule")

    # A role with no packs prints nothing.
    prompt_packs.set_role_packs({})
    cli_mesh._print_role_packs(["worker"])
    assert capsys.readouterr().out == ""


def test_packaged_roles_keep_a_short_stance_and_a_pack():
    from claude_launcher.daemon import mesh_roles

    rs = mesh_roles.resolve()
    for name, role in rs.roles.items():
        # One or two sentences: the working rules live in the role's pack.
        assert len(role.stance) < 400, name
        assert prompt_packs.DEFAULT_ROLE_PACKS.get(name), name
        for pack_id in prompt_packs.DEFAULT_ROLE_PACKS[name]:
            assert prompt_packs.shipped(pack_id).body


def test_session_def_keeps_the_recorded_text():
    sdef = SessionDef.from_dict({
        "name": "s", "prompt_packs": [{"id": "beads", "text": "t"}, "junk"],
    })
    assert sdef.prompt_packs == ({"id": "beads", "text": "t"},)
    assert SessionDef.from_dict(sdef.to_dict()) == sdef
    assert "prompt_packs" not in SessionDef(name="bare").to_dict()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _spy_openings():
    seen: dict = {}
    real = harness_mod.build_command

    def spy(sdef, **kw):
        seen[sdef.name] = kw.get("opening", "")
        return real(sdef, **kw)

    harness_mod.build_command = spy
    return seen, lambda: setattr(harness_mod, "build_command", real)


def test_bulk_install_preserves_edits_updates_stale_and_reports_errors(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    prompt_packs.install(ws, "common")
    prompt_packs.install(ws, "beads")
    directory = prompt_packs.install_dir(ws)
    (directory / "common.md").write_text("my rules", encoding="utf-8")
    (directory / "house.md").write_text("local rules", encoding="utf-8")
    (directory / "beads.md").write_text("old rules", encoding="utf-8")
    record = prompt_packs._record(ws)
    record["beads"] = prompt_packs._sha("old rules")
    prompt_packs._write_record(ws, record)
    real_install = prompt_packs.install

    def fail_one(root, pack_id, **kwargs):
        if pack_id == "role-worker":
            raise PermissionError("read only")
        return real_install(root, pack_id, **kwargs)

    monkeypatch.setattr(prompt_packs, "install", fail_one)
    results = {r["id"]: r for r in prompt_packs.install_all(ws)}
    assert results["common"] == {"id": "common", "was": "edited", "changed": False}
    assert results["beads"]["was"] == "stale"
    assert results["beads"]["changed"] is True
    assert results["role-worker"]["error"] == "read only"
    assert results["role-leader"]["changed"] is True
    assert (directory / "common.md").read_text() == "my rules"
    assert (directory / "house.md").read_text() == "local rules"
    monkeypatch.setattr(prompt_packs, "install", real_install)
    retry = prompt_packs.install_all(ws)
    assert [r["id"] for r in retry if r["changed"]] == ["role-worker"]
    assert not any(r["changed"] for r in prompt_packs.install_all(ws))


def test_bulk_install_route_requires_registered_workspace(home, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr, None)
        try:
            for body in ({}, {"workspace": "unknown"}):
                response = await client.post("/api/prompt-packs/install-all", headers=BEARER, json=body)
                assert response.status == 400
            assert not prompt_packs.install_dir(ws).exists()
            response = await client.post("/api/prompt-packs/install-all", headers=BEARER,
                                         json={"workspace": "proj"})
            assert response.status == 200
            doc = await response.json()
            assert len(doc["batch"]) == len(prompt_packs.catalog())
            assert all(r["changed"] for r in doc["batch"])
            assert all(r["status"] == "current" for r in doc["workspaces"][0]["packs"])
        finally:
            await client.close()

    asyncio.run(run())


def test_settings_routes_install_remove_and_save_the_mapping(home, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr, None)
        try:
            doc = await (await client.get("/api/prompt-packs", headers=BEARER)).json()
            assert doc["role_packs"] == prompt_packs.DEFAULT_ROLE_PACKS
            assert doc["default_packs"] == ["common"]
            assert doc["always_packs"] == []
            assert "unreal" in doc["roles"]
            row = next(w for w in doc["workspaces"] if w["name"] == "proj")
            assert {p["id"]: p["status"] for p in row["packs"]}["beads"] == "missing"

            resp = await client.post("/api/prompt-packs/install", headers=BEARER,
                                     json={"workspace": "proj", "pack": "beads"})
            doc = await resp.json()
            assert resp.status == 200 and doc["was"] == "missing"
            assert (ws / ".claunch" / "prompts" / "beads.md").is_file()

            resp = await client.post("/api/prompt-packs/install", headers=BEARER,
                                     json={"workspace": "nope", "pack": "beads"})
            assert resp.status == 400

            offer = await (await client.get(
                "/api/prompt-packs/offer", params={"cwd": str(ws / "sub")},
                headers=BEARER,
            )).json()
            assert offer["workspace"]["name"] == "proj"
            assert [p["id"] for p in offer["packs"]] == ["beads"]
            # The catalog rides along so the form can name a required pack
            # that goes in as claunch's copy.
            assert "role-worker" in [p["id"] for p in offer["shipped"]]

            resp = await client.put("/api/prompt-packs/role-packs", headers=BEARER,
                                    json={"role_packs": {"unreal": ["x y"]},
                                          "default_packs": ["beads"]})
            assert resp.status == 400
            # The bad half refused the good half too.
            assert prompt_packs.default_packs() == ["common"]
            resp = await client.put("/api/prompt-packs/role-packs", headers=BEARER,
                                    json={"role_packs": {"worker": ["beads"]},
                                          "default_packs": ["beads"]})
            doc = await resp.json()
            assert doc["role_packs"] == {"worker": ["beads"]}
            assert doc["default_packs"] == ["beads"]

            # The every-opening list: a bad id refuses the whole body, a good
            # one is saved alone, read back by Settings and by the form's
            # offer, and null empties it.
            resp = await client.put("/api/prompt-packs/role-packs", headers=BEARER,
                                    json={"default_packs": ["common"],
                                          "always_packs": ["Bad Id"]})
            assert resp.status == 400
            assert prompt_packs.default_packs() == ["beads"]
            resp = await client.put("/api/prompt-packs/role-packs", headers=BEARER,
                                    json={"always_packs": ["common"]})
            doc = await resp.json()
            assert resp.status == 200 and doc["always_packs"] == ["common"]
            assert doc["default_packs"] == ["beads"]
            offer = await (await client.get(
                "/api/prompt-packs/offer", params={"cwd": str(ws)},
                headers=BEARER,
            )).json()
            assert offer["always_packs"] == ["common"]
            resp = await client.put("/api/prompt-packs/role-packs", headers=BEARER,
                                    json={"always_packs": None})
            assert (await resp.json())["always_packs"] == []
            resp = await client.put("/api/prompt-packs/role-packs", headers=BEARER,
                                    json={})
            assert resp.status == 400

            resp = await client.post("/api/prompt-packs/remove", headers=BEARER,
                                     json={"workspace": "proj", "pack": "beads"})
            assert resp.status == 200
            assert not (ws / ".claunch" / "prompts" / "beads.md").exists()
        finally:
            await client.close()

    asyncio.run(run())


def test_create_puts_ticked_packs_ahead_of_the_task(home, tmp_path):
    _register_py_harness()
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")
    prompt_packs.install(ws, "common")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr, MeshManager(mgr, root=tmp_path / "mesh"))
        seen, restore = _spy_openings()
        try:
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "p1", "profile": "py", "cwd": str(ws),
                "task": "do the thing", "beads": False,
                "prompt_packs": ["common"],
            })
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["prompt_packs"] == ["common"]
            opening = seen["p1"]
            # Standing rules first, then the arranged part with the task.
            assert opening.index("# claunch prompt pack: common") < opening.index("do the thing")
            recorded = mgr.get("p1").sdef.prompt_packs
            assert [r["id"] for r in recorded] == ["common"]
            ids = rebrief.addressable("p1", manager=mgr, mesh_mgr=None)
            assert any(v["kind"] == "prompt pack (common)" for v in ids.values())

            # A pack the workspace does not hold, ticked with no mesh and no
            # role, goes in as claunch's copy and is reported as such.
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "p2", "profile": "py", "cwd": str(ws),
                "task": "x", "beads": False, "prompt_packs": ["beads"],
            })
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["prompt_packs"] == ["beads"]
            assert doc["prompt_packs_shipped"] == ["beads"]
            assert ("# claunch prompt pack: beads -- shipped with claunch, "
                    "ticked at session creation") in seen["p2"]

            # One that exists nowhere is refused, nothing staged.
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "p3", "profile": "py", "cwd": str(ws),
                "task": "x", "beads": False, "prompt_packs": ["ghost"],
            })
            assert resp.status == 400
            assert "nor shipped with claunch" in (await resp.json())["error"]
            assert "p3" not in [s.sdef.name for s in mgr.list()]
        finally:
            restore()
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_unreal_subrole_forces_its_pack(home, tmp_path):
    _register_py_harness()
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        seen, restore = _spy_openings()
        try:
            mm.create("team")
            body = {
                "profile": "py", "cwd": str(ws), "task": "build it",
                "beads": False, "mesh": "team", "role": "worker",
                "subroles": ["unreal"],
            }
            # Required but not installed: claunch's copy goes in, and the
            # report and the opening both say so.
            resp = await client.post("/api/sessions", headers=BEARER,
                                     json={**body, "name": "u1"})
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["prompt_packs"] == ["role-worker", "unreal-operator"]
            assert doc["prompt_packs_required"] == {
                "role-worker": ["worker"], "unreal-operator": ["unreal"],
            }
            assert doc["prompt_packs_shipped"] == ["role-worker", "unreal-operator"]
            assert (
                "# claunch prompt pack: unreal-operator -- shipped with claunch, "
                "required by role unreal" in seen["u1"]
            )

            prompt_packs.install(ws, "unreal-operator")
            resp = await client.post("/api/sessions", headers=BEARER,
                                     json={**body, "name": "u2"})
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["prompt_packs"] == ["role-worker", "unreal-operator"]
            assert doc["prompt_packs_shipped"] == ["role-worker"]
            u2 = seen["u2"]
            assert u2.index("# claunch prompt pack: unreal-operator") < u2.index("build it")
            assert "# claunch prompt pack: unreal-operator -- installed" in u2
            # The briefing's stance is the short one; the pack holds the rest.
            assert "You are a PRODUCER" in u2
            assert "role prompt packs: role-worker, unreal-operator" in u2

            # A spawned child gets the same rule.
            resp = await client.post("/api/sessions/u2/children", headers=BEARER,
                                     json={"name": "u3", "task": "help",
                                           "role": "worker", "subroles": ["unreal"]})
            doc = await resp.json()
            assert resp.status == 201, doc
            assert "# claunch prompt pack: unreal-operator" in seen["u3"]
        finally:
            restore()
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_text_route_shows_what_the_opening_would_get(home, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")
    prompt_packs.install(ws, "common")
    prompt_packs.install(ws, "beads")
    beads = ws / ".claunch" / "prompts" / "beads.md"
    beads.write_text(beads.read_text(encoding="utf-8") + "\nOUR OWN RULE\n", encoding="utf-8")
    (ws / ".claunch" / "prompts" / "house.md").write_text(
        "---\ntitle: House\n---\nonly here\n", encoding="utf-8"
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr, None)

        async def text(pack_id, cwd=ws / "sub"):
            resp = await client.get("/api/prompt-packs/text", headers=BEARER,
                                    params={"id": pack_id, "cwd": str(cwd)})
            return resp.status, await resp.json()

        try:
            status, doc = await text("common")
            assert status == 200, doc
            assert (doc["source"], doc["status"]) == ("workspace", "current")
            assert doc["body"] == prompt_packs.shipped("common").body
            assert doc["lines"] == len(doc["body"].splitlines())
            assert doc["workspace"]["name"] == "proj"
            # The id the opening prints beside the same text.
            assert doc["text_id"] == prompt_packs.text_id(doc["body"])

            # An edit in the workspace is what the session would read.
            status, doc = await text("beads")
            assert (doc["source"], doc["status"]) == ("workspace", "edited")
            assert "OUR OWN RULE" in doc["body"]

            status, doc = await text("house")
            assert (doc["source"], doc["status"]) == ("workspace", "local")

            # Not installed here: claunch's copy, as pack_copy would give it.
            status, doc = await text("role-worker")
            assert status == 200 and doc["source"] == "package"
            assert "status" not in doc
            assert doc["body"] == prompt_packs.shipped("role-worker").body

            # Outside any workspace, the shipped copy too.
            status, doc = await text("beads", cwd=tmp_path / "elsewhere")
            assert doc["source"] == "package" and "OUR OWN RULE" not in doc["body"]
            assert doc["workspace"] is None

            for bad in ("ghost", "Bad Id", ""):
                status, doc = await text(bad)
                assert status == 404, (bad, doc)
        finally:
            await client.close()

    asyncio.run(run())
