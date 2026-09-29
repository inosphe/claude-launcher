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
from pathlib import Path

import pytest
import yaml

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

from test_daemon_wedge import _stub_connect

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


async def _wait_exited(session, timeout: float = 20.0) -> None:
    """Wait for a killed session's reader to see EOF and mark the record."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.exited:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{session.name!r} never exited")


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
    # An unlabelled member falls to free-role, the packaged default — no
    # role's powers, but free to carry out whatever its creator gave it.
    assert infer_role("alice") == "free-role"


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
    # The ack duty rides the delivery itself, not just the skill: this is the
    # only surface a member sees at the moment it applies, whatever its role
    # and whether or not it ever activated /mesh.
    assert "--type ack" in block
    # ...and never on a batch that explicitly owes nothing.
    assert "--type ack" not in format_delivery("m1", "leader", fyi_only)


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
    assert "to: bob" in block  # the header names the terminal it was typed into


def test_delivery_entries_carry_no_recipient_list():
    """Who else received a message is the log's to keep, not the terminal's.

    An entry used to repeat the message's own ``to``: the reader's own handle
    again when the send was 1:1, and one line per co-recipient when it was a
    multi-send, in every one of those recipients' terminals. Parse the YAML
    instead of grepping it — ``"to: bob" in block`` also matches the block's
    top-level header, which is how the old assertion passed without ever
    reading an entry.
    """
    blocks = {}
    for name, to in (
        ("direct", "bob"),
        ("multi", ["bob", "cleo", "dara"]),
        ("broadcast", "*"),
    ):
        blocks[name] = format_delivery(
            "m1", "bob", [{"id": "m-1", "from": "leader", "to": to, "body": "hi"}]
        )
        doc = yaml.safe_load(blocks[name])
        assert doc["to"] == "bob"  # the header still names the reader
        assert "to" not in doc["batch"][0]
    # and a multi-send's co-recipients are nowhere in that reader's text either
    assert "cleo" not in blocks["multi"] and "dara" not in blocks["multi"]

    # Everything else an entry carries is unchanged. Pin the whole key set,
    # not just the absence: that is what catches both a re-added 'to' and a
    # field lost while removing it.
    plain = yaml.safe_load(blocks["direct"])["batch"][0]
    assert set(plain) == {"id", "from", "body"}
    decorated = yaml.safe_load(
        format_delivery(
            "m1",
            "bob",
            [{"id": "m-2", "from": "leader", "to": ["bob", "cleo"],
              "type": "fyi", "reply_to": "m-1", "body": "hi"}],
            origins={"leader": "otherbox"},
        )
    )["batch"][0]
    assert set(decorated) == {"id", "from", "machine", "type", "reply_to", "body"}
    assert decorated["machine"] == "otherbox (remote)"
    assert decorated["type"] == "fyi"
    assert decorated["reply_to"] == "m-1"


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


def test_delivery_holds_while_a_human_is_typing(home, tmp_path):
    """A recipient whose keyboard is live is mid-composition: the worker holds
    the cursor exactly as it does for a busy turn, and delivers once the
    keyboard has been quiet for the guard window."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.1, busy_hold=30.0)
        mm.start()
        mm.create("m4")
        a = mgr.create(SessionDef(name="talker", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="typist", harness="py", cwd=str(tmp_path)))
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        await mm.join("m4", "talker", handle="alice")
        await mm.join("m4", "typist", handle="bob")

        b.note_human_input()  # a human keystroke just landed in bob's pane
        await mm.send("m4", "alice", "bob", "mid-typing message")
        await asyncio.sleep(1.5)  # worker runs but must hold the cursor
        assert mm.get("m4").pending("bob")
        assert "mid-typing message" not in _screen_text(b)

        # the keyboard goes quiet: age the mark past the guard window
        b._last_human_input = time.monotonic() - session_mod.TYPING_GUARD
        await _wait_screen(b, "mid-typing message")
        await _wait_drained(mm.get("m4"), "bob")

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
        # Wait on the session's state, not on its screen: the queued delivery
        # lands the moment the child is up, and the block is long enough to
        # scroll the harness banner out of the viewport before a 0.1s poll
        # can see it. What matters is that the message arrives, which the
        # next two assertions establish.
        await revived.wait_for("idle", timeout=20.0, threshold=0.5)
        await _wait_screen(revived, "are you there")
        assert mm.get("m2").pending("bob") == []

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_send_to_an_exited_member_says_so_and_how_to_revive(home, tmp_path):
    """The sender is told at SEND time, with the command and the condition.

    Delivery still queues the message (that contract is
    :func:`test_delivery_waits_for_respawn`); what changes is that 'sent to
    bob' no longer reads like bob got it.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mm.create("m3")
        a = mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        await mm.join("m3", "lead", handle="leader")
        await mm.join("m3", "w1", handle="bob")

        alive = await mm.send("m3", "leader", "bob", "still here?")
        assert alive["undeliverable"] == []
        assert alive["notice"] is None

        await b.send_keys(["quit", "Enter"])
        await b.wait_for("exited", timeout=10.0, threshold=0.5)

        dead = await mm.send("m3", "leader", "bob", "and now?")
        assert dead["recipients"] == ["bob"]  # still accepted, still queued
        assert dead["undeliverable"] == [
            {"handle": "bob", "session": "w1", "state": "exited"}
        ]
        notice = dead["notice"]
        assert "has exited" in notice
        assert "claunch respawn w1" in notice      # how to revive it
        assert "ONLY if this message must actually land" in notice  # and when

        # A broadcast narrows to neighbours the same way and reports the same.
        cast = await mm.send("m3", "leader", "*", "everyone")
        assert [e["handle"] for e in cast["undeliverable"]] == ["bob"]

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_member_that_dies_holding_mail_is_reported_to_its_senders(home, tmp_path):
    """The other order of events: accepted into a live terminal, then it died.

    Nobody is holding a send result to read in that case, so the daemon goes
    and tells the senders — once per death, from ``policy``, as ``fyi``.

    The latch is per sender, not per death: a latch on the dead member
    alone told whoever happened to have mail waiting at the moment of the
    first delivery attempt and left everyone after that in silence. So a
    sender who arrives after the first report is told too, about HER mail,
    and nobody is told twice.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, busy_hold=5.0)
        mm.create("m4")
        a = mgr.create(SessionDef(name="lead2", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="w2", harness="py", cwd=str(tmp_path)))
        c = mgr.create(SessionDef(name="w3", harness="py", cwd=str(tmp_path)))
        d = mgr.create(SessionDef(name="w4", harness="py", cwd=str(tmp_path)))
        for s in (a, b, c, d):
            await _wait_screen(s, "READY")
        await mm.join("m4", "lead2", handle="leader")
        await mm.join("m4", "w2", handle="bob")
        await mm.join("m4", "w3", handle="carol")
        await mm.join("m4", "w4", handle="dave")

        mesh = mm.get("m4")
        # Two senders, so the report has to find both and neither twice.
        await mm.send("m4", "leader", "bob", "one")
        await mm.send("m4", "carol", "bob", "two")
        assert len(mesh.pending("bob")) == 2

        await b.send_keys(["quit", "Enter"])
        await b.wait_for("exited", timeout=10.0, threshold=0.5)

        member = mesh.members["bob"]
        await mm._deliver_to(mesh, member)
        reports = [m for m in mesh.messages if m["from"] == "policy"]
        assert len(reports) == 2
        report = reports[0]
        assert [r["to"] for r in reports] == [["carol"], ["leader"]]
        assert report["type"] == "fyi"          # informs, never asks
        assert "claunch respawn w2" in report["body"]
        assert all("1 message(s) of yours are waiting" in r["body"] for r in reports)
        assert "bob" not in report["to"]        # bob is not told about bob
        # Reporting changes nothing about the mail: it is still held for the
        # respawn, which is the contract test_delivery_waits_for_respawn pins.
        assert len(mesh.pending("bob")) == 2

        # The delivery worker runs every few seconds and the backlog never
        # drains on its own: without a latch this is a message per tick.
        for _ in range(3):
            await mm._deliver_to(mesh, member)
        assert len([m for m in mesh.messages if m["from"] == "policy"]) == 2
        # dave now sends into the same closed terminal. His send result says
        # so (the other half of this contract), and the mail queues -- and
        # he is told, about his mail, while the two already told are not.
        await mm.send("m4", "dave", "bob", "late")
        await mm._deliver_to(mesh, member)
        reports = [m for m in mesh.messages if m["from"] == "policy"]
        assert len(reports) == 3, "the late sender heard nothing"
        assert reports[2]["to"] == ["dave"]
        assert "1 message(s) of yours are waiting" in reports[2]["body"]
        for _ in range(3):
            await mm._deliver_to(mesh, member)
        assert len([m for m in mesh.messages if m["from"] == "policy"]) == 3

        # Respawn re-arms it: a second death is news again.
        revived = mgr.respawn("w2")
        await revived.wait_for("idle", timeout=20.0, threshold=0.5)
        await mm._deliver_to(mesh, mesh.members["bob"], force=True)
        await _wait_drained(mesh, "bob", timeout=20.0)
        await mm.send("m4", "leader", "bob", "three")
        await revived.send_keys(["quit", "Enter"])
        await revived.wait_for("exited", timeout=10.0, threshold=0.5)
        await mm._deliver_to(mesh, mesh.members["bob"])
        assert len([m for m in mesh.messages if m["from"] == "policy"]) == 4

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_stranded_reports_survive_restart_and_ignore_broadcasts(home, tmp_path):
    """Old general announcements must not wake their author after a restart."""
    from types import SimpleNamespace

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        mesh = mm.create("stranded")
        mesh.policy = {"backpressure": {"inbox_max": 100}}
        for handle in ("alice", "bob", "carol", "dave"):
            mesh.members[handle] = mesh_mod.Member(handle, handle)
        mm._persist_def(mesh)
        for intent in ("fyi", "say", "ask"):
            mm._send_core(mesh, "alice", "*", "announcement", type=intent)
        mm._send_core(mesh, "carol", "bob", "one", type="fyi")
        mm._send_core(mesh, "carol", ["bob"], "two")
        mm._send_core(mesh, "dave", "bob", "three")
        await mm._deliver_to(mesh, mesh.members["bob"])
        reports = [m for m in mesh.messages if m["from"] == "policy"]
        assert [m["to"] for m in reports] == [["carol"], ["dave"]]
        assert "2 message(s)" in reports[0]["body"]
        assert "1 message(s)" in reports[1]["body"]
        pending_ids = [m["id"] for m in mesh.pending("bob")]
        assert len(pending_ids) == 6  # broadcasts stay queued

        restored = MeshManager(mgr, root=tmp_path / "meshes")
        restored.load_all()
        mesh = restored.get("stranded")
        await restored._deliver_to(mesh, mesh.members["bob"])
        assert len([m for m in mesh.messages if m["from"] == "policy"]) == 2
        assert [m["id"] for m in mesh.pending("bob")] == pending_ids

        # A broadcast-only sender can later receive a directed warning.
        restored._send_core(mesh, "alice", "bob", "direct question", type="ask")
        await restored._deliver_to(mesh, mesh.members["bob"])
        reports = [m for m in mesh.messages if m["from"] == "policy"]
        assert len(reports) == 3
        assert reports[-1]["to"] == ["alice"]
        assert "1 message(s)" in reports[-1]["body"]

        # A live observation with an empty queue must durably re-arm notices.
        mesh.cursors["bob"] = len(mesh.messages)
        original_get = mgr.get
        mgr.get = lambda name: SimpleNamespace(exited=False)
        await restored._deliver_to(mesh, mesh.members["bob"])
        mgr.get = original_get
        again = MeshManager(mgr, root=tmp_path / "meshes")
        again.load_all()
        mesh = again.get("stranded")
        again._send_core(mesh, "carol", "bob", "after second exit")
        await again._deliver_to(mesh, mesh.members["bob"])
        reports = [m for m in mesh.messages if m["from"] == "policy"]
        assert len(reports) == 4
        assert reports[-1]["to"] == ["carol"]
        assert "1 message(s)" in reports[-1]["body"]

    asyncio.run(run())


def test_stranded_notice_separates_revivable_from_gone():
    """``exited`` and ``missing`` need opposite advice, so they read apart."""
    exited = mesh_mod.stranded_notice(
        [{"handle": "bob", "session": "w1", "state": "exited"}]
    )
    assert "claunch respawn w1" in exited
    missing = mesh_mod.stranded_notice(
        [{"handle": "bob", "session": "w1", "state": "missing"}]
    )
    assert "gone from the registry" in missing
    assert "respawn" not in missing  # nothing to respawn; do not offer it
    assert mesh_mod.stranded_notice([]) is None


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

            mgr.create(SessionDef(
                name="w1", harness="py", cwd=str(tmp_path),
                task="a task only the detail panel needs", env={"EXAMPLE": "value"},
            ))
            resp = await client.post(
                "/api/mesh/web/members",
                json={"session": "w1", "handle": "worker_a"},
                headers=bearer,
            )
            assert resp.status == 201
            member = await resp.json()
            assert member["role"] == "worker"

            resp = await client.get("/api/mesh?view=rail", headers=bearer)
            rail_mesh = (await resp.json())["meshes"]
            assert rail_mesh == [{
                "name": "web", "project": "default", "primary": None,
                "members": [{
                    "handle": "worker_a", "session": "w1", "role": "worker",
                    "subroles": [], "roles": ["worker"],
                    "local": True,
                }],
                "member_count": 1, "messages": 0, "requests": 0,
                "visibility": "private",
            }]

            resp = await client.get(
                "/api/sessions?view=rail&state=active", headers=bearer
            )
            rail_session = (await resp.json())["sessions"][0]
            assert rail_session["name"] == "w1"
            assert "task" not in rail_session
            assert "env" not in rail_session

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
# a batch's non-sectioned recipients get the shared preamble and nothing else
# --------------------------------------------------------------------------- #
def test_non_sectioned_recipients_get_the_preamble_and_are_flagged(home, tmp_path):
    """The delivery rule and the advisory that guards it.

    A section-bearing send still reaches every recipient in ``to``; one with
    no section reads the shared preamble alone. That is deliberate — a
    sprint goal announced to all with assignments for two — so the send is
    NOT narrowed to the sectioned members. What is new is the warning when
    the preamble is too thin to be a message on its own, which is the case
    that woke nine terminals with a four-word heading (claunch-u8ko).
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr)
        mm.create("b")
        for n, h in (("s1", "leader"), ("s2", "w1"), ("s3", "w2"), ("s4", "w3")):
            mgr.create(SessionDef(name=n, harness="py", cwd=str(tmp_path)))
            await mm.join("b", n, handle=h)

        # ---- the rule: no section means the preamble, delivered ---------- #
        goal = (
            "sprint goal: finish auth before Friday. Everyone rebases onto "
            "master first; the two names below also have a slice of their own."
        )
        sent = await mm.send(
            "b", "leader", "*", goal,
            sections={"w1": "you take the login API.",
                      "w2": "you take token refresh."},
        )
        assert sorted(sent["recipients"]) == ["w1", "w2", "w3"]
        msg = mm.get("b").messages[-1]
        block_w3 = format_delivery("b", "w3", [msg])
        assert "sprint goal" in block_w3
        assert "login API" not in block_w3 and "token refresh" not in block_w3
        # a substantive preamble is the intended use: no preamble advisory
        # (the '*' itself still draws the BROADCAST one)
        assert sent["notice"].startswith("BROADCAST")
        assert "ONLY" not in sent["notice"]

        # ---- the guard: a heading, not a message ------------------------ #
        thin = await mm.send(
            "b", "leader", "*", "s127(leader): landing notice.",
            sections={"w1": "your branch 92a7106 is in.",
                      "w2": "yours is next in the queue."},
        )
        note = thin["notice"] or ""
        assert "w3" in note and "ONLY" in note
        assert "1 recipient(s) with no section" in note
        # the delivery itself is unchanged — the advisory does not narrow it
        assert sorted(thin["recipients"]) == ["w1", "w2", "w3"]

        # ---- no uncovered recipient: nothing to warn about -------------- #
        covered = await mm.send(
            "b", "leader", ["w1", "w2"], "short heading.",
            sections={"w1": "a", "w2": "b"},
        )
        assert covered["notice"] is None

        # ---- a long one-line preamble still stands on its own ----------- #
        long_line = "landing notice: " + "x" * 200
        ok = await mm.send(
            "b", "leader", "*", long_line, sections={"w1": "yours is in."},
        )
        assert ok["notice"].startswith("BROADCAST")
        assert "ONLY" not in ok["notice"]

        # ---- and the pre-existing floor still holds: no body, no section - #
        with pytest.raises(MeshError):
            await mm.send("b", "leader", "*", "", sections={"w1": "solo"})

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
# unanswered mail (the 'owed' ledger behind `mesh owed` and the web dashboard)
# --------------------------------------------------------------------------- #
def _mark_delivered(mesh) -> None:
    """What the delivery worker does to the cursor, without a live PTY."""
    for handle in mesh.members:
        mesh.cursors[handle] = len(mesh.messages)


def test_owed_is_delivered_mail_nobody_answered(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mesh = mm.create("owe")
        for name in ("a1", "b1"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("owe", "a1", handle="leader")
        await mm.join("owe", "b1", handle="worker_1")

        await mm.send("owe", "a1", "worker_1", "do the thing")
        # Undelivered mail is the DAEMON's debt, not the member's — the two
        # states are diagnosed differently and must not be conflated.
        assert mesh.pending("worker_1")
        assert mesh.owed("worker_1") == []

        _mark_delivered(mesh)
        assert [m["body"] for m in mesh.owed("worker_1")] == ["do the thing"]
        assert mesh.owed("leader") == []      # the sender owes nothing

        # fyi/ack were sent precisely to say nothing is owed
        await mm.send("owe", "a1", "worker_1", "for info", type="fyi")
        await mm.send("owe", "a1", "worker_1", "noted", type="ack")
        _mark_delivered(mesh)
        assert len(mesh.owed("worker_1")) == 1

        # ...an unknown type is reply-expected, so it DOES count (type_notice)
        await mm.send("owe", "a1", "worker_1", "labelled", type="worker")
        _mark_delivered(mesh)
        assert len(mesh.owed("worker_1")) == 2

        # A reply of ANY kind clears the ledger — the same forgiving rule the
        # heartbeat uses, so the dashboard can never disagree with the nudger.
        await mm.send("owe", "b1", "leader", "on it", type="ack")
        assert mesh.owed("worker_1") == []

        # ...and a fresh question re-arms it
        await mm.send("owe", "a1", "worker_1", "and this?", type="ask")
        _mark_delivered(mesh)
        assert [m["body"] for m in mesh.owed("worker_1")] == ["and this?"]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_owed_counts_a_batch_slice_per_recipient(home, tmp_path):
    """A batch message owes only the recipients whose OWN slice expects one."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mesh = mm.create("batch")
        for name in ("a1", "b1", "c1"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("batch", "a1", handle="leader")
        await mm.join("batch", "b1", handle="w1")
        await mm.join("batch", "c1", handle="w2")

        await mm.send(
            "batch", "a1", "*", "shared preamble",
            sections={"w1": {"text": "you build it", "type": "ask"},
                      "w2": {"text": "you just need to know", "type": "fyi"}},
        )
        _mark_delivered(mesh)
        owed = mesh.owed("w1")
        assert len(owed) == 1
        assert mesh.owed("w2") == []  # its slice was fyi

        # the report shows w1 ITS slice, never w2's instructions
        report = mm.owed_report(mesh)
        row = next(r for r in report["members"] if r["handle"] == "w1")
        assert row["messages"][0]["batch"] is True
        assert "you build it" in row["messages"][0]["body"]
        assert "you just need to know" not in row["messages"][0]["body"]
        assert report["owed"] == 1 and report["owing"] == 1
        await mgr.shutdown_all()

    asyncio.run(run())


def test_owed_report_and_route(home, tmp_path):
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
            mesh = mm.create("dash")
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
            await mm.join("dash", "w1", handle="worker_1")
            await mm.send("dash", "operator", "worker_1", "answer me",
                          external=True, type="ask")

            resp = await client.get("/api/mesh/dash/owed", headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            # still undelivered: owed 0, pending 1 — the debt is the daemon's
            row = doc["members"][0]
            assert doc["owed"] == 0 and row["pending"] == 1

            _mark_delivered(mesh)
            doc = await (await client.get("/api/mesh/dash/owed", headers=bearer)).json()
            row = doc["members"][0]
            assert doc["owed"] == 1 and doc["owing"] == 1
            assert row["source"] == "log" and row["local"] is True
            assert row["messages"][0]["from"] == "operator"
            assert row["messages"][0]["type"] == "ask"
            assert row["messages"][0]["age"] is not None
            assert row["oldest_age"] is not None
            # the dashboard must say when nothing is chasing the debt
            assert doc["heartbeat"]["enabled"] is False

            # the members view carries the same count, next to 'pending'
            info = await (await client.get("/api/mesh/dash", headers=bearer)).json()
            assert info["members"][0]["owed"] == 1

            resp = await client.get("/api/mesh/nosuch/owed", headers=bearer)
            assert resp.status == 400
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_history_says_where_each_message_got_to(home, tmp_path):
    """The log stores the address; the sequence view needs the arrival.

    Three facts the raw log cannot give a reader, all re-derived the way
    delivery itself derives them: who a ``"*"`` actually reached, that a cut
    edge takes someone off a message already accepted, and which recipients
    have had it typed in rather than merely queued for them.
    """
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
            mesh = mm.create("trace")
            for name in ("a1", "b1", "c1"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("trace", "a1", handle="leader")
            await mm.join("trace", "b1", handle="w1")
            await mm.join("trace", "c1", handle="w2")
            # A member on another daemon: its cursor lives over there, so this
            # daemon can name it a recipient but must not claim it was reached.
            mesh.members["far"] = mesh_mod.Member(
                "far", "s9", machine="pcB", role="worker"
            )
            # wired, as a real join would leave it: the leader was wired on
            # arrival, and for a wired member an unrecorded pair is closed
            mesh.member_edges[mesh.member_key("leader", "far")] = True

            await mm.send("trace", "a1", "*", "all hands")
            await mm.send("trace", "a1", "w1", "just you", type="ask")

            async def history():
                resp = await client.get("/api/mesh/trace/messages", headers=bearer)
                assert resp.status == 200
                return (await resp.json())["messages"]

            bcast, direct = await history()
            # '*' resolved: everyone but the sender, the remote member included
            assert set(bcast["recipients"]) == {"w1", "w2", "far"}
            assert bcast["delivered"] == []          # queued, not yet injected
            assert bcast["remote"] == ["far"]        # unknowable here, said so
            assert direct["recipients"] == ["w1"]

            mesh.cursors["w1"] = len(mesh.messages)  # what the worker does
            bcast, direct = await history()
            assert bcast["delivered"] == ["w1"] and direct["delivered"] == ["w1"]
            assert "w2" not in bcast["delivered"]
            # the remote one is never delivered from here, cursor or not
            assert bcast["remote"] == ["far"]

            # Cutting an edge takes w2 off a broadcast already in the log —
            # the same direction delivery re-resolves in, so the picture and
            # the daemon cannot disagree about who is being spoken to.
            await mm.set_member_link("trace", "leader", "w2", enabled=False)
            bcast, _ = await history()
            assert "w2" not in bcast["recipients"]

            # the fields are additive: what the log was written with survives
            assert bcast["from"] == "leader" and bcast["to"] == "*"
            assert bcast["seq"] == 0 and bcast["body"] == "all hands"
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_history_page_filters_archived_members(home, tmp_path):
    """The dashboard can keep inactive mesh conversations off its first page."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr)
        try:
            mm.create("paged")
            for name, handle in (("old", "old_handle"), ("live", "live_handle")):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
                await mm.join("paged", name, handle=handle)

            await mm.send("paged", "operator", "old_handle", "archived", external=True)
            await mm.send("paged", "operator", "live_handle", "current-1", external=True)
            await mm.send("paged", "operator", "live_handle", "current-2", external=True)
            await mgr.get("old").shutdown()
            mgr.archive("old")

            archived = mm.history_annotated_page(
                "paged", limit=25, message_filter="archived"
            )
            assert [m["body"] for m in archived["messages"]] == ["archived"]
            assert archived["page"] == {
                "filter": "archived", "limit": 25, "offset": 0,
                "total": 1, "counts": {"all": 3, "current": 2, "archived": 1},
                "has_newer": False, "has_older": False,
            }

            current = mm.history_annotated_page(
                "paged", limit=1, message_filter="current"
            )
            assert [m["body"] for m in current["messages"]] == ["current-2"]
            assert current["page"]["total"] == 2
            assert current["page"]["has_older"] is True
            older = mm.history_annotated_page(
                "paged", limit=1, offset=1, message_filter="current"
            )
            assert [m["body"] for m in older["messages"]] == ["current-1"]
            assert older["page"]["has_newer"] is True
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_dismiss_writes_off_unanswered_mail(home, tmp_path):
    """The operator's closure: the one that is not a reply.

    Dismissing has to leave the ledger and the nudger agreeing — that is the
    whole rule the Unanswered box is built on — so it also settles the
    heartbeat's ``last_asked`` when nothing is left owed.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        mesh = mm.create("drop")
        for name in ("a1", "b1"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("drop", "a1", handle="leader")
        await mm.join("drop", "b1", handle="worker_1")

        await mm.send("drop", "a1", "worker_1", "first", type="ask")
        await mm.send("drop", "a1", "worker_1", "second", type="ask")
        _mark_delivered(mesh)
        # what the heartbeat would be looking at, had a real delivery stamped it
        mesh.activity["worker_1"] = {"anchor": 0.0, "last_asked": time.monotonic()}
        first, second = (m["id"] for m in mesh.owed("worker_1"))

        result = mm.dismiss_owed("drop", "worker_1", [first])
        assert result["dismissed"] == [first] and result["owed"] == 1
        assert [m["body"] for m in mesh.owed("worker_1")] == ["second"]
        # one still stands, so the member is still legitimately being chased
        assert mesh.activity["worker_1"]["last_asked"] > 0

        row = next(
            r for r in mm.owed_report(mesh)["members"] if r["handle"] == "worker_1"
        )
        assert row["owed"] == 1 and row["can_dismiss"] is True
        assert row["can_nudge"] is True

        # ...and now the rest, which settles the heartbeat with it
        assert mm.dismiss_owed("drop", "worker_1")["dismissed"] == [second]
        assert mesh.owed("worker_1") == []
        assert mesh.activity["worker_1"]["last_asked"] == 0.0

        # pressing × twice on a row the poll has not redrawn yet is a no-op,
        # not an error — the message is already written off
        assert mm.dismiss_owed("drop", "worker_1", [first])["dismissed"] == []
        # ...but a debt that was never owed is refused: a suppression nothing
        # could ever clear is worse than a failed button
        with pytest.raises(MeshError, match="does not owe"):
            mm.dismiss_owed("drop", "worker_1", ["msg-nosuch"])
        with pytest.raises(MeshError, match="no member"):
            mm.dismiss_owed("drop", "nobody", None)

        # survives a restart — the write-off is delivery state, and lives
        # with the cursors
        mm2 = MeshManager(mgr)
        mm2.load_all()
        mesh2 = mm2.get("drop")
        assert mesh2.dismissed["worker_1"] == {first, second}
        assert mesh2.owed("worker_1") == []

        # A reply closes everything before it anyway, so the ids stop
        # suppressing anything: the next dismissal prunes them away rather
        # than carrying them for the life of the log.
        await mm.send("drop", "b1", "leader", "on it", type="ack")
        await mm.send("drop", "a1", "worker_1", "and this?", type="ask")
        _mark_delivered(mesh)
        third = mesh.owed("worker_1")[0]["id"]
        mm.dismiss_owed("drop", "worker_1", [third])
        assert mesh.dismissed["worker_1"] == {third}
        await mgr.shutdown_all()

    asyncio.run(run())


def test_nudge_and_dismiss_routes(home, tmp_path):
    """The two buttons on the Unanswered box, over HTTP.

    The nudge is asserted on the recipient's actual screen: it is the same
    injected block the heartbeat sends, so 'the operator nudged' and 'the
    engine nudged' cannot drift into two delivery paths.
    """
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            mesh = mm.create("act")
            session = mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")
            await mm.join("act", "w1", handle="worker_1")
            await mm.send("act", "operator", "worker_1", "answer me",
                          external=True, type="ask")
            mm.start()
            await _wait_drained(mesh, "worker_1")
            assert len(mesh.owed("worker_1")) == 1

            resp = await client.post(
                "/api/mesh/act/members/worker_1/nudge", headers=bearer, json={}
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["queued"] is False and doc["owed"] == 1
            assert doc["body"] == mesh.policy["heartbeat"]["body"]
            await _wait_screen(session, "kind: nudge")
            # the engine must not pile a second one on top of a fresh nudge
            assert mesh.activity["worker_1"]["hb_next"] > time.monotonic()

            # a note of the operator's own, when the stock reminder will not do
            resp = await client.post(
                "/api/mesh/act/members/worker_1/nudge", headers=bearer,
                json={"body": "the schema review is blocked on you"},
            )
            assert (await resp.json())["body"] == "the schema review is blocked on you"
            await _wait_screen(session, "the schema review is blocked on you")

            mid = mesh.owed("worker_1")[0]["id"]
            resp = await client.delete(
                f"/api/mesh/act/members/worker_1/owed/{mid}", headers=bearer
            )
            assert resp.status == 200
            assert (await resp.json())["owed"] == 0
            assert mesh.owed("worker_1") == []

            doc = await (await client.get("/api/mesh/act/owed", headers=bearer)).json()
            assert doc["owed"] == 0 and doc["owing"] == 0

            # nudging is not conditional on a debt (an operator looking at the
            # row has already made that call), but the member must exist
            for path in ("/api/mesh/act/members/nobody/nudge",):
                assert (await client.post(path, headers=bearer, json={})).status == 400
            resp = await client.delete(
                "/api/mesh/act/members/worker_1/owed/msg-nosuch", headers=bearer
            )
            assert resp.status == 400
        finally:
            await client.close()
            await mgr.shutdown_all()

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


def test_policy_engine_polls_warns_and_chases(home, tmp_path):
    """The three policies on one pair of sessions: a task-poll to the idle
    worker, a stall warning to the leader, then the heartbeat -- which an fyi
    never arms and an unanswered ask does."""
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

        # heartbeat, on the same pair: the poll and the warning are switched
        # off so the only block that can land from here is the chase
        mm.set_policy(
            "tp",
            {
                "task_poll": {"enabled": False},
                "stall_warn": {"enabled": False},
                "heartbeat": {"enabled": True, "interval": 1},
            },
        )
        # an fyi delivery does NOT arm the heartbeat: draining it leaves the
        # member owing nothing
        await mm.send("tp", "leader", "worker_b", "status update", type="fyi")
        await _wait_screen(b, "status update")
        await asyncio.sleep(3)
        assert "kind: heartbeat" not in _screen_text(b)

        await mm.send("tp", "leader", "worker_b", "please reply")
        await _wait_screen(b, "please reply")
        # worker_b never sends anything back -> the heartbeat block lands
        await _wait_screen(b, "kind: heartbeat")
        assert "kind: heartbeat" not in _screen_text(a)  # leader answered nothing,
        # but nothing was ever delivered to it either — no heartbeat for it

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
        # Tall enough to hold the whole block: this session has no role (the
        # 'py' harness takes no system prompt), so the briefing pastes the
        # stance rather than pointing at it, and a 30-row screen would scroll
        # the header off before the assertions read it.
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path), rows=80))
        s = mgr.get("s1")
        await _wait_screen(s, "READY")
        await mm.join("brief", "s1", handle="worker_1")
        # Wait for the block's LAST line, not its first: the child echoes the
        # paste line by line, so waiting on the header and reading the screen
        # races the rest of the block onto it.
        await _wait_screen(s, "typed into this terminal", timeout=30.0)
        text = _screen_text(s)
        assert "you: worker_1 (role: worker)" in text
        assert "claunch mesh send brief" in text
        # the daemon prompts the agent to activate the member-protocol skill
        assert "/mesh brief" in text
        assert "'mesh' skill" in text
        # nothing carries this session's stance but the briefing, so it is in
        # the briefing — pointer AND prose
        assert "claunch mesh stance brief" in text
        # ...and the prose arrives NAMED: an id printed beside it is what
        # lets a later reminder ask "is this still in your context?" without
        # pasting the stance again to ask.
        assert "stance (worker), binding [text id: " in text
        assert "You are a PRODUCER" in text
        await mgr.shutdown_all()

    asyncio.run(run())


def test_mesh_install_project(tmp_path, home):
    from claude_launcher import install

    done = install.install_into_project(tmp_path)
    # one server, seven skills — and nothing outside the project: workflow
    # seeding is the global/profile installs' business
    assert len([line for line in done if line.startswith("skill ->")]) == 7
    # the topology skills (wire peers, delegate a domain, re-draw the tree)
    # land beside mesh
    for name in ("mesh-wire", "mesh-delegate", "mesh-retopology"):
        text = (tmp_path / ".claude" / "skills" / name / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert text.startswith(f"---\nname: {name}\n")
    retopo = (
        tmp_path / ".claude" / "skills" / "mesh-retopology" / "SKILL.md"
    ).read_text(encoding="utf-8")
    # every other reparent: the four moves, the briefing that must follow,
    # and the daemon's own refusal words so a lead recognises them
    assert "reparent" in retopo and "ONE batch send" in retopo
    for move in ("tier done", "parent died", "wrong tier", "undo delegate"):
        assert move in retopo
    for refusal in ("does not command", "cannot move itself", "has exited",
                    "make a cycle", "level(s) deep"):
        assert refusal in retopo
    delegate = (
        tmp_path / ".claude" / "skills" / "mesh-delegate" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "reparent" in delegate
    # the nested worker is an improv-worker that keeps the area as a
    # stacked pull request in a stack sub run (claunch-u8wjx.3), and
    # children it spawns start on the stack via rebase_onto
    assert "improv-mid" not in delegate
    assert "`stack` **sub run**" in delegate and "stacked pull request" in delegate
    assert "rebase_onto: <MID>-stack" in delegate
    assert "--rebase-merges" in delegate
    assert sum(1 for line in done if line.startswith("mcp server")) == 1
    assert not [line for line in done if line.startswith("workflow ->")]
    doc = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    server = doc["mcpServers"]["claunch"]
    assert server["args"][-1] == "mcp"
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
    assert "prefix the worktree name" in skill # spawn worktrees get a session prefix
    # the cflow skills land from the same install
    assert (tmp_path / ".claude" / "skills" / "cflow" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "cflow-author" / "SKILL.md").is_file()
    # ...and so does commit-stamp, teaching an agent to sign its commits
    stamp = (tmp_path / ".claude" / "skills" / "commit-stamp" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert stamp.startswith("---\nname: commit-stamp\n")
    assert "Claunch-Session" in stamp          # the session trailer
    assert "Claunch-Worktree" in stamp         # the worktree trailer
    assert "CLAUNCH_SESSION" in stamp          # where the session name comes from
    assert "--git-common-dir" in stamp         # how a linked worktree is detected
    # installing again is idempotent and keeps other servers
    doc["mcpServers"]["other"] = {"command": "x"}
    (tmp_path / ".mcp.json").write_text(json.dumps(doc), encoding="utf-8")
    install.install_into_project(tmp_path)
    doc2 = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert set(doc2["mcpServers"]) == {"claunch", "other"}


def test_global_install_targets_the_user_scope(tmp_path, home, monkeypatch):
    """``--global`` writes where the user's own claude actually reads.

    With CLAUDE_CONFIG_DIR set (the conftest fixture sets it), both the
    skills and the user-scope ``.claude.json`` live inside that directory.
    """
    import os

    from claude_launcher import config, install

    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    done = install.install_into_user()
    assert len([line for line in done if line.startswith("skill ->")]) == 7
    assert [line for line in done if line.startswith("workflow ->")]
    assert (cfg / "skills" / "mesh" / "SKILL.md").is_file()
    doc = json.loads((cfg / ".claude.json").read_text(encoding="utf-8"))
    assert "claunch" in doc["mcpServers"]

    # without CLAUDE_CONFIG_DIR, the user-scope config is ~/.claude.json —
    # a sibling of ~/.claude, not a file inside it
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    fake_home = tmp_path / ".fake-user-home"
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: fake_home))
    assert config.user_claude_json() == fake_home / ".claude.json"
    assert config.default_config_dir() == fake_home / ".claude"


def test_install_supersedes_the_split_servers(tmp_path, home):
    """An upgrade must switch the old servers off, not run them alongside.

    Two live servers would offer the agent every tool twice — the same tool
    name from two processes, with nothing to say which is current.
    """
    from claude_launcher import install

    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cflow": {"command": "claunch", "args": ["cflow", "mcp"]},
                    "mesh": {"command": "claunch", "args": ["mesh", "mcp"]},
                    "other": {"command": "x"},
                }
            }
        ),
        encoding="utf-8",
    )
    install.install_into_project(tmp_path)
    doc = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert set(doc["mcpServers"]) == {"claunch", "other"}


def test_merged_server_offers_both_toolsets():
    from claude_launcher import (
        mcp_server, mesh_mcp, status_checks_mcp, wait_mcp, window_mcp, observer_mcp,
        operator_mcp, search_mcp,
    )
    from claude_launcher.cflow import mcp as cflow_mcp

    names = [t["name"] for t in mcp_server.TOOLS]
    assert names == [t["name"] for t in cflow_mcp.TOOLS] + [
        t["name"] for t in mesh_mcp.TOOLS
    ] + [t["name"] for t in status_checks_mcp.TOOLS] + [
        t["name"] for t in window_mcp.TOOLS
    ] + [t["name"] for t in wait_mcp.TOOLS] + [t["name"] for t in observer_mcp.TOOLS] + [
        t["name"] for t in operator_mcp.TOOLS
    ] + [t["name"] for t in search_mcp.TOOLS]
    assert len(set(names)) == len(names)  # merge() guards this too
    listed = mcp_server.SERVER.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert [t["name"] for t in listed["result"]["tools"]] == names
    init = mcp_server.SERVER.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}
    )
    assert init["result"]["serverInfo"]["name"] == "claunch"
    assert cflow_mcp.AGENT_AUTHORITY in init["result"]["instructions"]
    cflow_names = {t["name"] for t in cflow_mcp.TOOLS}
    for tool in listed["result"]["tools"]:
        if tool["name"] in cflow_names:
            assert tool["description"].startswith(cflow_mcp.AGENT_AUTHORITY + "\n\n")
        else:
            assert cflow_mcp.AGENT_AUTHORITY not in tool["description"]


def test_merged_server_routes_errors_to_the_owning_half(home, monkeypatch):
    """A refusal from either half must come back as a tool error, not a crash."""
    from claude_launcher import mcp_server

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    # mesh half: nothing to reach (MeshMcpError, not a traceback)
    resp = mcp_server.SERVER.handle(
        {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "children", "arguments": {}},
        }
    )
    assert resp["result"]["isError"] is True
    assert "daemon is not running" in resp["result"]["content"][0]["text"]
    # cflow half: no run here
    resp = mcp_server.SERVER.handle(
        {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "status", "arguments": {}},
        }
    )
    assert resp["result"]["isError"] is False
    # and an unknown name is still an agent-readable error
    resp = mcp_server.SERVER.handle(
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }
    )
    assert resp["result"]["isError"] is True
    assert "unknown tool" in resp["result"]["content"][0]["text"]


def test_merge_refuses_colliding_tool_names():
    from claude_launcher import mcp_rpc

    a = mcp_rpc.Server("a", ({"name": "dup"},), lambda n, x: {}, (ValueError,))
    b = mcp_rpc.Server("b", ({"name": "dup"},), lambda n, x: {}, (ValueError,))
    with pytest.raises(mcp_rpc.ToolNameCollision):
        mcp_rpc.merge("both", [a, b])


def test_merge_preserves_instructions_from_each_server():
    from claude_launcher import mcp_rpc

    a = mcp_rpc.Server("a", (), lambda n, x: {}, (ValueError,), instructions="Scope A")
    b = mcp_rpc.Server("b", (), lambda n, x: {}, (ValueError,), instructions="Scope B")
    silent = mcp_rpc.Server("silent", (), lambda n, x: {}, (ValueError,))
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    assert "instructions" not in silent.handle(request)["result"]
    merged = mcp_rpc.merge("all", [a, silent, b])
    assert merged.handle(request)["result"]["instructions"] == "Scope A\n\nScope B"


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

    _stub_connect(monkeypatch, mesh_mcp.daemon_client, FakeClient)

    init = mesh_mcp._handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert init["result"]["serverInfo"]["name"] == "claunch-mesh"
    tools = mesh_mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in tools["result"]["tools"]] == [
        # talking ...
        "send", "members", "history",
        # ... reading a peer's checkout and coordinating on shared keys
        "peer_file", "peer_git", "lease",
        # ... building the team that does it: made, counted, re-oriented,
        # ended, re-drawn, wired
        "spawn", "children", "rebrief", "kill", "handoff", "reparent", "connect",
        "disconnect",
        # ... and answering the members who asked to be wired themselves
        "wire_requests",
        # ... and the ledger of what this session is waiting on
        "loops", "loop_add", "loop_close",
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

    # kill addresses the caller's own subtree — the route carries the scope,
    # so the tool cannot express "end somebody else's child" at all
    killed = []

    class KillClient(FakeClient):
        def post(self, path, body=None, **kw):
            killed.append(path)
            return {"name": "w1", "exited": True}

    _stub_connect(monkeypatch, mesh_mcp.daemon_client, KillClient)
    resp = mesh_mcp._handle(
        {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "kill", "arguments": {"session": "w1"}},
        }
    )
    assert resp["result"]["isError"] is False
    resp = mesh_mcp._handle(
        {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "kill",
                       "arguments": {"session": "w1", "force": True}},
        }
    )
    assert resp["result"]["isError"] is False
    assert killed == [
        "/api/sessions/s0/children/w1/kill",
        "/api/sessions/s0/children/w1/kill?force=1",
    ]

    # and it names what to end rather than defaulting to something
    resp = mesh_mcp._handle(
        {
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "kill", "arguments": {}},
        }
    )
    assert resp["result"]["isError"] is True
    assert "'session' is required" in resp["result"]["content"][0]["text"]


def test_member_rows_carry_the_lifecycle_partition(home, tmp_path):
    """The roster's filter needs the four words the session rail filters by.

    ``reachability`` collapses every ended record into ``exited``, and the
    roster is where that collapse costs most: 8 of the 12 members of the
    daemon's own gds6 mesh are ended records, so "exited" leaves the reader
    unable to tell one that was respawnable from one waiting to be resumed
    from one put away on purpose. The words come from the single function that
    defines them (``session.session_category``), which is what the rail
    filters by too — otherwise one record could be filed two ways on one
    screen. ``reachability`` itself is unchanged: the CLI prints it and the
    spawn tests read it.
    """
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    mm.create("m")

    async def run():
        for name in ("live", "killed", "paused", "away"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("m", "live", handle="alice")
        await mm.join("m", "killed", handle="bob")
        await mm.join("m", "paused", handle="carol")
        await mm.join("m", "away", handle="dave")

        def categories() -> dict:
            mesh = mm.get("m")
            return {
                r["handle"]: r["category"] for r in mm.mesh_info(mesh)["members"]
            }

        assert categories() == {
            "alice": "running", "bob": "running",
            "carol": "running", "dave": "running",
        }

        mgr.kill("killed")
        mgr.pause("paused")
        mgr.kill("away", force=True)
        for name in ("killed", "paused", "away"):
            await _wait_exited(mgr.get(name))
        mgr.archive("away")

        # Four records, four words — the partition the roster's filter buttons
        # are built on, and what lets their counts be added up.
        assert categories() == {
            "alice": "running", "bob": "killed",
            "carol": "paused", "dave": "archived",
        }
        rows = {r["handle"]: r for r in mm.mesh_info(mm.get("m"))["members"]}
        assert rows["bob"]["reachability"] == "exited"  # the old word, unchanged
        assert rows["dave"]["reachability"] == "exited"

        # A record that is gone is neither running nor killed: the word says
        # so, and the filter that drops ended records drops it too.
        mgr.remove("away")
        assert categories()["dave"] == "missing"

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_roster_is_filtered_before_it_is_built(home, tmp_path):
    """``state`` decides which members get a record, not which are drawn.

    A member's record costs a walk of the message log twice (``pending`` and
    ``owed``), so the roster is members times messages. Almost all of it is
    hidden: the view's default filter shows the live members and a long-lived
    mesh is mostly ended ones. Measured against mesh-0826 on 2026-09-20 --
    251 members, 27708 messages -- the answer was 4.0s and 1.66MB with every
    member and 11ms and 4.7KB with the eight running ones.

    The counts stay whole, because a filter bar that cannot say what it is
    hiding is worse than no filter; and the default is still every member, so
    a caller that says nothing gets what it always got.
    """
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    mm.create("m")

    async def run():
        for name in ("live", "gone"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("m", "live", handle="alice")
        await mm.join("m", "gone", handle="bob")
        mgr.kill("gone")
        await _wait_exited(mgr.get("gone"))
        mesh = mm.get("m")

        everyone = mm.mesh_info(mesh)
        assert [r["handle"] for r in everyone["members"]] == ["alice", "bob"]

        current = mm.mesh_info(mesh, state="current")
        assert [r["handle"] for r in current["members"]] == ["alice"]
        # Taken over every member either way.
        assert current["member_counts"]["all"] == 2
        assert current["member_counts"]["killed"] == 1
        assert current["member_counts"]["current"] == 1
        assert current["member_state"] == "current"

        # The pair table follows the roster: an edge is only as visible as
        # both of its ends, and this is the half that grows quadratically.
        assert everyone["member_links"] == [
            {"a": "alice", "b": "bob", "enabled": True}
        ]
        assert current["member_links"] == []

        # And the word for one category alone.
        only_dead = mm.mesh_info(mesh, state="killed")
        assert [r["handle"] for r in only_dead["members"]] == ["bob"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_owed_ledger_takes_the_same_roster_filter(home, tmp_path):
    """The ledger is the larger of the two walks, and the least worth paying
    for a member that has ended: there is nothing left to nudge.

    mesh-0826 answered this route in 4.2s over 251 members and 27708
    messages (2026-09-20), polled every 5 seconds by a view that draws the
    live ones. The totals follow the rows the answer contains, so a narrowed
    report reads as narrowed; ``member_counts`` still counts everybody.
    """
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    mm.create("m")

    async def run():
        for name in ("live", "gone"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("m", "live", handle="alice")
        await mm.join("m", "gone", handle="bob")
        mgr.kill("gone")
        await _wait_exited(mgr.get("gone"))
        mesh = mm.get("m")

        everyone = mm.owed_report(mesh)
        assert [r["handle"] for r in everyone["members"]] == ["alice", "bob"]
        assert everyone["member_state"] == "all"

        current = mm.owed_report(mesh, state="current")
        assert [r["handle"] for r in current["members"]] == ["alice"]
        assert current["member_counts"]["all"] == 2
        assert current["member_counts"]["killed"] == 1

        await mgr.shutdown_all()

    asyncio.run(run())


def test_members_tool_response_is_linear_without_the_pair_table(monkeypatch):
    """The MCP ``members`` tool must not ship the daemon's O(n²) pair table.

    A roster heavy with exited members is exactly where that table blows up —
    141 of 146 members exited made ``member_links`` 10,585 pairs / 570KB of a
    603KB reply (the 54k-line regression this pins). Reachability is already
    computed for the caller; the pair table is the web diagram's, and the MCP
    contract (handle, role, machine, reachability, pending) never listed it.
    """
    from claude_launcher import mesh_mcp

    def payload_size(n: int) -> int:
        members = [
            {"handle": f"w{i}", "session": f"s{i}"} for i in range(n)
        ]
        pair_table = [
            {"a": members[i]["handle"], "b": members[j]["handle"],
             "enabled": True}
            for i in range(n)
            for j in range(i + 1, n)
        ]
        assert len(pair_table) == n * (n - 1) // 2

        class FakeClient:
            def get(self, path, **kw):
                assert path == "/api/mesh/dev"
                return {
                    "members": members,
                    "member_links": pair_table,
                    "peers": [],
                    "relay": {"configured": False},
                }

        _stub_connect(monkeypatch, mesh_mcp.daemon_client, FakeClient)
        resp = mesh_mcp._handle(
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "members", "arguments": {"mesh": "dev"}},
            }
        )
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert "member_links" not in payload
        assert len(payload["members"]) == n
        return len(json.dumps(payload))

    size_10 = payload_size(10)
    size_20 = payload_size(20)
    # Doubling the roster must not quadruple the reply: with the pair table
    # (O(n²)) shipped the 20-member reply is ~4x the 10-member one; without
    # it the reply scales with the roster itself (~2x). A bound of 3x keeps a
    # comfortable margin on either side and still catches reintroducing it.
    assert size_20 < 3.0 * size_10, f"{size_10} -> {size_20} looks quadratic"


def test_every_offered_spawn_field_is_forwarded():
    """A field the schema offers but the forwarder drops is a silent no-op
    the caller reads as 'the daemon ignored me' — the two lists must not
    drift apart (worktree once did exactly that)."""
    from claude_launcher import mesh_mcp

    spawn_tool = next(t for t in mesh_mcp.TOOLS if t["name"] == "spawn")
    offered = set(spawn_tool["inputSchema"]["properties"])
    # A field may reach the API under another name, but only through the
    # declared map — an undeclared rename is the same silent no-op.
    assert offered <= set(mesh_mcp._SPAWN_KEYS) | set(mesh_mcp._SPAWN_RENAMED)


def test_the_board_answer_reaches_the_api_in_its_own_spelling(monkeypatch):
    """`no_issue: true` is a falsey `beads: false` on the wire — the exact
    shape a truthiness filter drops, so it is pinned."""
    from claude_launcher import mesh_mcp

    sent = {}

    class FakeClient:
        def post(self, path, body):
            sent.clear()
            sent.update({"path": path, "body": body})
            return {"session": {"name": "kid"}}

    monkeypatch.setattr(mesh_mcp, "_client", lambda: FakeClient())
    monkeypatch.setattr(mesh_mcp, "_session", lambda: "lead")

    mesh_mcp._spawn({"task": "go", "issue": "cl-9"})
    assert sent["path"] == "/api/sessions/lead/children"
    assert sent["body"]["issue"] == "cl-9" and "beads" not in sent["body"]

    mesh_mcp._spawn({"task": "go", "no_issue": True})
    assert sent["body"]["beads"] is False and "issue" not in sent["body"]

    # the other half of that answer: the same empty board, and the opposite
    # instruction to the child. It travels as a value of the same key, so a
    # spawn can never carry both instructions at once.
    mesh_mcp._spawn({"task": "go", "no_issue_auto": True})
    assert sent["body"]["beads"] == "none-auto" and "issue" not in sent["body"]

    # a caller that sets both gets the auto answer, not a coin toss: it is
    # the one that says what the child should DO
    mesh_mcp._spawn({"task": "go", "no_issue": True, "no_issue_auto": True})
    assert sent["body"]["beads"] == "none-auto"

    # saying nothing must send nothing: the daemon reads that as "mint one"
    mesh_mcp._spawn({"task": "go"})
    assert "beads" not in sent["body"] and "issue" not in sent["body"]

    # the third answer travels under its own name — a lead that wrote the
    # specification hands it over instead of letting the child mint from a
    # task that is only the first instruction
    mesh_mcp._spawn({"task": "go", "issue_text": "Rail must answer"})
    assert sent["body"]["issue_text"] == "Rail must answer"
    assert "issue" not in sent["body"] and "beads" not in sent["body"]


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


def test_mesh_get_answers_which_member_the_asking_session_is(home, tmp_path):
    """``?session=`` is the seam the CLI stopped guessing across.

    ``stance`` and ``leave`` used to pick their own handle out of the roster
    by matching a blank ``machine``, which is the daemon's rule to apply and
    means opposite things on an authority and a mirror. The endpoint answers
    it now; a poll that names no session still gets ``None``, so "nobody
    asked" and "you are nobody" stay apart.
    """
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            mm.create("web")
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
            await mm.join("web", "w1", handle="worker_1")

            doc = await (await client.get(
                "/api/mesh/web?session=w1", headers=bearer)).json()
            assert doc["you"] == "worker_1"
            # and the locality it used to make readers re-derive
            assert doc["members"][0]["local"] is True

            for q in ("", "?session=", "?session=ghost"):
                doc = await (await client.get(
                    f"/api/mesh/web{q}", headers=bearer)).json()
                assert doc["you"] is None

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_dead_member_is_not_rescanned_while_the_log_is_quiet(home, tmp_path):
    """A stranded backlog never drains on its own, so the delivery tick used
    to rescan the log from the dead member's cursor every second -- on a
    16k-message mesh with 170 exited members that was 1.4M messages a tick
    and half the daemon's CPU (claunch-qx5c). Now a dead member is looked
    at once per append; a respawn or a new message brings it back."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, busy_hold=5.0)
        mm.create("m9")
        a = mgr.create(SessionDef(name="lead9", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="w9", harness="py", cwd=str(tmp_path)))
        for s in (a, b):
            await _wait_screen(s, "READY")
        await mm.join("m9", "lead9", handle="leader")
        await mm.join("m9", "w9", handle="bob")
        mesh = mm.get("m9")
        await mm.send("m9", "leader", "bob", "one")
        await b.send_keys(["quit", "Enter"])
        await b.wait_for("exited", timeout=10.0, threshold=0.5)

        calls = []
        real = mesh.pending

        def counted(handle):
            # Only the delivery tick's scans count: send-time congestion
            # checks and the policy tick ask ``pending`` for their own reasons.
            if sys._getframe(1).f_code.co_name == "_deliver_to":
                calls.append(handle)
            return real(handle)

        mesh.pending = counted
        member = mesh.members["bob"]
        for _ in range(5):
            await mm._deliver_to(mesh, member)
        assert calls == ["bob"], "a quiet log was rescanned"
        # An append is news: the member is looked at again, exactly once.
        await mm.send("m9", "leader", "bob", "two")
        for _ in range(3):
            await mm._deliver_to(mesh, member)
        assert calls.count("bob") == 2
        assert len(real("bob")) == 2            # still held for the respawn
        # A live session is never skipped: the memo clears on revival.
        revived = mgr.respawn("w9")
        await revived.wait_for("idle", timeout=20.0, threshold=0.5)
        await mm._deliver_to(mesh, mesh.members["bob"], force=True)
        await _wait_drained(mesh, "bob", timeout=20.0)
        assert "bob" not in mesh._stranded_scan

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# '*' is warned on every send; '@in_review' names the landing queue instead
# --------------------------------------------------------------------------- #
def test_every_agent_broadcast_carries_the_broadcast_advisory(home, tmp_path):
    """A '*' reaches finished sessions too, and each spends a turn on it
    (claunch-424v4). The sender is told on every such send; a named send and
    the human's dashboard send are not."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr)
        mm.create("b")
        for n, h in (("s1", "leader"), ("s2", "w1"), ("s3", "w2")):
            mgr.create(SessionDef(name=n, harness="py", cwd=str(tmp_path)))
            await mm.join("b", n, handle=h)

        cast = await mm.send("b", "leader", "*", "master moved.", type="fyi")
        note = cast["notice"] or ""
        assert note.startswith("BROADCAST")
        assert "2 terminal(s)" in note
        assert "@in_review" in note  # the alternative is named

        named = await mm.send("b", "leader", ["w1"], "yours is next.")
        assert named["notice"] is None

        human = await mm.send("b", "operator", "*", "hello all", external=True)
        assert human["notice"] is None

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_in_review_selector_resolves_to_connected_assignees(home, tmp_path):
    """'@in_review' becomes the explicit handle list of the members whose
    session holds an in_review issue; the log records that list, never the
    selector, and assignees it could not reach are reported."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr)
        mm.create("b")
        for n, h in (("s1", "leader"), ("s2", "w1"), ("s3", "w2"), ("s4", "w3")):
            mgr.create(SessionDef(name=n, harness="py", cwd=str(tmp_path)))
            await mm.join("b", n, handle=h)

        with pytest.raises(MeshError, match="has none wired"):
            await mm.send("b", "leader", "@in_review", "remeasure")

        asked = []

        async def resolver(session, status):
            asked.append((session, status))
            # s2 and s4 are landing; s9 is an assignee with no member here
            return ["s2", "s4", "s9"]

        mm.audience_resolver = resolver
        sent = await mm.send("b", "leader", "@in_review", "remeasure on abc")
        assert asked == [("s1", "in_review")]
        assert sent["recipients"] == ["w1", "w3"]
        assert mm.get("b").messages[-1]["to"] == ["w1", "w3"]
        assert "s9" in (sent["notice"] or "")
        assert "BROADCAST" not in (sent["notice"] or "")

        with pytest.raises(MeshError, match="unknown audience selector"):
            await mm.send("b", "leader", "@everyone", "x")

        async def nobody(session, status):
            return []

        mm.audience_resolver = nobody
        with pytest.raises(MeshError, match="nothing was sent"):
            await mm.send("b", "leader", "@in_review", "x")

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_mesh_audience_reads_the_senders_board(tmp_path):
    """The API's resolver: assignees of issues in the status, on the board
    the sender's directory files on — distinct, sorted, blanks dropped."""
    from types import SimpleNamespace

    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.manager import ManagerError

    class _Mgr:
        def get(self, name):
            if name != "lead":
                raise ManagerError(name)
            return SimpleNamespace(sdef=SimpleNamespace(cwd=str(tmp_path)))

    class _Board:
        async def root_for(self, cwd):
            assert cwd == str(tmp_path)
            return tmp_path

        async def issues(self, root):
            return [
                {"id": "a", "status": "in_review", "assignee": "s2"},
                {"id": "b", "status": "in_review", "assignee": "s2"},
                {"id": "c", "status": "in_progress", "assignee": "s3"},
                {"id": "d", "status": "in_review", "assignee": ""},
                {"id": "e", "status": "in_review", "assignee": "s1"},
            ]

    got = asyncio.run(api_mod._mesh_audience(_Mgr(), _Board(), "lead", "in_review"))
    assert got == ["s1", "s2"]
    with pytest.raises(MeshError, match="not one"):
        asyncio.run(api_mod._mesh_audience(_Mgr(), _Board(), "ghost", "in_review"))
