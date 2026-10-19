"""What the dashboard's recurring polls put on the wire.

The rail and the sidebar are fed by ``/api/sessions`` and ``/api/mesh``
every few seconds, from every open tab. Measured on a working daemon (193
sessions, nine meshes) the pair was 1.3MB per tick: a fifth of the session
list was ``\\uXXXX`` escapes of Korean task text, and 480KB of the mesh
list was the pairwise member graph that only the per-mesh views draw. These
pin the shape that replaced it — UTF-8 bodies, gzip past a threshold, and a
mesh list without the graph — so a later "simplification" cannot put the
megabyte back without a test saying so.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from claude_launcher import store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_json_goes_out_as_utf8_and_a_lone_surrogate_falls_back():
    """Korean text is sent as itself, three bytes a character rather than
    six of escape; a string that cannot be encoded still gets an answer."""
    text = api_mod._dumps({"task": "세션 목록"})
    assert text == '{"task": "세션 목록"}'
    # A lone surrogate (what a filename decoded with surrogateescape looks
    # like) cannot be UTF-8; the payload is escaped instead of raising.
    text = api_mod._dumps({"path": "bad\udcff"})
    assert text == json.dumps({"path": "bad\udcff"})
    assert text.encode("utf-8")  # the escaped form is plain ASCII


def test_large_bodies_are_gzipped_when_asked_and_small_ones_are_not(home, tmp_path):
    """The threshold, seen from the client: a body past ``JSON_GZIP_MIN`` comes
    back compressed for a client that accepts it, a small one as it is, and a
    client that does not accept gzip gets plain text either way."""

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            # Four FAQ answers near the field's own cap make the catalogue
            # cross the threshold; a real route, since the router is frozen
            # once the server is up.
            for i in range(4):
                resp = await client.post(
                    "/api/briefing/faq", headers=BEARER,
                    json={"question": f"q{i}", "answer": "x" * 4900},
                )
                assert resp.status in (200, 201), await resp.text()
            resp = await client.get(
                "/api/briefing/faq", headers={**BEARER, "Accept-Encoding": "gzip"}
            )
            assert resp.status == 200
            assert resp.headers.get("Content-Encoding") == "gzip"
            big = await resp.json()  # the client inflates it
            assert len(json.dumps(big)) >= api_mod.JSON_GZIP_MIN
            resp = await client.get(
                "/api/briefing/faq", headers={**BEARER, "Accept-Encoding": "identity"}
            )
            assert resp.headers.get("Content-Encoding") is None
            assert (await resp.json()) == big
            resp = await client.get("/api/health", headers={"Accept-Encoding": "gzip"})
            assert resp.headers.get("Content-Encoding") is None
            assert resp.headers["Content-Type"].startswith("application/json")
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_mesh_list_leaves_the_member_graph_to_the_mesh_view(home, tmp_path):
    """``/api/mesh?view=rail`` (the sidebar's poll) carries no
    ``member_links``; ``/api/mesh/<name>`` (what the mesh page and the flow
    view fetch) still does, so the diagram that draws the graph has it and
    the rail that never did stops paying for it."""

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name in ("lead", "dev"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
                await mm.join("team", name)
            listed = await (
                await client.get("/api/mesh?view=rail", headers=BEARER)
            ).json()
            (entry,) = [m for m in listed["meshes"] if m["name"] == "team"]
            assert "member_links" not in entry
            assert {m["handle"] for m in entry["members"]} == {"lead", "dev"}
            one = await (await client.get("/api/mesh/team", headers=BEARER)).json()
            pairs = {frozenset((e["a"], e["b"])) for e in one["member_links"]}
            assert frozenset(("lead", "dev")) in pairs
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_rail_polls_carry_no_number_that_moves_by_itself(home, tmp_path, monkeypatch):
    """A rail answer is the same bytes until something happens (claunch-4thlk.1).

    Measured on a working daemon, the sessions, cflow and mesh rails were 0/5
    identical five seconds apart, and none of the differences was news: a
    countdown's remaining seconds, the tps reading's age, the relay's last
    round trip. The countdowns became absolute times (test_cflow_timers,
    test_reminder_clock); these two are left off the rail, which does not
    draw them -- the age is drawn from ``ts``, and the latency badge reads the
    relay's round trip off the control socket's pong. The full mesh view
    still carries the round trip for whoever asks for it."""

    _register_py_harness()
    relay = {
        "configured": True, "connected": True, "count": 1, "connected_count": 1,
        "relays": [{"id": "r1", "url": "wss://r", "connected": True,
                    "rtt_ms": 41.5, "rtt_age": 3.2, "pending_ms": None}],
    }

    def tps(info, *args, **kwargs):
        info["tps"] = {"ts": "2026-10-19T10:00:00+00:00", "age_s": 12, "idle": False}
        return info

    monkeypatch.setattr(api_mod.metering, "attach", tps)

    async def run():
        from aiohttp.test_utils import TestClient, TestServer

        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm,
                        relay_state=lambda: json.loads(json.dumps(relay)))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
            rail = await (await client.get("/api/mesh?view=rail", headers=BEARER)).json()
            (row,) = rail["relay"]["relays"]
            assert row == {"id": "r1", "url": "wss://r", "connected": True}
            full = await (await client.get("/api/mesh", headers=BEARER)).json()
            assert full["relay"]["relays"][0]["rtt_ms"] == 41.5

            listed = await (await client.get(
                "/api/sessions?view=rail&state=current", headers=BEARER)).json()
            (w1,) = [s for s in listed["sessions"] if s["name"] == "w1"]
            assert w1["tps"] == {"ts": "2026-10-19T10:00:00+00:00", "idle": False}
            # The non-rail list is unchanged: its readers may want the age.
            listed = await (await client.get("/api/sessions", headers=BEARER)).json()
            (w1,) = [s for s in listed["sessions"] if s["name"] == "w1"]
            assert w1["tps"]["age_s"] == 12
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_sessions_rail_sends_only_the_rows_a_page_does_not_hold(home, tmp_path):
    """The row delta (claunch-4thlk.3), through the control socket that
    carries it, with shared reads on -- the list is one answer for every
    page, and the cut from it is each page's own.

    A read that sends ``rows: {}`` gets every row with its digest; sent
    again with those digests it gets no rows, every name, ``delta``; a row
    that changed comes back alone; a page holding other digests (a second
    tab) gets its own cut; and HTTP, which carries no digests, gets the list
    it always had."""

    _register_py_harness()
    path = "/api/sessions?view=rail&state=current"

    async def read(ws, rid, rows):
        await ws.send_json({"type": "read", "id": rid, "paths": [path],
                            "args": {path: {"rows": rows}}})
        while True:
            data = json.loads((await asyncio.wait_for(ws.receive(), 10)).data)
            if data.get("type") == "read_result" and data.get("id") == rid:
                return data["answers"][path]

    async def run():
        from aiohttp.test_utils import TestClient, TestServer

        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm,
                        shared_read_max_age=60.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for name in ("a1", "a2"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive()  # init
                first = await read(ws, 1, {})
                assert first["delta"] is True
                assert sorted(first["names"]) == ["a1", "a2"]
                assert sorted(r["name"] for r in first["sessions"]) == ["a1", "a2"]
                held = first["row_hashes"]
                assert sorted(held) == ["a1", "a2"]

                second = await read(ws, 2, held)
                assert second["sessions"] == [] and second["row_hashes"] == {}
                assert second["names"] == first["names"]

                # Another page holding a stale digest for a1 gets a1, from the
                # same shared answer.
                other = await read(ws, 3, {**held, "a1": "0" * 16})
                assert [r["name"] for r in other["sessions"]] == ["a1"]
                assert other["row_hashes"] == {"a1": held["a1"]}

            # HTTP carries no digests: the whole list, no delta.
            whole = await (await client.get(path, headers=BEARER)).json()
            assert "delta" not in whole
            assert sorted(r["name"] for r in whole["sessions"]) == ["a1", "a2"]
            assert whole["sessions"] == first["sessions"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_rail_names_each_status_check_once_and_defers_exited_commits():
    """claunch-4thlk.5: what repeated on every rail row is said once."""
    check = {"id": "c1", "name": "C", "question": "committed?", "answer": "yes",
             "reported_at": "2026-10-19T00:00:00+00:00", "source": "agent"}
    rows = [
        {"name": "live", "status": "running", "status_checks": [dict(check)],
         "rail_commits": {"commits": [{"sha": "a"}], "more": 0}},
        {"name": "gone", "status": "exited",
         "status_checks": [dict(check, answer="no")],
         "rail_commits": {"commits": [{"sha": "b"}], "more": 3}},
        {"name": "bare", "status": "exited"},
    ]
    defs = api_mod._rail_slim_rows(rows)
    assert defs == {"c1": {"name": "C", "question": "committed?"}}
    assert rows[0]["status_checks"] == [{"id": "c1", "answer": "yes",
                                         "reported_at": "2026-10-19T00:00:00+00:00",
                                         "source": "agent"}]
    assert rows[1]["status_checks"][0]["answer"] == "no"
    # A live card keeps its chips; an exited one reads them when selected.
    assert rows[0]["rail_commits"]["commits"] == [{"sha": "a"}]
    assert "rail_commits" not in rows[1] and rows[1]["rail_commits_deferred"] is True
    assert rows[2] == {"name": "bare", "status": "exited"}
