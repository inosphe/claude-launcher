"""The response nudge settles on the same rule the owed ledger settles on.

``Mesh.owed`` closes a debt when the member sends anything at all -- its
walk stops at the member's own last send -- and its docstring says why the
rule is that one: "a dashboard that disagreed with the heartbeat would be
worse than no dashboard". The response watch used a stricter rule, clearing
only on a reply carrying ``reply_to``.

So an ack sent without ``--reply-to`` closed the debt in the ledger and in
the heartbeat, and left the watch standing. The watch then nudged its
sender at 5, 10, 15 and 20 minutes about a message that had been answered,
and each nudge is typed into that session's terminal.

Observed on mesh-0826 (s586, 2026-09-21): the ledger reported nothing owed
across the whole mesh while two nudges arrived for one message, and the
dismissal that would have settled it was refused -- ``'s469' does not owe
an answer to: msg-...`` -- because by the ledger's reckoning there was
nothing left to dismiss.

Decided by the leader (claunch-response-nudge-disagrees-owed-ga9v8): one
rule, the ledger's.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from claude_launcher.daemon.mesh import MeshManager, Member

from test_mesh import _manager


def _team(tmp_path):
    mm = MeshManager(_manager(), root=tmp_path / "mesh")
    mesh = mm.create("team")
    mesh.members = {
        "lead": Member("lead", "s1"),
        "worker": Member("worker", "s2"),
        "other": Member("other", "s3"),
    }
    mm._persist_def(mesh)
    mm.set_policy("team", {"backpressure": {"inbox_max": 0}})
    return mm, mesh


async def _asked(mm, mesh, *, to="worker"):
    """One delivered question, watched, as the tick would find it.

    Marked delivered on both books: the ledger reads ``delivered_ids`` and
    the watch is its own record, and these tests are about the two agreeing.
    """
    sent = await mm.send("team", "lead", to, "please check", type="ask")
    original = next(m for m in mesh.messages if m["id"] == sent["id"])
    mesh.delivered_ids.setdefault(to, set()).add(sent["id"])
    mm._watch_delivered_responses(mesh, to, [original])
    assert mesh.response_watches
    return sent


def test_an_ack_without_reply_to_settles_the_watch(tmp_path):
    """The case that was reported: answered, and nudged anyway."""

    async def run():
        mm, mesh = _team(tmp_path)
        await _asked(mm, mesh)
        await mm.send("team", "worker", "lead", "on it", type="ack")
        assert not mesh.response_watches

    asyncio.run(run())


def test_any_message_from_the_recipient_settles_it(tmp_path):
    """Any message, which is the ledger's rule -- one reply closes three
    questions, and the watch may not be stricter than that."""

    async def run():
        for kind in ("say", "fyi", "ask"):
            mm, mesh = _team(tmp_path / kind)
            await _asked(mm, mesh)
            await mm.send("team", "worker", "lead", "something else", type=kind)
            assert not mesh.response_watches, kind

    asyncio.run(run())


def test_a_threaded_reply_still_settles_it(tmp_path):
    """The path that already worked keeps working."""

    async def run():
        mm, mesh = _team(tmp_path)
        sent = await _asked(mm, mesh)
        await mm.send(
            "team", "worker", "lead", "taking it", type="ack", reply_to=sent["id"]
        )
        assert not mesh.response_watches

    asyncio.run(run())


def test_somebody_else_speaking_settles_nothing(tmp_path):
    """The debt belongs to the member it was delivered to. A third member
    talking is not that member answering."""

    async def run():
        mm, mesh = _team(tmp_path)
        await _asked(mm, mesh)
        await mm.send("team", "other", "lead", "not mine to answer")
        assert mesh.response_watches

    asyncio.run(run())


def test_the_nudge_does_not_fire_after_an_unthreaded_answer(tmp_path):
    """End of the path, at the clock: the tick has nothing to send."""

    async def run():
        mm, mesh = _team(tmp_path)
        await _asked(mm, mesh)
        await mm.send("team", "worker", "lead", "on it", type="ack")
        for watch in mesh.response_watches.values():
            watch["delivered_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=25)
            ).isoformat(timespec="seconds")
        mm._response_watch_tick(mesh)
        assert [m for m in mesh.messages if m["from"] == "policy"] == []

    asyncio.run(run())


def test_the_watch_and_the_ledger_agree(tmp_path):
    """Said as the property rather than the mechanism: what the ledger has
    stopped counting is not something the watch still chases."""

    async def run():
        mm, mesh = _team(tmp_path)
        await _asked(mm, mesh)
        assert mesh.owed("worker"), "the premise: a debt both sides can see"

        await mm.send("team", "worker", "lead", "on it", type="ack")
        assert mesh.owed("worker") == []
        assert not mesh.response_watches

    asyncio.run(run())


def test_a_question_asked_after_the_answer_is_still_watched(tmp_path):
    """Settling looks backwards, as the ledger's walk does. A question
    delivered after the member last spoke is still owed."""

    async def run():
        mm, mesh = _team(tmp_path)
        await _asked(mm, mesh)
        await mm.send("team", "worker", "lead", "on it", type="ack")
        assert not mesh.response_watches

        await _asked(mm, mesh)
        assert mesh.response_watches, "the newer question was settled by an older answer"

    asyncio.run(run())
