"""The resume grace of the fault pause (claunch-x9agn).

A session brought back out of a pause, a hold or a kill -- or restarted on
another key or a borrowed profile -- can still show the usage-limit line its
previous incarnation stopped on. Read at once, that line paused it again
before the new key or profile could answer. The manager now stamps
``Session.resumed_at`` on those relaunches, ``fault_pause`` leaves a session
inside ``daemon.fault_resume_grace`` seconds of it alone (the key switch too,
which reads the same ``fault_to_pause``), and the timer line shows the grace
as a countdown chip.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import threading
from types import SimpleNamespace

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import fault_pause, key_switch, pty_backend
from claude_launcher.daemon import session_timers as st
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import STATUS_IDLE

NOW = 1_800_000_000.0


class FakeSession:
    def __init__(self, name, fault=None, resumed_at=None):
        self.sdef = SimpleNamespace(name=name, auto_key_switch=True, null_token=False)
        self.exited = False
        self.kill_requested = False
        self._fault = fault
        self.resumed_at = resumed_at
        self.paused_at = None
        self.archived_at = None
        self.ended_by = None

    def fault(self):
        return self._fault

    def status(self, threshold=None):
        return STATUS_IDLE


class FakeManager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.paused = []

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        return self.sessions[name]

    def pause(self, name, *, force=False, by=None):
        self.paused.append(name)
        session = self.sessions[name]
        session.exited = True
        session.paused_at = "2026-11-04T09:00:00+00:00"
        session.ended_by = by


def _limit(until="2026-11-09T00:00:00+00:00"):
    return {"kind": "usage_limit", "active": True, "until": until}


# --- the grace itself ---------------------------------------------------------

def test_resume_grace_reads_the_config_and_defaults_to_thirty_seconds():
    assert fault_pause.resume_grace({}) == 30.0
    assert fault_pause.resume_grace({"fault_resume_grace": 5}) == 5.0
    assert fault_pause.resume_grace({"fault_resume_grace": 0}) == 0.0
    assert fault_pause.resume_grace({"fault_resume_grace": -3}) == 0.0
    assert fault_pause.resume_grace({"fault_resume_grace": "x"}) == 30.0
    assert store.DAEMON_DEFAULTS["fault_resume_grace"] == 30.0


def test_a_session_inside_its_grace_is_not_paused():
    resumed = FakeSession("s1", _limit(), resumed_at=NOW - 10)
    assert fault_pause.fault_to_pause(resumed, grace=30, now=NOW) is None
    assert fault_pause.fault_to_pause(resumed, grace=30, now=NOW + 20) == _limit()


def test_the_grace_pauses_nothing_once_it_is_over(home, monkeypatch):
    session = FakeSession("s1", _limit(), resumed_at=NOW - 10)
    manager = FakeManager(session)
    monkeypatch.setattr(fault_pause.time, "time", lambda: NOW)
    assert fault_pause.sweep(manager, {}, grace=30) == []
    assert manager.paused == []
    # Past the grace the limit still on the screen pauses it as before.
    monkeypatch.setattr(fault_pause.time, "time", lambda: NOW + 21)
    assert fault_pause.sweep(manager, {}, grace=30) == ["s1"]


def test_a_session_never_resumed_or_a_grace_of_zero_is_paused_at_once(home, monkeypatch):
    monkeypatch.setattr(fault_pause.time, "time", lambda: NOW)
    fresh = FakeSession("fresh", _limit())
    off = FakeSession("off", _limit(), resumed_at=NOW - 1)
    assert fault_pause.sweep(FakeManager(fresh), {}, grace=30) == ["fresh"]
    assert fault_pause.sweep(FakeManager(off), {}, grace=0) == ["off"]


def test_the_clock_passes_its_config_grace(home, monkeypatch):
    monkeypatch.setattr(fault_pause.time, "time", lambda: NOW)
    session = FakeSession("s1", _limit(), resumed_at=NOW - 10)
    manager = FakeManager(session)
    assert fault_pause.FaultPauseClock(manager, config=lambda: {}).pass_once() == []
    clock = fault_pause.FaultPauseClock(manager, config=lambda: {"fault_resume_grace": 5})
    assert clock.pass_once() == ["s1"]


def test_the_key_switch_leaves_a_session_inside_its_grace_alone(home, monkeypatch):
    monkeypatch.setattr(fault_pause.time, "time", lambda: NOW)
    session = FakeSession("s1", _limit(), resumed_at=NOW - 10)
    asked = []
    monkeypatch.setattr(key_switch, "_candidate", lambda s: asked.append(s) or None)
    out = asyncio.run(key_switch.sweep(FakeManager(session), {"fault_key_switch_retries": 1}))
    assert out == [] and asked == []


# --- the timer line -----------------------------------------------------------

def test_the_grace_is_a_chip_on_the_session_timer_line():
    session = FakeSession("s1", fault=_limit(), resumed_at=NOW - 10)
    out = st.collect("s1", "", now=NOW, app={"manager": FakeManager(session)},
                     providers={"fault": st.fault_provider})
    assert out["errors"] == []
    [chip] = out["timers"]
    assert chip["source"] == "fault" and chip["kind"] == "grace"
    assert chip["due"] == NOW + 20 and chip["since"] == NOW - 10
    assert chip["state"] == st.STATE_WAITING
    assert "fault_resume_grace" in chip["detail"]


def test_no_chip_outside_the_grace_or_with_the_pause_off(monkeypatch):
    manager = FakeManager(FakeSession("over", fault=_limit(), resumed_at=NOW - 31),
                          FakeSession("fresh", fault=_limit()))
    for name in ("over", "fresh", "missing"):
        out = st.collect(name, "", now=NOW, app={"manager": manager},
                         providers={"fault": st.fault_provider})
        assert out["timers"] == [] and out["errors"] == []
    assert st.collect("over", "", now=NOW, providers={"fault": st.fault_provider})["timers"] == []
    monkeypatch.setattr(store, "daemon_config", lambda: {"fault_auto_pause": False})
    live = FakeManager(FakeSession("s1", resumed_at=NOW - 1))
    out = st.collect("s1", "", now=NOW, app={"manager": live},
                     providers={"fault": st.fault_provider})
    assert out["timers"] == []


class _BrokenFault(FakeSession):
    def fault(self):
        raise RuntimeError("no screen")


@pytest.mark.parametrize("fault", [
    None,
    {"kind": "usage_limit", "active": False, "until": "2026-11-09T00:00:00+00:00"},
    {"kind": "api_error", "active": True},
    "broken",
], ids=["no-fault", "cleared", "another-kind", "broken-reading"])
def test_no_chip_inside_the_grace_once_the_limit_has_cleared(fault):
    """The chip stands for a pause the grace is holding off: with no active
    usage-limit reading there is nothing to hold off, so it goes before the
    thirty seconds are up (claunch-xn9ir, th-839851)."""
    if fault == "broken":
        session = _BrokenFault("s1", resumed_at=NOW - 5)
    else:
        session = FakeSession("s1", fault=fault, resumed_at=NOW - 5)
    out = st.collect("s1", "", now=NOW, app={"manager": FakeManager(session)},
                     providers={"fault": st.fault_provider})
    assert out["timers"] == [] and out["errors"] == []


def test_the_fault_provider_is_registered():
    assert st.PROVIDERS.get("fault") is st.fault_provider


# --- the manager stamps the relaunches ----------------------------------------

class _Pty:
    pid = 4243

    def __init__(self):
        self.done = threading.Event()

    def read(self):
        self.done.wait(5)
        return b""

    def exit_code(self):
        return 0

    def close(self):
        pass

    def isalive(self):
        return not self.done.is_set()

    def terminate(self, force=False):
        self.done.set()

    def resize(self, cols, rows):
        pass

    def write(self, data, writer=None):
        pass

    def forget_writer(self, writer):
        pass


@pytest.fixture
def py_harness(home, monkeypatch):
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-c", "pass"]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")
    monkeypatch.setattr(pty_backend, "spawn", lambda argv, **kw: _Pty())


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=100, restore_default=True)


def test_a_respawn_stamps_resumed_at(py_harness):
    async def run():
        mgr = _manager()
        mgr._retire(SessionDef(name="old", harness="py"),
                    {"created_at": "2026-11-04T08:30:00+00:00",
                     "paused_at": "2026-11-04T08:40:00+00:00"})
        before = fault_pause.time.time()
        revived = await mgr.respawn_async("old")
        assert revived.resumed_at is not None and revived.resumed_at >= before
        assert fault_pause.in_grace(revived, 30)
        revived.pty.terminate()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_a_restart_under_a_changed_definition_stamps_resumed_at(py_harness):
    async def run():
        mgr = _manager()
        fresh = await mgr.launch_async(mgr.stage(SessionDef(name="live", harness="py")))
        assert fresh.resumed_at is None
        fresh.pty.terminate()
        await asyncio.sleep(0.05)
        record = mgr.get("live")
        relaunched = mgr._recreate("live", record, dataclasses.replace(record.sdef))
        assert relaunched.resumed_at is not None
        relaunched.pty.terminate()
        await asyncio.sleep(0.05)

    asyncio.run(run())
