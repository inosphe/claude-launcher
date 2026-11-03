"""The automatic key switch on a usage limit (claunch-d2t79).

Two halves, tested apart:

* the *rule* -- which key a session is moved onto
  (:func:`profile_keys.switch_candidate`), which is "usage min" over the keys
  that can still be spent, and which is pure once the usage reading is
  stubbed;
* the *clock* (:mod:`daemon.key_switch`), which spends one restart per fault,
  types ``continue`` afterwards, and gives the session back to the fault pause
  when it has nothing left to try.

Nothing here reads a real account: ``usage.health_for_token`` is replaced by a
dict lookup, so the tests state the readings instead of asking for them.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from claude_launcher import credentials, profile as profile_mod, profile_keys, usage
from claude_launcher.daemon import key_switch
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.session import STATUS_IDLE

TOKENS = ("sk-AAAA1111BBBB", "sk-CCCC2222DDDD", "sk-EEEE3333FFFF")


# --------------------------------------------------------------------------- #
# the reading: what makes a key unusable
# --------------------------------------------------------------------------- #
def _report(*windows):
    return usage.UsageReport(
        windows=[
            usage.UsageWindow(name=name, utilization=value, resets_at=None, status=status)
            for name, value, status in windows
        ],
        raw={},
    )


def test_health_keeps_the_peak_of_the_windows():
    health = usage.health_for_report(
        _report(("five_hour", 12.0, None), ("seven_day", 47.0, None))
    )

    assert health.peak == 47.0
    assert health.usable


def test_health_marks_the_window_that_is_spent():
    health = usage.health_for_report(
        _report(("five_hour", 12.0, None), ("seven_day", 100.0, None))
    )

    assert not health.usable
    assert health.blocked == "seven_day"
    # The peak is still reported: a record that only says "blocked" cannot be
    # compared with the other keys when the operator reads the log.
    assert health.peak == 100.0


def test_a_status_word_can_spend_a_window_below_100_percent():
    health = usage.health_for_report(_report(("five_hour", 40.0, "rate_limited")))

    assert not health.usable
    assert health.blocked == "five_hour"


def test_a_spent_dollar_balance_spends_the_window():
    report = usage.UsageReport(
        windows=[
            usage.UsageWindow(
                name="monthly_spend", utilization=88.0, resets_at=None,
                remaining_dollars=0.0,
            )
        ],
        raw={},
    )

    assert not usage.health_for_report(report).usable


def test_a_window_the_reader_did_not_rate_is_not_spent():
    """``detail`` is the "counted, not rated" line -- it is not a spent window."""
    health = usage.health_for_report(_report(("monthly_spend", 0.0, None)))

    assert health.usable
    assert health.peak == 0.0


def test_window_rank_puts_the_five_hour_window_first():
    assert usage.window_rank("five_hour") == 0
    assert usage.window_rank("5h") == 0
    assert usage.window_rank("seven_day") == 1
    assert usage.window_rank("7d") == 1
    assert usage.window_rank("monthly_spend") == 2
    assert usage.window_rank("credit") == 3


# --------------------------------------------------------------------------- #
# the rule: which key
# --------------------------------------------------------------------------- #
def _pooled(name="p1", count=2):
    prof = profile_mod.create(name)
    credentials.save_token(prof, TOKENS[0])
    for token in TOKENS[1:count]:
        profile_keys.add(prof, token)
    pool = profile_keys.load(prof)
    return prof, [key.id for key in pool.keys]


def _healths(monkeypatch, table):
    """Answer ``usage.health_for_token`` from ``table`` keyed by the token."""
    def fake(profile, token):
        return table.get(token)

    monkeypatch.setattr(usage, "health_for_token", fake)


def test_the_lowest_peak_among_the_usable_keys_wins(home, monkeypatch):
    prof, ids = _pooled(count=3)
    _healths(monkeypatch, {
        TOKENS[0]: usage.KeyHealth(peak=90.0),
        TOKENS[1]: usage.KeyHealth(peak=20.0),
        TOKENS[2]: usage.KeyHealth(peak=55.0),
    })

    picked = profile_keys.switch_candidate(prof, current_key_id=ids[0])

    assert picked.key_id == ids[1]
    assert picked.reason == ""


def test_a_spent_key_is_never_chosen_however_low_its_peak(home, monkeypatch):
    """The whole difference from a plain usage-min launch pick."""
    prof, ids = _pooled(count=3)
    _healths(monkeypatch, {
        TOKENS[0]: usage.KeyHealth(peak=100.0, blocked="five_hour"),
        TOKENS[1]: usage.KeyHealth(peak=10.0, blocked="seven_day"),
        TOKENS[2]: usage.KeyHealth(peak=80.0),
    })

    picked = profile_keys.switch_candidate(prof, current_key_id=ids[0])

    assert picked.key_id == ids[2]


def test_the_key_in_use_is_not_offered_back(home, monkeypatch):
    prof, ids = _pooled(count=2)
    _healths(monkeypatch, {
        TOKENS[0]: usage.KeyHealth(peak=5.0),
        TOKENS[1]: usage.KeyHealth(peak=60.0),
    })

    picked = profile_keys.switch_candidate(prof, current_key_id=ids[0])

    assert picked.key_id == ids[1]


def test_the_shorter_window_breaks_a_tie(home, monkeypatch):
    """Equal peaks, different shapes: the key that frees up sooner wins."""
    prof, ids = _pooled(count=3)
    _healths(monkeypatch, {
        TOKENS[0]: usage.KeyHealth(peak=100.0, blocked="five_hour"),
        # 50 everywhere: free on the 5-hour window before the other one.
        TOKENS[1]: usage.KeyHealth(
            peak=50.0, windows=(("five_hour", 90.0), ("seven_day", 10.0))),
        TOKENS[2]: usage.KeyHealth(
            peak=50.0, windows=(("five_hour", 10.0), ("seven_day", 90.0))),
    })

    picked = profile_keys.switch_candidate(prof, current_key_id=ids[0])

    assert picked.key_id == ids[2]


def test_nothing_usable_says_which_way_the_pool_came_up_empty(home, monkeypatch):
    prof, ids = _pooled(count=2)
    _healths(monkeypatch, {
        TOKENS[0]: usage.KeyHealth(peak=100.0, blocked="five_hour"),
        TOKENS[1]: usage.KeyHealth(peak=100.0, blocked="seven_day"),
    })

    picked = profile_keys.switch_candidate(prof, current_key_id=ids[0])

    assert picked.key_id is None
    assert "usage could be read" in picked.reason
    assert any("blocked by seven_day" in row for row in picked.readings)


def test_the_only_usable_key_being_the_one_in_use_is_reported_as_such(
    home, monkeypatch
):
    prof, ids = _pooled(count=2)
    _healths(monkeypatch, {
        TOKENS[0]: usage.KeyHealth(peak=30.0),
        TOKENS[1]: usage.KeyHealth(peak=100.0, blocked="five_hour"),
    })

    picked = profile_keys.switch_candidate(prof, current_key_id=ids[0])

    assert picked.key_id is None
    assert "already the one" in picked.reason


def test_an_unreadable_key_is_skipped_not_guessed(home, monkeypatch):
    prof, ids = _pooled(count=2)
    _healths(monkeypatch, {TOKENS[1]: usage.KeyHealth(peak=12.0)})

    picked = profile_keys.switch_candidate(prof, current_key_id=ids[0])

    assert picked.key_id == ids[1]
    assert any("unreadable" in row for row in picked.readings)


def test_a_profile_with_no_pool_offers_nothing(home, monkeypatch):
    plain = profile_mod.create("plain")
    credentials.save_token(plain, TOKENS[0])

    picked = profile_keys.switch_candidate(plain)

    assert picked.key_id is None
    assert "no key pool" in picked.reason


# --------------------------------------------------------------------------- #
# the clock
# --------------------------------------------------------------------------- #
class FakeSession:
    def __init__(self, name, *, auto=True, fault=None, launched_key=None):
        self.sdef = SimpleNamespace(
            name=name, auto_key_switch=auto, profile="p1", borrow=None,
            key_id=None,
        )
        self.exited = False
        self.kill_requested = False
        self.paused_at = None
        self.archived_at = None
        self.ended_by = None
        self.launched_key = launched_key
        self._fault = fault if fault is not None else {
            "kind": "usage_limit", "active": True,
            "until": "2026-11-04T00:00:00+00:00",
        }
        self.switched_to = []

    def fault(self):
        return self._fault

    def status(self, threshold=None):
        return STATUS_IDLE

    async def await_input_ready(self):
        return None


class FakeManager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.switched = []

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        return self.sessions[name]

    async def set_key(self, name, key_id):
        self.sessions[name].switched_to.append(key_id)
        self.sessions[name].launched_key = key_id
        self.switched.append((name, key_id))
        return self.sessions[name]


class FakeInput:
    def __init__(self):
        self.typed = []

    async def type_line(self, session, text, **_kwargs):
        self.typed.append((session.sdef.name, text))
        return b""


@pytest.fixture()
def typed(monkeypatch):
    fake = FakeInput()
    monkeypatch.setattr("claude_launcher.daemon.session_input.type_line", fake.type_line)
    return fake


def _limit(until="2026-11-04T00:00:00+00:00"):
    return {"kind": "usage_limit", "active": True, "until": until}


def _candidate(key_id, reason=""):
    return profile_keys.SwitchCandidate(key_id, "p1", reason, ("k1: peak 10",))


def _patch_candidate(monkeypatch, key_id, reason=""):
    def fake(session):
        return _candidate(key_id, reason)

    monkeypatch.setattr(key_switch, "_candidate", fake)


def _cfg(retries=1):
    return {"fault_key_switch_retries": retries}


def test_retries_reads_the_machine_setting():
    assert key_switch.retries({"fault_key_switch_retries": 0}) == 0
    assert key_switch.retries({"fault_key_switch_retries": 3}) == 3
    # A value that is not a number falls back to the default rather than
    # turning the feature off by accident.
    assert key_switch.retries({"fault_key_switch_retries": "many"}) == 1
    assert key_switch.retries({}) == 1


def test_the_clock_switches_the_session_and_types_continue(home, typed, monkeypatch):
    manager = FakeManager(FakeSession("s1", launched_key="k-old"))
    _patch_candidate(monkeypatch, "k-new")

    switched = asyncio.run(key_switch.sweep(manager, _cfg()))

    assert switched == ["s1"]
    assert manager.switched == [("s1", "k-new")]
    assert typed.typed == [("s1", key_switch.CONTINUE)]


def test_a_session_that_did_not_ask_is_left_to_the_pause(home, typed, monkeypatch):
    manager = FakeManager(FakeSession("s1", auto=False))
    _patch_candidate(monkeypatch, "k-new")

    assert asyncio.run(key_switch.sweep(manager, _cfg())) == []
    assert manager.switched == []
    assert typed.typed == []


def test_zero_retries_turns_the_clock_off(home, typed, monkeypatch):
    manager = FakeManager(FakeSession("s1"))
    _patch_candidate(monkeypatch, "k-new")

    assert asyncio.run(key_switch.sweep(manager, _cfg(retries=0))) == []
    assert manager.switched == []


def test_the_second_fault_spends_the_next_attempt(home, typed, monkeypatch):
    manager = FakeManager(FakeSession("s1"))
    _patch_candidate(monkeypatch, "k-new")

    assert asyncio.run(key_switch.sweep(manager, _cfg(retries=1))) == ["s1"]
    # Same fault, and the cap is one: the session stays for the pause.
    assert asyncio.run(key_switch.sweep(manager, _cfg(retries=1))) == []
    assert manager.switched == [("s1", "k-new")]


def test_a_different_fault_starts_the_count_again(home, typed, monkeypatch):
    session = FakeSession("s1")
    manager = FakeManager(session)
    _patch_candidate(monkeypatch, "k-new")

    asyncio.run(key_switch.sweep(manager, _cfg(retries=1)))
    session._fault = _limit("2026-12-01T00:00:00+00:00")   # the next week's fault

    assert asyncio.run(key_switch.sweep(manager, _cfg(retries=1))) == ["s1"]


def test_the_count_survives_a_daemon_restart(home, typed, monkeypatch):
    """claunch-ijot0.1's lesson: the count is on disk, not in the clock."""
    manager = FakeManager(FakeSession("s1"))
    _patch_candidate(monkeypatch, "k-new")

    asyncio.run(key_switch.sweep(manager, _cfg(retries=1)))
    state = json.loads(
        (key_switch.paths.session_dir("s1") / key_switch.RECORD).read_text()
    )
    assert state["attempts"] == 1

    # A fresh process (nothing in memory) sees the same answer.
    typed.typed.clear()
    assert asyncio.run(key_switch.sweep(manager, _cfg(retries=1))) == []


def test_nothing_usable_restarts_nothing_and_records_why(home, typed, monkeypatch):
    manager = FakeManager(FakeSession("s1"))
    _patch_candidate(monkeypatch, None, "every usable key is already the one")

    assert asyncio.run(key_switch.sweep(manager, _cfg())) == []
    assert manager.switched == []
    state = json.loads(
        (key_switch.paths.session_dir("s1") / key_switch.RECORD).read_text()
    )
    assert "already the one" in state["reason"]


def test_a_hopeless_pool_is_not_re_read_every_pass(home, typed, monkeypatch):
    """A limit lasts days; the provider must not be asked twice a minute."""
    manager = FakeManager(FakeSession("s1"))
    calls = []

    def fake(session):
        calls.append(1)
        return _candidate(None, "every key is spent")

    monkeypatch.setattr(key_switch, "_candidate", fake)

    assert asyncio.run(key_switch.sweep(manager, _cfg())) == []
    assert asyncio.run(key_switch.sweep(manager, _cfg())) == []
    assert len(calls) == 1

    # Once the cached answer is old enough, the pool is read again — a window
    # that reset in the meantime is what makes the switch possible again.
    state = key_switch.read_state("s1")
    state["checked_at"] = key_switch._now() - key_switch.RECONSULT - 1
    key_switch.write_state("s1", state)
    assert asyncio.run(key_switch.sweep(manager, _cfg())) == []
    assert len(calls) == 2


def test_a_failed_restart_does_not_consume_the_fault_twice(home, typed, monkeypatch):
    class Broken(FakeManager):
        async def set_key(self, name, key_id):
            raise RuntimeError("relaunch failed")

    manager = Broken(FakeSession("s1"))
    _patch_candidate(monkeypatch, "k-new")

    assert asyncio.run(key_switch.sweep(manager, _cfg())) == []
    # The attempt was written before the restart, so a crash costs one try
    # rather than restarting the session until somebody notices.
    state = json.loads(
        (key_switch.paths.session_dir("s1") / key_switch.RECORD).read_text()
    )
    assert state["attempts"] == 1


# --------------------------------------------------------------------------- #
# the definition
# --------------------------------------------------------------------------- #
def test_the_answer_round_trips_and_says_nothing_when_off():
    on = SessionDef(name="s1", auto_key_switch=True)
    assert on.to_dict()["auto_key_switch"] is True
    assert SessionDef.from_dict(on.to_dict()).auto_key_switch is True

    off = SessionDef(name="s1")
    assert "auto_key_switch" not in off.to_dict()
    assert SessionDef.from_dict(off.to_dict()).auto_key_switch is False


# --------------------------------------------------------------------------- #
# the two routes: the session's answer, and the machine's retry cap
# --------------------------------------------------------------------------- #
class _Req:
    """The little of aiohttp's request these handlers read."""

    def __init__(self, manager=None, *, name="", body=None):
        self.app = {"manager": manager}
        self.match_info = {"name": name}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def _manager(tmp_path):
    from claude_launcher.daemon.manager import SessionManager

    return SessionManager(
        idle_threshold=0.5, scrollback=200, restore_default=True
    )


def _post(handler, request):
    return asyncio.run(handler(request))


def test_the_session_route_sets_the_answer_without_restarting(home, tmp_path):
    from claude_launcher.daemon import api

    async def run():
        # stage() takes the running loop for the session's own event queue, so
        # the whole scenario lives inside one `asyncio.run`.
        profile_mod.create("p1")      # stage() resolves the profile it is given
        manager = _manager(tmp_path)
        staged = manager.stage(SessionDef(name="s1", profile="p1"))
        assert staged.sdef.auto_key_switch is False

        resp = await api.h_session_auto_key_switch(
            _Req(manager, name="s1", body={"enabled": True})
        )
        assert resp.status == 200
        assert json.loads(resp.text)["auto_key_switch"] is True
        # Still the same object: an answer about the next fault must not cost
        # the running process (that is the key pin's job).
        assert manager.get("s1") is staged
        assert staged.sdef.auto_key_switch is True

        off = await api.h_session_auto_key_switch(
            _Req(manager, name="s1", body={"enabled": False})
        )
        assert json.loads(off.text).get("auto_key_switch") is None
        assert staged.sdef.auto_key_switch is False

    asyncio.run(run())


def test_the_session_route_refuses_a_body_that_is_not_a_boolean(home, tmp_path):
    from claude_launcher.daemon import api

    async def run():
        profile_mod.create("p1")
        manager = _manager(tmp_path)
        manager.stage(SessionDef(name="s1", profile="p1"))

        resp = await api.h_session_auto_key_switch(
            _Req(manager, name="s1", body={"enabled": "yes"})
        )

        assert resp.status == 400
        assert manager.get("s1").sdef.auto_key_switch is False

    asyncio.run(run())


def test_the_machine_route_bounds_the_retries(home, tmp_path):
    from claude_launcher.daemon import api

    first = json.loads(_post(api.h_key_switch_setting_get, _Req()).text)
    assert first["retries"] == 1

    put = json.loads(
        _post(api.h_key_switch_setting_set, _Req(body={"retries": 3})).text
    )
    assert put["retries"] == 3
    assert key_switch.retries() == 3

    assert _post(
        api.h_key_switch_setting_set, _Req(body={"retries": 99})
    ).status == 400
    assert key_switch.retries() == 3
