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
def test_the_catalog_ships_the_three_packs_and_common_is_the_default():
    ids = [p.id for p in prompt_packs.catalog()]
    assert {"beads", "common", "unreal-operator"} <= set(ids)
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
    assert prompt_packs.resolve(ws, ["beads"])[0].body.endswith("local rule")
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


def test_resolve_refuses_what_is_not_installed(tmp_path):
    with pytest.raises(prompt_packs.PackError, match="no registered workspace"):
        prompt_packs.resolve(None, ["beads"])
    with pytest.raises(prompt_packs.PackError, match="not installed"):
        prompt_packs.resolve(tmp_path, ["beads"])
    with pytest.raises(prompt_packs.PackError, match="bad prompt pack id"):
        prompt_packs.resolve(tmp_path, ["../x"])
    assert prompt_packs.resolve(None, []) == []


def test_opening_block_names_the_id_rebrief_uses():
    block = prompt_packs.opening([{"id": "beads", "text": "rule one"}])
    assert "# claunch prompt pack: beads" in block
    assert f"text id: {rebrief.block_digest('rule one')}" in block
    assert block.endswith("rule one")


def test_role_packs_default_save_and_clear(home):
    assert prompt_packs.role_packs() == {"unreal": ["unreal-operator"]}
    saved = prompt_packs.set_role_packs({"Worker": "beads, common", "pm": []})
    assert saved == {"worker": ["beads", "common"]}
    assert prompt_packs.required_for(["worker", "unreal"]) == {
        "beads": ["worker"], "common": ["worker"],
    }
    # Clearing every row is kept as cleared, not read back as the default.
    assert prompt_packs.set_role_packs({}) == {}
    assert prompt_packs.set_role_packs(None) == {"unreal": ["unreal-operator"]}
    with pytest.raises(prompt_packs.PackError):
        prompt_packs.set_role_packs({"unreal": ["Bad Id"]})


def test_default_packs_follow_the_headers_until_saved(home):
    assert prompt_packs.default_packs() == ["common"]
    assert prompt_packs.set_default_packs(["beads", "common"]) == ["beads", "common"]
    assert prompt_packs.set_default_packs([]) == []
    assert prompt_packs.set_default_packs(None) == ["common"]


def test_choose_adds_required_packs_and_names_the_role(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    prompt_packs.install(ws, "common")
    mapping = {"unreal": ["unreal-operator"]}
    with pytest.raises(prompt_packs.PackError, match="required by role unreal"):
        prompt_packs.choose(ws, ["common"], ["worker", "unreal"], mapping=mapping)
    prompt_packs.install(ws, "unreal-operator")
    recs, required = prompt_packs.choose(
        ws, ["common"], ["worker", "unreal"], mapping=mapping
    )
    assert [r["id"] for r in recs] == ["common", "unreal-operator"]
    assert required == {"unreal-operator": ["unreal"]}


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


def test_settings_routes_install_remove_and_save_the_mapping(home, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr, None)
        try:
            doc = await (await client.get("/api/prompt-packs", headers=BEARER)).json()
            assert doc["role_packs"] == {"unreal": ["unreal-operator"]}
            assert doc["default_packs"] == ["common"]
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

            # A pack the workspace does not hold is refused, nothing staged.
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "p2", "profile": "py", "cwd": str(ws),
                "task": "x", "beads": False, "prompt_packs": ["beads"],
            })
            assert resp.status == 400
            assert "not installed" in (await resp.json())["error"]
            assert "p2" not in [s.sdef.name for s in mgr.list()]
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
            # Required but not installed: refused, and the role is named.
            resp = await client.post("/api/sessions", headers=BEARER,
                                     json={**body, "name": "u1"})
            assert resp.status == 400
            err = (await resp.json())["error"]
            assert "unreal-operator" in err and "required by role unreal" in err

            prompt_packs.install(ws, "unreal-operator")
            resp = await client.post("/api/sessions", headers=BEARER,
                                     json={**body, "name": "u2"})
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["prompt_packs"] == ["unreal-operator"]
            assert doc["prompt_packs_required"] == {"unreal-operator": ["unreal"]}
            u2 = seen["u2"]
            assert u2.index("# claunch prompt pack: unreal-operator") < u2.index("build it")

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
