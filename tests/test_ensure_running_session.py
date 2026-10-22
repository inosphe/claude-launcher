"""A session's shell never starts a daemon; a user's shell does not start a
second one beside a daemon that is only slow.

claunch-19liw.2.1: 504 of 704 spawn_daemon records since 10-14 came from
session shells, and a daemon started there carried that session's
environment into every later generation. Writes that may land late (mesh
send) are queued instead and applied once by the daemon.
claunch-4ui: UNRESPONSIVE is not an absence, outside a session either.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from claude_launcher import cli, daemon_client, request_spool
from claude_launcher.daemon import paths, spool_drain
from claude_launcher.daemon.mesh import MeshManager

from test_mesh_wire import _Manager, _connect, _member


def _report(state: str) -> dict:
    return {"state": state, "pid": 4242, "probes": 2, "budget": 3.0,
            "why": f"canned {state}", "base_url": "http://127.0.0.1:1"}


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(daemon_client, "START_TIMEOUT", 0.3)
    monkeypatch.setattr(daemon_client, "RECOVER_GAP", 0.05)


@pytest.fixture
def spawns(monkeypatch):
    calls = []

    def fake_spawn(env=None, **kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(daemon_client, "spawn_daemon", fake_spawn)
    return calls


def _looks(monkeypatch, *states, client=None):
    """connect_with_diagnosis answers ``states`` in order, the last forever;
    a state of None means serving (``client``)."""
    seq = list(states)

    def look(**kw):
        state = seq.pop(0) if len(seq) > 1 else seq[0]
        if state is None:
            return client, {"state": daemon_client.SERVING}
        return None, _report(state)

    monkeypatch.setattr(daemon_client, "connect_with_diagnosis", look)


def _spooled() -> list:
    return [doc for _, doc in request_spool.pending()]


# --------------------------------------------------------------------- #
# inside a session: no spawn, ever
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("state", [daemon_client.NOT_RUNNING,
                                   daemon_client.STALE_RECORD,
                                   daemon_client.UNRESPONSIVE])
def test_a_session_shell_never_spawns(state, quick, spawns, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    _looks(monkeypatch, state)
    client = daemon_client.ensure_running()
    assert isinstance(client, daemon_client.SpoolingClient)
    assert spawns == []


def test_a_session_shell_leaves_no_spawn_record_in_daemon_log(quick, monkeypatch):
    """The completion criterion: no ``session=<name>`` spawn line, with the
    real spawn_daemon in place (it would write that line first)."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    _looks(monkeypatch, daemon_client.NOT_RUNNING)
    daemon_client.ensure_running()
    log = paths.log_file()
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "spawn_daemon:" not in text


def test_a_daemon_back_within_the_window_is_used_not_spooled(quick, spawns, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    sentinel = object()
    _looks(monkeypatch, daemon_client.UNRESPONSIVE, daemon_client.NOT_RUNNING,
           None, client=sentinel)
    assert daemon_client.ensure_running() is sentinel
    assert spawns == []


def test_session_mesh_send_is_queued_once_and_exits_zero(quick, spawns, monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    _looks(monkeypatch, daemon_client.NOT_RUNNING)
    assert cli.main(["mesh", "send", "home", "lead", "hello"]) == 0
    out = capsys.readouterr().out
    assert "queued rq-" in out and "do not resend" in out
    rows = _spooled()
    assert len(rows) == 1
    doc = rows[0]
    assert doc["session"] == "s-test"
    assert doc["method"] == "POST" and doc["path"] == "/api/mesh/home/messages"
    assert doc["body"]["from"] == "s-test" and doc["body"]["body"] == "hello"
    assert doc["queued_at"]
    assert spawns == []


def test_session_read_command_fails_and_queues_nothing(quick, spawns, monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    _looks(monkeypatch, daemon_client.UNRESPONSIVE)
    assert cli.main(["mesh", "history", "home"]) == 1
    err = capsys.readouterr().err
    assert "did not answer" in err and "claunch daemon start" in err
    assert _spooled() == []
    assert spawns == []


def test_a_write_outside_the_allow_list_is_not_queued(quick, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    client = daemon_client.SpoolingClient("daemon is not running")
    with pytest.raises(daemon_client.DaemonClientError) as exc:
        client.post("/api/sessions/s9/kill", {})
    assert not isinstance(exc.value, daemon_client.DaemonSpooled)
    assert _spooled() == []


def test_a_send_that_times_out_in_a_session_is_queued_with_its_id(monkeypatch):
    """Port 1 refuses: the transport fails after the client was handed out."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s-test")
    client = daemon_client.DaemonClient("http://127.0.0.1:1", "t")
    with pytest.raises(daemon_client.DaemonSpooled) as exc:
        client.post("/api/mesh/home/messages", {"from": "s-test", "to": "x", "body": "b"})
    rows = _spooled()
    assert [r["id"] for r in rows] == [exc.value.request_id]


# --------------------------------------------------------------------- #
# outside a session (claunch-4ui)
# --------------------------------------------------------------------- #
def test_user_shell_does_not_spawn_beside_an_unresponsive_daemon(quick, spawns, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _looks(monkeypatch, daemon_client.UNRESPONSIVE)
    with pytest.raises(daemon_client.DaemonClientError) as exc:
        daemon_client.ensure_running()
    assert "did not answer" in str(exc.value)
    assert "not running" not in str(exc.value)
    assert spawns == []


def test_user_shell_still_starts_an_absent_daemon_at_once(spawns, monkeypatch):
    """The user's decision on claunch-19liw.2: auto-start stays, and with the
    lock free nothing is coming back, so it does not wait first."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(daemon_client, "START_TIMEOUT", 0.2)
    _looks(monkeypatch, daemon_client.NOT_RUNNING)
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    t0 = time.monotonic()
    with pytest.raises(daemon_client.DaemonClientError, match="did not come up"):
        daemon_client.ensure_running()
    assert len(spawns) == 1
    assert time.monotonic() - t0 < 2.0


# --------------------------------------------------------------------- #
# the daemon applies the queue once
# --------------------------------------------------------------------- #
def _mesh():
    mgr = _Manager()
    for n in ("lead", "s1"):
        mgr.add(n)
    mm = MeshManager(mgr, settle=0.01)
    mm.create("home")
    home = mm.get("home")
    _member(home, "lead", "lead", role="leader")
    _member(home, "w1", "s1", role="worker")
    _connect(home, "lead", "w1")
    return mm, home


def _queue(text: str = "hello", sender: str = "s1") -> str:
    rid = request_spool.new_request_id()
    request_spool.put(sender, "POST", "/api/mesh/home/messages",
                      {"from": sender, "to": "lead", "body": text}, rid)
    return rid


def test_drain_delivers_once_from_the_original_sender_with_its_time():
    mm, home = _mesh()
    rid = _queue()
    queued_at = _spooled()[0]["queued_at"]
    app = {"mesh": mm, "manager": None, "spool_seen": request_spool.SeenIds()}

    assert asyncio.run(spool_drain.drain_once(app)) == 1
    assert asyncio.run(spool_drain.drain_once(app)) == 0
    sent = [m for m in home.messages if "hello" in str(m.get("body"))]
    assert len(sent) == 1
    assert sent[0]["from"] == "w1"
    assert sent[0]["ref"]["spooled"] == {"request_id": rid, "queued_at": queued_at}
    assert _spooled() == []


def test_an_id_applied_before_a_restart_is_not_applied_again():
    mm, home = _mesh()
    rid = _queue()
    request_spool.SeenIds().add(rid)          # the previous daemon took it live
    app = {"mesh": mm, "manager": None, "spool_seen": request_spool.SeenIds()}
    assert asyncio.run(spool_drain.drain_once(app)) == 0
    assert not [m for m in home.messages if "hello" in str(m.get("body"))]
    assert _spooled() == []


def test_a_request_that_cannot_be_applied_is_kept_in_failed():
    mm, _ = _mesh()
    rid = request_spool.new_request_id()
    request_spool.put("s1", "POST", "/api/mesh/nosuch/messages",
                      {"from": "s1", "to": "lead", "body": "x"}, rid)
    app = {"mesh": mm, "manager": None, "spool_seen": request_spool.SeenIds()}
    assert asyncio.run(spool_drain.drain_once(app)) == 0
    kept = json.loads((request_spool.root() / "failed" / f"{rid}.json").read_text())
    assert kept["id"] == rid and kept["error"]
    assert _spooled() == []


def test_seen_ids_expire_and_survive_reload(monkeypatch):
    seen = request_spool.SeenIds()
    seen.add("rq-a")
    assert "rq-a" in request_spool.SeenIds()
    assert "rq-a" not in request_spool.SeenIds(ttl=-1.0)


def test_the_live_daemon_drains_at_startup_and_answers_a_resend_as_duplicate():
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mm, home = _mesh()
        rid = _queue("from the spool")
        app = build_app(mm.manager, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit", request_spool.HEADER: rid}
        try:
            for _ in range(50):
                if not _spooled():
                    break
                await asyncio.sleep(0.05)
            resp = await client.post(
                "/api/mesh/home/messages", headers=auth,
                json={"from": "s1", "to": "lead", "body": "from the spool"},
            )
            data = await resp.json()
            assert resp.status == 200, data
            assert data["duplicate"] == rid
        finally:
            await client.close()
        return home

    home = asyncio.run(run())
    assert len([m for m in home.messages if "from the spool" in str(m.get("body"))]) == 1
