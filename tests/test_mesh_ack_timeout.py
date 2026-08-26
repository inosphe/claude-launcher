"""The ack timeout: a clock on mail the mesh had only ever accounted for.

Every ledger in ``mesh.py`` counted; none of them aged. A delivered question
stayed owed until the member answered it or an operator wrote it off by hand,
and a queued message kept weighing on ``backpressure.inbox_max`` until it was
delivered. Both closures need the member's terminal to still be there.

When it is not, neither can ever happen. The delivery worker holds an exited
member's cursor and returns, so its ``pending`` never falls; past the cap,
``_send_core`` refuses every later 1:1 send with :class:`MeshBusy` and queues
nothing. That is a wall, not a gate — it never comes down, and the bounce a
sender reads ("wait about 90s and re-send") is advice that cannot come true.

This was observed, not imagined. On the mesh these tests were written for,
four members sat past a cap of four with their sessions exited; the leader hit
the refusal three times, recorded the cause three different ways, and spent 73
minutes waiting on a report that had no path to arrive.

What the tests below hold on to:

* the door reopens ON ITS OWN once mail ages past ``door_secs``, and the aged
  mail is still queued — the timeout forgives the CAP, it does not drop mail;
* a debt past ``owed_secs`` leaves the ledger, so a question nobody can answer
  stops being counted against a member forever;
* the nudger and the ledger expire together, because a heartbeat chasing a
  debt the dashboard has written off is the disagreement ``Mesh.owed``'s
  docstring exists to forbid;
* a joining member inherits none of the log it arrived after.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher import store
from claude_launcher.daemon import mesh_policy
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshBusy, MeshManager, utcnow

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


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _wait_screen(session, needle: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in "\n".join(session.capture()):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{needle!r} never appeared on screen")


def _age(mesh, handle: str, secs: float) -> None:
    """Backdate everything queued for ``handle`` by ``secs``.

    The clock is read from the message's own ``ts`` rather than a monotonic
    stamp, precisely so that ageing is expressible: the debts this timeout
    exists for outlive daemon restarts, and a monotonic timer would forget
    them at the moment they matter most.
    """
    then = (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat(
        timespec="seconds"
    )
    for msg in mesh.pending(handle):
        msg["ts"] = then


# --------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------- #
def test_the_section_ships_on_because_the_gate_it_releases_does():
    """``backpressure`` is enabled by default; shipping its release valve
    disabled is what turned a pacing gate into a permanent wall.

    Both waits validate as durations, and 0 is the documented "never
    expires" rather than a rejected value — that IS the old behaviour, and a
    mesh must be able to ask for it.
    """
    at = mesh_policy.default_policy()["ack_timeout"]
    assert at["enabled"] is True
    assert at["owed_secs"] > 0 and at["door_secs"] > 0

    base = mesh_policy.default_policy()
    got = mesh_policy.merge_policy(base, {"ack_timeout": {"door_secs": 30.0}})
    assert got["ack_timeout"]["door_secs"] == 30.0
    assert got["ack_timeout"]["enabled"] is True      # untouched keys survive

    off = mesh_policy.merge_policy(
        base, {"ack_timeout": {"owed_secs": 0, "door_secs": 0}}
    )
    assert off["ack_timeout"]["owed_secs"] == 0.0
    assert off["ack_timeout"]["door_secs"] == 0.0

    for bad in ({"owed_secs": "soon"}, {"door_secs": -1}, {"nope": 1}):
        with pytest.raises(mesh_policy.PolicyError):
            mesh_policy.merge_policy(base, {"ack_timeout": bad})

    # A mesh.json from before the section existed still loads, and loads WITH
    # it: an old file must not silently keep the wall.
    old = mesh_policy.load_policy({"backpressure": {"inbox_max": 3}})
    assert old["ack_timeout"] == mesh_policy.default_policy()["ack_timeout"]


# --------------------------------------------------------------------- #
# the door
# --------------------------------------------------------------------- #
def test_an_exited_member_stops_holding_the_door_shut(home, tmp_path):
    """The deadlock, and its release.

    A member whose session has exited can never drain its queue, so before
    the timeout the cap was permanent: every later 1:1 send refused, nothing
    queued, no path back. Here the same queue ages past ``door_secs`` and the
    door opens by itself — while the aged mail stays exactly where it was,
    because respawning that session must still deliver it.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        # No delivery worker: everything sent stays pending, which is how an
        # exited member's queue behaves anyway (the worker holds its cursor).
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        mm.set_policy("team", {
            "backpressure": {"inbox_max": 2, "retry_after": 45.0},
            "ack_timeout": {"door_secs": 600.0},
        })
        mesh = mm.get("team")

        for n in range(2):
            await mm.send("team", "lead", "w1", f"do task {n}")
        assert len(mesh.pending("w1")) == 2

        # The wall: at the cap, the next send is refused and not queued.
        with pytest.raises(MeshBusy) as caught:
            await mm.send("team", "lead", "w1", "and this one")
        assert caught.value.entries[0]["queued"] == 2
        assert "Nothing was queued" in str(caught.value)
        assert len(mesh.messages) == 2

        # Nothing about that changes on its own while the mail is fresh.
        with pytest.raises(MeshBusy):
            await mm.send("team", "lead", "w1", "still nothing")

        # Age it past the door. Only the CAP forgives — the mail is untouched.
        _age(mesh, "w1", 900.0)
        assert mm.inbox_depth(mesh, "w1") == 2      # still waiting for it
        assert mm.countable_inbox(mesh, "w1") == 0  # but no longer weighed
        assert mm.congested_recipients(mesh, ["w1"]) == []

        got = await mm.send("team", "lead", "w1", "reaches it now")
        assert got["recipients"] == ["w1"]
        # Queued behind the aged mail, in order: the older messages were
        # never dropped, so a respawn still gets all three.
        assert [m["body"] for m in mesh.pending("w1")] == [
            "do task 0", "do task 1", "reaches it now",
        ]

        # And the bucket refills: one fresh message is under a cap of two,
        # two is at it. A dead terminal takes inbox_max per door_secs
        # instead of inbox_max ever.
        await mm.send("team", "lead", "w1", "one more")
        with pytest.raises(MeshBusy):
            await mm.send("team", "lead", "w1", "over again")

        await mgr.shutdown_all()

    asyncio.run(run())


def test_turning_the_door_clock_off_restores_the_permanent_wall(home, tmp_path):
    """``door_secs: 0`` is the pre-timeout behaviour, and a mesh may ask for
    it — but it must be an explicit choice, not the default it used to be."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        mm.set_policy("team", {
            "backpressure": {"inbox_max": 2},
            "ack_timeout": {"door_secs": 0},
        })
        mesh = mm.get("team")

        for n in range(2):
            await mm.send("team", "lead", "w1", f"m{n}")
        _age(mesh, "w1", 86_000.0)  # a day old and still weighed
        assert mm.countable_inbox(mesh, "w1") == 2
        with pytest.raises(MeshBusy):
            await mm.send("team", "lead", "w1", "refused forever")

        await mgr.shutdown_all()

    asyncio.run(run())


def test_undatable_mail_is_never_forgiven_by_either_clock(home, tmp_path):
    """A message the clock cannot date is counted, not expired.

    A timeout that forgave what it could not measure would turn a parsing
    difference (an older daemon, a hand-edited log) into silently dropped
    backpressure — failing open on exactly the evidence it needs.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        mm.set_policy("team", {
            "backpressure": {"inbox_max": 2},
            "ack_timeout": {"door_secs": 1.0},
        })
        mesh = mm.get("team")

        for n in range(2):
            await mm.send("team", "lead", "w1", f"m{n}")
        for msg in mesh.pending("w1"):
            msg["ts"] = "not-a-timestamp"
        assert mm.countable_inbox(mesh, "w1") == 2
        with pytest.raises(MeshBusy):
            await mm.send("team", "lead", "w1", "refused")

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------- #
def test_a_debt_nobody_can_discharge_leaves_the_ledger(home, tmp_path):
    """``owed`` closed only on the member speaking or an operator dismissing.

    An exited member can do neither, so its debts were permanent: the leader
    reading ``mesh owed`` saw obligations against sessions that no longer
    exist, mixed in with the ones that still matter. The clock is the third
    door, and it is the one that does not need a human.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        mm.start()
        mm.create("team")
        a = mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        mesh = mm.get("team")
        mm.set_policy("team", {"ack_timeout": {"owed_secs": 600.0}})

        await mm.send("team", "lead", "w1", "what is your number?", type="ask")
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and mesh.pending("w1"):
            await asyncio.sleep(0.1)
        assert not mesh.pending("w1"), "the ask never reached the terminal"
        assert [m["body"] for m in mesh.owed("w1")] == ["what is your number?"]

        # The member goes; the question stays, with no way left to answer it.
        await b.send_keys(["quit", "Enter"])
        await b.wait_for("exited", timeout=10.0, threshold=0.5)
        assert len(mesh.owed("w1")) == 1

        # Aged past owed_secs it is written off — the same closure a
        # dismissal is, arrived at by the clock instead of by hand. The
        # member's join is moved back with it: the real ordering is join,
        # then the question, then time passing, and a question backdated
        # past its own recipient's arrival would be excluded by the join
        # floor instead of by the clock under test.
        mesh.members["w1"].joined_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1800)
        ).isoformat(timespec="seconds")
        for msg in mesh.owed_all("w1"):
            msg["ts"] = (
                datetime.now(timezone.utc) - timedelta(seconds=900)
            ).isoformat(timespec="seconds")
        assert mesh.owed("w1") == []
        # owed_all is deliberately unaged: it is the window a dismissal set
        # is pruned against, and pruning against an expiring view would
        # forget write-offs that are still doing work.
        assert len(mesh.owed_all("w1")) == 1

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_nudger_and_the_ledger_expire_together(home, tmp_path):
    """``Mesh.owed``'s docstring forbids the two disagreeing.

    It argued the case one way — the ledger must not claim a debt the
    heartbeat is not chasing. The timeout opens the other: once the clock
    writes a debt off, a heartbeat still chasing it would nudge a member
    about mail the dashboard says it does not owe. The activity report is
    what the policy engine reads, so it is where they are held together.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        mm.start()
        mm.create("team")
        a = mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        mesh = mm.get("team")
        mm.set_policy("team", {"ack_timeout": {"owed_secs": 600.0}})

        await mm.send("team", "lead", "w1", "answer me", type="ask")
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and mesh.pending("w1"):
            await asyncio.sleep(0.1)
        assert not mesh.pending("w1")
        assert mm._activity_report(mesh)["w1"]["unanswered"] is True

        mesh.members["w1"].joined_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1800)
        ).isoformat(timespec="seconds")
        for msg in mesh.owed_all("w1"):
            msg["ts"] = (
                datetime.now(timezone.utc) - timedelta(seconds=900)
            ).isoformat(timespec="seconds")
        report = mm._activity_report(mesh)["w1"]
        assert mesh.owed("w1") == []
        assert report["unanswered"] is False, "the heartbeat outlived the debt"
        assert report["owed"] == 0

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_joiner_owes_nothing_from_before_it_arrived(home, tmp_path):
    """A member's cursor jumps to the end of the log when it joins, which
    made every earlier message read as "already delivered" to it.

    ``owed_all`` then walked back to the member's own last send — and a
    member that has never spoken has no such floor, so it was charged with
    the entire history it arrived after. Observed as a session fifteen
    minutes old owing seven messages, the oldest sent seven hours before it
    existed. Its join is the floor.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2", "s3"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        mesh = mm.get("team")

        # History the newcomer was not here for, broadcast and reply-expecting.
        for n in range(3):
            await mm.send("team", "lead", "*", f"old notice {n}", type="ask")
        old = (
            datetime.now(timezone.utc) - timedelta(seconds=3600)
        ).isoformat(timespec="seconds")
        for msg in mesh.messages:
            msg["ts"] = old

        await mm.join("team", "s3", handle="w2")
        assert mesh.cursors["w2"] == len(mesh.messages)  # the jump itself
        assert mesh.owed("w2") == [], "a joiner inherited the backlog as debt"

        # It does owe what arrives AFTER it joins.
        await mm.send("team", "lead", "w2", "welcome, respond", type="ask")
        mesh.cursors["w2"] = len(mesh.messages)  # as delivery would
        assert [m["body"] for m in mesh.owed("w2")] == ["welcome, respond"]

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------- #
# the heartbeat
# --------------------------------------------------------------------- #
def test_the_heartbeat_finally_has_a_condition_under_which_it_gives_up(
    home, tmp_path, monkeypatch
):
    """Before the timeout the heartbeat chased forever.

    It backs off — doubling to ``max_interval`` — but backing off is not
    stopping, and nothing in the loop ever concluded that an answer was not
    coming. The only exits were the member speaking, an operator dismissing
    the mail, or the edge being cut; the first is impossible for a member
    whose session has gone, and the other two need a human. So the nudge
    outlived every debt it was sent about.

    It now stops when the ledger writes the debt off, because ``unanswered``
    is the watermark CONFIRMED against ``Mesh.owed`` rather than the
    watermark alone. That is the same settlement ``_mark_member_edge`` and
    ``dismiss`` perform by hand at their mutation sites — generalised,
    because expiry has no mutation site to hook: it happens by time passing.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        mm.start()
        mm.create("hb")
        a = mgr.create(SessionDef(name="a1", harness="py", cwd=str(tmp_path)))
        b = mgr.create(SessionDef(name="b1", harness="py", cwd=str(tmp_path)))
        await _wait_screen(a, "READY")
        await _wait_screen(b, "READY")
        await mm.join("hb", "a1", handle="leader")
        await mm.join("hb", "b1", handle="worker_b")
        mesh = mm.get("hb")
        mm.set_policy("hb", {
            "heartbeat": {"enabled": True, "interval": 1.0},
            "ack_timeout": {"owed_secs": 600.0},
        })

        fired: list = []
        monkeypatch.setattr(
            mesh_policy, "dispatch",
            lambda *a, **k: fired.append(a[4]) or asyncio.sleep(0),
        )

        await mm.send("hb", "leader", "worker_b", "please reply", type="ask")
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and mesh.pending("worker_b"):
            await asyncio.sleep(0.1)
        assert not mesh.pending("worker_b")
        await mgr.get("b1").wait_for("idle", timeout=20.0, threshold=0.5)

        # It chases while the debt stands.
        mesh.activity.setdefault("worker_b", {"anchor": 0.0})["hb_next"] = 0.0
        await mesh_policy.tick(mm, mesh)
        assert fired == ["heartbeat"], "the heartbeat never armed"

        # Age the debt past owed_secs. The member still has not spoken, no
        # operator has touched anything, and the edge is intact — the three
        # closures that existed before are all unavailable here.
        mesh.members["worker_b"].joined_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1800)
        ).isoformat(timespec="seconds")
        for msg in mesh.owed_all("worker_b"):
            msg["ts"] = (
                datetime.now(timezone.utc) - timedelta(seconds=900)
            ).isoformat(timespec="seconds")
        assert mesh.owed("worker_b") == []

        fired.clear()
        mesh.activity["worker_b"]["hb_next"] = 0.0
        await mesh_policy.tick(mm, mesh)
        assert fired == [], "the heartbeat outlived the debt it was sent about"

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())
