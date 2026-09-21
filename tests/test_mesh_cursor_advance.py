"""A delivery pass costs the messages that arrived, not the whole log.

``Mesh.pending`` walks ``messages[cursor:]`` and asks ``addressed_to`` of
every message it finds, and ``addressed_to`` consults the member graph. The
cursor advanced only when something was actually typed into a terminal, so
a member nobody had addressed since it joined kept its join-time cursor and
re-walked the whole log on every pass of the delivery worker -- once per
member, for every member, several times a second.

Measured on the live daemon (s586, 2026-09-21; mesh-0826 carries 314
members): a py-spy sample put 18.2% of the event loop thread's time under
``_worker -> _deliver_to``, of which ``pending`` and ``addressed_to`` were
15.6% and 9.7%. The terminal sockets are on that loop.

A scan that found nothing for a member has established that those messages
are not for it, and a message the scan passed over never becomes pending
later: a member that joins is already caught up (``join`` sets its cursor
to the end of the log), so the backlog is not replayed for a member whose
edges change either. So the cursor moves on a scan that found nothing, and
these pin both halves: it moves when there is nothing pending, and it stays
where it is whenever something is still owed.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher.daemon.mesh import STATUS_IDLE, Member, MeshManager

from test_mesh import _manager


class _Terminal:
    """A session that accepts everything typed into it: idle, unheld, and
    keeping what it was given."""

    def __init__(self, name: str = "s1"):
        self.name = name
        self.exited = False
        self.delivered: list = []

    def delivery_held(self) -> bool:
        return False

    def keyboard_busy(self) -> bool:
        return False

    def status(self) -> str:
        return STATUS_IDLE

    async def deliver(self, block, force=False):
        self.delivered.append(block)
        return True


class _Busy(_Terminal):
    """A session mid-turn: delivery is refused and retried next pass."""

    async def deliver(self, block, force=False):
        return False


class _Gone(_Terminal):
    def __init__(self, name: str = "s1"):
        super().__init__(name)
        self.exited = True


def _mesh(tmp_path, sessions: dict):
    """A mesh whose members resolve to the given fake sessions."""
    mm = MeshManager(_manager(), root=tmp_path / "mesh")
    mesh = mm.create("team")
    mesh.members = {
        handle: Member(handle, f"sess-{handle}") for handle in sessions
    }
    mm._persist_def(mesh)
    mm.set_policy("team", {"backpressure": {"inbox_max": 0}})
    by_session = {f"sess-{h}": s for h, s in sessions.items()}
    mm.manager.get = lambda name: by_session[name]  # type: ignore[assignment]
    for handle in mesh.members:
        mesh.cursors[handle] = 0
    return mm, mesh


async def _chatter(mm, n: int, *, frm="lead", to="worker"):
    for i in range(n):
        await mm.send("team", frm, to, f"message {i}")


def test_a_member_nothing_was_addressed_to_stops_rescanning(tmp_path):
    """The case that was costing the loop: a bystander on a busy mesh."""

    async def run():
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": _Terminal(),
                                    "bystander": _Terminal()})
        await _chatter(mm, 20)
        await mm._deliver_to(mesh, mesh.members["bystander"])
        assert mesh.cursors["bystander"] == len(mesh.messages)

    asyncio.run(run())


def test_the_scan_after_that_reads_only_what_arrived(tmp_path):
    """Said as the cost rather than the cursor: the second pass asks about
    the messages since the first one, and no more."""

    async def run():
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": _Terminal(),
                                    "bystander": _Terminal()})
        await _chatter(mm, 50)
        await mm._deliver_to(mesh, mesh.members["bystander"])

        looked = []
        real = type(mesh).addressed_to

        def watched(self, msg, handle):
            looked.append(msg.get("id"))
            return real(self, msg, handle)

        type(mesh).addressed_to = watched
        try:
            await _chatter(mm, 3)
            await mm._deliver_to(mesh, mesh.members["bystander"])
        finally:
            type(mesh).addressed_to = real
        assert len(looked) == 3, f"re-read {len(looked)} messages"

    asyncio.run(run())


def test_a_message_that_was_delivered_advances_it_too(tmp_path):
    """The path that already worked keeps working."""

    async def run():
        term = _Terminal()
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": term})
        await _chatter(mm, 4)
        await mm._deliver_to(mesh, mesh.members["worker"])
        assert term.delivered, "nothing was typed in"
        assert mesh.cursors["worker"] == len(mesh.messages)

    asyncio.run(run())


def test_an_undelivered_message_holds_the_cursor(tmp_path):
    """A terminal mid-turn refuses the paste, and the next pass must find
    the same messages waiting."""

    async def run():
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": _Busy()})
        await _chatter(mm, 4)
        before = mesh.cursors["worker"]
        await mm._deliver_to(mesh, mesh.members["worker"])
        assert mesh.cursors["worker"] == before
        assert len(mesh.pending("worker")) == 4

    asyncio.run(run())


def test_a_stranded_member_holds_its_cursor(tmp_path):
    """A member whose terminal is gone keeps what is owed to it, for the
    respawn that reads it."""

    async def run():
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": _Gone()})
        await _chatter(mm, 4)
        before = mesh.cursors["worker"]
        await mm._deliver_to(mesh, mesh.members["worker"])
        assert mesh.cursors["worker"] == before
        assert len(mesh.pending("worker")) == 4

    asyncio.run(run())


def test_a_gone_member_with_nothing_owed_advances(tmp_path):
    """An exited member that nobody addressed is not a reason to re-walk
    the log either."""

    async def run():
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": _Terminal(),
                                    "bystander": _Gone()})
        await _chatter(mm, 10)
        await mm._deliver_to(mesh, mesh.members["bystander"])
        assert mesh.cursors["bystander"] == len(mesh.messages)
        assert mesh.pending("bystander") == []

    asyncio.run(run())


def test_the_next_message_still_arrives(tmp_path):
    """Advancing past what was not for it does not shut the member out."""

    async def run():
        term = _Terminal()
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": _Terminal(),
                                    "bystander": term})
        await _chatter(mm, 10)
        await mm._deliver_to(mesh, mesh.members["bystander"])
        await mm.send("team", "lead", "bystander", "this one is yours")
        await mm._deliver_to(mesh, mesh.members["bystander"])
        assert term.delivered and "this one is yours" in term.delivered[0]

    asyncio.run(run())


def test_a_broadcast_is_not_skipped_by_an_advanced_cursor(tmp_path):
    """``*`` resolves per member at scan time, so the cursor must not run
    ahead of one."""

    async def run():
        term = _Terminal()
        mm, mesh = _mesh(tmp_path, {"lead": _Terminal(), "worker": _Terminal(),
                                    "bystander": term})
        await _chatter(mm, 5)
        await mm._deliver_to(mesh, mesh.members["bystander"])
        await mm.send("team", "lead", "*", "everyone")
        await mm._deliver_to(mesh, mesh.members["bystander"])
        assert term.delivered and "everyone" in term.delivered[0]

    asyncio.run(run())
