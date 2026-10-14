"""One answer per read, however many dashboards ask (claunch-jkvpv).

What :mod:`daemon.shared_reads` promises: reads of the same path and query
that arrive together run the route once, a finished answer is served again
for ``max_age`` seconds, an accepted write the route's rule names drops it at
once (and one it does not name leaves it), a failure is never kept, and with
``max_age`` 0 -- an app built without the daemon's configuration -- every
read runs the route as before.
"""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import shared_reads
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.shared_reads import Invalidation, SharedReads

BEARER = {"Authorization": "Bearer sekrit"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Counted:
    """A test route that counts its runs and can be held open."""

    def __init__(self) -> None:
        self.runs = 0
        self.status = 200
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def __call__(self, request):
        self.runs += 1
        run = self.runs
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.status != 200:
            return api_mod.json_error(self.status, "refused")
        return api_mod.json_response({"run": run, "q": dict(request.query)})


async def _ok(request):
    return api_mod.json_response({"ok": True})


async def _refuse(request):
    return api_mod.json_error(409, "no")


RULES = {
    "counted": Invalidation(
        prefixes=("/api/_test/w",), excluded=frozenset({"/api/_test/w/typing"}),
    ),
}


async def _serve(max_age=4.0, clock=None, routes=None):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                    shared_read_max_age=max_age)
    if clock is not None:
        app["shared_reads"] = SharedReads(max_age, clock=clock, rules=RULES)
    counted = Counted()
    app.router.add_get("/api/_test/counted", shared_reads.shared("counted")(counted))
    # Before the variable route, which would otherwise answer it.
    app.router.add_post("/api/_test/w/typing", _ok)
    app.router.add_post("/api/_test/w/{x}", _ok)
    app.router.add_post("/api/_test/w/{x}/refuse", _refuse)
    app.router.add_post("/api/_test/elsewhere", _ok)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, counted


async def _get(client, path):
    resp = await client.get(path, headers=BEARER)
    return resp.status, await resp.json()


def test_reads_that_arrive_together_run_the_route_once(home):
    async def run():
        client, counted = await _serve(clock=Clock())
        try:
            counted.gate = asyncio.Event()
            reads = [asyncio.ensure_future(_get(client, "/api/_test/counted"))
                     for _ in range(5)]
            await counted.started.wait()
            await asyncio.sleep(0.05)
            counted.gate.set()
            answers = await asyncio.gather(*reads)
            assert counted.runs == 1
            assert all(a == (200, {"run": 1, "q": {}}) for a in answers)
            stats = client.app["shared_reads"].stats()["routes"]["counted"]
            assert stats["computes"] == 1 and stats["joins"] == 4
        finally:
            await client.close()

    asyncio.run(run())


def test_a_finished_answer_is_served_until_max_age_then_built_again(home):
    async def run():
        clock = Clock()
        client, counted = await _serve(clock=clock)
        try:
            assert (await _get(client, "/api/_test/counted"))[1]["run"] == 1
            clock.now += 3.9
            assert (await _get(client, "/api/_test/counted"))[1]["run"] == 1
            clock.now += 0.2
            assert (await _get(client, "/api/_test/counted"))[1]["run"] == 2
            assert counted.runs == 2
            assert client.app["shared_reads"].stats()["routes"]["counted"]["hits"] == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_the_key_is_path_and_query_in_any_order(home):
    async def run():
        client, counted = await _serve(clock=Clock())
        try:
            await _get(client, "/api/_test/counted?a=1&b=2")
            await _get(client, "/api/_test/counted?b=2&a=1")
            assert counted.runs == 1
            _, other = await _get(client, "/api/_test/counted?a=2&b=2")
            assert counted.runs == 2 and other["q"] == {"a": "2", "b": "2"}
        finally:
            await client.close()

    asyncio.run(run())


def test_a_named_write_drops_the_answer_and_others_leave_it(home):
    async def run():
        client, counted = await _serve(clock=Clock())
        try:
            await _get(client, "/api/_test/counted")
            # Not named by the rule, a refused write, and the typing route the
            # rule excludes: the stored answer stays.
            for path in ("/api/_test/elsewhere", "/api/_test/w/x/refuse",
                         "/api/_test/w/typing"):
                await client.post(path, headers=BEARER)
                await _get(client, "/api/_test/counted")
            assert counted.runs == 1
            # A batch is a POST too, and only ever a read.
            await client.post("/api/batch", headers=BEARER,
                              json={"paths": ["/api/health"]})
            await _get(client, "/api/_test/counted")
            assert counted.runs == 1
            resp = await client.post("/api/_test/w/x", headers=BEARER)
            assert resp.status == 200
            assert (await _get(client, "/api/_test/counted"))[1]["run"] == 2
            stats = client.app["shared_reads"].stats()["routes"]["counted"]
            assert stats["invalidations"] == 1
            assert stats["invalidated_by"] == {"/api/_test/w/{x}": 1}
        finally:
            await client.close()

    asyncio.run(run())


def test_a_failed_answer_is_not_kept(home):
    async def run():
        client, counted = await _serve(clock=Clock())
        try:
            counted.status = 503
            assert (await _get(client, "/api/_test/counted"))[0] == 503
            assert (await _get(client, "/api/_test/counted"))[0] == 503
            counted.status = 200
            assert (await _get(client, "/api/_test/counted"))[0] == 200
            assert counted.runs == 3
        finally:
            await client.close()

    asyncio.run(run())


def test_a_write_during_the_build_keeps_the_answer_out_of_the_store(home):
    """The readers already waiting get the answer they waited for; the next
    reader does not join that build, and nothing built before the write is
    served after it."""

    async def run():
        client, counted = await _serve(clock=Clock())
        try:
            counted.gate = asyncio.Event()
            first = asyncio.ensure_future(_get(client, "/api/_test/counted"))
            await counted.started.wait()
            await client.post("/api/_test/w/x", headers=BEARER)
            second = asyncio.ensure_future(_get(client, "/api/_test/counted"))
            await asyncio.sleep(0.05)
            counted.gate.set()
            assert (await first)[1]["run"] == 1
            assert (await second)[1]["run"] == 2
            assert (await _get(client, "/api/_test/counted"))[1]["run"] == 2
            assert counted.runs == 2
        finally:
            await client.close()

    asyncio.run(run())


def test_off_by_default_every_read_runs_the_route(home):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        counted = Counted()
        app.router.add_get("/api/_test/counted", shared_reads.shared("counted")(counted))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            assert app["shared_reads"].enabled is False
            for _ in range(3):
                await _get(client, "/api/_test/counted")
            assert counted.runs == 3
        finally:
            await client.close()

    asyncio.run(run())


def test_the_daemon_turns_it_on_and_wraps_the_polled_routes(home):
    assert store.DAEMON_DEFAULTS["shared_read_max_age"] == shared_reads.DEFAULT_MAX_AGE
    assert api_mod.h_sessions_list.shared_read == "sessions"
    assert api_mod.h_beads_queues.shared_read == "beads-queues"
    assert api_mod.h_profiles.shared_read == "profiles"
    assert set(shared_reads.INVALIDATES) == {"sessions", "beads-queues", "profiles"}


def test_every_route_a_rule_names_is_a_registered_write(home):
    """A renamed route must not leave a rule silently matching nothing."""

    async def build():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        return build_app(mgr, "sekrit", started_at=time.monotonic())

    app = asyncio.run(build())
    writes = {
        route.resource.canonical
        for route in app.router.routes()
        if route.method not in shared_reads.READ_METHODS and route.method != "*"
        and route.resource is not None
    }
    for name, rule in shared_reads.INVALIDATES.items():
        for template in rule.exact | rule.excluded:
            assert template in writes, (name, template)
        for prefix in rule.prefixes:
            assert any(rule.matches(t) for t in writes if t.startswith(prefix)), (name, prefix)
    sessions = shared_reads.INVALIDATES["sessions"]
    assert sessions.matches("/api/sessions/{name}/kill")
    assert not sessions.matches("/api/sessions/{name}/keys")
    assert not sessions.matches("/api/mesh/{mesh}/messages")
    queues = shared_reads.INVALIDATES["beads-queues"]
    assert queues.matches("/api/beads/{id}/assign")
    assert queues.matches("/api/cflow2/approve")
    assert queues.matches("/api/sessions/{name}/children")
    assert not queues.matches("/api/sessions/{name}/keys")
    assert not queues.matches("/api/window/acquire")
    assert not queues.matches("/api/batch")
    profiles = shared_reads.INVALIDATES["profiles"]
    assert profiles.matches("/api/profiles/permission-mode")
    assert profiles.matches("/api/profiles/pi-tui-mode")
    assert profiles.matches("/api/harnesses/{name}/models")
    assert profiles.matches("/api/install/apply")
    assert not profiles.matches("/api/sessions/{name}/keys")
    assert not profiles.matches("/api/batch")


def test_shared_session_list_reads_equal_the_direct_get_on_every_path(home):
    """The session list through batch and the control socket, shared, is the
    same answer as the direct GET -- and one run serves all three."""

    async def run():
        client, _ = await _serve(max_age=60.0)
        try:
            path = "/api/sessions?view=rail&state=current"
            resp = await client.get(path, headers=BEARER)
            direct = await resp.json()
            batch = await (await client.post("/api/batch", headers=BEARER,
                                             json={"paths": [path]})).json()
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive()  # init
                await ws.send_json({"type": "read", "id": 1, "paths": [path]})
                while True:
                    msg = json.loads((await asyncio.wait_for(ws.receive(), 10)).data)
                    if msg.get("type") == "read_result":
                        break
            assert batch["answers"][path] == direct
            assert msg["answers"][path] == direct
            stats = (await (await client.get("/api/shared-reads", headers=BEARER)).json())
            routes = stats["routes"]["sessions"]
            assert stats["enabled"] is True
            assert routes["computes"] == 1 and routes["hits"] == 2
        finally:
            await client.close()

    asyncio.run(run())


def test_the_profile_listing_is_built_once_and_dropped_by_a_profile_write(home):
    """Every dashboard that boots or opens the new-session form reads
    ``/api/profiles``; on the live daemon that read held 36.5% of the event
    loop (claunch-8utpv). Two reads inside ``max_age`` build it once; an
    accepted write to a profile route builds it again on the next read."""

    async def run():
        client, _ = await _serve(max_age=60.0)
        try:
            first = await _get(client, "/api/profiles")
            second = await _get(client, "/api/profiles")
            assert first == second and first[0] == 200
            reads = client.server.app["shared_reads"]
            assert reads.stats()["routes"]["profiles"]["computes"] == 1
            assert reads.stats()["routes"]["profiles"]["hits"] == 1
            reads.wrote("POST", "/api/profiles/permission-mode", 200)
            await _get(client, "/api/profiles")
            assert reads.stats()["routes"]["profiles"]["computes"] == 2
        finally:
            await client.close()

    asyncio.run(run())
