"""Backpressure: the mesh stops accepting mail a terminal cannot keep up with.

A leader with a dozen children is a fan-in, and nothing in the delivery path
used to bound it. Every send was accepted, the backlog only ever grew, and
``busy_hold`` guaranteed that after a minute the daemon would type into the
running turn anyway — so a burst of twelve reports cost twelve interruptions
and the sender was told ``sent`` every time.

Two gates fix that, at opposite ends:

* **the door** (:meth:`MeshManager._send_core`) — a recipient whose backlog
  has reached ``inbox_max`` stops accepting. The send is REFUSED, not
  queued, and the sender is told so synchronously so it can spend the turn
  on something else. Refused for every recipient is
  :class:`~claude_launcher.daemon.mesh.MeshBusy` (HTTP 429); refused for
  some is an ordinary send that names them in ``deferred``.
* **the pacing gate** (:meth:`MeshManager._deliver_to`) — at most one block
  typed into one terminal per ``min_gap``. Last of the automatic gates, so
  it still binds after ``busy_hold`` has given up; the wait is never lost
  work, because everything pending goes in as one block.

What the tests below hold on to is mostly the *refusals*: that nothing is
queued behind the sender's back (a bounce that secretly queued would be
worse than no bounce), that the address is narrowed so a ``"*"`` cannot
sneak in on the next tick, and that the human at the dashboard is never the
one turned away.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon import mesh_policy
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshBusy, MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _cap(mm, mesh, **patch):
    """Set this mesh's backpressure knobs for the test at hand.

    Through :func:`mesh_policy.merge_policy` rather than by poking the dict,
    so a test can only ask for a configuration an operator could also ask
    for — the validator is part of what is under test.
    """
    mm.set_policy(mesh, {"backpressure": patch})


async def _await_exit(mgr, *names):
    """Wait for each session's record to read ``exited``.

    ``kill``/``pause`` return as soon as the signal is sent; ``exited``
    flips on the PTY's EOF, which is a moment later. Every gate that asks
    whether a receiver is reading — delivery, the absolute cap — reads the
    record, so a test that does not wait here is testing a live session.
    """
    for name in names:
        for _ in range(100):
            if mgr.get(name).exited:
                break
            await asyncio.sleep(0.05)
        assert mgr.get(name).exited, name


# --------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------- #
def test_the_section_is_on_by_default_and_validates_its_own_shape():
    """Unlike the three nudges beside it, backpressure ships enabled: the
    nudges SPEND a recipient's turn (so switching one on is a choice), and
    this is the only thing that stops a fan-in from spending them for it.

    The knobs are validated as what they are — a queue depth is a whole
    count, the two waits are durations — because a typo that becomes a cap
    nobody chose is exactly the failure a validated policy exists to
    prevent.
    """
    bp = mesh_policy.default_policy()["backpressure"]
    assert bp["enabled"] is True
    assert bp["inbox_max"] >= 1 and float(bp["inbox_max"]).is_integer()
    assert bp["min_gap"] > 0 and bp["retry_after"] > 0

    base = mesh_policy.default_policy()
    got = mesh_policy.merge_policy(base, {"backpressure": {"inbox_max": 2}})
    assert got["backpressure"]["inbox_max"] == 2
    assert got["backpressure"]["enabled"] is True     # untouched keys survive

    # 0 is the documented "no limit"/"no pacing", not a rejected value.
    got = mesh_policy.merge_policy(
        base, {"backpressure": {"inbox_max": 0, "min_gap": 0}}
    )
    assert got["backpressure"]["inbox_max"] == 0
    assert got["backpressure"]["min_gap"] == 0.0

    for bad in ({"inbox_max": 2.5}, {"inbox_max": -1}, {"inbox_max": "lots"},
                {"min_gap": "soon"}, {"nope": 1}):
        with pytest.raises(mesh_policy.PolicyError):
            mesh_policy.merge_policy(base, {"backpressure": bad})

    # A mesh.json from before the section existed still loads, and loads
    # WITH the section: an old file must not silently opt out of the gate.
    old = mesh_policy.load_policy({"heartbeat": {"interval": 30.0}})
    assert old["backpressure"] == mesh_policy.default_policy()["backpressure"]


# --------------------------------------------------------------------- #
# the door
# --------------------------------------------------------------------- #
def test_a_full_inbox_refuses_the_send_and_queues_nothing(home, tmp_path):
    """The whole point: past the cap the sender is told NO, and the message
    does not exist afterwards.

    A bounce that quietly queued anyway would be the worst of both — the
    sender backs off AND the recipient still gets the interruption — so the
    assertions are as much about the log as about the exception. The refusal
    carries what a caller needs to act without parsing the sentence: who,
    how deep, and how long to wait.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        # No daemon, so no delivery worker: everything sent STAYS pending,
        # which is how the backlog reaches the cap in the first place.
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            _cap(mm, "team", inbox_max=2, retry_after=45.0)

            for n in range(2):
                await mm.send("team", "w1", "lead", f"report {n}")
            mesh = mm.get("team")
            assert len(mesh.pending("lead")) == 2
            before = len(mesh.messages)

            with pytest.raises(MeshBusy) as caught:
                await mm.send("team", "w1", "lead", "report 2")
            exc = caught.value
            assert exc.entries == [
                {"handle": "lead", "queued": 2, "inbox_max": 2,
                 "retry_after": 45.0, "remote": False}
            ]
            assert exc.retry_after == 45.0
            # The sentence an agent reads in its own terminal has to say the
            # two things it would otherwise get wrong: that nothing is
            # queued, and to WAIT rather than retry immediately (a burst of
            # retries is the flood this gate exists to stop).
            assert "NOT DELIVERED" in str(exc)
            assert "45s" in str(exc)
            assert "Nothing was queued" in str(exc)

            # Nothing appended, nothing pending, nothing to arrive later.
            assert len(mesh.messages) == before
            assert len(mesh.pending("lead")) == 2
            assert [m["body"] for m in mesh.pending("lead")] == [
                "report 0", "report 1"
            ]

            # Refusals are remembered against the recipient — the only trace
            # a bounce leaves here, and what the dashboard reads to explain
            # a backlog that has stopped growing.
            assert [r["from"] for r in mm.refusals(mesh, "lead")] == ["w1"]

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


def test_an_explicit_hold_caps_the_full_queue_at_four(home, tmp_path):
    """A delivery hold has no time limit, so its queue limit uses full depth.

    The traffic limit stops counting messages after ``door_secs``.  That
    relaxation remains valid for ordinary delivery, while a receiver held by
    its operator must still reject a fifth queued message.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            _cap(mm, "team", inbox_max=4, retry_after=45.0)
            mm.set_policy("team", {"ack_timeout": {"door_secs": 1.0}})
            lead = mgr.get("s1")
            lead.set_delivery_hold(True)

            for n in range(4):
                await mm.send("team", "w1", "lead", f"report {n}")
            mesh = mm.get("team")
            for msg in mesh.pending("lead"):
                msg["ts"] = "2000-01-01T00:00:00+00:00"

            assert mm.countable_inbox(mesh, "lead") == 0
            assert len(mesh.pending("lead")) == 4
            with pytest.raises(MeshBusy) as caught:
                await mm.send("team", "w1", "lead", "report 4")

            assert caught.value.entries == [{
                "handle": "lead",
                "queued": 4,
                "inbox_max": 4,
                "retry_after": 0.0,
                "remote": False,
                "reason": "delivery_hold",
            }]
            assert caught.value.retry_after == 0.0
            assert "delivery resumes" in str(caught.value)
            assert len(mesh.pending("lead")) == 4

            lead.set_delivery_hold(False)
            result = await mm.send("team", "w1", "lead", "report 4")
            assert result["recipients"] == ["lead"]
            assert len(mesh.pending("lead")) == 5

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


def test_a_partial_refusal_narrows_the_address_it_stores(home, tmp_path):
    """A broadcast where one recipient is full is still a send — to the
    others.

    The subtle part is the ADDRESS. The log stores ``"*"`` and delivery
    re-derives recipients from it (``Mesh.addressed_to``), so leaving the
    address alone would deliver to the refused member on the next tick and
    make the bounce a lie. The stored address is narrowed to who actually
    took it, and the sender is told who did not.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2", "s3"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            await mm.join("team", "s3", handle="w2")
            mesh = mm.get("team")
            # Everyone talks to everyone here, so a '*' from w1 addresses
            # both peers and only one of them is behind.
            _cap(mm, "team", inbox_max=1)
            await mm.send("team", "w2", "lead", "filling the lead's inbox")
            assert len(mesh.pending("lead")) == 1

            result = await mm.send("team", "w1", "*", "status ping")
            assert result["recipients"] == ["w2"]
            assert [e["handle"] for e in result["deferred"]] == ["lead"]
            assert "NOT DELIVERED" in (result["notice"] or "")

            # The stored address is the narrowed one; the refused member is
            # not addressed by it now and cannot become addressed later.
            stored = mesh.messages[-1]
            assert stored["to"] == ["w2"]
            assert mesh.addressed_to(stored, "lead") is False
            assert [m["body"] for m in mesh.pending("lead")] == [
                "filling the lead's inbox"
            ]

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


def test_a_batch_drops_only_the_refused_recipients_section(home, tmp_path):
    """A sectioned send is one message with per-recipient slices. When one
    recipient is refused, its slice goes with it — the log must not keep an
    instruction addressed to somebody who never received the message."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2", "s3"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            await mm.join("team", "s3", handle="w2")
            mesh = mm.get("team")
            _cap(mm, "team", inbox_max=1)
            await mm.send("team", "w2", "lead", "already waiting")

            result = await mm.send(
                "team", "w1", ["lead", "w2"], "shared preamble",
                sections={"lead": "do the lead thing", "w2": "do the w2 thing"},
            )
            assert result["recipients"] == ["w2"]
            assert [e["handle"] for e in result["deferred"]] == ["lead"]
            stored = mesh.messages[-1]
            assert set(stored["sections"]) == {"w2"}
            assert "do the lead thing" not in stored["body"]

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


def test_the_person_at_the_dashboard_is_never_turned_away(home, tmp_path):
    """An external send is a human, not the fan-in this gate bounds.

    They send one message and read the answer, and they already have
    "deliver now" for the backlog. Refusing a person to protect an agent's
    turn gets the priority backwards, so the door is open for them however
    deep the backlog is.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            _cap(mm, "team", inbox_max=1)
            await mm.send("team", "w1", "lead", "first")
            with pytest.raises(MeshBusy):
                await mm.send("team", "w1", "lead", "second")

            result = await mm.send("team", "operator", "lead", "from a human",
                                   external=True)
            assert result["recipients"] == ["lead"]
            assert result["deferred"] == []
            assert [m["body"] for m in mm.get("team").pending("lead")] == [
                "first", "from a human"
            ]

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


def test_turning_the_cap_off_restores_the_unbounded_queue(home, tmp_path):
    """``inbox_max: 0`` (or ``enabled: false``) is the documented way back to
    the old behaviour — a mesh that wants an unbounded queue keeps one."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            _cap(mm, "team", enabled=False, inbox_max=1)
            for n in range(5):
                await mm.send("team", "w1", "lead", f"m{n}")
            assert len(mm.get("team").pending("lead")) == 5

            _cap(mm, "team", enabled=True, inbox_max=0)
            await mm.send("team", "w1", "lead", "still fine")
            assert len(mm.get("team").pending("lead")) == 6

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


def test_a_send_that_arrived_over_the_wire_is_not_refused(home, tmp_path):
    """A guest's forward (or a resequenced outbox entry) carries an id: it
    has already been accepted somewhere. Refusing it here would not un-send
    it — it would lose it, silently — so the door only judges sends that
    originate on this daemon."""
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            mesh = mm.get("team")
            _cap(mm, "team", inbox_max=1)
            await mm.send("team", "w1", "lead", "first")

            result = mm._send_core(
                mesh, "w1", "lead", "already sequenced elsewhere",
                msg_id="msg-deadbeef0001",
            )
            assert result["deferred"] == []
            assert len(mesh.pending("lead")) == 2

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


# --------------------------------------------------------------------- #
# the pacing gate
# --------------------------------------------------------------------- #
def test_pacing_holds_a_second_block_and_coalesces_it_instead(home, tmp_path):
    """``min_gap`` bounds how OFTEN a terminal is written to, which nothing
    did before: ``busy_hold`` bounds how long ONE message waits and then
    interrupts the running turn regardless.

    So the gate is checked where it matters — after the idle-gate has been
    satisfied — and what it buys is checked too: the held message is not
    lost, it goes in with the next one as a single block.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            mm.create("team")
            for name in ("s1", "s2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            mesh = mm.get("team")
            lead = mgr.get("s1")
            await lead.wait_for("idle", timeout=10.0, threshold=0.5)
            lead.status = lambda threshold=None: session_mod.STATUS_IDLE
            _cap(mm, "team", min_gap=3600.0, inbox_max=0)

            await mm.send("team", "w1", "lead", "one")
            await mm._deliver_to(mesh, mesh.members["lead"])
            assert mesh.pending("lead") == []          # first block goes in
            assert mm.paced_for(mesh, "lead") > 0      # and starts the gate

            await mm.send("team", "w1", "lead", "two")
            await mm._deliver_to(mesh, mesh.members["lead"])
            assert [m["body"] for m in mesh.pending("lead")] == ["two"]

            # Nothing was lost: a third arrival joins the held one, and the
            # wait turned two interruptions into one block.
            await mm.send("team", "w1", "lead", "three")
            assert [m["body"] for m in mesh.pending("lead")] == ["two", "three"]

            # "deliver now" is the operator declining exactly this wait.
            await mm._deliver_to(mesh, mesh.members["lead"], force=True)
            assert mesh.pending("lead") == []

            await mgr.shutdown_all()
        finally:
            pass

    asyncio.run(run())


# --------------------------------------------------------------------- #
# what the dashboard reads
# --------------------------------------------------------------------- #
def test_the_queued_endpoint_reports_the_door_and_the_pacing(home, tmp_path):
    """The header chip and the panel are drawn from ``/queued``, so it has to
    carry both halves.

    ``state`` gains ``paced`` — last in the ladder, where the delivery gate
    puts it. ``backpressure`` is reported separately because the door is not
    a hold: a member at the cap has a backlog that has STOPPED growing,
    which looks exactly like calm from every other field, and the only way a
    person learns that senders are being turned away is if something says
    so.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name in ("s1", "s2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            await mm.join("team", "s1", handle="lead")
            await mm.join("team", "s2", handle="w1")
            mesh = mm.get("team")
            lead = mgr.get("s1")
            await lead.wait_for("idle", timeout=10.0, threshold=0.5)
            lead.status = lambda threshold=None: session_mod.STATUS_IDLE
            _cap(mm, "team", inbox_max=2, min_gap=3600.0, retry_after=30.0)

            for n in range(2):
                await mm.send("team", "w1", "lead", f"m{n}")

            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            body = await resp.json()
            bp = body["backpressure"]
            assert bp["enabled"] is True
            assert (bp["queued"], bp["congested"], bp["refused"]) == (2, True, 0)
            assert bp["handles"][0]["mesh"] == "team"
            assert bp["handles"][0]["inbox_max"] == 2

            # A refused send: 429 with the retry the sender should honour,
            # and the recipient's record grows a refusal the panel can name.
            resp = await client.post(
                "/api/mesh/team/messages",
                json={"from": "w1", "to": "lead", "body": "one too many"},
                headers=BEARER,
            )
            assert resp.status == 429
            assert resp.headers["Retry-After"] == "30"
            doc = await resp.json()
            assert [e["handle"] for e in doc["deferred"]] == ["lead"]
            assert doc["retry_after"] == 30.0
            assert "NOT DELIVERED" in doc["error"]

            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            body = await resp.json()
            assert body["backpressure"]["refused"] == 1
            assert body["backpressure"]["refused_from"] == [
                {"from": "w1", "count": 1, "ago": pytest.approx(0, abs=30)}
            ]

            # Pacing shows up in the ladder only once nothing about the
            # SESSION is holding delivery — it is the last automatic gate.
            await mm._deliver_to(mesh, mesh.members["lead"], force=True)
            resp = await client.get("/api/sessions/s1/queued", headers=BEARER)
            body = await resp.json()
            assert body["state"] == "paced"
            assert body["backpressure"]["paced_for"] > 0

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------- #
# the absolute wall: receivers whose queue cannot drain
# --------------------------------------------------------------------- #
def test_an_exited_receiver_stays_shut_after_its_mail_ages_out(home, tmp_path):
    """A dead terminal is capped on full depth, not on recent traffic.

    ``door_secs`` stops aged mail weighing on the door, which is right for a
    receiver that is merely slow: its queue drains, so the aging only
    forgives pressure that has already gone. For a receiver nothing is
    reading, the queue never drains and the aging reopened the door once per
    ``door_secs`` forever — ``inbox_max`` more messages each time, with no
    ceiling. The backlog that revealed it was 45 deep and would have been
    typed into that terminal in one block on respawn.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        _cap(mm, "team", inbox_max=4, retry_after=45.0)
        mm.set_policy("team", {"ack_timeout": {"door_secs": 1.0}})

        for n in range(4):
            await mm.send("team", "w1", "lead", f"report {n}")
        mesh = mm.get("team")
        # Paused rather than killed to pin the state the report came from;
        # the two are the same record to delivery, which the test below
        # holds on to.
        mgr.pause("s1", force=True)
        await _await_exit(mgr, "s1")
        for msg in mesh.pending("lead"):
            msg["ts"] = "2000-01-01T00:00:00+00:00"

        # The traffic window is empty and the true depth is not: before the
        # wall, this pair is exactly what let a fifth message in.
        assert mm.countable_inbox(mesh, "lead") == 0
        assert len(mesh.pending("lead")) == 4

        with pytest.raises(MeshBusy) as caught:
            await mm.send("team", "w1", "lead", "report 4")

        assert caught.value.entries == [{
            "handle": "lead",
            "queued": 4,
            "inbox_max": 4,
            "retry_after": 0.0,
            "remote": False,
            "reason": "exited",
        }]
        # Waiting never opens this door, so the notice names the remedy that
        # does instead of a number of seconds.
        assert caught.value.retry_after == 0.0
        # Lowercased for the compare only: the notice capitalises its first
        # letter, and which action lands first is not what this test is for.
        assert "respawn lead" in str(caught.value).lower()
        assert "45s" not in str(caught.value)
        # Refused means refused: nothing may land behind the sender's back.
        assert len(mesh.pending("lead")) == 4

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_wall_does_not_ask_whether_the_session_was_killed_or_paused(
    home, tmp_path
):
    """Kill and pause are one state to delivery, so they are one state here.

    ``_deliver_to`` branches on ``session.exited`` alone and never reads
    ``paused_at``; ``respawn`` accepts either record. A cap that told them
    apart would be a distinction the delivery path does not make.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2", "s3"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="killed")
        await mm.join("team", "s2", handle="paused")
        await mm.join("team", "s3", handle="w1")
        _cap(mm, "team", inbox_max=4, retry_after=45.0)
        mm.set_policy("team", {"ack_timeout": {"door_secs": 1.0}})

        for handle in ("killed", "paused"):
            for n in range(4):
                await mm.send("team", "w1", handle, f"report {n}")
        mesh = mm.get("team")
        mgr.kill("s1", force=True)
        mgr.pause("s2", force=True)
        await _await_exit(mgr, "s1", "s2")
        for handle in ("killed", "paused"):
            for msg in mesh.pending(handle):
                msg["ts"] = "2000-01-01T00:00:00+00:00"

        assert getattr(mgr.get("s1"), "paused_at", None) is None
        assert getattr(mgr.get("s2"), "paused_at", None) is not None

        refused = mm.congested_recipients(mesh, ["killed", "paused"])
        assert [e["handle"] for e in refused] == ["killed", "paused"]
        assert {e["reason"] for e in refused} == {"exited"}
        assert {e["queued"] for e in refused} == {4}

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_wall_is_the_operators_cap_and_not_a_constant(home, tmp_path):
    """A dead receiver is weighed on full depth against ``inbox_max``.

    Which depth the door weighs is what the receiver's state decides; the
    number it is weighed against stays the one the operator set. A mesh that
    raised the cap to accept long backlogs still accepts them from a
    terminal that has died.
    """
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        _cap(mm, "team", inbox_max=8, retry_after=45.0)
        mm.set_policy("team", {"ack_timeout": {"door_secs": 1.0}})

        for n in range(6):
            await mm.send("team", "w1", "lead", f"report {n}")
        mesh = mm.get("team")
        mgr.kill("s1", force=True)
        await _await_exit(mgr, "s1")
        for msg in mesh.pending("lead"):
            msg["ts"] = "2000-01-01T00:00:00+00:00"

        # Six deep under a cap of eight: still accepting, aged mail or not.
        assert mm.congested_recipients(mesh, ["lead"]) == []
        for n in range(6, 8):
            await mm.send("team", "w1", "lead", f"report {n}")
        assert len(mesh.pending("lead")) == 8

        with pytest.raises(MeshBusy) as caught:
            await mm.send("team", "w1", "lead", "report 8")
        assert caught.value.entries[0]["inbox_max"] == 8
        assert caught.value.entries[0]["queued"] == 8
        assert caught.value.entries[0]["reason"] == "exited"

        await mgr.shutdown_all()

    asyncio.run(run())
