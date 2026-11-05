"""/api/vm (vm-setup-8s6, step 3): who may call what, and the lifecycle
routes over a fake tart."""
from __future__ import annotations

import asyncio
import contextlib

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import actors, vm_api, vm_tart
import pytest

from claude_launcher.daemon.vmhub import Registry, VmError

from claude_launcher.daemon import vm_channel
from test_vm_channel import World
from test_vm_tart import Fake

# The VM routes answer vm_disabled until the feature is switched on.
pytestmark = pytest.mark.usefixtures("vm_on")

AGENT = {actors.HEADER: "agent:s1"}
OTHER = {actors.HEADER: "agent:s2"}


@contextlib.asynccontextmanager
async def _api(tmp_path):
    fake = Fake()
    reg = Registry()
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5)
    world = World()

    async def no_sleep(_):
        await asyncio.sleep(0)

    hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=no_sleep)
    app = web.Application()
    vm_api.install(app, reg, mgr, hub)
    async with TestClient(TestServer(app)) as client:
        client.fake, client.reg, client.hub, client.world, client.mgr = fake, reg, hub, world, mgr
        yield client


def scenario(fn):
    """Run an async test body against a fresh app (the suite drives aiohttp
    with asyncio.run, as test_webdebug does)."""
    def test(tmp_path, monkeypatch):
        # Screenshots land under the daemon directory: keep it in the test's.
        monkeypatch.setattr(vm_api.paths, "daemon_dir", lambda: tmp_path)

        async def run():
            async with _api(tmp_path) as api:
                await fn(api)
        asyncio.run(run())
    test.__name__ = fn.__name__
    return test


async def _register(api, vm_id="a"):
    resp = await api.post("/api/vm", json={"id": vm_id, "ssh_key": "/keys/k"})
    assert resp.status == 201, await resp.text()


async def _up(api, vm_id="a"):
    """Registered, started, the guest channel marked ready (step 4's job)."""
    await _register(api, vm_id)
    assert (await api.post(f"/api/vm/{vm_id}/start")).status == 200
    assert await api.hub.channels[vm_id].wait_up(2)


@scenario
async def test_a_session_cannot_register_start_or_grant(api):
    for path, body in (("/api/vm", {"id": "x"}), ("/api/vm/a/start", {}),
                       ("/api/vm/a/grant", {"session": "s1", "scopes": ["control"]}),
                       ("/api/vm/a/human", {"action": "driving"}),
                       ("/api/vm/a/revoke_controller", {})):
        resp = await api.post(path, json=body, headers=AGENT)
        assert resp.status == 403, path
        assert (await resp.json())["code"] == "vm_user_only"


@scenario
async def test_the_user_registers_starts_and_grants(api):
    await _up(api)
    resp = await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    assert (await resp.json())["grants"] == ["view", "control"]
    got = await (await api.get("/api/vm/a")).json()
    assert got["state"] == "running" and got["ready"] and got["grants"]["s1"]


@scenario
async def test_a_session_acquires_as_itself_and_not_as_another(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    other = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=OTHER)
    assert other.status == 403
    ok = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert ok.status == 200 and (await ok.json())["epoch"] >= 1


@scenario
async def test_a_second_session_is_told_who_holds_it(api):
    await _up(api)
    for s in ("s1", "s2"):
        await api.post("/api/vm/a/grant", json={"session": s, "scopes": ["control"]})
    await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    resp = await api.post("/api/vm/a/acquire", json={"session": "s2"}, headers=OTHER)
    assert resp.status == 409
    assert (await resp.json())["held_by"] == "s1"


@scenario
async def test_acquire_without_a_grant_is_forbidden(api):
    await _up(api)
    resp = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert resp.status == 403 and (await resp.json())["code"] == "vm_no_grant"


@scenario
async def test_a_session_lists_only_what_it_may_see_and_only_its_own_grants(api):
    await _up(api)
    await _register(api, "b")
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["control"]})
    rows = (await (await api.get("/api/vm?session=s1")).json())["vms"]
    assert [r["id"] for r in rows] == ["a"]
    assert rows[0]["grants"] == {"s1": ["view"]}


@scenario
async def test_a_session_gets_no_host_paths_and_no_record_without_a_grant(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["view"]})
    mine = await (await api.get("/api/vm/a", headers=AGENT)).json()
    assert mine["grants"] == {"s1": ["view"]}
    assert not {"ssh_key", "ssh_user", "cua_path", "channel_error"} & set(mine)
    listed = (await (await api.get("/api/vm", headers=AGENT)).json())["vms"]
    assert listed[0]["grants"] == {"s1": ["view"]} and "ssh_key" not in listed[0]
    await _register(api, "b")
    other = await api.get("/api/vm/b", headers=AGENT)
    assert other.status == 403 and (await other.json())["code"] == "vm_no_grant"
    assert "grants" in await (await api.get("/api/vm/a")).json()  # the user sees all


@scenario
async def test_a_full_machine_answers_409_with_the_holders(api):
    await _register(api, "a")
    await _register(api, "b")
    await _register(api, "c")
    for v in "ab":
        assert (await api.post(f"/api/vm/{v}/start")).status == 200
    resp = await api.post("/api/vm/c/start")
    body = await resp.json()
    assert resp.status == 409 and body["code"] == "vm_slots_full"
    assert sorted(body["holders"]) == ["a", "b"]


@scenario
async def test_stop_that_times_out_is_a_gateway_timeout_and_force_is_separate(api):
    await _up(api)
    api.fake.shutdown_stops_vm = False
    resp = await api.post("/api/vm/a/stop")
    assert resp.status == 504 and (await resp.json())["code"] == "vm_stop_timeout"
    # the guest still answers: it is given back running, and its channel reopened
    got = await (await api.get("/api/vm/a")).json()
    assert got["state"] == "running" and got["ssh_ready"]
    assert await api.hub.channels["a"].wait_up(2)
    resp = await api.post("/api/vm/a/force_stop")
    assert resp.status == 200 and (await resp.json())["state"] == "stopped"


@scenario
async def test_the_user_taking_the_screen_blocks_a_session(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    await api.post("/api/vm/a/human", json={"action": "driving"})
    resp = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert resp.status == 423 and (await resp.json())["code"] == "vm_busy"
    await api.post("/api/vm/a/human", json={"action": "idle"})
    resp = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert resp.status == 200


@scenario
async def test_unknown_vm_and_bad_bodies(api):
    assert (await api.get("/api/vm/nope")).status == 404
    resp = await api.post("/api/vm", data="not json")
    assert resp.status == 400
    resp = await api.post("/api/vm/a/human", json={"action": "dance"})
    assert resp.status in (400, 404)


def test_the_settings_have_defaults():
    cfg = store.DAEMON_DEFAULTS
    assert cfg["vm_slot_limit"] == 2 and cfg["vm_human_idle"] == 15
    assert cfg["vm_stop_wait"] == 120 and cfg["vm_graphics"] is True


@scenario
async def test_start_opens_the_guest_channel_and_stop_closes_it_first(api):
    await _up(api)
    got = await (await api.get("/api/vm/a")).json()
    assert got["channel"] == "up" and got["cua_ready"] and got["ready"]
    assert len(api.world.opened) == 1
    assert (await api.post("/api/vm/a/stop")).status == 200
    got = await (await api.get("/api/vm/a")).json()
    assert got["state"] == "stopped" and got["channel"] == "idle" and not got["cua_ready"]
    assert "a" not in api.hub.channels


def _png_result():
    import base64
    return {"content": [{"type": "text", "text": "desktop"},
                        {"type": "image", "mimeType": "image/png",
                         "data": base64.b64encode(b"PNGDATA").decode()}],
            "isError": False}


@scenario
async def test_a_look_needs_view_a_click_needs_control_and_the_epoch(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    look = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                          headers=AGENT)
    assert look.status == 200
    click = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click"},
                           headers=AGENT)
    assert click.status == 403 and (await click.json())["code"] == "vm_no_grant"

    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    epoch = (await (await api.post("/api/vm/a/acquire", json={"session": "s1"},
                                   headers=AGENT)).json())["epoch"]
    first = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click",
                                                    "epoch": epoch}, headers=AGENT)
    assert first.status == 409 and (await first.json())["code"] == "vm_observe_first"
    await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                   headers=AGENT)
    ok = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click",
                                                 "epoch": epoch}, headers=AGENT)
    assert ok.status == 200
    stale = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click",
                                                    "epoch": epoch - 1}, headers=AGENT)
    assert stale.status == 409 and (await stale.json())["code"] == "vm_stale"


@scenario
async def test_screen_images_become_files_not_base64(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    chan = api.hub.channels["a"]

    async def fake_call(name, arguments=None, timeout=30):
        return _png_result()

    chan.call = fake_call
    body = await (await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                                 headers=AGENT)).json()
    image = [b for b in body["result"]["content"] if b["type"] == "image"][0]
    assert "data" not in image
    from pathlib import Path
    assert Path(image["path"]).read_bytes() == b"PNGDATA"


@scenario
async def test_a_down_channel_answers_unavailable_with_retry_after(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    await api.hub.channels["a"].stop()
    api.reg.set_channel("a", "down", "ssh closed")
    resp = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                          headers=AGENT)
    body = await resp.json()
    assert resp.status == 503 and body["code"] == "vm_unavailable" and body["retry_after"] == 5


@scenario
async def test_exec_needs_its_own_grant_and_control(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    epoch = (await (await api.post("/api/vm/a/acquire", json={"session": "s1"},
                                   headers=AGENT)).json())["epoch"]
    refused = await api.post("/api/vm/a/exec", json={"session": "s1", "command": "id",
                                                      "epoch": epoch}, headers=AGENT)
    assert refused.status == 403 and (await refused.json())["code"] == "vm_no_grant"
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["exec"]})
    await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                   headers=AGENT)
    done = await api.post("/api/vm/a/exec", json={"session": "s1", "command": "id",
                                                   "epoch": epoch}, headers=AGENT)
    out = await done.json()
    assert done.status == 200 and out["rc"] == 0 and out["stdout"] == "ok\n"
    assert api.fake.execs[-1][-3:] == ["/bin/sh", "-c", "id"]


@scenario
async def test_tools_lists_which_are_look_only(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    body = await (await api.get("/api/vm/a/tools?session=s1", headers=AGENT)).json()
    assert body["tools"] == [{"name": "get_screen_size", "scope": "view", "description": ""}]


def test_every_cua_tool_that_installs_or_configures_needs_exec():
    assert vm_api.tool_scope("get_desktop_state") == "view"
    assert vm_api.tool_scope("click") == "control"
    assert vm_api.tool_scope("run_script") == "control"
    assert vm_api.tool_scope("a_tool_nobody_has_heard_of") == "control"
    for name in ("install_extension", "install_ffmpeg", "set_config"):
        assert vm_api.tool_scope(name) == "exec"
    assert not (vm_api.VIEW_TOOLS & vm_api.EXEC_TOOLS)


@scenario
async def test_duplicate_control_is_refused_until_the_command_queue_exists(api):
    await _up(api)
    for who in ("s1", "s2"):
        await api.post("/api/vm/a/grant", json={"session": who, "scopes": ["control"]})
    await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    resp = await api.post("/api/vm/a/extra_controller", json={"session": "s2", "reason": "pair"})
    assert resp.status == 501 and (await resp.json())["code"] == "vm_duplicate_unavailable"
    assert api.reg.get("a").extra_controllers == {}


def test_run_steps_may_carry_control_actions_only():
    ok = {"steps": [{"tool": "click", "args": {"pid": 1}},
                    {"type_text": {"text": "x"}, "expect": [{"text": "x"}]},
                    {"expect": [{"text": "done"}]}]}
    vm_api.check_nested("run_steps", ok)
    for bad in ({"tool": "set_config", "args": {}}, {"install_extension": {}},
                {"tool": "click", "set_config": {}}, {"tool": 7}, {"run_script": {}}):
        with pytest.raises(VmError) as e:
            vm_api.check_nested("run_steps", {"steps": [{"tool": "click"}, bad]})
        assert e.value.code == "vm_tool_not_allowed" or e.value.code == "vm_bad_request"
    for bad in ({"steps": "x"}, {"steps": ["click"]}, {}):
        with pytest.raises(VmError):
            vm_api.check_nested("run_steps", bad)
    vm_api.check_nested("click", {"steps": "not looked at"})  # only run_steps nests


def test_the_cua_tool_scopes_hold_for_the_whole_driver_list():
    """Every tool of cua-driver 0.38.0 (62) has a scope, and the ones that
    write files, install or reconfigure are exec."""
    names = """bring_to_front browser_click browser_dialog browser_download browser_navigate
        browser_pointer browser_prepare browser_select browser_set_input_files browser_type
        check_for_update check_permissions click clipboard_read clipboard_write double_click drag
        end_session escalate_session get_accessibility_tree get_agent_cursor_state
        get_browser_state get_config get_cursor_position get_desktop_state get_guide
        get_recording_state get_screen_size get_session get_session_state get_window_state
        health_report hotkey install_extension install_ffmpeg invoke_menu kill_app launch_app
        list_apps list_sessions list_windows move_cursor page parse_visual_regions press_key
        replay_trajectory right_click run_script run_steps scroll set_agent_cursor_enabled
        set_agent_cursor_motion set_agent_cursor_theme set_config set_value set_window_frame
        start_recording start_session stop_recording type_text verify_state zoom""".split()
    assert len(names) == 62 == len(set(names))
    by = {n: vm_api.tool_scope(n) for n in names}
    assert {n for n, s in by.items() if s == "exec"} == {
        "install_extension", "install_ffmpeg", "set_config", "start_recording",
        "browser_download", "browser_set_input_files", "replay_trajectory"}
    for n in ("run_script", "run_steps", "kill_app", "clipboard_read",
              "get_browser_state", "launch_app", "page", "check_for_update"):
        assert by[n] == "control", n
    assert sum(1 for s in by.values() if s == "view") == len(vm_api.VIEW_TOOLS)


@scenario
async def test_a_bad_timeout_is_a_400_not_a_crash(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["exec"]})
    for bad in ("soon", [1], -1, 0, 100000):
        resp = await api.post("/api/vm/a/exec", headers=AGENT,
                              json={"session": "s1", "command": "true", "timeout": bad, "epoch": 0})
        assert resp.status == 400 and (await resp.json())["code"] == "vm_bad_request", bad


@scenario
async def test_a_failed_stop_brings_the_channel_back(api):
    await _up(api)
    run = api.mgr._run

    def refuse(argv, timeout):
        if argv[-1] == vm_tart.SHUTDOWN:
            return 1, "", "Error: unavailable (14): Transport became inactive"
        return run(argv, timeout)
    api.mgr._run = refuse
    resp = await api.post("/api/vm/a/stop")
    assert resp.status >= 400
    assert (await resp.json())["code"] == "vm_stop_failed"
    assert api.reg.get("a").state == "running"
    assert await api.hub.channels["a"].wait_up(2)


def test_a_restart_recovers_each_running_guest_behind_the_start(tmp_path, monkeypatch):
    """The start is not held up by tart or by one guest; a tart that cannot be
    read at first is tried again; a guest that comes up gets its channel."""
    monkeypatch.setattr(vm_api.paths, "daemon_dir", lambda: tmp_path)

    async def go():
        fake = Fake()
        reg = Registry(tmp_path / "vms.json")
        reg.add("a", ssh_key="/keys/k")
        reg.claim_slot("a")
        reg.set_state("a", "running")
        fake.tart["a"] = "running"
        lists = []
        run = fake.run

        def flaky(argv, timeout):
            if argv[:2] == ["tart", "list"]:
                lists.append(1)
                if len(lists) == 1:
                    return 1, "", "tart: busy"
            return run(argv, timeout)
        mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=flaky, spawn=fake.spawn, alive=lambda p: True,
                              sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5)
        world = World()

        async def hub_sleep(_):
            await asyncio.sleep(0)
        hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=hub_sleep)
        real_sleep = asyncio.sleep

        async def fast(_):
            await real_sleep(0)
        # vm_api.asyncio is the asyncio module itself, so this shortens every
        # wait in the process -- a polling loop written in this test would
        # spend its whole budget without time passing and see nothing.
        monkeypatch.setattr(vm_api.asyncio, "sleep", fast)
        app = web.Application()
        vm_api.install(app, reg, mgr, hub)
        async with TestClient(TestServer(app)):
            # The recovery runs behind the app's start (vm_api._start_hub),
            # and its return is the second tart list: wait for that task, not
            # for a clock. The hub's other two tasks never end.
            recovery = next(t for t in app["vm_hub"]["tasks"]
                            if t is not None and t.get_coro().__name__ == "recover")
            await asyncio.wait_for(recovery, 10)
            assert len(lists) >= 2
            for _ in range(200):
                if "a" in hub.channels and hub.channels["a"].registry.get("a").channel == "up":
                    break
                await real_sleep(0.01)
            assert reg.get("a").state == "running" and reg.get("a").channel == "up"
    asyncio.run(go())


@scenario
async def test_changes_by_the_user_routes_are_audited_with_the_declared_actor(api):
    await _register(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["exec"]})
    await api.post("/api/vm/a/revoke", json={"session": "s1", "scopes": ["exec"]})
    rows = [r for r in api.reg.audit_log if r["what"] in ("register", "user_grant", "user_revoke")]
    assert [r["what"] for r in rows] == ["register", "user_grant", "user_revoke"]
    assert all(r["actor"] == "user/cli" for r in rows)
    assert rows[1]["to"] == "s1" and rows[1]["scopes"] == ["view", "exec"]
    # a request that declares nothing is the user's; one that declares a
    # session is refused and leaves no change behind
    before = len(api.reg.audit_log)
    assert (await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["exec"]},
                           headers=AGENT)).status == 403
    assert len(api.reg.audit_log) == before


def test_the_page_says_its_limit_and_the_heartbeat_ignores_tab_visibility():
    from pathlib import Path
    static = Path(vm_api.__file__).resolve().parent.parent / "web" / "static"
    js = (static / "vm.js").read_text(encoding="utf-8")
    beat = js[js.index("function beat()"):js.index("function manageTouch")]
    assert "document.hidden" not in beat and "new Worker" in beat
    assert 'id="vm-limit"' in (static / "vm.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# re-verification findings (N1-N9)
# --------------------------------------------------------------------------- #
def test_a_session_that_ends_gives_back_its_vm_through_the_real_exit_hooks(tmp_path):
    """The hook is wired in build_app and reads the name where a Session keeps it
    (session.sdef.name); a registry-level test cannot see either."""
    import time
    from types import SimpleNamespace
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    async def go():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        reg = app["vm_registry"]
        reg.add("a")
        reg.claim_slot("a")
        reg.set_state("a", "running")
        reg.set_ready("a", ssh=True, gui=True, cua=True, ip="192.168.64.2")
        reg.grant("a", "s1", ["control"])
        reg.grant("a", "s2", ["control"])
        reg.acquire("a", "s1")
        mgr._run_exit_hooks(SimpleNamespace(sdef=SimpleNamespace(name="s2")))
        assert reg.get("a").controller == "s1"       # another session's exit changes nothing
        mgr._run_exit_hooks(SimpleNamespace(sdef=SimpleNamespace(name="s1")))
        assert reg.get("a").controller == "" and reg.get("a").grants["s1"]
    asyncio.run(go())


def test_status_codes_for_the_errors_the_routes_raise():
    for code, status in (("vm_tool_not_allowed", 403), ("vm_suspend_timeout", 504)):
        assert vm_api._STATUS[code] == status


def test_a_step_whose_tool_is_not_a_string_is_a_400_not_a_crash():
    for bad in ({"tool": ["click"]}, {"tool": {"a": 1}}, {"tool": 3}):
        with pytest.raises(VmError) as e:
            vm_api.check_nested("run_steps", {"steps": [bad]})
        assert e.value.code == "vm_tool_not_allowed"


@scenario
async def test_reconcile_probes_and_opens_the_channel_of_a_guest_it_adopts(api):
    await _up(api)
    api.reg.adopt("a", "stopped")                      # the record lost the VM; tart still runs it
    await api.hub.drop("a")
    assert api.fake.tart["a"] == "running"
    resp = await api.post("/api/vm-reconcile")
    body = await resp.json()
    assert body["probing"] == ["a"] and body["changed"]
    for _ in range(300):
        if api.reg.get("a").channel == "up":
            break
        await asyncio.sleep(0.01)
    vm = api.reg.get("a")
    assert vm.state == "running" and vm.ssh_ready and vm.channel == "up"


@scenario
async def test_a_guest_that_never_answers_ends_in_fault_not_in_silence(api):
    await _up(api)
    api.reg.adopt("a", "running")                      # readiness gone, as after a restart

    async def hopeless(vm_id):
        raise VmError("vm_boot_timeout", "no ssh")
    api.mgr.reprobe = hopeless
    real = asyncio.sleep

    async def quick(_):
        await real(0)
    import unittest.mock as mock
    with mock.patch.object(vm_api.asyncio, "sleep", quick):
        assert await vm_api._bring_up(api.server.app, "a") is False
    vm = api.reg.get("a")
    assert vm.state == "fault" and "did not come up" in vm.fault_reason
    assert [r for r in api.reg.audit_log if r["what"] == "reprobe_failed"]
