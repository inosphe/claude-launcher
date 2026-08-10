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
from claude_launcher.daemon import session as session_mod
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


async def _wait_drained(mesh, handle: str, timeout: float = 10.0) -> None:
    """Wait for ``handle``'s cursor to catch up (delivery ends a beat after
    the block hits the screen — the submitting Enter is a delayed write)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mesh.pending(handle) == []:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{handle!r} still has pending: {mesh.pending(handle)}")


# --------------------------------------------------------------------------- #
# paste encoding
# --------------------------------------------------------------------------- #
def test_encode_paste_newlines_become_cr():
    data = keys_mod.encode_paste("a\nb\r\nc", bracketed=False)
    assert data == b"a\rb\rc"


def test_encode_paste_bracketed_wrap():
    data = keys_mod.encode_paste("x\ny", bracketed=True)
    assert data == b"\x1b[200~x\ry\x1b[201~"


def test_paste_enter_is_a_separate_write(monkeypatch):
    """The submitting CR must land in its own PTY write — bundled into the
    paste chunk, a bracketed-paste TUI folds it into the pasted text and the
    block just sits in the composer."""
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    writes: list = []

    class FakeSession:
        exited = False
        sdef = SessionDef(name="s")
        screen = ScreenState(20, 5)
        paste = session_mod.Session.paste

        async def write_bytes(self, data: bytes) -> None:
            writes.append(data)

    s = FakeSession()
    s.screen.feed(b"\x1b[?2004h")  # program opted into bracketed paste
    data = asyncio.run(s.paste("x\ny", enter=True))
    assert writes == [b"\x1b[200~x\ry\x1b[201~", b"\r"]
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
    # Aliases, which the old six-entry table had none of: a fleet named with
    # interconnect's usual handles (coder1, coder2, ...) used to land every
    # one of them on the meaningless "member" and so fall out of every
    # role-targeted policy.
    assert infer_role("coder1") == "worker"
    assert infer_role("dev-2") == "worker"
    assert infer_role("qa") == "reviewer"
    assert infer_role("mod") == "leader"
    # An unlabelled member audits rather than rubber-stamps (interconnect's
    # rule); "member" — a role nothing acted on — is gone.
    assert infer_role("alice") == "reviewer"


def test_message_intents():
    assert mesh_mod.expects_reply(None) is True          # default 'say'
    assert mesh_mod.expects_reply("say") is True
    assert mesh_mod.expects_reply("ask") is True
    assert mesh_mod.expects_reply("worker") is True      # unknown -> reply-expected
    for t in ("fyi", "ack", "ping", "Ack", " FYI "):     # normalised on read
        assert mesh_mod.expects_reply(t) is False
    assert mesh_mod.type_notice("ask") is None
    assert mesh_mod.type_notice(None) is None
    # a role leaked into 'type' draws the reply-all advisory
    assert "not a known intent" in mesh_mod.type_notice("worker")


def test_format_delivery_no_reply_batch():
    fyi_only = [
        {"from": "policy", "to": "leader", "type": "fyi", "body": "stall: w1"},
        {"from": "w2", "to": "leader", "type": "ack", "body": "done"},
    ]
    block = format_delivery("m1", "leader", fyi_only)
    assert "needs_reply: false" in block
    assert "no reply expected" in block
    assert "type: fyi" in block and "type: ack" in block
    # one reply-expecting message flips the whole batch back
    mixed = fyi_only + [{"from": "w2", "to": "leader", "body": "question?"}]
    block = format_delivery("m1", "leader", mixed)
    assert "needs_reply: true" in block
    assert "reply with" in block


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
            await mm.join("dev", "nosuch")  # unknown session

        mgr.create(SessionDef(name="a1", harness="py", cwd=str(tmp_path)))
        mgr.create(SessionDef(name="b1", harness="py", cwd=str(tmp_path)))
        m = await mm.join("dev", "a1", handle="leader")
        assert m.role == "leader"
        await mm.join("dev", "b1")  # handle defaults to the session name
        assert "b1" in mesh.members

        with pytest.raises(MeshConflict):
            await mm.join("dev", "a1", handle="other")  # session already enrolled
        with pytest.raises(MeshConflict):
            mgr.create(SessionDef(name="c1", harness="py", cwd=str(tmp_path)))
            await mm.join("dev", "c1", handle="leader")  # handle taken

        result = await mm.send("dev", "a1", "*", "hello mesh")
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

        await mm.leave("dev", "b1")
        assert "b1" not in mesh.members
        with pytest.raises(MeshError):
            await mm.leave("dev", "b1")

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
        await mm.join("m", "s1", handle="alice")

        with pytest.raises(MeshError):
            await mm.send("m", "stranger", "alice", "hi")  # unknown non-external sender
        with pytest.raises(MeshError):
            await mm.send("m", "alice", "nosuch", "hi")  # unknown recipient
        with pytest.raises(MeshError):
            await mm.send("m", "alice", "*", "hi")  # nobody else to deliver to
        with pytest.raises(MeshError):
            await mm.send("m", "alice", "alice", "\x07\x08")  # empty after sanitizing

        # external (human) sender is allowed explicitly
        result = await mm.send("m", "operator", "*", "status?", external=True)
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
        await mm.join("m1", "alpha", handle="leader")
        await mm.join("m1", "beta", handle="worker_1")

        await mm.send("m1", "leader", "worker_1", "please build the thing")

        # the block is typed into beta's PTY; the echo child proves arrival
        await _wait_screen(b, "please build the thing")
        await _wait_screen(b, "mesh: m1")
        assert "please build the thing" not in _screen_text(a)  # not the sender

        # cursor advanced and persisted — the submitting Enter trails the
        # pasted block by PASTE_ENTER_DELAY, and the cursor moves after it
        mesh = mm.get("m1")
        await _wait_drained(mesh, "worker_1")
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
        await mm.join("m2", "src", handle="alice")
        await mm.join("m2", "dst", handle="bob")

        await b.send_keys(["quit", "Enter"])
        await b.wait_for("exited", timeout=10.0, threshold=0.5)

        await mm.send("m2", "alice", "bob", "are you there")
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
            # joining a remote address needs the relay too (no link verb any
            # more: membership is the only way in)
            resp = await client.post(
                "/api/mesh/elsewhere@pcX/members",
                json={"session": "w1", "handle": "w1"},
                headers=bearer,
            )
            assert resp.status == 400
            # the approval surface exists and is scoped to the owner
            resp = await client.get("/api/mesh/fed/invites", headers=bearer)
            assert resp.status == 200
            assert (await resp.json())["invites"] == []
            resp = await client.post(
                "/api/mesh/fed/requests/nope/approve", headers=bearer
            )
            assert resp.status == 400

            # peer endpoints sit outside /api: no daemon auth needed, the
            # per-link mesh token is the (only) gate — a bad one is a 400
            resp = await client.post(
                "/peer/mesh/sync",
                json={"mesh": "fed", "machine": "pcX", "token": "bad", "base": 0},
            )
            assert resp.status == 400
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# batch sections + reply_to
# --------------------------------------------------------------------------- #
def test_batch_sections_and_reply_to(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr)
        mm.create("b")
        for n, h in (("s1", "leader"), ("s2", "w1"), ("s3", "w2")):
            mgr.create(SessionDef(name=n, harness="py", cwd=str(tmp_path)))
            await mm.join("b", n, handle=h)

        sent = await mm.send(
            "b", "leader", ["w1", "w2"], "sprint goal: finish auth.",
            sections={
                "w1": "you take the login API.",
                "w2": {"text": "you take token refresh.", "type": "fyi"},
            },
        )
        assert sent["batched"] is True and sent["notice"] is None
        # ONE log entry, composite body, shared+sections stored
        msg = mm.get("b").messages[-1]
        assert msg["body"] == (
            "sprint goal: finish auth.\n\n@w1: you take the login API.\n\n"
            "@w2: you take token refresh."
        )
        assert msg["shared"] == "sprint goal: finish auth."

        # delivery slices per recipient: own section only, per-section intent
        block_w1 = format_delivery("b", "w1", [msg])
        assert "login API" in block_w1 and "token refresh" not in block_w1
        assert "needs_reply: true" in block_w1  # top-level 'say'
        assert msg["id"] in block_w1  # ids surface so reply_to is usable
        block_w2 = format_delivery("b", "w2", [msg])
        assert "token refresh" in block_w2 and "login API" not in block_w2
        assert "type: fyi" in block_w2
        assert "needs_reply: false" in block_w2  # w2's slice is fyi

        # reply_to threads and is shown in the delivery block
        reply = await mm.send("b", "w1", "leader", "on it", type="ack",
                        reply_to=msg["id"])
        assert reply["reply_to"] == msg["id"]
        assert f"reply_to: {msg['id']}" in format_delivery(
            "b", "leader", [mm.get("b").messages[-1]]
        )

        # validation: section for a non-recipient / the sender / empty slice
        with pytest.raises(MeshError):
            await mm.send("b", "leader", ["w1"], "x", sections={"w2": "not in to"})
        with pytest.raises(MeshError):
            await mm.send("b", "leader", "*", "x", sections={"leader": "self"})
        with pytest.raises(MeshError):
            await mm.send("b", "leader", ["w1", "w2"], "", sections={"w1": "only w1"})
        # sections-only send (empty shared body) is fine when all are covered
        ok = await mm.send("b", "leader", ["w1"], "", sections={"w1": "solo"})
        assert ok["batched"] is True

        # the separable advisory: @-addressing several recipients un-batched
        sep = await mm.send("b", "leader", ["w1", "w2"], "@w1 do X. @w2 do Y.")
        assert "BATCH" in (sep["notice"] or "")

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# federation v2 (primary/mirror) lives in tests/test_mesh_v2.py; only the
# relay-identity precondition stays here
# --------------------------------------------------------------------------- #
def test_invite_and_remote_join_require_relay_identity(home):
    mgr = _manager()
    mm = MeshManager(mgr)
    mm.create("m")
    with pytest.raises(MeshError):
        mm.invite("m")  # no relay identity

    async def run():
        # without a relay name this daemon has no address of its own, so a
        # remote join has nowhere to send the grant back to
        with pytest.raises(MeshError):
            await mm.join("other@pcX", "s1", handle="w1")

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# nudge policy (heartbeat / task-poll / stall warnings)
# --------------------------------------------------------------------------- #
def test_policy_merge_validation():
    from claude_launcher.daemon import mesh_policy

    base = mesh_policy.default_policy()
    for section in ("heartbeat", "task_poll", "stall_warn"):
        assert base[section]["enabled"] is False  # nudges cost turns: off by default

    merged = mesh_policy.merge_policy(
        base,
        {
            "heartbeat": {"enabled": True, "interval": 30},
            "task_poll": {"roles": ["Worker", "reviewer"]},
            "stall_warn": {"warn_secs": 0},  # 0 = disabled threshold, allowed
        },
    )
    assert merged["heartbeat"]["enabled"] is True
    assert merged["heartbeat"]["interval"] == 30.0
    assert merged["task_poll"]["roles"] == ["worker", "reviewer"]
    assert merged["heartbeat"]["body"] == base["heartbeat"]["body"]  # untouched
    assert base["heartbeat"]["enabled"] is False  # merge does not mutate base

    with pytest.raises(mesh_policy.PolicyError):
        mesh_policy.merge_policy(base, {"nosuch": {}})
    with pytest.raises(mesh_policy.PolicyError):
        mesh_policy.merge_policy(base, {"heartbeat": {"nosuch": 1}})
    with pytest.raises(mesh_policy.PolicyError):
        mesh_policy.merge_policy(base, {"heartbeat": {"interval": "soon"}})
    with pytest.raises(mesh_policy.PolicyError):
        mesh_policy.merge_policy(base, {"heartbeat": {"interval": 0}})  # < 1s
    with pytest.raises(mesh_policy.PolicyError):
        mesh_policy.merge_policy(base, {"task_poll": {"roles": "worker"}})

    # bad persisted policy degrades to defaults instead of failing the load
    assert mesh_policy.load_policy({"heartbeat": {"interval": -5}}) == (
        mesh_policy.default_policy()
    )


def test_policy_set_and_persist(home, tmp_path):
    async def run():
        mgr = _manager()
        root = tmp_path / "meshp"
        mm = MeshManager(mgr, root=root)
        mm.create("p1")
        policy = mm.set_policy("p1", {"heartbeat": {"enabled": True, "interval": 45}})
        assert policy["heartbeat"]["interval"] == 45.0
        with pytest.raises(MeshError):
            mm.set_policy("p1", {"heartbeat": {"interval": "NaNsense"}})

        mm2 = MeshManager(mgr, root=root)
        mm2.load_all()
        assert mm2.get("p1").policy["heartbeat"]["enabled"] is True
        assert mm2.get("p1").policy["heartbeat"]["interval"] == 45.0

    asyncio.run(run())


def test_policy_heartbeat_nudges_unanswered_member(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mm.create("hb")
        mgr.create(SessionDef(name="a1", harness="py", cwd=str(tmp_path)))
        mgr.create(SessionDef(name="b1", harness="py", cwd=str(tmp_path)))
        a = mgr.get("a1")
        b = mgr.get("b1")
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        await mm.join("hb", "a1", handle="leader")
        await mm.join("hb", "b1", handle="worker_b")
        mm.set_policy("hb", {"heartbeat": {"enabled": True, "interval": 1}})
        mm.start()

        # an fyi delivery does NOT arm the heartbeat: draining it leaves the
        # member owing nothing
        await mm.send("hb", "leader", "worker_b", "status update", type="fyi")
        await _wait_screen(b, "status update")
        await asyncio.sleep(3)
        assert "kind: heartbeat" not in _screen_text(b)

        await mm.send("hb", "leader", "worker_b", "please reply")
        await _wait_screen(b, "please reply")
        # worker_b never sends anything back -> the heartbeat block lands
        await _wait_screen(b, "kind: heartbeat")
        assert "kind: heartbeat" not in _screen_text(a)  # leader answered nothing,
        # but nothing was ever delivered to it either — no heartbeat for it

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_policy_task_poll_and_stall_warning(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mm.create("tp")
        mgr.create(SessionDef(name="a1", harness="py", cwd=str(tmp_path)))
        mgr.create(SessionDef(name="b1", harness="py", cwd=str(tmp_path)))
        a = mgr.get("a1")
        b = mgr.get("b1")
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        await mm.join("tp", "a1", handle="leader")
        await mm.join("tp", "b1", handle="worker_b")
        mm.set_policy(
            "tp",
            {
                "task_poll": {"enabled": True, "interval": 1},
                "stall_warn": {"enabled": True, "warn_secs": 1},
            },
        )
        mm.start()

        # worker_b is idle and caught up: it gets a task-poll; the leader is
        # not a polled role, so its screen stays clean of poll blocks
        await _wait_screen(b, "kind: task-poll")
        assert "kind: task-poll" not in _screen_text(a)

        # the stall warning about worker_b reaches the leader as a real mesh
        # message (from the external 'policy' sender)
        await _wait_screen(a, "stall: worker_b")
        assert any(
            m["from"] == "policy" and "stall: worker_b" in m["body"]
            and m.get("type") == "fyi"  # informs the leader, never asks
            for m in mm.get("tp").messages
        )

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# join briefing + MCP wrapper
# --------------------------------------------------------------------------- #
def test_join_briefing_lands_in_terminal(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mm.create("brief")
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        s = mgr.get("s1")
        await _wait_screen(s, "READY")
        await mm.join("brief", "s1", handle="worker_1")
        await _wait_screen(s, "join briefing", timeout=30.0)
        text = _screen_text(s)
        assert "you: worker_1 (role: worker)" in text
        assert "claunch mesh send brief" in text
        # the daemon prompts the agent to activate the member-protocol skill
        assert "/mesh brief" in text
        assert "'mesh' skill" in text
        await mgr.shutdown_all()

    asyncio.run(run())


def test_mesh_install_project(tmp_path):
    from claude_launcher import mesh_install

    done = mesh_install.install_into_project(tmp_path)
    assert len(done) == 2
    doc = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    server = doc["mcpServers"]["mesh"]
    assert "mesh" in server["args"] and "mcp" in server["args"]
    skill = (tmp_path / ".claude" / "skills" / "mesh" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert skill.startswith("---\nname: mesh\n")
    # the protocol essentials the briefing points at
    assert "CLAUNCH_SESSION" in skill          # self-identification
    assert "needs_reply" in skill              # reading delivery blocks
    assert "--section" in skill                # batch fan-out discipline
    assert "--reply-to" in skill               # threading
    assert "Recovery" in skill                 # compaction recovery
    # installing again is idempotent and keeps other servers
    doc["mcpServers"]["other"] = {"command": "x"}
    (tmp_path / ".mcp.json").write_text(json.dumps(doc), encoding="utf-8")
    mesh_install.install_into_project(tmp_path)
    doc2 = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert set(doc2["mcpServers"]) == {"mesh", "other"}


def test_mesh_mcp_tools(home, monkeypatch):
    from claude_launcher import mesh_mcp

    class FakeClient:
        def get(self, path, **kw):
            assert path == "/api/mesh/dev"
            return {
                "members": [{"handle": "w1"}],
                "peers": [],
                "relay": {"configured": False},
            }

        def post(self, path, body, **kw):
            assert path == "/api/mesh/dev/messages"
            assert body == {
                "from": "s0", "to": ["a", "b"], "body": "hi", "type": "fyi",
            }
            return {"id": "msg-1", "recipients": ["a", "b"], "relay": None}

    monkeypatch.setattr(mesh_mcp.daemon_client, "connect", lambda: FakeClient())

    init = mesh_mcp._handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert init["result"]["serverInfo"]["name"] == "claunch-mesh"
    tools = mesh_mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in tools["result"]["tools"]] == [
        "send", "members", "history",
    ]

    # send requires a session identity
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    resp = mesh_mcp._handle(
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "send",
                       "arguments": {"mesh": "dev", "to": "*", "body": "x"}},
        }
    )
    assert resp["result"]["isError"] is True
    assert "$CLAUNCH_SESSION" in resp["result"]["content"][0]["text"]

    monkeypatch.setenv("CLAUNCH_SESSION", "s0")
    resp = mesh_mcp._handle(
        {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "send",
                       "arguments": {"mesh": "dev", "to": "a, b", "body": "hi",
                                     "type": "fyi"}},
        }
    )
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["recipients"] == ["a", "b"]
    assert payload["relay"].startswith("relay: not configured")

    resp = mesh_mcp._handle(
        {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "members", "arguments": {"mesh": "dev"}},
        }
    )
    assert resp["result"]["isError"] is False
    assert json.loads(resp["result"]["content"][0]["text"])["members"] == [
        {"handle": "w1"}
    ]


def test_cursors_phase1_format_migrates(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        root = tmp_path / "meshold"
        mm = MeshManager(mgr, root=root)
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        mm.create("old")
        await mm.join("old", "s1", handle="w1")
        # rewrite cursors in the phase-1 flat format
        (root / "old" / "cursors.json").write_text(
            json.dumps({"w1": 3}), encoding="utf-8"
        )
        mm2 = MeshManager(mgr, root=root)
        mm2.load_all()
        loaded = mm2.get("old")
        assert loaded.cursors == {"w1": 3}
        assert loaded.link_cursors == {}
        await mgr.shutdown_all()

    asyncio.run(run())
