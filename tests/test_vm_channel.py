"""The guest channel (vm-setup-8s6, step 4) against a scripted MCP server
that stands in for ``tart exec -i ... cua-driver mcp``: no VM, no guest."""
from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher.daemon import vm_channel
from claude_launcher.daemon.vmhub import Registry, VmError


class _Stdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, data):
        if self.proc.closed:
            raise ConnectionResetError("closed")
        for line in data.decode().splitlines():
            self.proc.received.append(json.loads(line))
            self.proc.on_message(json.loads(line))

    async def drain(self):
        if self.proc.closed:
            raise ConnectionResetError("closed")

    def close(self):
        pass


class FakeProc:
    """An MCP server: answers initialize, the probe and tools/call; can be
    told to go silent or to close."""

    def __init__(self, behaviour):
        self.stdout = asyncio.StreamReader(limit=vm_channel.READ_LIMIT)
        self.stdin = _Stdin(self)
        self.received = []
        self.closed = False
        self.behaviour = behaviour

    def reply(self, ident, result):
        self.stdout.feed_data((json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n").encode())

    def die(self):
        self.closed = True
        self.stdout.feed_eof()

    def on_message(self, msg):
        if "id" not in msg:
            return
        method = msg["method"]
        if method == "initialize":
            if self.behaviour.get("silent_hello"):
                return
            self.reply(msg["id"], {"protocolVersion": "2024-11-05", "capabilities": {}})
        elif method == "tools/list":
            self.reply(msg["id"], {"tools": [{"name": "get_screen_size"}]})
        elif method == "tools/call" and msg["params"]["name"] == "big":
            self.reply(msg["id"], {"content": [{"type": "text", "text": "x" * 300_000}]})
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name == "hang":
                return
            if name == "odd":
                # Messages a sloppy server might send; none may end the channel.
                for odd in ({"jsonrpc": "2.0", "id": [1], "result": {}},
                            {"jsonrpc": "2.0", "id": {"a": 1}, "error": "x"},
                            {"jsonrpc": "2.0", "id": 99, "error": "no such call"},
                            {"jsonrpc": "2.0", "id": True, "result": 3},
                            {"jsonrpc": "2.0", "method": "ping", "id": [2]}, [1, 2], 7):
                    self.stdout.feed_data((json.dumps(odd) + "\n").encode())
            if name == "strerr":
                self.stdout.feed_data((json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": "boom"}) + "\n").encode())
                return
            if name == "listresult":
                self.reply(msg["id"], [1, 2])
                return
            if name == "get_screen_size" and self.behaviour.get("probe_error"):
                self.stdout.feed_data((json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "no daemon"}}) + "\n").encode())
                return
            self.reply(msg["id"], {"content": [{"type": "text", "text": name}], "isError": False})

    def terminate(self):
        self.die()

    kill = terminate

    async def wait(self):
        return 0


class World:
    def __init__(self, behaviours=None):
        self.opened = []
        self.behaviours = list(behaviours or [{}])

    async def opener(self, argv, log):
        behaviour = self.behaviours[min(len(self.opened), len(self.behaviours) - 1)]
        if behaviour.get("refuse"):
            self.opened.append(None)
            raise OSError("connection refused")
        proc = FakeProc(behaviour)
        self.opened.append((list(argv), proc))
        return proc


async def _no_sleep(_):
    await asyncio.sleep(0)


def _channel(tmp_path, world, **kw):
    reg = Registry()
    reg.add("a", ssh_user="admin", ssh_key="/keys/k")
    reg.claim_slot("a")
    reg.set_state("a", "running")
    reg.set_ready("a", ssh=True, gui=True, ip="192.168.64.2")
    kw.setdefault("sleep", _no_sleep)
    return reg, vm_channel.Channel(reg, "a", log_dir=tmp_path, opener=world.opener, **kw)


def run(coro):
    return asyncio.run(coro)


def test_the_command_runs_the_guests_cua_driver_by_absolute_path(tmp_path):
    reg, _ = _channel(tmp_path, World())
    argv = vm_channel.exec_argv(reg.get("a"))
    assert argv[-2:] == ["/Users/admin/.local/bin/cua-driver", "mcp"]
    assert argv == ["tart", "exec", "-i", "--", "a", "/Users/admin/.local/bin/cua-driver", "mcp"]
    assert "ssh" not in argv


def test_up_means_a_call_went_through_and_cua_is_ready_then(tmp_path):
    async def go():
        world = World()
        reg, ch = _channel(tmp_path, world)
        ch.start()
        assert await ch.wait_up(2)
        vm = reg.get("a")
        assert vm.channel == "up" and vm.cua_ready and vm.ready
        methods = [m["method"] for m in world.opened[0][1].received]
        assert methods == ["initialize", "notifications/initialized", "tools/call"]
        out = await ch.call("click", {"x": 1})
        assert out["content"][0]["text"] == "click"
        await ch.stop()
        assert not reg.get("a").cua_ready and reg.get("a").channel == "idle"
    run(go())


def test_a_dropped_ssh_is_reconnected_and_a_pending_call_fails_loudly(tmp_path):
    async def go():
        world = World()
        reg, ch = _channel(tmp_path, world)
        ch.start()
        await ch.wait_up(2)
        first = world.opened[0][1]
        pending = asyncio.create_task(ch.call("hang", timeout=5))
        await asyncio.sleep(0.01)
        first.die()
        try:
            await pending
        except VmError as exc:
            assert exc.code == "vm_unavailable" and exc.extra["outcome_unknown"]
        else:
            raise AssertionError("a call lost with the connection must raise")
        for _ in range(100):
            if len(world.opened) > 1 and reg.get("a").channel == "up":
                break
            await asyncio.sleep(0.01)
        assert len(world.opened) == 2 and reg.get("a").cua_ready
        await ch.stop()
    run(go())


def test_a_call_while_down_is_refused_with_retry_after_not_hung(tmp_path):
    async def go():
        world = World([{"refuse": True}])
        reg, ch = _channel(tmp_path, world, backoff=(0.0,) * 50)
        try:
            await ch.call("click")
        except VmError as exc:
            assert exc.code == "vm_unavailable" and exc.extra["retry_after"] == 5
        else:
            raise AssertionError("no channel, no answer")
    run(go())


def test_retries_are_finite_and_the_failure_is_reported(tmp_path):
    async def go():
        world = World([{"refuse": True}])
        failed = []
        reg, ch = _channel(tmp_path, world, backoff=(0.0, 0.0, 0.0),
                           on_failed=lambda vm, why: failed.append((vm, why)))
        ch.start()
        await ch._task
        vm = reg.get("a")
        assert vm.channel == "failed" and not vm.cua_ready
        assert "3 attempts" in vm.channel_error
        assert len(world.opened) == 3 and failed and failed[0][0] == "a"
        assert vm.state == "running"  # the VM is fine; its channel is not
    run(go())


def test_a_probe_the_guest_cannot_answer_is_not_up(tmp_path):
    async def go():
        world = World([{"probe_error": True}])
        reg, ch = _channel(tmp_path, world, backoff=(0.0, 0.0))
        ch.start()
        await ch._task
        assert reg.get("a").channel == "failed" and not reg.get("a").cua_ready
        assert "no daemon" in reg.get("a").channel_error
    run(go())


def test_a_silent_guest_times_out_the_handshake(tmp_path):
    async def go():
        world = World([{"silent_hello": True}])
        reg, ch = _channel(tmp_path, world, backoff=(0.0,), hello_timeout=0.05)
        ch.start()
        await ch._task
        assert reg.get("a").channel == "failed"
    run(go())


def test_a_connection_that_keeps_dropping_at_once_still_runs_out_of_retries(tmp_path):
    async def go():
        class Quick(FakeProc):
            def on_message(self, msg):
                super().on_message(msg)
                if msg.get("method") == "tools/call":
                    asyncio.get_running_loop().call_soon(self.die)

        world = World()

        async def opener(argv, log):
            proc = Quick({})
            world.opened.append((list(argv), proc))
            return proc

        reg = Registry()
        reg.add("a")
        reg.claim_slot("a")
        reg.set_state("a", "running")
        reg.set_ready("a", ssh=True, ip="1.2.3.4")
        ch = vm_channel.Channel(reg, "a", log_dir=tmp_path, opener=opener,
                                backoff=(0.0, 0.0, 0.0), sleep=_no_sleep)
        ch.start()
        await asyncio.wait_for(ch._task, 5)
        assert reg.get("a").channel == "failed" and len(world.opened) == 3
    run(go())


def test_a_channel_for_a_vm_that_is_not_running_stays_idle(tmp_path):
    async def go():
        world = World()
        reg = Registry()
        reg.add("a")
        ch = vm_channel.Channel(reg, "a", log_dir=tmp_path, opener=world.opener, sleep=_no_sleep)
        ch.start()
        await ch._task
        assert reg.get("a").channel == "idle" and world.opened == []
    run(go())


def test_the_hub_keeps_one_channel_per_vm_and_drops_it(tmp_path):
    async def go():
        world = World()
        reg, _ = _channel(tmp_path, world)
        hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=_no_sleep)
        one = hub.ensure("a")
        assert hub.ensure("a") is one
        await one.wait_up(2)
        await hub.drop("a")
        assert "a" not in hub.channels and reg.get("a").channel == "idle"
        try:
            hub.get("a")
        except VmError as exc:
            assert exc.code == "vm_unavailable"
    run(go())


def test_a_result_far_over_the_default_line_limit_does_not_close_the_channel(tmp_path):
    async def go():
        world = World()
        reg, ch = _channel(tmp_path, world)
        ch.start()
        await ch.wait_up(2)
        out = await ch.call("big")
        assert len(out["content"][0]["text"]) == 300_000
        assert reg.get("a").channel == "up"
        await ch.stop()
    run(go())


def test_the_real_opener_raises_the_line_limit():
    import inspect
    assert "limit=READ_LIMIT" in inspect.getsource(vm_channel.open_process)
    assert vm_channel.READ_LIMIT > 10 * 1024 * 1024


def test_odd_json_rpc_messages_do_not_end_the_channel(tmp_path):
    async def go():
        world = World()
        reg, ch = _channel(tmp_path, world)
        ch.start()
        assert await ch.wait_up(2)
        out = await ch.call("odd")
        assert out["content"][0]["text"] == "odd"
        with pytest.raises(VmError) as e:
            await ch.call("strerr")
        assert e.value.code == "vm_guest_error" and "boom" in str(e.value)
        assert await ch.call("listresult") == {}
        assert (await ch.call("get_screen_size"))["content"]
        assert reg.get("a").channel == "up" and len(world.opened) == 1
        await ch.stop()
    asyncio.run(go())


def test_a_new_connection_first_ends_the_stale_guest_processes_of_the_old_one(tmp_path):
    async def go():
        reaped = []

        async def reap(vm):
            reaped.append(vm.id)
        world = World([{}, {}])
        reg, ch = _channel(tmp_path, world, reap=reap)
        ch.start()
        assert await ch.wait_up(2)
        assert reaped == ["a"]
        ch._proc.die()                      # the guest side drops; the supervisor reconnects
        for _ in range(200):
            if len(world.opened) == 2 and ch._up.is_set():
                break
            await asyncio.sleep(0.01)
        assert reaped == ["a", "a"] and len(world.opened) == 2
        await ch.stop()
    asyncio.run(go())


def test_the_real_reaper_matches_mcp_only_and_not_its_own_command_line():
    import inspect
    src = inspect.getsource(vm_channel.reap_stale)
    assert '"[c]ua-driver mcp"' in src and '"/usr/bin/pkill"' in src
