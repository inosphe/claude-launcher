"""Report timers an agent sets for itself (claunch-lvrcp).

Covers the ledger in ``daemon/agent_timers.py`` (validation, fires, the end of
a bounded timer), the pause across a daemon that was not running, the exit
hook that drops a killed session's timers but keeps a held or paused one's,
the ``agent`` provider behind the terminal's timer line, delivery to a live,
held and paused session, and the ``POST /api/sessions/<name>/timers`` route.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import agent_timers as at
from claude_launcher.daemon import session_timers as st
from claude_launcher.daemon import manager as manager_mod


def _session(name="s1", *, exited=False, held=False, paused=False):
    return SimpleNamespace(
        sdef=SimpleNamespace(name=name),
        exited=exited,
        held_at="held" if held else None,
        paused_at="paused" if paused else None,
        resumed_by_human=False,
    )


# -- the ledger ----------------------------------------------------------------------------

def test_add_validates_the_ask(home):
    with pytest.raises(ValueError):
        at.add("s1", every=at.MIN_EVERY - 1, times=1, message="go")
    with pytest.raises(ValueError):
        at.add("s1", every=at.MAX_EVERY + 1, times=1, message="go")
    with pytest.raises(ValueError):
        at.add("s1", every=60, times=0, message="go")
    with pytest.raises(ValueError):
        at.add("s1", every=60, times=1.5, message="go")
    with pytest.raises(ValueError):
        at.add("s1", every=60, times=at.MAX_TIMES + 1, message="go")
    with pytest.raises(ValueError):
        at.add("s1", every=60, times=1, message="   ")
    with pytest.raises(ValueError):
        at.add("s1", every=60, times=1, message="x" * (at.MAX_MESSAGE + 1))
    with pytest.raises(ValueError):
        at.add("s1", every="60", times=1, message="go")
    assert at.load("s1") == []


def test_add_records_the_first_fire_one_interval_out(home):
    record = at.add("s1", every=600, times=3, message="  report now  ",
                    now=1000.0, by="s1")
    assert record["every"] == 600.0
    assert record["times"] == 3
    assert record["fires"] == 0
    assert record["message"] == "report now"
    assert record["next_due"] == 1600.0
    assert record["created_at"] == 1000.0
    assert at.ledger_file("s1").exists()
    assert at.session_names() == ["s1"]
    assert at.load("s1")[0]["id"] == record["id"]
    # An empty ledger file is removed rather than left behind.
    at.remove_session("s1")
    assert not at.ledger_file("s1").exists()


def test_a_session_may_not_stack_more_than_the_limit(home):
    for _ in range(at.MAX_ACTIVE):
        at.add("s1", every=60, times=1, message="go", now=1000.0)
    with pytest.raises(ValueError, match="limit"):
        at.add("s1", every=60, times=1, message="go", now=1000.0)
    # Another session is unaffected.
    at.add("s2", every=60, times=1, message="go", now=1000.0)


# -- firing --------------------------------------------------------------------------------

def test_scan_fires_counts_and_ends_a_bounded_timer(home):
    at.add("s1", every=60, times=2, message="report", now=1000.0)
    clock = at.TimerClock(None)

    assert clock.scan(now=1059.0) == []          # not due yet
    fired = clock.scan(now=1060.0)
    assert [name for name, _ in fired] == ["s1"]
    record = fired[0][1][0]
    assert record["fires"] == 1
    # The next fire is one interval from now, not a catch-up burst.
    assert record["next_due"] == 1120.0
    assert at.load("s1")[0]["fires"] == 1

    fired = clock.scan(now=1400.0)               # long past due
    assert fired[0][1][0]["fires"] == 2
    assert at.load("s1") == []                   # bounded timer is gone
    assert not at.ledger_file("s1").exists()

    clock.scan(now=2000.0)
    assert at.load("s1") == []


def test_scan_leaves_the_other_session_alone(home):
    at.add("a", every=60, times=5, message="go", now=1000.0)
    at.add("b", every=600, times=5, message="go", now=1000.0)
    fired = at.TimerClock(None).scan(now=1100.0)
    assert [name for name, _ in fired] == ["a"]
    assert at.load("b")[0]["fires"] == 0


def test_fire_block_is_framed_as_machine_generated():
    record = {"id": "t-1", "every": 60.0, "times": 2, "fires": 2,
              "message": "status report please"}
    block = at.fire_block(record)
    assert "machine-generated, not typed by the user" in block
    assert "t-1" in block and "2/2 (last)" in block
    assert "message: status report please" in block
    first = at.fire_block({**record, "fires": 1})
    assert "1/2" in first and "(last)" not in first


# -- the daemon's downtime is paused time --------------------------------------------------

def test_resume_after_pause_shifts_every_timer_by_the_downtime(home):
    at.add("s1", every=600, times=3, message="go", now=1000.0)
    at.add("s2", every=600, times=3, message="go", now=1000.0)

    at._beat(1010.0)                                   # clock was alive here
    paused = at.resume_after_pause(now=1110.0)
    assert paused == 100.0
    # 100 s of the interval passed before the daemon stopped; the 100 s it
    # was down do not count, so the timer still has 500 s to go.
    assert at.load("s1")[0]["next_due"] == 1600.0 + 100.0
    assert at.load("s2")[0]["next_due"] == 1600.0 + 100.0
    at.resume_after_pause(now=1110.0)                  # idempotent at the same clock
    assert at.load("s1")[0]["next_due"] == 1700.0


def test_creating_a_timer_is_a_live_moment_for_the_heartbeat(home):
    at._beat(1010.0)
    at.add("s1", every=600, times=3, message="go", now=20000.0)
    assert at._last_beat() == 20000.0


def test_a_new_timer_is_not_billed_for_the_daemons_idle_time(home):
    """claunch-lvrcp review: the heartbeat stopped while no timer existed.

    A daemon up for hours without a timer carries a heartbeat from hours ago;
    a timer made 15 s before the stop must be paused for its own short life,
    not for the whole idle stretch (which pushed its first fire out by hours).
    """
    at._beat(1010.0)                                   # the last pass, long ago
    at.add("s1", every=600, times=3, message="go", now=20000.0)
    assert at.load("s1")[0]["next_due"] == 20600.0

    paused = at.resume_after_pause(now=30000.0)        # down from 20005 to 30000
    assert paused == 10000.0
    assert at.load("s1")[0]["next_due"] == 30600.0


def test_a_timer_never_shifts_more_than_it_has_existed(home):
    """The bound the fix rests on: a stale beat cannot bill a timer for time
    before it existed. The ledger here is written without the creation beat
    (:func:`agent_timers.add` writes one), so only the clamp can hold."""
    at._save("s1", [{
        "id": "t-old", "every": 600.0, "times": 3, "fires": 0,
        "message": "go", "created_at": 20000.0, "next_due": 20600.0,
        "created_by": "s1",
    }])
    at._beat(1010.0)

    assert at.resume_after_pause(now=30000.0) == 28990.0   # measured, as before
    assert at.load("s1")[0]["next_due"] == 30600.0         # but only 10000 moved


def test_an_unreadable_heartbeat_files_no_shift(home):
    at.add("s1", every=600, times=3, message="go", now=1000.0)
    at.heartbeat_file().parent.mkdir(parents=True, exist_ok=True)
    at.heartbeat_file().write_text("not json", encoding="utf-8")
    assert at.resume_after_pause(now=9999.0) == 0.0
    assert at.load("s1")[0]["next_due"] == 1600.0


# -- the session's ending ------------------------------------------------------------------

def test_exit_hook_drops_a_killed_sessions_timers(home):
    at.add("s1", every=60, times=3, message="go", now=1000.0)
    at.session_ended(_session("s1", exited=True))
    assert not at.ledger_file("s1").exists()


@pytest.mark.parametrize("kind", ["held", "paused"])
def test_exit_hook_keeps_the_timers_of_a_hold_or_pause(home, kind):
    at.add("s1", every=60, times=3, message="go", now=1000.0)
    at.session_ended(_session("s1", exited=True, **{kind: True}))
    assert at.load("s1"), "a hold or a pause is not the session's ending"


# -- the timer line ------------------------------------------------------------------------

def test_the_agent_provider_is_on_the_timer_line(home):
    assert "agent" in st.PROVIDERS
    at.add("s1", every=600, times=4, message="Write a short status report now",
           now=1000.0)
    out = st.collect("s1", "", now=1100.0)
    assert out["errors"] == []
    assert [(t["source"], t["kind"], t["state"]) for t in out["timers"]] == [
        ("agent", "timer", "waiting")]
    timer = out["timers"][0]
    assert timer["due"] == 1600.0
    assert timer["since"] == 1000.0
    assert timer["label"].startswith("Write a short status")
    assert "fire 1/4" in timer["detail"]


def test_strip_timers_copes_with_an_empty_ledger(home):
    assert at.strip_timers("nobody") == []


# -- delivery ------------------------------------------------------------------------------

class FakeSession:
    def __init__(self, name="s1", *, exited=False, held=False, paused=False):
        self.sdef = SimpleNamespace(name=name)
        self.exited = exited
        self.held_at = "held" if held else None
        self.paused_at = "paused" if paused else None
        self.resumed_by_human = False
        self.delivered = []

    def queue_delivery(self, text, **kwargs):
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self, session, *, relaunch=None):
        self.session = session
        self.relaunch = relaunch
        self.woken = []

    def get(self, name):
        if name != self.session.sdef.name:
            raise KeyError(name)
        return self.session

    async def respawn_async(self, name):
        self.woken.append(name)
        return self.relaunch


def _record():
    return {"id": "t-abc", "every": 60.0, "times": 3, "fires": 1,
            "message": "report in", "created_at": 1000.0, "next_due": 1060.0}


def test_delivery_to_a_live_session_types_the_block(home):
    session = FakeSession()
    asyncio.run(at.TimerClock(FakeManager(session))._deliver("s1", [_record()]))
    assert len(session.delivered) == 1
    assert "message: report in" in session.delivered[0]


def test_delivery_to_a_held_session_leaves_the_wake_to_the_queue(home):
    session = FakeSession(exited=True, held=True)
    manager = FakeManager(session)
    asyncio.run(at.TimerClock(manager)._deliver("s1", [_record()]))
    assert manager.woken == []            # queue_delivery's wake path takes it
    assert session.delivered == [at.fire_block(_record())]


def test_delivery_to_a_paused_session_respawns_it_first(home):
    paused = FakeSession(exited=True, paused=True)
    relaunched = FakeSession()
    manager = FakeManager(paused, relaunch=relaunched)
    asyncio.run(at.TimerClock(manager)._deliver("s1", [_record()]))
    assert manager.woken == ["s1"]
    assert relaunched.resumed_by_human is False   # the daemon's clock, not a person
    assert len(relaunched.delivered) == 1


def test_delivery_to_a_killed_session_delivers_nothing(home):
    killed = FakeSession(exited=True)
    manager = FakeManager(killed)
    asyncio.run(at.TimerClock(manager)._deliver("s1", [_record()]))
    assert killed.delivered == []
    assert manager.woken == []


def test_the_timer_wake_is_the_daemons_not_a_persons():
    assert "timer" in manager_mod.AUTOMATIC_WAKES


# -- the route -----------------------------------------------------------------------------

def test_post_creates_a_timer_and_the_line_shows_it(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        manager = SessionManager(idle_threshold=0.5, scrollback=100,
                                 restore_default=False)
        known = {"a": SimpleNamespace(sdef=SimpleNamespace(name="a", cwd=str(tmp_path)))}
        real_get = manager.get
        manager.get = lambda name: known[name] if name in known else real_get(name)
        app = build_app(manager, "secret", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer secret"}
        try:
            body = {"every": 120, "times": 3, "message": "status please"}
            assert (await client.post("/api/sessions/a/timers", json=body)).status == 401

            resp = await client.post("/api/sessions/a/timers", json=body, headers=headers)
            assert resp.status == 201
            created = (await resp.json())["timer"]
            assert created["every"] == 120.0 and created["times"] == 3
            assert created["fires"] == 0

            bad = await client.post(
                "/api/sessions/a/timers",
                json={"every": 1, "times": 3, "message": "x"}, headers=headers)
            assert bad.status == 400
            assert "every" in (await bad.json())["error"]

            line = await client.get("/api/sessions/a/timers", headers=headers)
            assert line.status == 200
            timers = (await line.json())["timers"]
            assert [(t["source"], t["kind"]) for t in timers] == [("agent", "timer")]

            missing = await client.post("/api/sessions/zz/timers", json=body,
                                        headers=headers)
            assert missing.status == 400
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())
    assert json.loads(at.ledger_file("a").read_text(encoding="utf-8"))["timers"]
