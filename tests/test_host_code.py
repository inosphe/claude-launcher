"""Which code the PTY host runs (claunch-xnnct.3).

The host reports a code id in its hello reply; the daemon compares it with
the code it loaded (``GET /api/daemon`` pty_host: off / connected / stale /
unreachable) and with the code on disk now (``GET /api/daemon/restart-check``).
A keep restart that would leave the host on other code is refused with 409
-- the shutdown and restart endpoints, the gate's approval and its timeout --
unless the caller says ``keep_stale``; ``--with-pty-host`` (mode full)
replaces the host and is never checked. The CLI refuses with exit 2 and the
two ways forward.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import pytest

from claude_launcher.daemon import host_code, pty_backend
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.ptyhost import changed_files, code_files, code_id

AUTH = {"Authorization": "Bearer sekrit"}


def _tree(root: Path, server: bytes = b"x = 1\n") -> Path:
    (root / "ptyhost").mkdir(parents=True, exist_ok=True)
    (root / "ptyhost" / "__init__.py").write_bytes(b"PROTO = 2\n")
    (root / "ptyhost" / "server.py").write_bytes(server)
    (root / "pty_backend.py").write_bytes(b"def spawn(): pass\n")
    return root


class _Host:
    """The attributes of a connected ``HostClient`` the comparisons read."""

    def __init__(self, files, code=None, pid=4242):
        self.host_pid = pid
        self.host_proto = 2
        self.host_code_files = files
        self.host_code = code_id(files) if code is None else code
        self.host_started_at = 1700000000.0

    def session_count(self):
        return 3


@pytest.fixture
def loaded(monkeypatch):
    """The daemon's loaded code is the real tree; no host unless a test sets one."""
    monkeypatch.setattr(host_code, "_loaded", None)
    monkeypatch.setattr(host_code, "_configured", False)
    monkeypatch.setattr(pty_backend, "current_host", lambda: None)
    return code_files()


def _with_host(monkeypatch, host):
    monkeypatch.setattr(pty_backend, "current_host", lambda: host)


def _stale_host():
    files = dict(code_files())
    files["ptyhost/server.py"] = "0" * 64
    return _Host(files)


# --------------------------------------------------------------------------- #
# the id
# --------------------------------------------------------------------------- #
def test_the_code_id_follows_the_bytes_of_the_host_files(tmp_path):
    a = code_files(_tree(tmp_path / "a"))
    b = code_files(_tree(tmp_path / "b"))
    assert set(a) == {"ptyhost/__init__.py", "ptyhost/server.py", "pty_backend.py"}
    assert code_id(a) == code_id(b)
    assert code_id(a).startswith("p2-") and len(code_id(a)) == len("p2-") + 16
    c = code_files(_tree(tmp_path / "c", server=b"x = 2\n"))
    assert code_id(c) != code_id(a)
    assert changed_files(a, c) == ["ptyhost/server.py"]
    # A file that appears or goes away changes it too.
    (tmp_path / "c" / "ptyhost" / "extra.py").write_bytes(b"")
    d = code_files(tmp_path / "c")
    assert changed_files(c, d) == ["ptyhost/extra.py"]


def test_the_real_tree_hashes_the_host_and_the_backend():
    files = code_files()
    assert "ptyhost/server.py" in files and "pty_backend.py" in files
    # host_code is the daemon's side of the comparison, never run by the host.
    assert not any("host_code" in rel for rel in files)


# --------------------------------------------------------------------------- #
# the state GET /api/daemon reports
# --------------------------------------------------------------------------- #
def test_no_host_is_off_or_unreachable(loaded, monkeypatch):
    info = host_code.info()
    assert info["state"] == "off" and info["stale"] is False and info["pid"] is None
    assert info["daemon_code"] == code_id(loaded)
    assert isinstance(info["now"], float)
    host_code.configure(True)
    assert host_code.info()["state"] == "unreachable"


def test_a_host_on_the_daemons_code_is_connected(loaded, monkeypatch):
    _with_host(monkeypatch, _Host(dict(loaded)))
    info = host_code.info()
    assert info["state"] == "connected" and info["stale"] is False
    assert (info["pid"], info["proto"], info["sessions"]) == (4242, 2, 3)
    assert info["started_at"] == 1700000000.0
    assert info["code"] == info["daemon_code"]


def test_a_host_on_other_code_is_stale(loaded, monkeypatch):
    _with_host(monkeypatch, _stale_host())
    info = host_code.info()
    assert info["state"] == "stale" and info["stale"] is True


def test_a_host_that_reports_no_code_is_stale(loaded, monkeypatch):
    # A proto-2 host from before the code id (claunch-xnnct.1) says nothing.
    _with_host(monkeypatch, _Host({}, code=None))
    host = pty_backend.current_host()
    host.host_code = None
    assert host_code.info()["state"] == "stale"
    check = host_code.keep_check()
    assert check["stale"] is True and check["host_code"] is None
    assert "ptyhost/server.py" in check["changed"] or check["more"]


def test_the_loaded_code_is_read_once(loaded, monkeypatch):
    first = host_code.loaded()
    monkeypatch.setattr(host_code, "code_files", lambda: {"x": "y"})
    assert host_code.loaded() is first


# --------------------------------------------------------------------------- #
# the keep check and its refusal
# --------------------------------------------------------------------------- #
def test_the_keep_check_compares_the_host_with_the_disk(loaded, monkeypatch):
    _with_host(monkeypatch, _Host(dict(loaded)))
    assert host_code.keep_check()["stale"] is False
    # The daemon loaded the same code the host runs; the checkout moved since.
    moved = dict(loaded)
    moved["ptyhost/server.py"] = "1" * 64
    monkeypatch.setattr(host_code, "code_files", lambda: moved)
    check = host_code.keep_check()
    assert check["stale"] is True and check["changed"] == ["ptyhost/server.py"]
    assert check["pid"] == 4242 and check["disk_code"] == code_id(moved)
    msg = host_code.refusal(check)
    assert "pid 4242" in msg and "ptyhost/server.py" in msg


def test_the_refusal_names_a_few_files_and_counts_the_rest():
    check = {"pid": 7, "changed": ["a", "b", "c", "d", "e"], "more": 3}
    assert "a, b, c, d, e and 3 more" in host_code.refusal(check)


def test_no_host_means_nothing_to_refuse(loaded):
    assert host_code.keep_check() == {"stale": False, "pid": None, "host_code": None,
                                      "disk_code": None, "changed": [], "more": 0}


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #
def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _client(gate_timeout=600):
    from aiohttp.test_utils import TestClient, TestServer

    mgr = _manager()
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=gate_timeout)
    client = TestClient(TestServer(app))
    await client.start_server()
    return mgr, app, client


def test_the_daemon_info_and_the_restart_check_carry_it(loaded, monkeypatch):
    _with_host(monkeypatch, _stale_host())

    async def run():
        mgr, app, client = await _client()
        try:
            info = await (await client.get("/api/daemon", headers=AUTH)).json()
            assert info["pty_host"]["state"] == "stale"
            check = await (await client.get("/api/daemon/restart-check", headers=AUTH)).json()
            assert check["stale"] is True and check["changed"] == ["ptyhost/server.py"]
            assert "refused" in check["message"] and "--keep-stale-host" in check["hint"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("path, body, status, mode", [
    ("/api/daemon/restart", None, 409, "stop"),
    ("/api/daemon/restart", {"mode": "keep"}, 409, "stop"),
    ("/api/daemon/restart", {"mode": "keep", "keep_stale": True}, 200, "keep"),
    # Only the literal true says keep it anyway.
    ("/api/daemon/restart", {"mode": "keep", "keep_stale": "yes"}, 409, "stop"),
    ("/api/daemon/restart", {"mode": "full"}, 200, "full"),
    ("/api/daemon/shutdown", {"mode": "keep"}, 409, "stop"),
    ("/api/daemon/shutdown", {"mode": "keep", "keep_stale": True}, 200, "keep"),
    # A stop ends the host anyway: nothing is kept, nothing to refuse.
    ("/api/daemon/shutdown", None, 200, "stop"),
])
def test_a_keep_restart_onto_a_stale_host_is_refused(loaded, monkeypatch, path, body,
                                                     status, mode):
    _with_host(monkeypatch, _stale_host())

    async def run():
        mgr, app, client = await _client()
        try:
            kw = {"json": body} if body is not None else {}
            resp = await client.post(path, headers=AUTH, **kw)
            assert resp.status == status
            assert app["restart_mode"] == mode
            if status == 409:
                out = await resp.json()
                assert out["stale"]["stale"] is True and "hint" in out
                assert not app["shutdown_event"].is_set()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_current_host_restarts_without_a_word(loaded, monkeypatch):
    _with_host(monkeypatch, _Host(dict(loaded)))

    async def run():
        mgr, app, client = await _client()
        try:
            resp = await client.post("/api/daemon/restart", headers=AUTH)
            assert resp.status == 200 and app["restart_mode"] == "keep"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("answer, mode", [({"pty_host": "full"}, "full"),
                                          ({"keep_stale": True}, "keep")])
def test_the_gate_approval_is_refused_then_answered(loaded, monkeypatch, answer, mode):
    _with_host(monkeypatch, _stale_host())
    monkeypatch.setattr(
        "claude_launcher.daemon.runtime_state.read_daemon_json",
        lambda: {"pid": 4242, "started_at": "2026-08-26T02:36:00+00:00"},
    )

    async def run():
        mgr, app, client = await _client()
        try:
            await client.post("/api/daemon/restart-request", json={"session": "s3"}, headers=AUTH)
            refused = await client.post("/api/daemon/restart-request/approve", headers=AUTH)
            assert refused.status == 409
            out = await refused.json()
            assert out["stale"]["pid"] == 4242 and "refused" in out["error"]
            rec = (await (await client.get("/api/daemon/restart-request", headers=AUTH)).json())["request"]
            assert rec["status"] == "pending" and rec["stale"]["stale"] is True
            assert not app["shutdown_event"].is_set()
            ok = await client.post("/api/daemon/restart-request/approve", json=answer, headers=AUTH)
            assert ok.status == 200
            assert app["restart_requested"] is True and app["restart_mode"] == mode
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_gate_asked_to_keep_stale_approves_on_its_own(loaded, monkeypatch):
    _with_host(monkeypatch, _stale_host())

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=0.05)
        app["restart_gate"].submit(session="s5", keep_stale=True)
        try:
            await asyncio.wait_for(app["shutdown_event"].wait(), timeout=3.0)
        finally:
            await mgr.shutdown_all()
        assert app["restart_mode"] == "keep"

    asyncio.run(run())


def test_an_unanswered_stale_gate_restarts_nothing(loaded, monkeypatch):
    _with_host(monkeypatch, _stale_host())

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), gate_timeout=0.05)
        app["restart_gate"].submit(session="s5")
        try:
            await asyncio.sleep(0.4)
        finally:
            await mgr.shutdown_all()
        rec = app["restart_gate"].get()
        assert rec["status"] == "rejected" and rec["decided_by"] == "timeout"
        assert "pid 4242" in rec["refused"]
        assert not app["shutdown_event"].is_set()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #
def _args(**kw):
    base = dict(action="restart", all=False, force=False, with_pty_host=False,
                keep_stale_host=False)
    base.update(kw)
    return argparse.Namespace(**base)


class _Daemon:
    SERVING = "serving"
    NOT_RUNNING = "not_running"
    STALE_RECORD = "stale_record"
    WEDGED = "wedged"
    UNRESPONSIVE = "unresponsive"

    def __init__(self, check):
        self.check = check
        self.restarts = []

    def diagnose(self, **kw):
        return {"state": self.SERVING}

    def restart_check(self):
        return self.check

    def restart(self, **kw):
        self.restarts.append(kw)
        return argparse.Namespace(base_url="http://127.0.0.1:8377")


STALE_CHECK = {"stale": True, "pid": 4242, "changed": ["ptyhost/server.py"], "more": 0,
               "message": "refused: the PTY host (pid 4242) runs code that differs",
               "hint": host_code.HINT}


@pytest.mark.parametrize("kw, refused", [({}, True),
                                         ({"keep_stale_host": True}, False),
                                         ({"with_pty_host": True}, False)])
def test_the_cli_refuses_a_stale_keep_restart(monkeypatch, capsys, kw, refused):
    from claude_launcher import cli_sessions

    fake = _Daemon(STALE_CHECK)
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    rc = cli_sessions._cmd_daemon(_args(**kw))
    err = capsys.readouterr().err
    if refused:
        assert rc == 2 and fake.restarts == []
        assert "pid 4242" in err and "--keep-stale-host" in err and "--with-pty-host" in err
    else:
        assert rc == 0 and len(fake.restarts) == 1
        assert fake.restarts[0]["keep_stale"] is bool(kw.get("keep_stale_host"))


@pytest.mark.parametrize("check", [None, {"stale": False}])
def test_the_cli_restarts_silently_without_a_mismatch(monkeypatch, capsys, check):
    from claude_launcher import cli_sessions

    fake = _Daemon(check)
    monkeypatch.setattr(cli_sessions, "daemon_client", fake)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert cli_sessions._cmd_daemon(_args()) == 0
    assert len(fake.restarts) == 1
    assert "refused" not in capsys.readouterr().err


def test_the_keep_stale_host_flag_parses():
    from claude_launcher import cli

    assert cli.build_parser().parse_args(
        ["daemon", "restart", "--keep-stale-host"]).keep_stale_host is True
    assert cli.build_parser().parse_args(["daemon", "restart"]).keep_stale_host is False


@pytest.mark.parametrize("state, want", [
    ({"state": "off"}, "pty host: off"),
    ({"state": "unreachable", "pid": 9}, "unreachable"),
    ({"state": "connected", "pid": 5, "sessions": 2, "code": "p2-ab"},
     "pty host: pid 5, 2 session(s), code p2-ab"),
    ({"state": "stale", "pid": 5, "sessions": 1, "code": None}, "runs older code"),
])
def test_status_prints_the_pty_host_line(state, want):
    from claude_launcher import cli_sessions

    assert want in cli_sessions._pty_host_line(state)
