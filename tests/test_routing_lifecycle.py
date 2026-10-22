"""The daemon owns the routing shims (claunch-19liw.2.3).

Outside the daemon a shim is asked for, never started; the daemon starts the
shims its config wants, keeps the ones a live user holds, and retires the
rest once nothing needs them.
"""

from __future__ import annotations

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
        routing.ensure_shim("https://u.example/", {})


def test_outside_the_daemon_metering_does_not_fall_back_to_the_upstream(home, monkeypatch):
    monkeypatch.setenv("CLAUNCH_METERING", "1")
    monkeypatch.setattr(routing, "_spawn", lambda *a, **k: pytest.fail("only the daemon spawns"))
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    _providers(deepseek="https://d.example/anthropic")
    with pytest.raises(routing.ShimUnavailable):
        routing.front("https://d.example/anthropic", "deepseek")


def test_outside_the_daemon_the_shim_is_asked_of_the_daemon(home, monkeypatch):
    calls = []

    class _Client:
        def post(self, path, body, **kw):
            calls.append((path, body))
            return {"url": "http://127.0.0.1:31600/"}

    monkeypatch.setattr(routing, "_spawn", lambda *a, **k: pytest.fail("only the daemon spawns"))
    monkeypatch.setattr(daemon_client, "connect", lambda: _Client())
    assert routing.ensure_shim("https://u.example/", {"order": ["x"]}, "p") == "http://127.0.0.1:31600/"
    (path, body), = calls
    assert path == routing.ENSURE_API
    assert body["upstream"] == "https://u.example/" and body["spec"] == {"order": ["x"]}
    assert body["provider"] == "p" and isinstance(body["pid"], int)


def test_outside_the_daemon_a_running_shim_is_used_when_the_daemon_is_down(owner, monkeypatch):
    url = routing.ensure_shim("https://u.example/", {})
    monkeypatch.setattr(routing, "_OWNER", False)
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    monkeypatch.setattr(routing, "_spawn", lambda *a, **k: pytest.fail("only the daemon spawns"))
    assert routing.ensure_shim("https://u.example/", {}) == url


# --------------------------------------------------------------------------- #
# the daemon: one shim per provider, replaced on a config change
# --------------------------------------------------------------------------- #
def test_reconcile_starts_one_shim_per_provider_and_no_duplicates(owner):
    _providers(a="https://a.example/anthropic", b="https://b.example/anthropic")
    shims_mod.reconcile_once(None)
    shims_mod.reconcile_once(None)
    live = routing.instances()
    assert sorted(i["upstream"] for i in live) == [
        "https://a.example/anthropic", "https://b.example/anthropic"]


def test_a_config_change_starts_the_new_shim_and_retires_the_old_once_unused(owner):
    _providers(a="https://a.example/anthropic")
    routing.reconcile([])
    (old,) = routing.instances()
    routing.add_user(old["port"], session="s1")

    _providers(a="https://a2.example/anthropic")
    later = time.time() + routing.GRACE_S + 1
    routing.reconcile(["s1"], now=later)
    upstreams = sorted(i["upstream"] for i in routing.instances())
    # the new one is up, the old one stays while s1 (launched against it) lives
    assert upstreams == ["https://a.example/anthropic", "https://a2.example/anthropic"]

    result = routing.reconcile([], now=later)
    assert result["retired"] == [f"{old['fingerprint']}:{old['port']}"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and routing.health(old["port"]) is not None:
        time.sleep(0.05)
    assert [i["upstream"] for i in routing.instances()] == ["https://a2.example/anthropic"]


def test_a_new_shim_is_not_retired_within_the_grace(owner):
    url = routing.ensure_shim("https://launch-only.example/", {})
    assert routing.reconcile([])["retired"] == []
    assert routing.health(int(url.rsplit(":", 1)[1].strip("/"))) is not None


def test_a_session_on_the_shim_is_registered_as_its_user(owner):
    url = routing.ensure_shim("https://u.example/", {})
    env = {"ANTHROPIC_BASE_URL": url}
    routing.apply_session(env, "s42")
    port = routing._port_of(url)
    assert routing._load_users()[str(port)]["sessions"] == ["s42"]
    # kept while s42 lives, past the grace
    assert routing.reconcile(["s42"], now=time.time() + routing.GRACE_S + 1)["retired"] == []
    assert routing._load_users()[str(port)]["sessions"] == ["s42"]


def test_the_ensure_api_registers_the_asking_process(owner):
    url = shims_mod._ensure_for({"upstream": "https://u.example/", "spec": {}, "pid": 4242})
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
            app["inflight"] = 1  # a stream still going through
            resp = await client.post(routing.SHUTDOWN_PATH + "?when=idle")
            assert (await resp.json())["inflight"] == 1
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
def test_a_shim_a_test_started_is_reaped_even_when_its_record_is_gone(owner, spawned_shims):
    import shutil

    routing.ensure_shim("https://reap.example/", {})
    (child,) = spawned_shims
    # The temp home the record lived in is gone: routing.stop() is blind,
    # which is how two test shims outlived their run.
    shutil.rmtree(routing.state_dir())
    assert routing.stop() == []
    assert child.poll() is None
    spawned_shims.reap()
    assert child.poll() is not None
