"""What is waiting to be typed into a session, and who can see it.

Everything on this queue is cflow's -- the clock's reminders and stall pings,
and the dashboard's nudges (the callers of ``Session.queue_delivery``). Mesh
messages take their own path through ``MeshManager`` and were already
published by the queued view; this is the half that nothing showed.

Such a message is accepted immediately and typed in later: the delivery waits for the harness to be ready and for any
half-written line in the composer to be sent. While an agent works a turn
that can be minutes.

Until this, nothing outside the daemon could see that wait. Pressing a cflow
button and seeing no change read exactly like the message having been
dropped, so the button was pressed again -- which is how four identical
nudges arrive in one batch (claunch-restart-disconnect-banner-12p2). These
pin the reading that makes the wait visible, and the honesty of it: an
accepted message is not a delivered one, and the queue does not survive the
daemon.
"""

from __future__ import annotations

import asyncio
import threading

from claude_launcher.daemon import session as session_mod


class _Fake(session_mod.Session):
    """A session with the child taken out: only the delivery queue is under
    test, and a real PTY would make the test about timing instead."""

    def __init__(self):
        self.sdef = type("D", (), {"name": "s1", "to_dict": lambda self: {"name": "s1"}})()
        self.exited = False
        self._loop = asyncio.get_running_loop()
        self._deferred_deliveries = set()
        self._deferred_delivery_lock = asyncio.Lock()
        self._pending_deliveries = []
        self._delivery_seq = 0
        self.delivered = []
        self.gate = asyncio.Event()

    async def deliver(self, text, **kwargs):
        await self.gate.wait()      # stands in for "the agent is mid-turn"
        self.delivered.append(text)
        return True


def test_an_accepted_message_is_visible_while_it_waits():
    """The reading this exists for: between the press and the message
    appearing in the terminal, there is something to look at."""

    async def run():
        s = _Fake()
        assert s.queue_delivery("cflow: continue per the /cflow protocol")
        waiting = s.pending_deliveries()
        assert len(waiting) == 1
        assert waiting[0]["preview"] == "cflow: continue per the /cflow protocol"
        assert waiting[0]["chars"] == len("cflow: continue per the /cflow protocol")
        assert waiting[0]["at"]

        # And it stops being visible once it has gone in -- a queue that only
        # grows would be worse than no queue at all.
        s.gate.set()
        await asyncio.gather(*tuple(s._deferred_deliveries))
        assert s.pending_deliveries() == []
        assert s.delivered == ["cflow: continue per the /cflow protocol"]

    asyncio.run(run())


def test_the_queue_keeps_the_order_the_messages_will_go_in():
    """Oldest first, because that is the order they will be typed."""

    async def run():
        s = _Fake()
        for i in range(3):
            s.queue_delivery(f"message {i}")
        assert [d["preview"] for d in s.pending_deliveries()] == [
            "message 0", "message 1", "message 2",
        ]
        assert [d["id"] for d in s.pending_deliveries()] == [1, 2, 3]

    asyncio.run(run())


def test_a_failed_delivery_stops_waiting_too():
    """It left the queue whichever way it left: a message that raised is not
    still on its way in."""

    async def run():
        s = _Fake()

        async def boom(text, **kwargs):
            raise RuntimeError("the pty went away")

        s.deliver = boom
        s.queue_delivery("nudge")
        assert len(s.pending_deliveries()) == 1
        await asyncio.gather(*tuple(s._deferred_deliveries), return_exceptions=True)
        assert s.pending_deliveries() == []

    asyncio.run(run())


def test_a_preview_names_the_message_rather_than_its_header():
    """Machine-generated deliveries open with a rule and a heading, so the
    first line alone does not say which message this is."""
    briefing = (
        "---\n"
        "# claunch mesh: automated message delivery\n"
        "mesh: mesh-0826\n"
    )
    assert session_mod._delivery_preview(briefing) == "claunch mesh: automated message delivery"
    assert session_mod._delivery_preview("") == ""
    assert session_mod._delivery_preview("---\n\n") == ""

    long = "x" * (session_mod.DELIVERY_PREVIEW_CHARS + 50)
    preview = session_mod._delivery_preview(long)
    assert len(preview) == session_mod.DELIVERY_PREVIEW_CHARS
    assert preview.endswith("\u2026")


def test_a_delivery_from_a_worker_thread_is_still_typed():
    """claunch-0ccn4.1: ``queue_delivery`` reached through
    ``asyncio.to_thread`` was a silent loss.

    Guest thread messages and mega-thread deliveries take exactly this route
    (``threads_api.mention_delivery`` -> ``deliver`` -> ``queue_delivery``),
    and ``asyncio.create_task`` raises "no running event loop" on a worker
    thread. The coroutine was never awaited, the message was never typed,
    and the caller's ``except Exception`` swallowed it -- so the thread row
    reported the message as accepted while nothing arrived.

    The task belongs to the session's loop, not the caller's: it is created
    there, whichever thread asked for it.
    """

    async def run():
        s = _Fake()
        s.gate.set()
        assert await asyncio.to_thread(s.queue_delivery, "guest: 확인 부탁") is True
        assert len(s.pending_deliveries()) == 1    # accepted from this thread

        # The task is created on the loop's next pass, not in the worker
        # thread -- one turn is enough for the send to finish.
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(s._deferred_deliveries))
        assert s.delivered == ["guest: 확인 부탁"], "a worker-thread delivery was lost"
        assert s.pending_deliveries() == []

    asyncio.run(run())


def test_a_session_without_a_loop_refuses_instead_of_losing_the_message():
    """A double, or a session reached from a thread with no loop behind it,
    cannot run the send. Accepting it would be the same silent loss: say no,
    and leave nothing waiting that nobody will type.

    A session that does hold a loop is not this case -- it is scheduled
    there, whichever thread asked (the test above)."""

    async def run():
        s = _Fake()
        s._loop = None
        seen = {}

        def off_loop():
            seen["accepted"] = s.queue_delivery("nudge")

        thread = threading.Thread(target=off_loop)
        thread.start()
        thread.join()
        assert seen["accepted"] is False
        assert s.pending_deliveries() == []

    asyncio.run(run())


def test_a_session_that_ended_accepts_nothing():
    """Queueing for a dead session would show a wait that will never end."""

    async def run():
        s = _Fake()
        s.exited = True
        assert s.queue_delivery("nudge") is False
        assert s.pending_deliveries() == []

    asyncio.run(run())


def test_the_rail_poll_carries_it():
    """The rail draws one row per session, so the question is asked of the
    fleet at once rather than per session."""
    from claude_launcher.daemon import api as api_mod

    # The rail response is filtered to a field list; a field the rail draws
    # and the filter drops is a pill that never appears. The list is a module
    # constant now, so this reads it instead of scraping the source for the
    # literal (a test that greps for text passes even when the filter is
    # built somewhere else).
    assert "pending_deliveries" in api_mod.RAIL_FIELDS
