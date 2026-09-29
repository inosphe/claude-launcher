"""Mesh addresses and renamed daemons (docs/mesh-design.md "Mesh addresses").

N. Addresses
   N1 a mirror written before addresses (keyed by its bare name) is moved to
      name@origin on load, and the bare name still resolves to it
   N2 an address naming the daemon that holds authority now (not the one
      that created the mesh) still finds the mirror

R. Renamed daemons
   R1 the OWNER is renamed: its peers are told over the link they hold for
      the old name; the mirrors re-key to name@new, the roster and rank
      list follow, and traffic keeps flowing
   R2 a GUEST is renamed: the owner moves the link and the guest's members
      to the new name and fans the roster out to the other guests
   R3 an operator's rename-peer does the same by hand, refuses to merge two
      peers, and refuses this daemon's own name
"""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshConflict, MeshError, MeshManager

from test_mesh_attach import _dispatch_peer as _dispatch_attach_peer
from test_mesh_join import _manager, _primary_with_alice, _register_py_harness


def _dispatch_peer(mm: MeshManager, path: str, body: dict):
    if path == "/peer/mesh/renamed":
        return mm.peer_renamed_accept(
            body["mesh"], body["machine"], body.get("token") or "",
            body.get("old") or "",
        )
    return _dispatch_attach_peer(mm, path, body)


def _wire(machines: dict, *, rename: bool = False) -> None:
    async def call(machine, path, body):
        result = _dispatch_peer(machines[machine], path, body)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    for name, mm in machines.items():
        if rename:
            mm.machine = name  # the setter is what notices a rename
        else:
            mm._machine = name
            for mesh in mm._meshes.values():
                mesh.me = name
        mm.peer_transport = call
        mm.relay_connected = lambda: True


async def _three(mgr, tmp_path):
    """pcA owns 'm' (alice); pcB (bob) and pcC (carl) are guests."""
    mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
    mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
    _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})
    mgr.create(SessionDef(name="sc", harness="py", cwd=str(tmp_path), rows=80))
    for mm, sess, handle in ((mm_b, "sb", "bob"), (mm_c, "sc", "carl")):
        code = mm_a.invite("m")["code"]
        await mm.join("m@pcA", sess, handle=handle, code=code)
    return mm_a, mm_b, mm_c


# --------------------------------------------------------------------------- #
# N1 / N2
# --------------------------------------------------------------------------- #
def test_legacy_mirror_is_moved_to_its_address(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        from test_mesh_join import _wire as _join_wire
        _join_wire({"pcA": mm_a, "pcB": mm_b})
        code = mm_a.invite("m")["code"]
        await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        # rewrite B's mirror the way a daemon before addresses left it
        root = tmp_path / "meshB"
        (root / "m@pcA").rename(root / "m")
        doc_path = root / "m" / "mesh.json"
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        doc.pop("origin")
        doc_path.write_text(json.dumps(doc), encoding="utf-8")

        again = MeshManager(mgr, settle=0.05, root=root)
        again.load_all()
        mesh = again.get("m@pcA")
        assert mesh.origin == "pcA" and mesh.members["bob"].machine == "pcB"
        assert again.get("m") is mesh  # the bare name still reaches it
        assert (root / "m@pcA" / "mesh.json").is_file()
        assert not (root / "m").exists()
        assert json.loads((root / "m@pcA" / "mesh.json").read_text())["origin"] == "pcA"
        # a local mesh of that name can now be created beside it
        again.create("m")
        assert again.get("m").origin == "" and again.get("m@pcA") is mesh
        # N2: the authority moved — the address by its holder still resolves
        mesh.peers = ["pcZ", "pcA", "pcB"]
        assert again.get("m@pcZ") is mesh
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# R1: the owner is renamed
# --------------------------------------------------------------------------- #
def test_renamed_owner_tells_its_peers(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b, mm_c = await _three(mgr, tmp_path)
        # the relay now knows pcA as pcA2; the uplink hands the new name over
        _wire({"pcA2": mm_a, "pcB": mm_b, "pcC": mm_c}, rename=True)
        mesh_a = mm_a.get("m")
        assert mesh_a.peers[0] == "pcA2"
        assert mesh_a.members["alice"].machine == "pcA2"
        assert sorted(mesh_a.rename_notice["pending"]) == ["pcB", "pcC"]
        assert mesh_a.rename_notice["old"] == "pcA"

        await mm_a._flush_rename_notice(mesh_a)
        assert mesh_a.rename_notice is None
        for mm, other in ((mm_b, "pcC"), (mm_c, "pcB")):
            mirror = mm.get("m@pcA2")
            assert mirror.name == "m@pcA2" and mirror.primary == "pcA2"
            assert mirror.members["alice"].machine == "pcA2"
            assert "pcA2" in mirror.links and "pcA" not in mirror.links
            assert mm.get("m") is mirror
            with pytest.raises(MeshError):
                mm.get("m@pcA")
        assert (tmp_path / "meshB" / "m@pcA2").is_dir()

        # traffic keeps flowing over the renamed link
        await mm_b.send("m@pcA2", "bob", "alice", "after the rename")
        assert [x["body"] for x in mesh_a.messages] == ["after the rename"]

        # the notice survives a restart until delivered
        mesh_a.rename_notice = {"old": "pcA", "pending": ["pcB"]}
        mm_a._persist_def(mesh_a)
        again = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        again.load_all()
        assert again.get("m").rename_notice == {"old": "pcA", "pending": ["pcB"]}
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# R2: a guest is renamed
# --------------------------------------------------------------------------- #
def test_renamed_guest_is_moved_on_the_owner(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b, mm_c = await _three(mgr, tmp_path)
        _wire({"pcA": mm_a, "pcB2": mm_b, "pcC": mm_c}, rename=True)
        mirror_b = mm_b.get("m@pcA")
        assert mirror_b.members["bob"].machine == "pcB2"
        assert "pcA" in mirror_b.rename_notice["pending"]

        await mm_b._flush_rename_notice(mirror_b)
        mesh_a = mm_a.get("m")
        assert "pcB2" in mesh_a.links and "pcB" not in mesh_a.links
        assert "pcB2" in mesh_a.peers and "pcB" not in mesh_a.peers
        assert mesh_a.members["bob"].machine == "pcB2"
        assert all("pcB|" not in k and "|pcB" not in k
                   for k in mesh_a.pair_links)
        # the owner still reaches bob under the new name
        await mm_a.send("m", "alice", "bob", "hello renamed guest")
        await mm_a._flush_guest(mesh_a, "pcB2")
        assert any(x["body"] == "hello renamed guest"
                   for x in mirror_b.messages)
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# R3: the operator's rename-peer
# --------------------------------------------------------------------------- #
def test_operator_rename_peer(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b, mm_c = await _three(mgr, tmp_path)
        mm_b._offers["other@pcA"] = {
            "mesh": "other", "machine": "pcA", "token": "t", "members": 0,
        }
        result = mm_b.rename_peer("pcA", "pcA3")
        assert result["meshes"] == ["m@pcA"]
        assert result["rekeyed"] == [{"from": "m@pcA", "to": "m@pcA3"}]
        mirror = mm_b.get("m@pcA3")
        assert mirror.primary == "pcA3" and "pcA3" in mirror.links
        assert [o["machine"] for o in mm_b.offers_received()] == ["pcA3"]

        with pytest.raises(MeshError):
            mm_b.rename_peer("pcB", "pcX")  # this daemon's own name
        with pytest.raises(MeshError):
            mm_b.rename_peer("pcC", "local")
        # two peers of one mesh are never merged into one name, and nothing
        # is half-renamed when that is refused
        with pytest.raises(MeshConflict):
            mm_a.rename_peer("pcC", "pcB")
        assert {"pcB", "pcC"} <= set(mm_a.get("m").peers)
        assert mm_a.get("m").members["carl"].machine == "pcC"
        await mgr.shutdown_all()

    asyncio.run(run())
