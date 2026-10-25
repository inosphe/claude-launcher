"""The daemon owns the routing shims (claunch-19liw.2.3).

Outside the daemon a shim is asked for, never started; the daemon starts the
shims its config wants, keeps the ones a live user holds, and retires the
rest once nothing needs them.
"""

from __future__ import annotations

import os
import time

import pytest

from claude_launcher import daemon_client, routing, store
from claude_launcher.daemon import shims as shims_mod


@pytest.fixture
def owner(home, monkeypatch):
    """This test process stands in for the daemon."""
    monkeypatch.setenv("CLAUNCH_METERING", "1")
    monkeypatch.setattr(routing, "_OWNER", True)
    yield
    routing.stop()


#: A shim is found by its fingerprint's ports, not by the home it was recorded
#: in, so two xdist workers (or two sessions' runs) asking for the same
#: upstream share one shim and one's teardown stops the other's. Upstreams a
#: test starts and retires carry this process's own suffix.
_RUN = os.urandom(3).hex()


def _upstream(name: str) -> str:
    return f"https://{name}-{_RUN}.example/anthropic"


def _providers(**upstreams):
    def _mutate(doc):
        doc["providers"] = {
            name: {"env": {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": "sk-x"}}
            for name, url in upstreams.items()
        }

    store.update(_mutate)


# --------------------------------------------------------------------------- #
# outside the daemon: look up, never start
# --------------------------------------------------------------------------- #
def test_outside_the_daemon_a_missing_shim_is_an_error_and_nothing_is_spawned(home, monkeypatch):
    monkeypatch.setattr(routing, "_spawn", lambda *a, **k: pytest.fail("only the daemon spawns"))
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    with pytest.raises(routing.ShimUnavailable, match="daemon"):
        routing.ensure_shim(_upstream("u"), {})


def test_outside_the_daemon_metering_does_not_fall_back_to_the_upstream(home, monkeypatch):
    monkeypatch.setenv("CLAUNCH_METERING", "1")
    monkeypatch.setattr(routing, "_spawn", lambda *a, **k: pytest.fail("only the daemon spawns"))
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    _providers(deepseek=_upstream("d"))
    with pytest.raises(routing.ShimUnavailable):
        routing.front(_upstream("d"), "deepseek")


def test_outside_the_daemon_the_shim_is_asked_of_the_daemon(home, monkeypatch):
    calls = []

    class _Client:
        def post(self, path, body, **kw):
            calls.append((path, body))
            return {"url": "http://127.0.0.1:31600/"}

    monkeypatch.setattr(routing, "_spawn", lambda *a, **k: pytest.fail("only the daemon spawns"))
    monkeypatch.setattr(daemon_client, "connect", lambda: _Client())
    assert routing.ensure_shim(_upstream("u"), {"order": ["x"]}, "p") == "http://127.0.0.1:31600/"
    (path, body), = calls
    assert path == routing.ENSURE_API
    assert body["upstream"] == _upstream("u") and body["spec"] == {"order": ["x"]}
    assert body["provider"] == "p" and isinstance(body["pid"], int)


def test_outside_the_daemon_a_running_shim_is_used_when_the_daemon_is_down(owner, monkeypatch):
    url = routing.ensure_shim(_upstream("u"), {})
    monkeypatch.setattr(routing, "_OWNER", False)
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    monkeypatch.setattr(routing, "_spawn", lambda *a, **k: pytest.fail("only the daemon spawns"))
    assert routing.ensure_shim(_upstream("u"), {}) == url


# --------------------------------------------------------------------------- #
# the daemon: one shim per provider, replaced on a config change
# --------------------------------------------------------------------------- #
def test_reconcile_starts_one_shim_per_provider_and_no_duplicates(owner):
    _providers(a=_upstream("a"), b=_upstream("b"))
    shims_mod.reconcile_once(None)
    shims_mod.reconcile_once(None)
    live = routing.instances()
    assert sorted(i["upstream"] for i in live) == [_upstream("a"), _upstream("b")]


def test_a_config_change_starts_the_new_shim_and_retires_the_old_once_unused(owner):
    _providers(a=_upstream("a"))
    routing.reconcile([])
    (old,) = routing.instances()
    routing.add_user(old["port"], session="s1")

    _providers(a=_upstream("a2"))
    later = time.time() + routing.GRACE_S + 1
    routing.reconcile(["s1"], now=later)
    upstreams = sorted(i["upstream"] for i in routing.instances())
    # the new one is up, the old one stays while s1 (launched against it) lives
    assert upstreams == [_upstream("a"), _upstream("a2")]

    result = routing.reconcile([], now=later)
    assert result["retired"] == [f"{old['fingerprint']}:{old['port']}"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and routing.health(old["port"]) is not None:
        time.sleep(0.05)
    assert [i["upstream"] for i in routing.instances()] == [_upstream("a2")]


def test_a_new_shim_is_not_retired_within_the_grace(owner):
    url = routing.ensure_shim(_upstream("launch-only"), {})
    assert routing.reconcile([])["retired"] == []
    assert routing.health(int(url.rsplit(":", 1)[1].strip("/"))) is not None


def test_a_session_on_the_shim_is_registered_as_its_user(owner):
    url = routing.ensure_shim(_upstream("u"), {})
    env = {"ANTHROPIC_BASE_URL": url}
    routing.apply_session(env, "s42")
    port = routing._port_of(url)
    assert routing._load_users()[str(port)]["sessions"] == ["s42"]
    # kept while s42 lives, past the grace
    assert routing.reconcile(["s42"], now=time.time() + routing.GRACE_S + 1)["retired"] == []
    assert routing._load_users()[str(port)]["sessions"] == ["s42"]


def test_a_session_registered_during_a_pass_is_kept_and_its_shim_not_retired(owner, monkeypatch):
    # A shim the config does not want (a Pi or dynamic route) that a new
    # session attaches to while a reconcile pass is running: the pass took its
    # users snapshot before the session existed (s1404 review of 723b944c).
    url = routing.ensure_shim(_upstream("launch-only"), {})
    port = routing._port_of(url)
    real_instances = routing.instances

    def _instances_while_a_session_launches():
        rows = real_instances()
        routing.add_user(port, session="late")  # harness.build_command -> apply_session
        return rows

    monkeypatch.setattr(routing, "instances", _instances_while_a_session_launches)
    later = time.time() + routing.GRACE_S + 1
    assert routing.reconcile([], now=later)["retired"] == []
    assert routing._load_users()[str(port)]["sessions"] == ["late"]
    monkeypatch.setattr(routing, "instances", real_instances)
    # the next pass sees "late" alive and keeps it
    assert routing.reconcile(["late"], now=later)["retired"] == []
    assert routing.health(port) is not None
    # and once it has ended, the shim goes
    assert routing.reconcile([], now=later)["retired"] == [f"{routing.fingerprint(_upstream("launch-only"), {})}:{port}"]


def test_a_shim_handed_out_again_is_within_the_grace(owner):
    url = routing.ensure_shim(_upstream("launch-only"), {})
    port = routing._port_of(url)
    fp = routing.fingerprint(_upstream("launch-only"), {})
    record = routing._record_file(fp, port)
    data = routing._read_json(record)
    data["ensured"] = time.time() - routing.GRACE_S - 5  # handed out long ago
    record.write_text(__import__("json").dumps(data), encoding="utf-8")
    routing.ensure_shim(_upstream("launch-only"), {})  # a launch takes it again
    assert routing.reconcile([])["retired"] == []


def test_a_pass_cannot_retire_a_shim_between_its_lookup_and_its_stamp(owner, monkeypatch):
    # s1424 review of fac43725: the reuse path found the shim, a pass retired
    # it on the old "ensured" stamp, and the launch got the URL of a shim
    # that then stopped. The pass runs in its own thread, as in the daemon.
    import threading

    url = routing.ensure_shim(_upstream("launch-only"), {})
    port = routing._port_of(url)
    fp = routing.fingerprint(_upstream("launch-only"), {})
    record = routing._record_file(fp, port)
    data = routing._read_json(record)
    data["ensured"] = time.time() - routing.GRACE_S - 5  # handed out long ago
    record.write_text(__import__("json").dumps(data), encoding="utf-8")

    real_find = routing._find
    result = {}
    pass_ = threading.Thread(target=lambda: result.update(routing.reconcile([])))

    def _find_then_a_pass(fp_):
        found = real_find(fp_)
        if not pass_.is_alive() and not result:
            pass_.start()
            pass_.join(timeout=2)  # long enough for an unguarded pass to retire it
        return found

    monkeypatch.setattr(routing, "_find", _find_then_a_pass)
    assert routing.ensure_shim(_upstream("launch-only"), {}) == url
    routing.apply_session({"ANTHROPIC_BASE_URL": url}, "s-new")
    pass_.join(timeout=10)
    assert result["retired"] == []
    assert routing.health(port) is not None


def test_a_shim_told_to_stop_is_not_handed_out(home, monkeypatch):
    fp = routing.fingerprint(_upstream("u"), {})
    port = routing.candidate_ports(fp)[0]
    monkeypatch.setattr(routing, "_port_free", lambda p: p != port)
    info = {"marker": routing.MARKER, "fingerprint": fp, "inflight": 1}
    monkeypatch.setattr(routing, "health", lambda p, timeout=0.6: dict(info) if p == port else None)
    assert routing._find(fp) == port
    info["stopping"] = True
    assert routing._find(fp) is None


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX TIME_WAIT rule")
def test_a_port_left_in_time_wait_by_an_exited_shim_reads_free(home):
    # The shim closes each health probe's connection first, so its port keeps
    # a TIME_WAIT after it exits; tests that start and stop shims back to back
    # ran out of the eight candidate ports that way.
    import socket

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = server.accept()
    conn.close()  # the server side closes first -> TIME_WAIT on ``port``
    client.recv(1)
    client.close()
    server.close()
    assert routing._port_free(port)
    # a live listener still reads taken
    live = socket.socket()
    live.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    live.bind(("127.0.0.1", port))
    live.listen()
    try:
        assert not routing._port_free(port)
    finally:
        live.close()


def test_the_ensure_api_registers_the_asking_process(owner):
    url = shims_mod._ensure_for({"upstream": _upstream("u"), "spec": {}, "pid": 4242})
    assert routing._load_users()[str(routing._port_of(url))]["pids"] == [4242]


def test_a_shim_from_an_older_build_is_never_retired(owner, monkeypatch):
    # No ``inflight`` in its health answer: it cannot say whether it is mid-stream.
    assert routing.retire({"port": 31999, "fingerprint": "x"}) is False


def test_an_idle_only_shutdown_waits_for_the_request_in_flight(home):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher import routing_shim

    async def run():
        app = routing_shim.build_app("https://u.example/", {}, "fp")
        app["stop"] = asyncio.Event()
        async with TestClient(TestServer(app)) as client:
            assert (await (await client.get(routing.HEALTH_PATH)).json())["stopping"] is False
            app["inflight"] = 1  # a stream still going through
            resp = await client.post(routing.SHUTDOWN_PATH + "?when=idle")
            assert (await resp.json())["inflight"] == 1
            # still serving that stream, but no longer handed out
            assert (await (await client.get(routing.HEALTH_PATH)).json())["stopping"] is True
            await asyncio.sleep(0.1)
            assert not app["stop"].is_set()
            app["inflight"] = 0
            resp = await client.post(routing.SHUTDOWN_PATH + "?when=idle")
            assert "stopped" in await resp.json()
            await asyncio.sleep(0.1)
            assert app["stop"].is_set()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# tests leave no shim behind (claunch-19liw.2.5)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# the first pass waits for the boot's restore (claunch-esmr3)
# --------------------------------------------------------------------------- #
def test_no_reconcile_pass_runs_before_the_fleet_is_restored(owner, monkeypatch):
    """A pass during restore sees no sessions and retires their shims.

    On 2026-10-24 and again on 2026-10-25 the boot's first pass ran seconds
    ahead of ``adopted ... s1736``, pruned s1736's shim users and stopped the
    shim that session was still talking to. Its process then hung on connect
    (a closed loopback port here times out instead of being refused) and
    retried ``Request timed out.`` until it was restarted (claunch-esmr3).
    """
    import asyncio

    calls = []
    monkeypatch.setattr(shims_mod, "reconcile_once",
                        lambda manager: calls.append(("pass", len(calls))) or
                        {"started": [], "retired": [], "errors": []})
    # the second pass is the one the test would wait RECONCILE_S for
    monkeypatch.setattr(routing, "RECONCILE_S", 0.05)

    async def run():
        app = {"boot": {"phase": "restoring"}, "shims_manager": None}
        task = asyncio.get_running_loop().create_task(shims_mod._loop(app))
        try:
            await asyncio.sleep(0.2)
            assert calls == [], "a pass ran while the boot was still restoring"
            app["boot"]["phase"] = "ready"
            for _ in range(100):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.05)
            assert len(calls) >= 2, "the pass never ran after the restore finished"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_the_boot_wait_gives_up_and_reconciles_anyway(owner, monkeypatch):
    """A restore that never finishes must not hold the shims forever."""
    import asyncio

    monkeypatch.setattr(shims_mod, "BOOT_WAIT_S", 0.1)
    calls = []
    monkeypatch.setattr(shims_mod, "reconcile_once",
                        lambda manager: calls.append(1) or
                        {"started": [], "retired": [], "errors": []})
    monkeypatch.setattr(routing, "RECONCILE_S", 0.05)

    async def run():
        app = {"boot": {"phase": "restoring"}, "shims_manager": None}
        task = asyncio.get_running_loop().create_task(shims_mod._loop(app))
        try:
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.05)
            assert calls, "the pass never ran after the boot wait expired"
            assert app["boot"]["phase"] == "restoring", "the wait must not end the boot"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_a_shim_a_test_started_is_reaped_even_when_its_record_is_gone(owner, spawned_shims):
    import shutil

    routing.ensure_shim(_upstream("reap"), {})
    (child,) = spawned_shims
    # The temp home the record lived in is gone: routing.stop() is blind,
    # which is how two test shims outlived their run.
    shutil.rmtree(routing.state_dir())
    assert routing.stop() == []
    assert child.poll() is None
    spawned_shims.reap()
    assert child.poll() is not None
