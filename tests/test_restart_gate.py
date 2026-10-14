"""The approval gate on agent-requested daemon restarts.

An agent session's ``claunch daemon restart`` no longer stops the daemon on
the spot: the CLI opens a gate in the daemon (``POST /api/daemon/
restart-request``), the web UI's notification card shows it with Approve /
Reject, and an unanswered gate counts as approved after its timeout — at
which point the restart goes through the daemon's own door, exactly like the
web button's: record the request (so the successor owes the asking session
the account of the boot), mark the intent, trip the ordinary shutdown path.

The human's doors stay immediate: ``claunch daemon restart`` from a
session-less shell and the web UI's Restart button never touch this gate.
That contract is asserted here for the API (nothing about the gate changes
:func:`h_daemon_restart`) and in ``test_restart_notice`` for the CLI (the
record-before-stop path is the human's now).
"""

from __future__ import annotations

import argparse
import asyncio
import time

from claude_launcher.daemon import restart_notice
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}

SERVING = "serving"
NOT_RUNNING = "not_running"
STALE_RECORD = "stale_record"


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _future() -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc) + timedelta(minutes=4)
    ).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# a scripted daemon_client for the CLI's half
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Serves the gate endpoints: POST opens pending, GET returns ``record``
    (whose status the test sets to steer the poll's next read)."""

    def __init__(self, record):
        self.base_url = "http://127.0.0.1:8377"
        self.record = record
        self.posted = []
        self.gets = []

    def post(self, path, body=None, **kw):
        self.posted.append((path, body))
        return {"ok": True, "request": {**self.record, "status": "pending"}}

    def get(self, path, **kw):
        self.gets.append(path)
        return {"request": dict(self.record)}


class _FakeDaemon:
    """Stands in for the whole ``daemon_client`` module under test."""

    SERVING = SERVING
    NOT_RUNNING = NOT_RUNNING
    STALE_RECORD = STALE_RECORD
    WEDGED = "wedged"
    UNRESPONSIVE = "unresponsive"

    def __init__(self, state=SERVING, record=None):
        self.state = state
        self.client = _FakeClient(record or {})
        self.stop_calls = 0

    def diagnose(self, **kw):
        return {"state": self.state}

    def ensure_running(self, **kw):
        return self.client

    def connect(self):
        return self.client

    def connect_with_diagnosis(self, **kw):
        return self.client, {"state": self.state}

    def stop(self, **kw):
        self.stop_calls += 1
        return True

    def restart(self, **kw):
        self.stop(**kw)
        return self.ensure_running()

    def unreachable_reason(self, report):
        return report.get("why") or "unreachable"


def _daemon_args(action: str = "restart", **kw) -> argparse.Namespace:
    return argparse.Namespace(action=action, all=False, force=False, **kw)


# --------------------------------------------------------------------------- #
# the endpoint: open, settle, refuse
# --------------------------------------------------------------------------- #
def test_submit_and_get_round_trip(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request", json={"session": "s1"}, headers=AUTH
            )
            assert resp.status == 200
            rec = (await resp.json())["request"]
            assert rec["session"] == "s1"
            assert rec["status"] == "pending"
            assert rec["id"]
            assert rec["requested_at"] < rec["deadline"]

            resp = await client.get(
                "/api/daemon/restart-request", headers=AUTH
            )
            assert resp.status == 200
            assert (await resp.json())["request"]["id"] == rec["id"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_submit_needs_a_session(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request", json={}, headers=AUTH
            )
            assert resp.status == 400
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_gate_takes_one_request_at_a_time(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            assert (
                await client.post(
                    "/api/daemon/restart-request",
                    json={"session": "s1"},
                    headers=AUTH,
                )
            ).status == 200
            resp = await client.post(
                "/api/daemon/restart-request", json={"session": "s2"}, headers=AUTH
            )
            assert resp.status == 409

            # A settled request frees the gate for the next ask.
            await client.post("/api/daemon/restart-request/reject", headers=AUTH)
            assert (
                await client.post(
                    "/api/daemon/restart-request",
                    json={"session": "s2"},
                    headers=AUTH,
                )
            ).status == 200
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# approve: the daemon's own door, with the asker's name on the record
# --------------------------------------------------------------------------- #
def test_approve_restarts_and_records_the_asker(home, monkeypatch):
    """The record needs a daemon announced (it names the process about to
    die), so the test seeds daemon.json like the notice tests do."""
    monkeypatch.setattr(
        "claude_launcher.daemon.runtime_state.read_daemon_json",
        lambda: {"pid": 4242, "started_at": "2026-08-26T02:36:00+00:00"},
    )

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await client.post(
                "/api/daemon/restart-request", json={"session": "s3"}, headers=AUTH
            )
            resp = await client.post(
                "/api/daemon/restart-request/approve", headers=AUTH
            )
            assert resp.status == 200
            assert (await resp.json())["restarting"] is True
            assert app["restart_requested"] is True
            await asyncio.wait_for(app["shutdown_event"].wait(), timeout=2.0)

            # The successor owes the asking session an account of the boot —
            # the one difference from the web button's restart: the asker's
            # name rides on the record written before the stop.
            [record] = restart_notice.read_requests()
            assert record["kind"] == restart_notice.KIND_RESTART
            assert record["via"] == "agent-approval"
            assert record["requested_by"] == "s3"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_approve_with_nothing_pending_is_a_conflict(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request/approve", headers=AUTH
            )
            assert resp.status == 409
            assert app["restart_requested"] is False
            assert not app["shutdown_event"].is_set()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_approve_and_reject_need_a_credential(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for path in (
                "/api/daemon/restart-request",
                "/api/daemon/restart-request/approve",
                "/api/daemon/restart-request/reject",
            ):
                assert (await client.get(path)).status == 401
                assert (await client.post(path, json={})).status == 401
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# reject: settle, restart nothing
# --------------------------------------------------------------------------- #
def test_reject_settles_without_restarting(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await client.post(
                "/api/daemon/restart-request", json={"session": "s4"}, headers=AUTH
            )
            resp = await client.post(
                "/api/daemon/restart-request/reject", headers=AUTH
            )
            assert resp.status == 200
            assert (await resp.json())["rejected"] is True
            assert app["restart_requested"] is False
            assert not app["shutdown_event"].is_set()
            assert restart_notice.read_requests() == []

            # The settled record stays readable — the asking CLI's poll reads
            # "rejected" from here.
            rec = (
                await (
                    await client.get("/api/daemon/restart-request", headers=AUTH)
                ).json()
            )["request"]
            assert rec["status"] == "rejected"
            assert rec["decided_by"] == "web"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_reject_with_nothing_pending_is_a_conflict(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            assert (
                await client.post(
                    "/api/daemon/restart-request/reject", headers=AUTH
                )
            ).status == 409
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the timeout: five minutes unanswered counts as approval
# --------------------------------------------------------------------------- #
def test_an_unanswered_gate_counts_as_approval(home, monkeypatch):
    """The gate's own clock, shortened instead of waiting the real five
    minutes out: the timer fires, the daemon marks the intent, trips the
    shutdown event, and the request is on record for the successor as if the
    web Approve had been clicked."""
    monkeypatch.setattr(
        "claude_launcher.daemon.runtime_state.read_daemon_json",
        lambda: {"pid": 4242, "started_at": "2026-08-26T02:36:00+00:00"},
    )

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=0.05)
        gate = app["restart_gate"]
        rec = gate.submit(session="s5")
        assert rec["status"] == "pending"
        try:
            await asyncio.wait_for(app["shutdown_event"].wait(), timeout=3.0)
        finally:
            await mgr.shutdown_all()
        assert app["restart_requested"] is True
        settled = gate.get()
        assert settled["status"] == "approved"
        assert settled["decided_by"] == "timeout"
        [record] = restart_notice.read_requests()
        assert record["requested_by"] == "s5"
        assert record["via"] == "agent-approval"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the CLI's half: who goes through the gate, and what it reports back
# --------------------------------------------------------------------------- #
def test_a_sessions_restart_opens_the_gate_and_waits(home, monkeypatch, capsys):
    from claude_launcher import cli_sessions

    future = _future()
    fake = _FakeDaemon(record={"id": "g1", "session": "s9", "deadline": future})
    fake.client.record["status"] = "approved"
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.setattr(cli_sessions.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    assert cli_sessions._cmd_daemon(_daemon_args()) == 0

    assert fake.client.posted == [("/api/daemon/restart-request", {"session": "s9"})]
    assert fake.stop_calls == 0
    err = capsys.readouterr().err
    assert "restart requested by session s9" in err
    assert "counts as approved" in err


def test_a_rejected_gate_is_reported_and_nothing_dies(home, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone

    from claude_launcher import cli_sessions

    future = (
        datetime.now(timezone.utc) + timedelta(minutes=4)
    ).isoformat(timespec="seconds")
    fake = _FakeDaemon(record={"id": "g2", "session": "s9", "deadline": future})
    fake.client.record["status"] = "rejected"
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.setattr(cli_sessions.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    assert cli_sessions._cmd_daemon(_daemon_args()) == 1
    assert fake.stop_calls == 0
    assert "rejected" in capsys.readouterr().err


def test_a_session_restarting_an_absent_daemon_starts_it(home, monkeypatch, capsys):
    """A restart with no daemon is a start on the immediate path, and stays
    one here — there is no daemon to host a gate."""
    from claude_launcher import cli_sessions

    fake = _FakeDaemon(state=NOT_RUNNING)
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    assert cli_sessions._cmd_daemon(_daemon_args()) == 0
    assert fake.client.posted == []
    assert fake.stop_calls == 0
    assert "daemon started" in capsys.readouterr().out


def test_a_sessions_force_stands_down_into_the_gate(home, monkeypatch, capsys):
    """``--force`` against a daemon that answers lands in the gate too.

    ``_force_replace`` re-diagnoses before ending anything, and a daemon that
    answers sends it down "the ordinary way" -- which, inside a session, is
    the gate. That hand-off is a second door into ``_gated_restart``, reached
    by a flag whose own docstring says it does not pass through here, so it is
    pinned separately from the plain restart above.

    It was covered by accident until 2026-08-27 and by nothing after:
    ``tests/test_daemon_wedge.py`` drove this line only because
    ``CLAUNCH_SESSION`` happened to be set in the process running the suite,
    which made three of its tests fail inside a claunch session and pass
    outside one (``claunch-uf7m``). That module now declares the immediate
    path, so this is where the session half of the stand-down lives.
    """
    from claude_launcher import cli_sessions

    fake = _FakeDaemon(record={"id": "g4", "session": "s9", "deadline": _future()})
    fake.client.record["status"] = "approved"
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.setattr(cli_sessions.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    args = _daemon_args()          # _daemon_args pins force=False positionally
    args.force = True

    assert cli_sessions._cmd_daemon(args) == 0

    err = capsys.readouterr().err
    assert "no force needed" in err                 # force stood down
    assert "restart requested by session s9" in err  # ...into the gate
    assert fake.client.posted == [("/api/daemon/restart-request", {"session": "s9"})]
    assert fake.stop_calls == 0                      # nothing died on the spot


def test_a_human_shell_stays_on_the_immediate_path(home, monkeypatch, capsys):
    from claude_launcher import cli_sessions

    fake = _FakeDaemon(record={"id": "g3", "session": "x", "deadline": _future()})
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.setattr(cli_sessions.time, "sleep", lambda s: None)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)

    assert cli_sessions._cmd_daemon(_daemon_args()) == 0
    # The gate was never opened; the daemon was stopped and restarted
    # on the spot, as before this feature.
    assert fake.client.posted == []
    assert fake.stop_calls == 1
    assert "daemon restarted" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the five minutes: the value, and the wiring that carries it
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Extend: the third button, which settles nothing
# --------------------------------------------------------------------------- #
def test_extend_moves_the_deadline_and_leaves_it_pending(home):
    """The countdown answers for the person when they say nothing. Extend is
    how somebody who is at the page takes that answer back without making a
    decision: the deadline moves, the status does not."""
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request", json={"session": "s1"}, headers=AUTH
            )
            first = (await resp.json())["request"]

            resp = await client.post("/api/daemon/restart-request/extend", headers=AUTH)
            assert resp.status == 200
            moved = (await resp.json())["request"]
            assert moved["status"] == "pending", "an extension settles nothing"
            assert moved["deadline"] > first["deadline"]
            assert moved["extensions"] == 1
            assert moved["extended_by"] == "web"
            assert app["restart_gate"].get()["deadline"] == moved["deadline"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_extend_adds_to_the_deadline_not_to_now(home):
    """Pressing early must not shorten the wait it was meant to lengthen.

    A gate opened for ten minutes and extended one second later has to come
    out at fifteen, not at five -- which is what resetting the clock to
    ``now + EXTENSION`` would do, and it is why the arithmetic is written
    against the deadline rather than against the moment of the press.

    """
    from datetime import datetime

    from claude_launcher.daemon import restart_gate

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=60)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request", json={"session": "s1"}, headers=AUTH
            )
            before = datetime.fromisoformat((await resp.json())["request"]["deadline"])
            resp = await client.post("/api/daemon/restart-request/extend", headers=AUTH)
            after = datetime.fromisoformat((await resp.json())["request"]["deadline"])
            grew = (after - before).total_seconds()
            assert grew == restart_gate.EXTENSION, (
                f"the deadline grew by {grew}s, not by one EXTENSION"
            )
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_extend_has_no_limit(home):
    """Every press is honoured, however many came before (claunch-mm8zv).

    The gate used to refuse the third press (two presses, 870 seconds in all)
    so that it ended inside cflow's ``restart.timeout`` ceiling. The
    RestartClock now keeps the waiting shell alive while the gate holds its
    request (``test_cflow_restart``), so the person at the page decides how
    long the restart waits.
    """
    from datetime import datetime

    from claude_launcher.daemon import restart_gate

    presses = 10

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=300)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request", json={"session": "s1"}, headers=AUTH
            )
            filed = datetime.fromisoformat((await resp.json())["request"]["deadline"])
            for press in range(presses):
                resp = await client.post(
                    "/api/daemon/restart-request/extend", headers=AUTH
                )
                assert resp.status == 200, f"press {press + 1} was refused"
            held = app["restart_gate"].get()
            assert held["status"] == "pending"
            assert held["extensions"] == presses
            assert "max_extensions" not in held
            grew = (datetime.fromisoformat(held["deadline"]) - filed).total_seconds()
            assert grew == presses * restart_gate.EXTENSION
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_gate_opened_wide_still_extends(home):
    """A ``restart_approval_timeout`` past the old 870-second ceiling used to
    open a gate that could not be extended at all; now it extends like any
    other."""
    from datetime import datetime

    from claude_launcher.daemon import restart_gate

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=1500)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request", json={"session": "s1"}, headers=AUTH
            )
            filed = (await resp.json())["request"]["deadline"]
            resp = await client.post("/api/daemon/restart-request/extend", headers=AUTH)
            assert resp.status == 200
            moved = (await resp.json())["request"]["deadline"]
            grew = (datetime.fromisoformat(moved) - datetime.fromisoformat(filed)).total_seconds()
            assert grew == restart_gate.EXTENSION
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_extend_with_nothing_pending_is_a_conflict(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/daemon/restart-request/extend", headers=AUTH)
            assert resp.status == 409
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_holding_names_the_session_whose_request_is_pending(home):
    """``holding`` is what the RestartClock asks before it kills a restart
    shell at the step's timeout: True only while THIS daemon's gate holds a
    pending request from that session."""
    from claude_launcher.daemon import restart_gate

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        gate = app["restart_gate"]
        try:
            assert not restart_gate.holding("s1"), "nothing filed yet"
            gate.submit(session="s1")
            assert restart_gate.holding("s1")
            assert not restart_gate.holding("s2"), "another session's request"
            assert not restart_gate.holding(""), "a nameless run holds nothing"
            gate.extend()
            assert restart_gate.holding("s1"), "an extension keeps it held"
            gate.reject()
            assert not restart_gate.holding("s1"), "a settled request holds nothing"
        finally:
            await gate.cancel()
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the reason: why the asker wants the restart, shown on the card
# --------------------------------------------------------------------------- #
def test_the_reason_travels_with_the_request(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            reason = "restarting: older code\ncommits the live daemon does not serve yet (1):"
            resp = await client.post(
                "/api/daemon/restart-request",
                json={"session": "s1", "reason": f"  {reason}  "},
                headers=AUTH,
            )
            assert resp.status == 200
            assert (await resp.json())["request"]["reason"] == reason
            resp = await client.get("/api/daemon/restart-request", headers=AUTH)
            assert (await resp.json())["request"]["reason"] == reason
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_request_without_a_reason_is_still_filed(home):
    """An older CLI does not send the field; its request must not fail."""
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request", json={"session": "s1"}, headers=AUTH
            )
            assert resp.status == 200
            assert (await resp.json())["request"]["reason"] == ""
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_reason_that_is_not_text_is_refused(home):
    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=600)
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart-request",
                json={"session": "s1", "reason": ["x"]},
                headers=AUTH,
            )
            assert resp.status == 400
            assert app["restart_gate"].get() is None, "a refused ask opens nothing"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_long_reason_is_cut_not_refused():
    from claude_launcher.daemon import restart_gate

    kept = restart_gate.clean_reason("x" * (restart_gate.REASON_MAX + 50))
    assert kept.startswith("x" * restart_gate.REASON_MAX)
    assert kept.endswith("(truncated)")
    assert len(kept) < restart_gate.REASON_MAX + 30


def test_the_cli_sends_the_reason_and_prints_it(home, monkeypatch, capsys):
    from claude_launcher import cli_sessions

    fake = _FakeDaemon(record={"id": "g1", "session": "s9", "deadline": _future()})
    fake.client.record["status"] = "approved"
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.setattr(cli_sessions.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    args = _daemon_args(reason="  3 merges are not served  ")
    assert cli_sessions._cmd_daemon(args) == 0
    assert fake.client.posted == [(
        "/api/daemon/restart-request",
        {"session": "s9", "reason": "3 merges are not served"},
    )]
    assert "reason: 3 merges are not served" in capsys.readouterr().err


def test_the_reason_flag_parses():
    from claude_launcher import cli

    args = cli.build_parser().parse_args(["daemon", "restart", "--reason", "why"])
    assert args.reason == "why"


def test_the_cli_waits_out_a_deadline_the_operator_extended(home, monkeypatch, capsys):
    """The asking session has to learn that its deadline moved.

    The CLI reads the deadline once when it files the request and then polls.
    Trusting that first copy makes the extension invisible on this side: the
    command announces "no answer before the deadline", returns as if the gate
    had auto-approved, and the operator who pressed Extend to buy time bought
    none. The poll re-reads it instead.
    """
    import time as real_time
    from datetime import datetime, timedelta, timezone

    from claude_launcher import cli_sessions

    now = datetime.now(timezone.utc)
    base = {"id": "g9", "session": "s9"}
    fake = _FakeDaemon(record={
        **base,
        "deadline": (now + timedelta(seconds=0.3)).isoformat(),
        "extensions": 0,
        "status": "pending",
    })
    extended = (now + timedelta(minutes=9)).isoformat()

    def stepped_get(path, **kw):
        fake.client.gets.append(path)
        settled = "approved" if len(fake.client.gets) > 1 else "pending"
        return {"request": {
            **base, "deadline": extended, "extensions": 1, "status": settled,
        }}

    fake.client.get = stepped_get
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    # Long enough that the deadline filed at submit time is past by the first
    # poll: without the re-read the loop leaves on the turn after it.
    real_sleep = real_time.sleep   # bound before the patch: same module object
    monkeypatch.setattr(cli_sessions.time, "sleep", lambda s: real_sleep(0.4))
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    assert cli_sessions._cmd_daemon(_daemon_args()) == 0
    err = capsys.readouterr().err
    assert "extended the request" in err
    assert "no answer before the deadline" not in err, (
        "the CLI left on the deadline it filed with, not the one it was given"
    )
    assert len(fake.client.gets) >= 2


def test_the_five_minute_default_is_pinned_in_both_places(home):
    """The spec's "max timeout 5 minutes" lives in two constants that must
    agree: the gate's own default and the machine-config default. A drift
    between them silently changes what the gate waits for on a stock
    install — and every behavioral test injects its own timeout, so none of
    them can see either value. Changing one 300.0 to something else must
    make this red."""
    from claude_launcher import store
    from claude_launcher.daemon import restart_gate

    assert restart_gate.GATE_TIMEOUT == 300.0
    assert store.DAEMON_DEFAULTS["restart_approval_timeout"] == 300.0
    assert store.daemon_config()["restart_approval_timeout"] == 300.0


def test_the_configured_timeout_reaches_the_gate_deadline(home, monkeypatch):
    """The config value shapes the deadline through the *real* ``__main__``
    wiring: ``_serve`` reads ``cfg["restart_approval_timeout"]`` and hands
    it to ``build_app``, whose gate turns it into the record's deadline. The
    ``GATE_TIMEOUT`` default must not answer for a configured value —
    deleting the ``gate_timeout=...`` line in ``daemon/__main__.py`` makes
    this red (the deadline would come back 300s wide while the config says
    wait 7 seconds).

    The daemon runs in-process on an ephemeral port; the relay uplink is
    stubbed out so the test never dials a real relay.
    """
    import asyncio
    from datetime import datetime

    from claude_launcher import store
    from claude_launcher.daemon import __main__ as daemon_main
    from claude_launcher.daemon import runtime_state
    from claude_launcher.daemon_client import DaemonClient, DaemonClientError

    monkeypatch.setattr(daemon_main, "_start_uplink", lambda port: (None, None))

    # _serve indexes the config strictly (cfg["..."], no defaults), so a cfg
    # built by hand has to carry every key it reads -- and that list grows
    # with the daemon (the goto gate's timeout, then the focus scheduling
    # knobs each broke this test on arrival). Start from the packaged
    # defaults and override only what this test is about.
    cfg = dict(store.DAEMON_DEFAULTS)
    cfg.update({
        "host": "127.0.0.1",
        "port": 0,  # ephemeral: never collides with a live daemon
        "idle_threshold": 2.0,
        "scrollback_lines": 200,
        "restore": False,
        "restart_approval_timeout": 7,
        "goto_approval_timeout": 7,
    })
    bound: dict = {}

    async def run():
        serve = asyncio.ensure_future(
            daemon_main._serve("127.0.0.1", int(cfg["port"]), cfg, bound)
        )
        client = None
        try:
            deadline = time.monotonic() + 15.0
            while not bound.get("port"):
                if serve.done() or time.monotonic() > deadline:
                    raise AssertionError("in-process daemon did not come up")
                await asyncio.sleep(0.05)
            client = DaemonClient(
                f"http://127.0.0.1:{bound['port']}",
                runtime_state.load_or_create_token(),
            )
            # The client is synchronous urllib and the server lives on this
            # very loop — every call has to cross to a worker thread, or the
            # loop blocks on a server that cannot answer it.
            rec = (await asyncio.to_thread(
                client.post, "/api/daemon/restart-request", {"session": "s-wire"}
            ))["request"]
            asked = datetime.fromisoformat(rec["requested_at"])
            dead = datetime.fromisoformat(rec["deadline"])
            # 7.0 exactly: the gate computes deadline = requested + timeout,
            # and both stamps are second-precision ISO strings.
            assert (dead - asked).total_seconds() == 7.0, rec
            assert rec["status"] == "pending"
        finally:
            if client is not None:
                try:
                    await asyncio.to_thread(client.post, "/api/daemon/shutdown")
                except DaemonClientError:
                    pass
            await asyncio.wait_for(serve, timeout=15.0)

    asyncio.run(run())
