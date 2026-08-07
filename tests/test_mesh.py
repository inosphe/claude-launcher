"""Mesh: paste encoding, registry persistence, delivery-by-injection, API.

Reuses the tiny Python echo child from the daemon e2e tests so delivery can be
observed on a real PTY screen: every CR in an injected block submits a line,
which the child echoes back — proof the message physically reached the
recipient's terminal.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon import keys as keys_mod
from claude_launcher.daemon import mesh as mesh_mod
from claude_launcher.daemon import paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import (
    MeshConflict,
    MeshError,
    MeshManager,
    format_delivery,
    infer_role,
)
from claude_launcher.daemon.screen import ScreenState

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if line == 'quit':\n"
    "        print('BYE')\n"
    "        break\n"
    "    print('echo:' + line)\n"
)


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _screen_text(session) -> str:
    return "\n".join(session.capture())


async def _wait_screen(session, needle: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _screen_text(session):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"{needle!r} never appeared on screen; got:\n{_screen_text(session)}"
    )


# --------------------------------------------------------------------------- #
# paste encoding
# --------------------------------------------------------------------------- #
def test_encode_paste_newlines_become_cr():
    data = keys_mod.encode_paste("a\nb\r\nc", bracketed=False)
    assert data == b"a\rb\rc"


def test_encode_paste_bracketed_wrap_and_enter():
    data = keys_mod.encode_paste("x\ny", bracketed=True, enter=True)
    assert data == b"\x1b[200~x\ry\x1b[201~\r"


def test_encode_paste_strips_controls_and_escape():
    # An embedded ESC (e.g. a smuggled paste-end marker) must not survive.
    data = keys_mod.encode_paste("a\x1b[201~b\x07c\td", bracketed=True)
    assert data == b"\x1b[200~a[201~bc\td\x1b[201~"


def test_screen_tracks_bracketed_paste_mode():
    s = ScreenState(20, 5)
    assert s.bracketed_paste is False
    s.feed(b"\x1b[?2004h")
    assert s.bracketed_paste is True
    s.feed(b"\x1b[?2004l")
    assert s.bracketed_paste is False


# --------------------------------------------------------------------------- #
# roles / formatting
# --------------------------------------------------------------------------- #
def test_infer_role():
    assert infer_role("worker_1") == "worker"
    assert infer_role("moderator") == "leader"
    assert infer_role("reviewer.claude") == "reviewer"
    assert infer_role("alice") == "member"


def test_format_delivery_block():
    msgs = [
        {"from": "leader", "to": "*", "body": "line one\nline two"},
        {"from": "worker_1", "to": "bob", "body": "x" * 3000},
    ]
    block = format_delivery("m1", "bob", msgs)
    assert block.startswith("---\n# claunch mesh: automated message delivery")
    assert block.endswith("...")
    assert "mesh: m1" in block
    assert "line one" in block and "line two" in block
    assert "…[clipped — see mesh history]" in block  # long body clipped
    # direct messages keep their 'to'; broadcasts drop it
    assert "to: bob" in block


# --------------------------------------------------------------------------- #
# registry + persistence
# --------------------------------------------------------------------------- #
def test_mesh_registry_and_persistence(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mesh = mm.create("dev")
        assert (paths.mesh_dir("dev") / "mesh.json").is_file()

        with pytest.raises(MeshConflict):
            mm.create("dev")
        with pytest.raises(MeshError):
            mm.create("bad name")
        with pytest.raises(MeshError):
            mm.join("dev", "nosuch")  # unknown session

        mgr.create(SessionDef(name="a1", harness="py", cwd=str(tmp_path)))
        mgr.create(SessionDef(name="b1", harness="py", cwd=str(tmp_path)))
        m = mm.join("dev", "a1", handle="leader")
        assert m.role == "leader"
        mm.join("dev", "b1")  # handle defaults to the session name
        assert "b1" in mesh.members

        with pytest.raises(MeshConflict):
            mm.join("dev", "a1", handle="other")  # session already enrolled
        with pytest.raises(MeshConflict):
            mgr.create(SessionDef(name="c1", harness="py", cwd=str(tmp_path)))
            mm.join("dev", "c1", handle="leader")  # handle taken

        result = mm.send("dev", "a1", "*", "hello mesh")
        assert result["from"] == "leader"  # session name resolved to handle
        assert result["recipients"] == ["b1"]

        # a fresh manager reloads members, log and cursors from disk
        mm2 = MeshManager(mgr)
        mm2.load_all()
        loaded = mm2.get("dev")
        assert set(loaded.members) == {"leader", "b1"}
        assert loaded.messages[-1]["body"] == "hello mesh"
        assert loaded.pending("b1")  # not delivered yet (no worker ran)
        assert not loaded.pending("leader")  # sender doesn't get its own send

        mm.leave("dev", "b1")
        assert "b1" not in mesh.members
        with pytest.raises(MeshError):
            mm.leave("dev", "b1")

        mm.delete("dev")
        with pytest.raises(MeshError):
            mm.get("dev")

        await mgr.shutdown_all()

    asyncio.run(run())


def test_send_validation(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr)
        mm.create("m")
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        mm.join("m", "s1", handle="alice")

        with pytest.raises(MeshError):
            mm.send("m", "stranger", "alice", "hi")  # unknown non-external sender
        with pytest.raises(MeshError):
            mm.send("m", "alice", "nosuch", "hi")  # unknown recipient
        with pytest.raises(MeshError):
            mm.send("m", "alice", "*", "hi")  # nobody else to deliver to
        with pytest.raises(MeshError):
            mm.send("m", "alice", "alice", "\x07\x08")  # empty after sanitizing

        # external (human) sender is allowed explicitly
        result = mm.send("m", "operator", "*", "status?", external=True)
        assert result["from"] == "operator"
        assert result["recipients"] == ["alice"]

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #
def test_delivery_injects_into_recipient_terminal(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.2, busy_hold=5.0)
        mm.start()
        mm.create("m1")
        a = mgr.create(SessionDef(name="alpha", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="beta", harness="py", cwd=str(tmp_path)))
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        mm.join("m1", "alpha", handle="leader")
        mm.join("m1", "beta", handle="worker_1")

        mm.send("m1", "leader", "worker_1", "please build the thing")

        # the block is typed into beta's PTY; the echo child proves arrival
        await _wait_screen(b, "please build the thing")
        await _wait_screen(b, "mesh: m1")
        assert "please build the thing" not in _screen_text(a)  # not the sender

        # cursor advanced and persisted
        mesh = mm.get("m1")
        assert mesh.pending("worker_1") == []
        cursors = json.loads(
            (paths.mesh_dir("m1") / "cursors.json").read_text(encoding="utf-8")
        )
        assert cursors["worker_1"] == 1

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_delivery_waits_for_respawn(home, tmp_path):
    """Messages to an exited member stay queued and land after respawn."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.1, busy_hold=5.0)
        mm.start()
        mm.create("m2")
        a = mgr.create(SessionDef(name="src", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="dst", harness="py", cwd=str(tmp_path)))
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        mm.join("m2", "src", handle="alice")
        mm.join("m2", "dst", handle="bob")

        await b.send_keys(["quit", "Enter"])
        await b.wait_for("exited", timeout=10.0, threshold=0.5)

        mm.send("m2", "alice", "bob", "are you there")
        await asyncio.sleep(1.5)  # worker runs but must hold the cursor
        assert mm.get("m2").pending("bob")

        revived = mgr.respawn("dst")
        await _wait_screen(revived, "READY")
        await _wait_screen(revived, "are you there")
        assert mm.get("m2").pending("bob") == []

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# CLI (daemon-free paths only; the rest is thin plumbing over the API)
# --------------------------------------------------------------------------- #
def test_cli_mesh_ls_without_daemon(home, capsys):
    from claude_launcher import cli

    assert cli.main(["mesh", "ls"]) == 0
    assert "daemon is not running" in capsys.readouterr().out


def test_cli_mesh_join_requires_session_identity(home, capsys, monkeypatch):
    from claude_launcher import cli

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert cli.main(["mesh", "join", "dev"]) == 1
    assert "$CLAUNCH_SESSION" in capsys.readouterr().err


def test_cli_mesh_send_requires_sender(home, capsys, monkeypatch):
    from claude_launcher import cli

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert cli.main(["mesh", "send", "dev", "*", "hello"]) == 1
    assert "no sender" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_mesh_api(home, tmp_path):
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.get("/api/mesh")
            assert resp.status == 401  # authed like everything else

            resp = await client.post("/api/mesh", json={"name": "web"}, headers=bearer)
            assert resp.status == 201
            resp = await client.post("/api/mesh", json={"name": "web"}, headers=bearer)
            assert resp.status == 409

            mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/mesh/web/members",
                json={"session": "w1", "handle": "worker_a"},
                headers=bearer,
            )
            assert resp.status == 201
            member = await resp.json()
            assert member["role"] == "worker"

            # sending: external human sender, broadcast
            resp = await client.post(
                "/api/mesh/web/messages",
                json={"from": "operator", "to": "*", "body": "hi", "external": True},
                headers=bearer,
            )
            assert resp.status == 200
            sent = await resp.json()
            assert sent["recipients"] == ["worker_a"]
            assert sent["relay"]["configured"] is False  # surfaced on every send

            resp = await client.post(
                "/api/mesh/web/messages",
                json={"from": "ghost", "to": "*", "body": "hi"},
                headers=bearer,
            )
            assert resp.status == 400  # unknown sender without external

            resp = await client.get("/api/mesh/web/messages?limit=10", headers=bearer)
            assert resp.status == 200
            history = await resp.json()
            assert [m["body"] for m in history["messages"]] == ["hi"]

            resp = await client.get("/api/mesh/web", headers=bearer)
            info = await resp.json()
            assert info["members"][0]["pending"] == 1  # no worker started here
            assert info["members"][0]["reachability"] in ("starting", "busy", "idle")
            assert info["relay"]["configured"] is False

            resp = await client.delete(
                "/api/mesh/web/members/worker_a", headers=bearer
            )
            assert resp.status == 200
            resp = await client.delete("/api/mesh/web", headers=bearer)
            assert resp.status == 200
            resp = await client.get("/api/mesh/web", headers=bearer)
            assert resp.status == 400  # gone

            # paste endpoint (the mesh delivery prerequisite)
            resp = await client.post(
                "/api/sessions/w1/keys",
                json={"paste": "multi\nline", "enter": True},
                headers=bearer,
            )
            assert resp.status == 200
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
