"""The packs pane's daemon half (claunch-6esft.1): a session's prompt packs,
the block sending one types, and the held record that lets a sent pack come
back after a compaction through the same door as the opening's packs (the
reminder names its id, ``rebrief`` answers for it).

Staged sessions for the unit half -- nothing needs a PTY to be read -- and
the real app for the routes, which is where send, hold and drop meet.
"""

from __future__ import annotations

import asyncio
import sys
import time

from claude_launcher import lineage, profile, prompt_packs, store, workspaces
from claude_launcher.daemon import rebrief, session_packs, session_reminder
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


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _screen_has(session, needle: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await session.screen_synced()
        if needle in "\n".join(session.capture(history=True) + session.capture()):
            return True
        await asyncio.sleep(0.1)
    return False


def _staged(tmp_path, **kw):
    _register_py_harness()
    mgr = _manager()

    async def stage():
        mgr.stage(SessionDef(name="w1", harness="py", cwd=str(tmp_path), **kw))

    asyncio.run(stage())
    return mgr


def test_the_view_splits_the_opening_from_the_rest(home, tmp_path):
    beads = prompt_packs.shipped("beads").body
    mgr = _staged(tmp_path, prompt_packs=[
        {"id": "beads", "text": beads, "source": "package", "ticked": True},
    ])
    out = session_packs.view("w1", manager=mgr)
    assert [g["id"] for g in out["given"]] == ["beads"]
    given = out["given"][0]
    assert given["text_id"] == prompt_packs.text_id(beads)
    assert given["ticked"] is True and given["changed"] is False
    kinds = {a["id"]: a["kind"] for a in out["available"]}
    assert "beads" not in kinds
    assert kinds["mcp-qa"] == session_packs.MCP
    assert kinds["retrospective"] == session_packs.ON_DEMAND
    assert kinds["korean-replies"] == prompt_packs.OPENING
    assert out["sent"] == []


def test_each_kind_is_sent_as_the_block_the_session_knows(home, tmp_path):
    beads = prompt_packs.shipped("beads").body
    mgr = _staged(tmp_path, prompt_packs=[{"id": "beads", "text": beads, "source": "package"}])

    block, how, text = session_packs.block_for("w1", "beads", manager=mgr)
    assert how == session_packs.GIVEN and text == beads
    assert block == prompt_packs.opening([{"id": "beads", "text": beads, "source": "package"}])

    block, how, text = session_packs.block_for("w1", "MCP-QA", manager=mgr)
    assert how == session_packs.MCP
    assert block == prompt_packs.loaded_block(prompt_packs.loadable_copy(None, "mcp-qa"))
    assert session_packs.text_id_of(block) == prompt_packs.text_id(text)

    block, how, _ = session_packs.block_for("w1", "retrospective", manager=mgr)
    assert how == session_packs.ON_DEMAND and "sent on demand" in block

    block, how, text = session_packs.block_for("w1", "korean-replies", manager=mgr)
    assert how == session_packs.OTHER
    assert "sent by a person to this running session, not part of its opening" in block
    assert f"text id: {prompt_packs.text_id(text)}" in block


def test_a_held_send_is_named_by_the_reminder_and_recalled_by_rebrief(home, tmp_path):
    mgr = _staged(tmp_path, task="dig")
    mm = MeshManager(mgr)
    block, how, text = session_packs.block_for("w1", "korean-replies", manager=mgr)
    ident = session_packs.text_id_of(block)
    session_packs.record_sent("w1", session_packs.sent_row("korean-replies", how, block, True), text)

    entries = session_reminder.pack_entries(mgr.get("w1").sdef)
    assert {"id": "korean-replies", "digest": ident, "source": "sent", "required_by": []} in entries
    lines = session_reminder.pack_section(entries, [])
    assert f"prompt pack korean-replies: text id {ident} (sent)" in lines

    found = rebrief.recall("w1", ident, manager=mgr, mesh_mgr=mm)
    assert found["status"] == "recalled"
    assert found["kind"] == "prompt pack (korean-replies, sent)"
    assert found["text"] == text.strip()
    held = {a["id"]: a.get("held") for a in session_packs.view("w1", manager=mgr)["available"]}
    assert held["korean-replies"]["text_id"] == ident

    assert session_packs.drop("w1", "korean-replies") is True
    assert session_packs.drop("w1", "korean-replies") is False
    assert not [e for e in session_reminder.pack_entries(mgr.get("w1").sdef) if e["source"] == "sent"]
    assert rebrief.recall("w1", ident, manager=mgr, mesh_mgr=mm)["status"] == "stale_id"
    # The log of what was sent stays.
    assert [r["pack"] for r in session_packs.sent("w1")] == ["korean-replies"]


def test_the_held_text_is_the_one_sent_even_after_the_file_changes(home, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")
    prompt_packs.install(ws, "korean-replies")
    path = prompt_packs.install_dir(ws) / "korean-replies.md"
    mgr = _staged(ws)
    mm = MeshManager(mgr)
    block, how, text = session_packs.block_for("w1", "korean-replies", manager=mgr)
    ident = session_packs.text_id_of(block)
    session_packs.record_sent("w1", session_packs.sent_row("korean-replies", how, block, True), text)
    path.write_text(path.read_text(encoding="utf-8") + "\nlater edit\n", encoding="utf-8")
    found = rebrief.recall("w1", ident, manager=mgr, mesh_mgr=mm)
    assert found["status"] == "recalled" and "later edit" not in found["text"]


def test_an_undelivered_send_is_logged_but_not_held(home, tmp_path):
    mgr = _staged(tmp_path)
    block, how, text = session_packs.block_for("w1", "mcp-sim", manager=mgr)
    session_packs.record_sent("w1", session_packs.sent_row("mcp-sim", how, block, False), text)
    assert session_packs.held("w1") == {}
    assert session_packs.sent("w1")[0]["delivered"] is False


def test_the_routes_preview_send_hold_and_drop(home, tmp_path):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr, settle=0.05)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/sessions", headers=BEARER,
                json={"name": "root", "profile": "py", "cwd": str(tmp_path),
                      "prompt_packs": []},
            )
            assert resp.status == 201, await resp.text()
            resp = await client.get("/api/sessions/root/packs", headers=BEARER)
            view = await resp.json()
            assert view["session"] == "root" and view["given"] == []
            assert any(a["id"] == "mcp-qa" for a in view["available"])

            resp = await client.get("/api/sessions/root/packs?pack=mcp-qa", headers=BEARER)
            preview = await resp.json()
            assert preview["how"] == "mcp" and "loaded on demand" in preview["block"]

            resp = await client.post("/api/sessions/root/packs", headers=BEARER,
                                     json={"pack": "mcp-qa"})
            sent = await resp.json()
            assert resp.status == 200 and sent["ok"] is True
            assert sent["delivered"] is True and sent["text_id"] == preview["text_id"]
            assert "mcp-qa" in session_packs.held("root")
            # delivered=True says the terminal took input; the echo says it
            # was this block.
            assert await _screen_has(mgr.get("root"), "echo:# claunch prompt pack: mcp-qa")

            # An on-demand pack asks for work once: sent, never held.
            resp = await client.post("/api/sessions/root/packs", headers=BEARER,
                                     json={"pack": "retrospective"})
            assert (await resp.json())["delivered"] is True
            assert "retrospective" not in session_packs.held("root")

            resp = await client.get("/api/sessions/root/packs", headers=BEARER)
            view = await resp.json()
            assert [s["pack"] for s in view["sent"]] == ["retrospective", "mcp-qa"]

            resp = await client.delete("/api/sessions/root/packs?pack=mcp-qa", headers=BEARER)
            assert (await resp.json())["dropped"] is True
            assert session_packs.held("root") == {}

            for body in ({"pack": "nope-pack"}, {"pack": "../x"}, {}):
                resp = await client.post("/api/sessions/root/packs", headers=BEARER, json=body)
                assert resp.status == 400, body
            # An unknown session is refused the same way by every method.
            resp = await client.get("/api/sessions/ghost/packs", headers=BEARER)
            assert resp.status == 400
            resp = await client.post("/api/sessions/ghost/packs", headers=BEARER,
                                     json={"pack": "mcp-qa"})
            assert resp.status == 400
            resp = await client.delete("/api/sessions/ghost/packs?pack=mcp-qa", headers=BEARER)
            assert resp.status == 400
        finally:
            try:
                mgr.kill("root", force=True)
            except Exception:
                pass
            await client.close()

    asyncio.run(scenario())
