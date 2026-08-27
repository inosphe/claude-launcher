"""A pinned delivery hold, and the step reminder's off switch, across a restart.

Two settings stop the daemon typing into a session, and a person sets both by
hand:

* the **delivery hold** — ``claunch delivery-hold``, or the terminal header's
  control. Nothing from a mesh is typed in until it is released.
* the **step reminder**, switched off for one run — the daemon stops re-typing
  that run's current step into its session.

They are separate mechanisms and one promise: a silence somebody chose stays
chosen. A daemon restart is not a decision, so it must not undo one — and a
setting that a restart quietly reverses is worse than a setting that was never
offered, because the person who made it has no reason to look again.

The reminder half was already durable and is locked here rather than changed:
its override lives in the run's own state file, which is on disk and is
re-read by the clock every tick. The hold half was not, and is what these
tests were written for. The two are checked side by side so that a later
change making either of them daemon-memory again fails on a test that says
why it must not be.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import state as cflow_state
from claude_launcher.daemon import paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.session import DeadSession

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _record(name: str) -> dict:
    entries = json.loads(paths.sessions_json().read_text(encoding="utf-8"))
    return next(e for e in entries if e["def"]["name"] == name)


# --------------------------------------------------------------------------- #
# the record carries it
# --------------------------------------------------------------------------- #
def test_the_hold_is_written_to_the_session_record(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="pinned", harness="py", cwd=str(tmp_path)))
            mgr.persist()
            # The absence is written too, not left out: a reader of the record
            # must not have to tell "open" apart from "an older daemon wrote
            # this file" by the shape of the key.
            assert _record("pinned")["delivery_hold"] is False

            s.set_delivery_hold(True)
            mgr.persist()
            assert _record("pinned")["delivery_hold"] is True
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_restart_brings_the_hold_back(home, tmp_path):
    """The whole point: the daemon goes down and the pin is still in.

    Failure here is silent in the way that matters — the restored session
    looks perfectly normal and starts taking mail again, so the first sign
    that a setting was dropped is a message landing in a terminal that was
    supposed to be shut.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            held = mgr.create(SessionDef(name="held", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="unheld", harness="py", cwd=str(tmp_path)))
            held.set_delivery_hold(True)
        finally:
            await mgr.shutdown_all()   # persists on the way down

        back = _manager()
        try:
            assert back.restore_all() == []
            assert back.get("held").delivery_held() is True
            # ...and the control, so the test cannot pass by holding everything
            assert back.get("unheld").delivery_held() is False
        finally:
            await back.shutdown_all()

    asyncio.run(run())


def test_the_hold_is_saved_when_it_is_set_not_at_the_next_shutdown(home, tmp_path):
    """Set through the API, on disk before the response is written.

    A daemon that is killed, crashes, or is replaced by an installer never
    reaches its orderly ``persist()``, and those are exactly the restarts
    nobody scheduled. Waiting for shutdown to save this would keep the
    setting through every restart except the ones it is needed for.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/s1/queued/hold", json={"hold": True}, headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["hold"] is True
            # No shutdown between the press and this read.
            assert _record("s1")["delivery_hold"] is True

            # And releasing is saved on the same terms — a stale `true` left
            # on disk would come back as a hold nobody is holding.
            await client.post(
                "/api/sessions/s1/queued/hold", json={"hold": False}, headers=BEARER,
            )
            assert _record("s1")["delivery_hold"] is False
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_relaunch_that_keeps_the_name_keeps_the_pin(home, tmp_path):
    """A redefine is this session continuing, so the setting continues with it."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        try:
            s = mgr.create(SessionDef(name="again", harness="py", cwd=str(tmp_path)))
            s.set_delivery_hold(True)
            back = await mgr.redefine("again", cols=100)
            assert back.delivery_held() is True
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the rail reads it off the list poll
# --------------------------------------------------------------------------- #
def test_the_session_list_carries_the_hold(home, tmp_path):
    """One row per session, and the hold on it.

    The per-session ``/queued`` endpoint has always answered this, one request
    at a time. The rail asks it of twenty rows at once, and the question it is
    really asking — which of these did I pin shut — is not worth twenty
    requests.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            s = mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            mgr.create(SessionDef(name="s2", harness="py", cwd=str(tmp_path)))
            s.set_delivery_hold(True)

            body = await (await client.get("/api/sessions", headers=BEARER)).json()
            rows = {r["name"]: r for r in body["sessions"]}
            assert rows["s1"]["delivery_hold"] is True
            assert rows["s2"]["delivery_hold"] is False
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_an_exited_record_reports_no_hold_and_still_answers_the_key(home, tmp_path):
    """A record with no terminal holds nothing back, and answers the key anyway.

    Both halves matter to the row: a pill drawn from a missing key is a
    crash, and a pill drawn on an exited record would name a hold that is not
    being applied to anything.
    """
    dead = DeadSession(SessionDef(name="gone", harness="py", cwd=str(tmp_path)))
    assert dead.set_delivery_hold(True) is False
    assert dead.info()["delivery_hold"] is False


# --------------------------------------------------------------------------- #
# the other half: the reminder's off switch was already durable
# --------------------------------------------------------------------------- #
LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "linear.yaml").write_text(LINEAR, encoding="utf-8")
    return d


def test_the_reminder_switch_is_on_disk_not_in_the_daemon(proj):
    """Switching the step reminder off writes a file, and nothing else.

    This is a lock, not a new behaviour. The reminder override has always
    lived in the run's own state — which is why turning it off survives a
    restart already, while the delivery hold beside it did not. What the test
    pins is the *reason*: it reads the state file directly rather than asking
    the engine, so a later move of this setting into daemon memory fails here
    instead of surfacing months later as a reminder that came back on by
    itself.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    cflow_engine.set_reminder(False, None, cwd=cwd, scope="w1")

    token = cflow_state.push_scope("w1")
    try:
        doc = json.loads(cflow_state._state_path(cwd).read_text(encoding="utf-8"))
    finally:
        cflow_state.pop_scope(token)
    assert doc["reminder"] == {"enabled": False}

    # And read back through the same door the clock uses: it loads the payload
    # from this file on every tick, so this is the reading a restarted daemon
    # gets.
    assert cflow_engine.status(cwd, scope="w1")["reminder"] == {"enabled": False}
