"""The VM screen (vm-setup-8s6, 1c): frames only while someone watches, one
loop per VM, and who may fetch them."""
from __future__ import annotations

import asyncio
import contextlib

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import actors, vm_api, vm_channel, vm_frames, vm_tart
from claude_launcher.daemon.vmhub import Registry

from test_vm_channel import World
from test_vm_tart import Fake

JPEG = b"\xff\xd8\xff\xe0JFIF-fake-frame"
AGENT = {actors.HEADER: "agent:s1"}


class Capture:
    def __init__(self):
        self.calls = 0
        self.rc = 0
        self.out = JPEG
        self.err = ""
        self.argv = []

    def __call__(self, argv, timeout):
        self.calls += 1
        self.argv = list(argv)
        return self.rc, self.out, self.err


def _running(reg, vm_id="a"):
    reg.add(vm_id, ssh_key="/keys/k")
    reg.claim_slot(vm_id)
    reg.set_state(vm_id, "running")
    reg.set_ready(vm_id, ssh=True, gui=True, ip="192.168.64.2")


def run(coro):
    return asyncio.run(coro)


def test_a_look_starts_the_loop_and_returns_a_jpeg():
    async def go():
        reg, cap = Registry(), Capture()
        _running(reg)
        frames = vm_frames.Frames(reg, run=cap, interval=0.01)
        frame = await frames.get("a", since=0)
        assert frame.data == JPEG and frame.seq >= 1
        assert frames.watched("a")
        await frames.stop_all()
    run(go())


def test_the_capture_command_is_one_ssh_that_cleans_its_temp_file():
    async def go():
        reg, cap = Registry(), Capture()
        _running(reg)
        frames = vm_frames.Frames(reg, run=cap, interval=0.01)
        await frames.get("a")
        await frames.stop_all()
        assert cap.argv[0] == "ssh" and "admin@192.168.64.2" in cap.argv
        cmd = cap.argv[-1]
        assert "screencapture -x -t jpg" in cmd and "rm -f" in cmd and "cat" in cmd
    run(go())


def test_the_loop_stops_when_nobody_asks_and_does_not_double_up():
    async def go():
        reg, cap = Registry(), Capture()
        _running(reg)
        frames = vm_frames.Frames(reg, run=cap, interval=0.01, watch=0.15)
        await asyncio.gather(*(frames.get("a") for _ in range(5)))
        assert frames.watched("a")
        n = cap.calls
        await asyncio.sleep(0.4)
        assert not frames.watched("a")
        stopped_at = cap.calls
        await asyncio.sleep(0.1)
        assert cap.calls == stopped_at and stopped_at >= n
    run(go())


def test_a_newer_frame_is_waited_for_and_none_means_the_same_seq():
    async def go():
        reg, cap = Registry(), Capture()
        _running(reg)
        frames = vm_frames.Frames(reg, run=cap, interval=0.05)
        first = await frames.get("a")
        second = await frames.get("a", since=first.seq, wait=1.0)
        assert second.seq > first.seq
        await frames.stop_all()
    run(go())


def test_a_failed_capture_is_reported_not_hidden():
    async def go():
        reg, cap = Registry(), Capture()
        cap.rc, cap.out, cap.err = 1, b"", "screencapture: not permitted"
        _running(reg)
        frames = vm_frames.Frames(reg, run=cap, interval=0.01, backoff=0.05)
        frame = await frames.get("a")
        assert not frame.data and "not permitted" in frame.error
        await frames.stop_all()
    run(go())


def test_a_vm_that_is_not_running_gives_a_reason_and_no_capture():
    async def go():
        reg, cap = Registry(), Capture()
        reg.add("a")
        frames = vm_frames.Frames(reg, run=cap, interval=0.01, backoff=0.05)
        frame = await frames.get("a")
        assert not frame.data and "stopped" in frame.error and cap.calls == 0
        await frames.stop_all()
    run(go())


@contextlib.asynccontextmanager
async def _api(tmp_path, cap):
    fake = Fake()
    reg = Registry()
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn,
                          alive=lambda p: True, sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5)
    world = World()

    async def no_sleep(_):
        await asyncio.sleep(0)

    hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=no_sleep)
    frames = vm_frames.Frames(reg, run=cap, interval=0.01)
    app = web.Application()
    vm_api.install(app, reg, mgr, hub, frames)
    async with TestClient(TestServer(app)) as client:
        client.reg = reg
        yield client


def test_the_route_serves_the_frame_to_the_user_and_to_a_session_with_view(tmp_path):
    async def go():
        async with _api(tmp_path, Capture()) as api:
            _running(api.reg)
            resp = await api.get("/api/vm/a/frame")
            assert resp.status == 200 and resp.content_type == "image/jpeg"
            assert await resp.read() == JPEG and int(resp.headers["X-Frame-Seq"]) >= 1
            assert resp.headers["Cache-Control"] == "no-store"

            no = await api.get("/api/vm/a/frame?session=s1", headers=AGENT)
            assert no.status == 403 and (await no.json())["code"] == "vm_no_grant"
            api.reg.grant("a", "s1", ["view"])
            yes = await api.get("/api/vm/a/frame?session=s1", headers=AGENT)
            assert yes.status == 200
            other = await api.get("/api/vm/a/frame?session=s2", headers=AGENT)
            assert other.status == 403
    run(go())


def test_the_route_answers_304_when_nothing_is_newer_and_503_with_a_reason(tmp_path):
    async def go():
        cap = Capture()
        async with _api(tmp_path, cap) as api:
            _running(api.reg)
            seq = int((await api.get("/api/vm/a/frame")).headers["X-Frame-Seq"])
            api.reg.set_state("a", "stopping")
            api.reg.set_state("a", "stopped")
            stale = await api.get(f"/api/vm/a/frame?since={seq + 100}")
            assert stale.status == 304
            assert (await api.get("/api/vm/nope/frame")).status == 404
    run(go())


def test_the_page_is_served(tmp_path):
    async def go():
        async with _api(tmp_path, Capture()) as api:
            resp = await api.get("/vm")
            assert resp.status == 200 and "text/html" in resp.content_type
    run(go())


def test_the_page_calls_only_routes_the_daemon_has():
    """The page's fetches name /api/vm routes; every one must exist (a typo
    here is a button that does nothing and says so in a status line nobody
    reads)."""
    import re
    from pathlib import Path
    static = Path(vm_api.__file__).resolve().parent.parent / "web" / "static"
    js = (static / "vm.js").read_text(encoding="utf-8")
    html = (static / "vm.html").read_text(encoding="utf-8")
    app = web.Application()
    vm_api.install(app, Registry())
    known = {r.resource.canonical for r in app.router.routes()}
    wanted = set(re.findall(r'api/vm[a-z-]*(?:/\$\{[^}]*\}(?:/[a-z_]+)?)?', js))
    for ref in wanted:
        shape = re.sub(r"\$\{[^}]*\}", "{id}", "/" + ref)
        assert shape in known or shape.rstrip("/") in known, shape
    assert "innerHTML" not in js, "session names and fault text are untrusted"
    for ident in re.findall(r'\$\("([a-z-]+)"\)', js):
        assert f'id="{ident}"' in html, ident
