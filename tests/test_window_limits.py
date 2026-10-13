"""The test window's limits are set on the main daemon (claunch-k7x63).

The machine has one window (claunch-afmzw), so it has one set of limits. The
default daemon owns them: it publishes its effective values (config plus the
overrides set on its Window page) into the shared ``window.json``, and a named
instance on the same machine enforces what was published. Two
:class:`WindowManager` objects on one state file stand in for the two daemon
processes, as in ``test_window_machine.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from claude_launcher.daemon import window as window_mod
from claude_launcher.daemon.window import LimitsNotEditable, WindowManager


def _make(path, instance, owns, caps=(1, 3), **limits) -> WindowManager:
    shape = dict(window_mod.DEFAULT_LIMITS, **limits)
    return WindowManager(
        None,
        caps=lambda: caps,
        limits=lambda: shape,
        state_path=path,
        cores=32,
        instance=instance,
        owns_limits=owns,
    )


def test_a_sub_daemon_enforces_what_the_main_daemon_published(tmp_path):
    path = tmp_path / "window.json"
    main = _make(path, "", True, caps=(1, 3), worker_budget=12)
    sub = _make(path, "kimyori", False, caps=(2, 9), worker_budget=40)
    view = sub.limits_view()
    assert view["source"] == "published" and not view["editable"]
    assert view["values"]["targeted_cap"] == 3
    assert view["values"]["worker_budget"] == 12
    assert sub.status()["caps"] == {"sweep": 1, "targeted": 3}
    assert main.limits_view()["editable"]


def test_a_sub_daemon_uses_its_own_config_until_something_is_published(tmp_path):
    sub = _make(tmp_path / "window.json", "kimyori", False, caps=(1, 7))
    view = sub.limits_view()
    assert view["source"] == "own"
    assert view["values"]["targeted_cap"] == 7


def test_only_the_main_daemon_sets_limits(tmp_path):
    path = tmp_path / "window.json"
    main = _make(path, "", True)
    sub = _make(path, "kimyori", False)
    with pytest.raises(LimitsNotEditable):
        sub.set_limits({"worker_budget": 6})
    main.set_limits({"worker_budget": 6, "targeted_cap": 2}, by="cli")
    view = sub.limits_view()
    assert view["values"]["worker_budget"] == 6
    assert view["values"]["targeted_cap"] == 2
    assert view["set_by"] == "cli" and view["set_at"]
    assert sub.status()["caps"]["targeted"] == 2


def test_clearing_an_override_returns_to_the_config_value(tmp_path):
    path = tmp_path / "window.json"
    main = _make(path, "", True, worker_budget=12)
    main.set_limits({"worker_budget": 5})
    assert main.limits_view()["overrides"] == {"worker_budget": 5}
    main.set_limits({"worker_budget": None})
    view = main.limits_view()
    assert view["overrides"] == {}
    assert view["values"]["worker_budget"] == 12


def test_overrides_survive_a_restart_of_the_main_daemon(tmp_path):
    path = tmp_path / "window.json"
    _make(path, "", True).set_limits({"sweep_width": 16})
    again = _make(path, "", True)
    assert again.limits_view()["values"]["sweep_width"] == 16


def test_the_main_daemons_config_edits_are_republished(tmp_path):
    path = tmp_path / "window.json"
    shape = dict(window_mod.DEFAULT_LIMITS, worker_budget=12)
    main = WindowManager(
        None, caps=lambda: (1, 3), limits=lambda: shape, state_path=path,
        cores=32, instance="", owns_limits=True,
    )
    sub = _make(path, "kimyori", False)
    shape["worker_budget"] = 20
    # The owner republishes its effective values on every save.
    main.set_limits({"sweep_width": 9})
    assert sub.limits_view()["values"]["worker_budget"] == 20


@pytest.mark.parametrize("changes", [
    {"worker_budget": -1},
    {"sweep_cap": 99},
    {"no_such_limit": 1},
    {"targeted_width": "wide"},
    {"targeted_width": 2.5},
])
def test_a_bad_request_changes_nothing(tmp_path, changes):
    main = _make(tmp_path / "window.json", "", True)
    before = main.limits_view()["values"]
    with pytest.raises(ValueError):
        main.set_limits(changes)
    assert main.limits_view()["values"] == before


def test_raising_a_cap_admits_a_waiter(tmp_path):
    async def run():
        main = _make(tmp_path / "window.json", "", True, caps=(1, 1))
        first = await main.acquire("targeted", session="a", pid=os.getpid())
        assert first["granted"]
        waiter = asyncio.create_task(
            main.acquire("targeted", session="b", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        main.set_limits({"targeted_cap": 2})
        granted = await asyncio.wait_for(waiter, 5)
        assert granted["granted"]

    asyncio.run(run())


def test_the_limits_block_is_kept_in_the_shared_state_file(tmp_path):
    path = tmp_path / "window.json"
    _make(path, "", True).set_limits({"targeted_per_session": 2}, by="web")
    doc = json.loads(path.read_text(encoding="utf-8"))
    block = doc["limits"]
    assert block["overrides"] == {"targeted_per_session": 2}
    assert block["values"]["targeted_per_session"] == 2
    assert block["by"] == "default" and block["set_by"] == "web"


def test_window_limits_api(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    path = tmp_path / "window.json"

    async def run():
        manager = SessionManager(
            idle_threshold=0.5, scrollback=100, restore_default=False
        )
        main = _make(path, "", True)
        sub = _make(path, "kimyori", False)
        headers = {"Authorization": "Bearer secret"}
        main_app = build_app(manager, "secret", started_at=time.monotonic(), window=main)
        sub_app = build_app(manager, "secret", started_at=time.monotonic(), window=sub)
        main_client = TestClient(TestServer(main_app))
        sub_client = TestClient(TestServer(sub_app))
        await main_client.start_server()
        await sub_client.start_server()
        try:
            got = await (await main_client.get(
                "/api/window/limits", headers=headers)).json()
            assert got["editable"] and "worker_budget" in got["values"]
            resp = await main_client.put(
                "/api/window/limits",
                json={"limits": {"worker_budget": 7}, "by": "web"},
                headers=headers,
            )
            assert resp.status == 200
            assert (await resp.json())["values"]["worker_budget"] == 7
            bad = await main_client.put(
                "/api/window/limits", json={"limits": {"sweep_cap": 0}},
                headers=headers,
            )
            assert bad.status == 400
            refused = await sub_client.put(
                "/api/window/limits", json={"limits": {"worker_budget": 3}},
                headers=headers,
            )
            assert refused.status == 409
            assert (await refused.json())["editable"] is False
            status = await (await sub_client.get(
                "/api/window", headers=headers)).json()
            assert status["limits_control"]["values"]["worker_budget"] == 7
            assert status["limits_control"]["source"] == "published"
        finally:
            await main_client.close()
            await sub_client.close()

    asyncio.run(run())
