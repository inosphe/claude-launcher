"""A workspace's own prompt: its packs headed ``delivery: always``.

Every session created in the workspace gets them, on every creation path
(``POST /api/sessions`` -- the create form and ``claunch new-session`` --
and a spawn), whatever was ticked and whatever the role; a session in
another workspace does not. The record keeps ``always`` through a reminder's
refresh and a daemon restart, so the opening header keeps saying why the
pack is there.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import lineage, profile, prompt_packs, store, workspaces
from claude_launcher.daemon import daemon_prompt, harness as harness_mod, session_reminder
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
RULE = "WORKSPACE RULE: isolate the launcher home"
HEAD = "# claunch prompt pack: house-rules -- this workspace's own prompt"


def _pack(directory, pack_id, body, delivery=None):
    directory.mkdir(parents=True, exist_ok=True)
    head = f"title: {pack_id}\n" + (f"delivery: {delivery}\n" if delivery else "")
    (directory / f"{pack_id}.md").write_text(f"---\n{head}---\n{body}\n", encoding="utf-8")


@pytest.fixture
def env(home, tmp_path, monkeypatch):
    """Two registered workspaces; ``ws`` has an always pack and a plain one."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    monkeypatch.setattr(prompt_packs, "PACKAGE_DIR", shipped)
    ws = tmp_path / "ws"
    other = tmp_path / "other"
    for d in (ws, other):
        d.mkdir()
        workspaces.add(str(d), d.name)
    _pack(ws / prompt_packs.INSTALL_DIR, "house-rules", RULE, delivery="always")
    _pack(ws / prompt_packs.INSTALL_DIR, "plain", "PLAIN NOTES")
    return {"ws": ws, "other": other, "shipped": shipped}


# --------------------------------------------------------------------------- #
# module
# --------------------------------------------------------------------------- #

def test_the_header_marks_a_workspace_copy_always(env):
    assert [p.id for p in prompt_packs.always_packs(env["ws"])] == ["house-rules"]
    assert prompt_packs.always_packs(env["other"]) == []
    assert prompt_packs.always_packs(None) == []
    rows = {r["id"]: r for r in prompt_packs.workspace_rows(env["ws"])}
    assert rows["house-rules"]["delivery"] == prompt_packs.ALWAYS
    assert "delivery" not in rows["plain"]
    # An unknown value is an ordinary opening pack, as before the header.
    assert prompt_packs.parse("x", "---\ndelivery: sometimes\n---\nb").delivery == prompt_packs.OPENING


def test_claunchs_own_copy_never_makes_a_pack_always(env):
    # A shipped pack is always only where a workspace's copy says so.
    _pack(env["shipped"], "shipped-always", "SHIPPED", delivery="always")
    assert [p.id for p in prompt_packs.always_packs(env["other"])] == []
    # And a shipped on-demand id stays out of openings whatever a copy says.
    _pack(env["shipped"], "retro-like", "LATER", delivery="on-demand")
    _pack(env["ws"] / prompt_packs.INSTALL_DIR, "retro-like", "LATER", delivery="always")
    assert [p.id for p in prompt_packs.always_packs(env["ws"])] == ["house-rules"]


def test_choose_puts_it_first_and_once(env):
    _pack(env["shipped"], "role-x", "ROLE X")
    recs, required = prompt_packs.choose(
        env["ws"], ["plain", "house-rules"], ["x"], mapping={"x": ["role-x", "house-rules"]},
    )
    assert [r["id"] for r in recs] == ["house-rules", "plain", "role-x"]
    assert recs[0] == {"id": "house-rules", "text": RULE, "always": True}
    assert "always" not in recs[1]
    opening = prompt_packs.opening(recs)
    assert HEAD in opening
    assert "plain -- installed in this workspace, chosen at session creation" in opening
    # Nothing ticked, no role: it is still given.
    recs, _ = prompt_packs.choose(env["ws"], [], [])
    assert [r["id"] for r in recs] == ["house-rules"]


# --------------------------------------------------------------------------- #
# every creation path
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


def test_every_session_created_in_the_workspace_gets_it(env, tmp_path):
    _register_py_harness()
    # A spawned child joins its parent's mesh under a role, whose pack has to
    # exist; the stand-in package dir gets a stub of each.
    for ids in prompt_packs.DEFAULT_ROLE_PACKS.values():
        for pack_id in ids:
            _pack(env["shipped"], pack_id, f"STUB {pack_id}")
    ws, other = env["ws"], env["other"]
    # A worktree inside the workspace belongs to it (workspaces.owning).
    tree = ws / ".claude" / "worktrees" / "w"
    tree.mkdir(parents=True)
    daemon_prompt.write("DAEMON RULE", by="web")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        seen, restore = _spy_openings()

        async def create(name, cwd, **extra):
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": name, "profile": "py", "cwd": str(cwd),
                "task": "do the thing", "beads": False, **extra,
            })
            doc = await resp.json()
            assert resp.status == 201, doc
            return [r["id"] for r in mgr.get(name).sdef.prompt_packs]

        try:
            # Nothing ticked: after the daemon's prompt, ahead of the task.
            assert await create("a1", ws) == [daemon_prompt.PACK_ID, "house-rules"]
            assert seen["a1"].index("DAEMON RULE") < seen["a1"].index(RULE) \
                < seen["a1"].index("do the thing")
            assert HEAD in seen["a1"]
            # Ticked too, or the daemon prompt turned off: once, still there.
            assert await create("a2", ws, prompt_packs=["plain", "house-rules"],
                                daemon_prompt=False) == ["house-rules", "plain"]
            assert await create("a3", tree, daemon_prompt=False) == ["house-rules"]
            # Another workspace has its own (none).
            assert await create("b1", other, daemon_prompt=False) == []
            assert RULE not in seen["b1"]

            # A spawned child, with and without packs of its own.
            resp = await client.post("/api/sessions/a1/children", headers=BEARER,
                                     json={"name": "c1", "task": "help",
                                           "daemon_prompt": False})
            assert resp.status == 201, await resp.json()
            ids = [r["id"] for r in mgr.get("c1").sdef.prompt_packs]
            assert ids[0] == "house-rules" and ids.count("house-rules") == 1, ids
            assert RULE in seen["c1"]
            resp = await client.post("/api/sessions/a1/children", headers=BEARER,
                                     json={"name": "c2", "task": "help",
                                           "prompt_packs": ["plain"]})
            assert resp.status == 201, await resp.json()
            ids = [r["id"] for r in mgr.get("c2").sdef.prompt_packs]
            assert ids[:3] == [daemon_prompt.PACK_ID, "house-rules", "plain"], ids

            # The create form's offer marks it, so the chip is drawn locked.
            offer = await (await client.get(
                "/api/prompt-packs/offer", params={"cwd": str(ws)}, headers=BEARER,
            )).json()
            delivery = {p["id"]: p.get("delivery") for p in offer["packs"]}
            assert delivery == {"house-rules": prompt_packs.ALWAYS, "plain": None}
        finally:
            restore()
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# after creation
# --------------------------------------------------------------------------- #

def test_the_record_keeps_always_through_a_refresh_and_a_restart(env):
    recs, _ = prompt_packs.choose(env["ws"], [], [])
    sdef = SessionDef(name="w1", cwd=str(env["ws"]), prompt_packs=tuple(recs))

    back = SessionDef.from_dict(sdef.to_dict())
    assert back.prompt_packs == ({"id": "house-rules", "text": RULE, "always": True},)

    class _S:
        def __init__(self, sdef):
            self.sdef = sdef

    _pack(env["ws"] / prompt_packs.INSTALL_DIR, "house-rules", RULE + " (edited)",
          delivery="always")
    new, notices = session_reminder.refresh_packs(_S(sdef))
    assert new == ({"id": "house-rules", "text": RULE + " (edited)", "always": True},)
    assert len(notices) == 1
    assert HEAD in prompt_packs.opening(new)
