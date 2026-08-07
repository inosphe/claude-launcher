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
        assert cursors["members"]["worker_1"] == 1

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

            # invite needs a relay identity — none here → 400, not a crash
            resp = await client.post("/api/mesh", json={"name": "fed"}, headers=bearer)
            assert resp.status == 201
            resp = await client.post("/api/mesh/fed/invite", headers=bearer)
            assert resp.status == 400
            assert "relay" in (await resp.json())["error"]
            resp = await client.post("/api/mesh/link", json={}, headers=bearer)
            assert resp.status == 400

            # peer endpoints sit outside /api: no daemon auth needed, the
            # per-link mesh token is the (only) gate — a bad one is a 400
            resp = await client.post(
                "/peer/mesh/messages",
                json={"mesh": "fed", "machine": "pcX", "token": "bad", "messages": []},
            )
            assert resp.status == 400
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# federation: two mesh managers wired with an in-process peer transport
# --------------------------------------------------------------------------- #
def _dispatch_peer(mm: MeshManager, path: str, body: dict) -> dict:
    """What the /peer/* HTTP handlers do, minus the HTTP."""
    if path == "/peer/mesh/link":
        return mm.peer_link_accept(
            body["mesh"], body["machine"], body["token"],
            body["reply_token"], body.get("members") or [],
        )
    if path == "/peer/mesh/messages":
        return mm.peer_messages_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("messages") or [],
        )
    if path == "/peer/mesh/members":
        return mm.peer_members_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("members") or [],
        )
    raise AssertionError(f"unexpected peer path {path!r}")


def _federate(machines: dict) -> None:
    """Give each manager an identity and a direct transport to the others."""
    async def call(machine, path, body):
        return _dispatch_peer(machines[machine], path, body)

    for name, mm in machines.items():
        mm.machine = name
        mm.peer_transport = call
        mm.relay_connected = lambda: True


def test_invite_and_link_require_relay_identity(home):
    mgr = _manager()
    mm = MeshManager(mgr)
    mm.create("m")
    with pytest.raises(MeshError):
        mm.invite("m")  # no relay identity

    async def run():
        with pytest.raises(MeshError):
            await mm.link("whatever")

    asyncio.run(run())


def test_federation_link_and_remote_delivery(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a = MeshManager(mgr, root=tmp_path / "meshA")
        mm_b = MeshManager(mgr, root=tmp_path / "meshB")
        _federate({"pcA": mm_a, "pcB": mm_b})

        mgr.create(SessionDef(name="sa", harness="py", cwd=str(tmp_path)))
        mgr.create(SessionDef(name="sb", harness="py", cwd=str(tmp_path)))

        mm_a.create("m1")
        mm_a.join("m1", "sa", handle="alice")
        code = mm_a.invite("m1")["code"]

        result = await mm_b.link(code)
        assert result["peer"] == "pcA"
        mesh_a, mesh_b = mm_a.get("m1"), mm_b.get("m1")
        # tokens were exchanged, and each side adopted the other's members
        assert "pcB" in mesh_a.links and "pcA" in mesh_b.links
        assert mesh_a.links["pcB"]["token_in"] == mesh_b.links["pcA"]["token_out"]
        assert mesh_a.links["pcB"]["token_out"] == mesh_b.links["pcA"]["token_in"]
        assert mesh_b.members["alice"].machine == "pcA"
        with pytest.raises(MeshError):
            await mm_b.link(code)  # invites are single-use

        # B enrols its own session; the member push makes it visible on A
        mm_b.join("m1", "sb", handle="bob")
        await mm_b._push_members(mesh_b)
        assert mesh_a.members["bob"].machine == "pcB"
        assert mm_a.mesh_info(mesh_a)["peers"][0]["machine"] == "pcB"

        # a remote-addressed message crosses on flush…
        sent = mm_b.send("m1", "bob", "alice", "hello across")
        assert sent["remote"] == ["alice"]
        assert sent["queued_remote"] == []  # relay is up
        await mm_b._flush_peer(mesh_b, "pcA")
        assert [m["body"] for m in mesh_a.pending("alice")] == ["hello across"]
        assert mesh_b.peer_status["pcA"]["ok"] is True

        # …exactly once: the cursor advanced, and replays dedupe by id
        await mm_b._flush_peer(mesh_b, "pcA")
        assert len([m for m in mesh_a.messages if m["body"] == "hello across"]) == 1
        replay = mm_a.peer_messages_accept(
            "m1", "pcB", mesh_a.links["pcB"]["token_in"],
            [{k: sent[k] for k in ("id", "ts", "from", "to", "body")}],
        )
        assert replay["accepted"] == 0

        # the reverse direction: A's broadcast reaches bob, and A does NOT
        # echo B's own message back (origin gate)
        mm_a.send("m1", "alice", "*", "ack from A")
        assert [m["body"] for m in mm_a.pending_for_machine(mesh_a, "pcB")] == [
            "ack from A"  # only A's own message — never B's, per the origin gate
        ]
        await mm_a._flush_peer(mesh_a, "pcB")
        bodies_b = [m["body"] for m in mesh_b.messages]
        assert "ack from A" in bodies_b
        assert bodies_b.count("hello across") == 1  # no echo

        # a wrong peer token is rejected
        with pytest.raises(MeshError):
            mm_a.peer_messages_accept("m1", "pcB", "forged", [])

        # links, peer cursors and remote members survive a reload
        mm_b2 = MeshManager(mgr, root=tmp_path / "meshB")
        mm_b2.load_all()
        loaded = mm_b2.get("m1")
        assert "pcA" in loaded.links
        assert loaded.members["alice"].machine == "pcA"
        assert loaded.peer_cursors["pcA"] == mesh_b.peer_cursors["pcA"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_federation_queue_and_reconnect(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a = MeshManager(mgr, root=tmp_path / "meshA")
        mm_b = MeshManager(mgr, root=tmp_path / "meshB")
        _federate({"pcA": mm_a, "pcB": mm_b})

        mgr.create(SessionDef(name="sa", harness="py", cwd=str(tmp_path)))
        mgr.create(SessionDef(name="sb", harness="py", cwd=str(tmp_path)))
        mm_a.create("m1")
        mm_a.join("m1", "sa", handle="alice")
        await mm_b.link(mm_a.invite("m1")["code"])
        mm_b.join("m1", "sb", handle="bob")
        mesh_b = mm_b.get("m1")

        # relay drops: sends are accepted but queue for the peer
        calls = []

        async def broken(machine, path, body):
            calls.append(path)
            raise MeshError("relay down")

        mm_b.peer_transport = broken
        mm_b.relay_connected = lambda: False

        sent = mm_b.send("m1", "bob", "alice", "while offline")
        assert sent["queued_remote"] == ["alice"]
        await mm_b._flush_peer(mesh_b, "pcA")
        status = mesh_b.peer_status["pcA"]
        assert status["ok"] is False and status["backoff"] > 0
        assert mm_b.pending_for_machine(mesh_b, "pcA") != []
        # the flusher backs off — an immediate retry doesn't even dial
        n = len(calls)
        await mm_b._flush_peer(mesh_b, "pcA")
        assert len(calls) == n

        # reconnect: the queue drains and the peer marks healthy again
        _federate({"pcA": mm_a, "pcB": mm_b})
        mesh_b.peer_status["pcA"]["retry_at"] = 0.0
        await mm_b._flush_peer(mesh_b, "pcA")
        assert [m["body"] for m in mm_a.get("m1").pending("alice")] == [
            "while offline"
        ]
        assert mesh_b.peer_status["pcA"]["ok"] is True
        assert mm_b.pending_for_machine(mesh_b, "pcA") == []

        await mgr.shutdown_all()

    asyncio.run(run())


def test_cursors_phase1_format_migrates(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        root = tmp_path / "meshold"
        mm = MeshManager(mgr, root=root)
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        mm.create("old")
        mm.join("old", "s1", handle="w1")
        # rewrite cursors in the phase-1 flat format
        (root / "old" / "cursors.json").write_text(
            json.dumps({"w1": 3}), encoding="utf-8"
        )
        mm2 = MeshManager(mgr, root=root)
        mm2.load_all()
        loaded = mm2.get("old")
        assert loaded.cursors == {"w1": 3}
        assert loaded.peer_cursors == {}
        await mgr.shutdown_all()

    asyncio.run(run())
