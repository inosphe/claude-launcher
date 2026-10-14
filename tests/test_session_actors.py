"""Who created a session and who ended it, recorded and kept (claunch-o6xws).

The rail used to say ``exit 0`` for a session a person killed, one a parent
agent ended, one a workflow run finished off and one whose agent typed
``/exit`` -- four different stories told the same way -- and nothing at all
about who had started it. ``created_by`` (on the definition) and
``ended_by`` (on the record) carry it: these tests follow each one from the
request that makes it, through the record, across a daemon restart, to the
list the rail reads.
"""

from __future__ import annotations

import asyncio
import io
import sys
import time

from claude_launcher import daemon_client, lineage, profile, store
from claude_launcher.daemon import actors, db, paths
from claude_launcher.daemon.api import build_app, cookie_name
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

WAIT = "import sys\nprint('READY')\nfor line in sys.stdin:\n    pass\n"
QUIT = "print('bye')\n"

BEARER = {"Authorization": "Bearer sekrit"}


def _register(name: str, child: str) -> None:
    store.update(
        lambda doc: doc.setdefault("harnesses", {}).update(
            {name: {"command": [sys.executable, "-u", "-c", child]}}
        )
    )
    if not profile.resolve(name).exists():
        lineage.set_harness(profile.create(name), name)


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _serve(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    mm = MeshManager(mgr, root=paths.daemon_dir() / "mesh-test")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _cookie(client) -> dict:
    resp = await client.post("/api/auth/session", json={"token": "sekrit"})
    assert resp.status == 200
    return {cookie_name("sekrit"): resp.cookies[cookie_name("sekrit")].value}


async def _wait_for(cond, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _create_body(name, tmp_path, profile_name="w"):
    return {"name": name, "profile": profile_name, "cwd": str(tmp_path),
            "beads": False, "workflow": "-"}


# --------------------------------------------------------------------------- #
# the actor vocabulary


def test_the_header_names_a_kind_and_optionally_a_session():
    assert actors.parse_header("agent:s12") == {"kind": "agent", "name": "s12"}
    assert actors.parse_header("cflow:lead") == {"kind": "cflow", "name": "lead"}
    assert actors.parse_header("user") == {"kind": "user"}
    # Observed kinds cannot be claimed, and junk is no declaration at all.
    assert actors.parse_header("self") is None
    assert actors.parse_header("os:x") is None
    assert actors.parse_header("root:x") is None
    assert actors.parse_header("") is None
    assert actors.parse_header(None) is None


def test_a_managed_shell_declares_its_session_and_a_person_s_shell_nothing():
    assert actors.env_header({"CLAUNCH_SESSION": "s7"}) == "agent:s7"
    assert actors.env_header({}) is None
    assert actors.env_header({"CLAUNCH_SESSION": "  "}) is None


def test_stored_actors_are_normalised_and_anything_else_dropped():
    assert actors.coerce({"kind": "user", "via": "web", "extra": 1}) == {
        "kind": "user", "via": "web"}
    assert actors.coerce({"kind": "wizard"}) is None
    assert actors.coerce("user") is None
    assert actors.coerce(None) is None


def test_the_client_sends_the_header_from_its_shell(monkeypatch):
    sent = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        sent.update({k.lower(): v for k, v in req.header_items()})
        return _Resp(b"{}")

    monkeypatch.setattr(daemon_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    daemon_client.DaemonClient("http://x", "t").get("/api/health")
    assert sent[actors.HEADER.lower()] == "agent:s9"

    sent.clear()
    monkeypatch.delenv("CLAUNCH_SESSION")
    daemon_client.DaemonClient("http://x", "t").get("/api/health")
    assert actors.HEADER.lower() not in sent

    sent.clear()
    client = daemon_client.DaemonClient("http://x", "t", actor="cflow:lead")
    client.get("/api/health")
    assert sent[actors.HEADER.lower()] == "cflow:lead"


def test_created_by_rides_the_definition_and_is_absent_when_unknown():
    sdef = SessionDef(name="a", created_by={"kind": "agent", "name": "lead"})
    assert SessionDef.from_dict(sdef.to_dict()).created_by == {"kind": "agent", "name": "lead"}
    plain = SessionDef(name="b").to_dict()
    assert "created_by" not in plain
    assert SessionDef.from_dict({**plain, "created_by": {"kind": "nobody"}}).created_by is None


# --------------------------------------------------------------------------- #
# creation


def test_creation_records_the_dashboard_a_shell_and_an_agent(home, tmp_path):
    _register("w", WAIT)

    async def run():
        mgr = _manager()
        client = await _serve(mgr)
        try:
            cookies = await _cookie(client)
            resp = await client.post("/api/sessions", json=_create_body("web1", tmp_path),
                                     cookies=cookies)
            assert resp.status == 201, await resp.json()
            assert (await resp.json())["created_by"] == {"kind": "user", "via": "web"}

            resp = await client.post("/api/sessions", json=_create_body("cli1", tmp_path),
                                     headers=BEARER)
            assert resp.status == 201, await resp.json()
            assert mgr.get("cli1").sdef.created_by == {"kind": "user", "via": "cli"}

            # A body cannot say who created it: the request does.
            body = {**_create_body("ag1", tmp_path),
                    "created_by": {"kind": "user", "via": "web"}}
            resp = await client.post("/api/sessions", json=body,
                                     headers={**BEARER, actors.HEADER: "agent:lead"})
            assert resp.status == 201, await resp.json()
            assert mgr.get("ag1").sdef.created_by == {"kind": "agent", "name": "lead"}
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_spawn_records_the_agent_the_dashboard_or_the_run(home, tmp_path):
    _register("w", WAIT)

    async def run():
        mgr = _manager()
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="lead", harness="w", profile="w", cwd=str(tmp_path)))
            spawn = {"mesh": "-", "workflow": "-", "beads": False}

            resp = await client.post("/api/sessions/lead/children",
                                     json={**spawn, "name": "byagent"},
                                     headers={**BEARER, actors.HEADER: "agent:lead"})
            assert resp.status == 201, await resp.json()
            assert (await resp.json())["session"]["created_by"] == {
                "kind": "agent", "name": "lead"}

            resp = await client.post("/api/sessions/lead/children",
                                     json={**spawn, "name": "byperson"},
                                     cookies=await _cookie(client))
            assert resp.status == 201, await resp.json()
            assert mgr.get("byperson").sdef.created_by == {"kind": "user", "via": "web"}

            resp = await client.post("/api/sessions/lead/children",
                                     json={**spawn, "name": "byrun"},
                                     headers={**BEARER, actors.HEADER: "cflow:lead"})
            assert resp.status == 201, await resp.json()
            assert mgr.get("byrun").sdef.created_by == {"kind": "cflow", "name": "lead"}

            # Never inherited: the parent's own creator is not the child's.
            assert mgr.get("lead").sdef.created_by is None
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# endings


def test_each_way_of_ending_names_its_ender(home, tmp_path):
    _register("w", WAIT)
    _register("q", QUIT)

    async def run():
        mgr = _manager()
        client = await _serve(mgr)
        try:
            for name in ("lead", "bycookie", "bycli"):
                mgr.create(SessionDef(name=name, harness="w", profile="w", cwd=str(tmp_path)))
            for name in ("child", "reaped"):
                mgr.spawn("lead", {"name": name})
            cookies = await _cookie(client)

            resp = await client.post("/api/sessions/bycookie/kill?winddown=0",
                                     cookies=cookies)
            assert resp.status == 200
            resp = await client.post("/api/sessions/bycli/kill?winddown=0", headers=BEARER)
            assert resp.status == 200
            resp = await client.post("/api/sessions/lead/children/child/kill",
                                     headers={**BEARER, actors.HEADER: "agent:lead"})
            assert resp.status == 200
            resp = await client.post("/api/sessions/lead/children/reaped/kill",
                                     headers={**BEARER, actors.HEADER: "cflow:lead"})
            assert resp.status == 200
            # The program ending by itself: nobody asked.
            mgr.create(SessionDef(name="quitter", harness="q", profile="q", cwd=str(tmp_path)))

            names = ("bycookie", "bycli", "child", "reaped", "quitter")
            await _wait_for(lambda: all(mgr.get(n).exited for n in names), "the exits")
            want = {
                "bycookie": {"kind": "user", "via": "web"},
                "bycli": {"kind": "user", "via": "cli"},
                "child": {"kind": "agent", "name": "lead"},
                "reaped": {"kind": "cflow", "name": "lead"},
                "quitter": {"kind": "self"},
            }
            for name, actor in want.items():
                assert mgr.get(name).ended_by == actor, name

            # The list the rail reads carries them.
            resp = await client.get("/api/sessions?state=all", headers=BEARER)
            rows = {r["name"]: r for r in (await resp.json())["sessions"]}
            for name, actor in want.items():
                assert rows[name]["ended_by"] == actor, name
            assert rows["lead"]["ended_by"] is None

            # A daemon restart retires them as records that still say so.
            mgr.persist()
            await mgr.shutdown_all()
            again = _manager()
            again.restore_all()
            for name, actor in want.items():
                assert again.get(name).ended_by == actor, name
                assert again.get(name).info()["ended_by"] == actor, name
            await again.shutdown_all()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_pause_names_its_asker_and_a_later_kill_takes_over(home, tmp_path):
    _register("w", WAIT)

    async def run():
        mgr = _manager()
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="p", harness="w", profile="w", cwd=str(tmp_path)))
            resp = await client.post("/api/sessions/p/pause", cookies=await _cookie(client))
            assert resp.status == 200
            await _wait_for(lambda: mgr.get("p").exited, "the pause")
            assert mgr.get("p").ended_by == {"kind": "user", "via": "web"}

            # Killing a paused record files it as killed -- by this asker.
            resp = await client.post("/api/sessions/p/kill",
                                     headers={**BEARER, actors.HEADER: "agent:boss"})
            assert (await resp.json())["unpaused"] is True
            assert mgr.get("p").ended_by == {"kind": "agent", "name": "boss"}
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_first_asker_keeps_the_ending(home, tmp_path):
    """A person's kill that starts a wind-down stays the person's when the
    wind-down's own kill (or kill-on-end) lands afterwards."""
    _register("w", WAIT)

    async def run():
        mgr = _manager()
        try:
            session = mgr.create(SessionDef(name="s", harness="w", profile="w",
                                            cwd=str(tmp_path)))
            session.note_ender(actors.make(actors.USER, via=actors.VIA_WEB))
            mgr.kill("s", by=actors.make(actors.CFLOW, "s", reason="kill-on-end"))
            await _wait_for(lambda: mgr.get("s").exited, "the kill")
            assert mgr.get("s").ended_by == {"kind": "user", "via": "web"}
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_kill_with_no_known_asker_is_not_filed_as_the_session_s_own(home, tmp_path):
    """Asked for, asker unknown (a path that passes none): left None, never
    guessed as ``self`` -- the rail then says nothing rather than a wrong
    thing."""
    _register("w", WAIT)

    async def run():
        mgr = _manager()
        try:
            mgr.create(SessionDef(name="s", harness="w", profile="w", cwd=str(tmp_path)))
            mgr.kill("s")
            await _wait_for(lambda: mgr.get("s").exited, "the kill")
            assert mgr.get("s").ended_by is None
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_an_old_record_without_the_field_restores_as_unknown(home):
    db.open_default().save(
        [{"def": SessionDef(name="old", harness="w").to_dict(),
          "was_running": False, "exit_code": 0}],
        prune=True, allow_empty=True,
    )
    mgr = _manager()
    mgr.restore_all()
    assert mgr.get("old").ended_by is None
    assert mgr.get("old").sdef.created_by is None
    assert mgr.get("old").info()["ended_by"] is None
