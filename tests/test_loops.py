"""Open loops: the ledger, its two automatic writers, and the doors onto it.

claunch-ik8n. A re-briefing re-derives everything the daemon can see; what
it could not restore was the wait an agent was in the middle of -- observed
on mesh-0826 (2026-09-10) when a leader's postponed re-send of two board
assignments survived only as a sentence in its own conversation summary.
These tests pin the ledger that now holds that half:

* the ledger itself: add, dedupe by key, close, stale past the horizon;
* the mesh writes a re-send loop when it refuses a send for a full inbox,
  and closes it when a later send to that member is accepted;
* a reply-wait is READ off the mesh's response watch, never stored twice,
  so it clears the moment the threaded reply lands;
* the re-briefing carries the open list, capped, with stale entries marked;
* the API routes and the MCP tools reach the same file.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher import mesh_mcp, store
from claude_launcher.daemon import loops, rebrief
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import Member, MeshBusy, MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #
def test_add_close_and_list(home):
    entry = loops.add(
        "s1", "  reply from  w2 ", resume_when="w2 answers", then="merge",
        refs={"issue": "claunch-1"},
    )
    assert entry["id"].startswith("loop-")
    assert entry["what"] == "reply from w2"          # whitespace folded
    assert entry["kind"] == "manual"
    assert entry["refs"] == {"issue": "claunch-1"}
    assert entry["closed_at"] is None
    assert loops.ledger_file("s1").is_file()

    rows = loops.open_entries("s1")
    assert [r["id"] for r in rows] == [entry["id"]]
    assert rows[0]["stale"] is False

    closed = loops.close("s1", entry["id"], note="merged")
    assert closed["closed_note"] == "merged"
    assert loops.open_entries("s1") == []
    # closed entries stay on file, readable after the fact
    assert [r["id"] for r in loops.all_entries("s1")] == [entry["id"]]
    # closing twice, or an unknown id, is a clear None -- not a second stamp
    assert loops.close("s1", entry["id"]) is None
    assert loops.close("s1", "loop-nope") is None


def test_the_ledger_is_per_session_and_an_empty_one_reads_empty(home):
    loops.add("s1", "wait a")
    assert loops.open_entries("s2") == []
    assert loops.all_entries("s2") == []
    assert loops.summary("s2")["open"] == []


def test_a_key_updates_the_open_entry_in_place(home):
    first = loops.add("s1", "re-send to lead: v1", key="resend:team:lead")
    second = loops.add("s1", "re-send to lead: v2", key="resend:team:lead")
    assert second["id"] == first["id"]
    rows = loops.open_entries("s1")
    assert len(rows) == 1 and rows[0]["what"] == "re-send to lead: v2"
    # once closed, the same key starts a NEW entry
    loops.close("s1", first["id"])
    third = loops.add("s1", "re-send to lead: v3", key="resend:team:lead")
    assert third["id"] != first["id"]
    assert loops.close_key("s1", "resend:team:lead")[0]["id"] == third["id"]


def test_what_is_required_and_the_horizon_must_be_positive(home):
    with pytest.raises(ValueError):
        loops.add("s1", "   ")
    with pytest.raises(ValueError):
        loops.add("s1", "x", expires_in=0)


def test_a_loop_past_its_horizon_is_stale_not_gone(home, monkeypatch):
    entry = loops.add("s1", "window opens", expires_in=60)
    assert loops.open_entries("s1")[0]["stale"] is False
    later = (datetime.now(timezone.utc) + timedelta(seconds=120)).replace(microsecond=0)
    monkeypatch.setattr(loops, "utcnow", lambda: later)
    rows = loops.open_entries("s1")
    assert rows[0]["id"] == entry["id"] and rows[0]["stale"] is True
    assert loops.summary("s1")["stale"] == 1
    # the default horizon applies when none is given
    fresh = loops.add("s1", "no horizon given")
    exp = datetime.fromisoformat(fresh["expires_at"])
    assert exp - later == timedelta(seconds=loops.DEFAULT_TTL_SECS)


def test_a_torn_ledger_file_reads_as_empty_and_is_rewritten(home):
    path = loops.ledger_file("s1")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert loops.open_entries("s1") == []
    loops.add("s1", "after the tear")
    assert [r["what"] for r in loops.open_entries("s1")] == ["after the tear"]


# --------------------------------------------------------------------------- #
# the mesh's writers
# --------------------------------------------------------------------------- #
def test_a_refused_send_leaves_a_resend_loop_on_the_sender(home, tmp_path):
    """The bounce says "nothing was queued" -- so the intent to re-send has
    nowhere to live but the sender's ledger. One entry per refused member,
    folded on repeat refusals, closed by the send that finally lands."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("s1", "s2", "s3"):
            mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        await mm.join("team", "s1", handle="lead")
        await mm.join("team", "s2", handle="w1")
        await mm.join("team", "s3", handle="w2")
        mm.set_policy("team", {"backpressure": {"inbox_max": 1, "retry_after": 45.0}})

        await mm.send("team", "w1", "lead", "report 0")
        assert loops.open_entries("s2") == []       # accepted: nothing to remember

        with pytest.raises(MeshBusy):
            await mm.send("team", "w1", "lead", "assignment: claunch-ynbx.7")
        rows = loops.open_entries("s2")
        assert len(rows) == 1
        loop = rows[0]
        assert loop["kind"] == "resend"
        assert loop["key"] == loops.resend_key("team", "lead")
        assert "lead" in loop["what"] and "claunch-ynbx.7" in loop["what"]
        assert "cap 1" in loop["resume_when"]
        assert 'claunch mesh send team lead "..."' in loop["then"]
        assert loop["refs"] == {"mesh": "team", "handle": "lead"}

        # a second refusal folds into the same entry, with the newer text
        with pytest.raises(MeshBusy):
            await mm.send("team", "w1", "lead", "assignment again")
        rows = loops.open_entries("s2")
        assert len(rows) == 1 and rows[0]["id"] == loop["id"]
        assert "assignment again" in rows[0]["what"]

        # a partial send: w2 has room, lead does not -> a loop for lead only
        result = await mm.send("team", "w1", ["lead", "w2"], "to both")
        assert result["recipients"] == ["w2"]
        assert [e["handle"] for e in result["deferred"]] == ["lead"]
        keys = sorted(r["key"] for r in loops.open_entries("s2"))
        assert keys == [loops.resend_key("team", "lead")]

        # the door opens and the re-send lands: the loop closes itself
        mm.set_policy("team", {"backpressure": {"inbox_max": 0}})
        await mm.send("team", "w1", "lead", "assignment, delivered")
        assert loops.open_entries("s2") == []
        done = loops.all_entries("s2")
        assert len(done) == 1 and done[0]["closed_at"]
        assert "a later send to lead was accepted" in done[0]["closed_note"]
        # the refused recipient's own ledger was never touched
        assert loops.all_entries("s1") == []

    asyncio.run(run())


def test_a_reply_wait_is_read_off_the_response_watch(home, tmp_path):
    """Not stored twice: the watch the mesh opens on delivery of an ask IS
    the loop, and the threaded reply that settles the watch ends it."""

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = mm.create("team")
        mesh.members = {"lead": Member("lead", "s1"), "worker": Member("worker", "s2")}
        mm._persist_def(mesh)
        mm.set_policy("team", {"backpressure": {"inbox_max": 0}})

        sent = await mm.send("team", "lead", "worker", "please check", type="ask")
        # Before delivery the watch does not exist: the worker has not seen it.
        assert loops.reply_waits("s1", mm) == []
        original = next(m for m in mesh.messages if m["id"] == sent["id"])
        mm._watch_delivered_responses(mesh, "worker", [original])

        waits = loops.reply_waits("s1", mm)
        assert len(waits) == 1
        w = waits[0]
        assert w["kind"] == "reply" and w["derived"] is True
        assert w["id"] == f"reply:{sent['id']}"
        assert "worker" in w["what"] and sent["id"] in w["what"]
        assert w["refs"] == {"mesh": "team", "message": sent["id"], "handle": "worker"}
        # the debtor's own view has nothing: it owes, it is not waiting
        assert loops.reply_waits("s2", mm) == []
        # and the stored ledger holds no copy
        assert loops.open_entries("s1") == []

        await mm.send("team", "worker", "lead", "done", type="ack", reply_to=sent["id"])
        assert loops.reply_waits("s1", mm) == []
        # a session in no mesh, or with no manager at all, simply has none
        assert loops.reply_waits("s9", mm) == []
        assert loops.reply_waits("s1", None) == []

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the re-briefing
# --------------------------------------------------------------------------- #
def _staged_compose(mgr, mm, sdef) -> str:
    """Stage ``sdef`` (once) and compose its re-briefing."""

    async def scenario() -> str:
        if sdef.name not in {s.sdef.name for s in mgr.list()}:
            mgr.stage(sdef)
        return rebrief.compose(sdef.name, manager=mgr, mesh_mgr=mm)

    return asyncio.run(scenario())


def test_the_rebrief_carries_open_loops_before_the_task(home, tmp_path, monkeypatch):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    sdef = SessionDef(name="w1", harness="py", cwd=str(tmp_path), task="dig")

    assert "claunch loops" not in _staged_compose(mgr, mm, sdef)

    old = loops.add("w1", "old wait", expires_in=60)
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    monkeypatch.setattr(loops, "utcnow", lambda: later)
    fresh = loops.add(
        "w1", "s501's inbox to drain", resume_when="delivery resumes",
        then="re-send claunch-ynbx.7/8",
    )
    block = _staged_compose(mgr, mm, sdef)
    assert "# claunch loops: what you were waiting on" in block
    assert "open: 2 (1 stale)" in block
    assert f"STALE [{old['id']}] old wait" in block
    assert f"[{fresh['id']}] s501's inbox to drain" in block
    assert "resumes when: delivery resumes; then: re-send claunch-ynbx.7/8" in block
    # placed just before the task, so "what was I waiting for" reads into
    # "what was I doing"
    assert block.index("claunch loops") < block.index("your opening task")
    assert "loop_close" in block


def test_the_rebrief_caps_the_list_and_counts_the_rest(home, tmp_path):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    for n in range(loops.REBRIEF_LIMIT + 3):
        loops.add("w1", f"wait {n:02d}")
    block = _staged_compose(
        mgr, mm, SessionDef(name="w1", harness="py", cwd=str(tmp_path), task="dig")
    )
    assert f"open: {loops.REBRIEF_LIMIT + 3}" in block
    assert "wait 00" in block
    assert f"wait {loops.REBRIEF_LIMIT - 1:02d}" in block
    assert f"wait {loops.REBRIEF_LIMIT:02d}" not in block
    assert "[... 3 more -- the 'loops' tool lists them]" in block
    # the task still fits after the ledger
    assert "your opening task" in block


def test_a_broken_ledger_does_not_sink_the_rebrief(home, tmp_path, monkeypatch):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)

    def boom(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(loops, "rebrief_section", boom)
    block = _staged_compose(
        mgr, mm, SessionDef(name="w1", harness="py", cwd=str(tmp_path), task="dig")
    )
    assert "dig" in block and "claunch loops" not in block


# --------------------------------------------------------------------------- #
# the doors
# --------------------------------------------------------------------------- #
def test_the_api_lists_adds_and_closes(home, tmp_path):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr, settle=0.05)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.stage(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
            resp = await client.get("/api/sessions/w1/loops", headers=BEARER)
            assert resp.status == 200
            assert (await resp.json()) == {"session": "w1", "open": [], "stale": 0}

            resp = await client.post(
                "/api/sessions/w1/loops", headers=BEARER,
                json={"what": "the sweep window", "then": "run sweep.py",
                      "refs": {"issue": "claunch-1"}, "expires_in": 30},
            )
            assert resp.status == 200, await resp.text()
            loop = (await resp.json())["loop"]
            assert loop["what"] == "the sweep window"
            assert loop["refs"] == {"issue": "claunch-1"}

            resp = await client.post(
                "/api/sessions/w1/loops", headers=BEARER, json={"what": ""},
            )
            assert resp.status == 400
            resp = await client.post(
                "/api/sessions/w1/loops", headers=BEARER,
                json={"what": "x", "expires_in": "soon"},
            )
            assert resp.status == 400

            resp = await client.get("/api/sessions/w1/loops", headers=BEARER)
            body = await resp.json()
            assert [r["id"] for r in body["open"]] == [loop["id"]]

            resp = await client.post(
                f"/api/sessions/w1/loops/{loop['id']}/close", headers=BEARER,
                json={"note": "ran"},
            )
            assert resp.status == 200
            assert (await resp.json())["loop"]["closed_note"] == "ran"
            resp = await client.post(
                f"/api/sessions/w1/loops/{loop['id']}/close", headers=BEARER,
                json={},
            )
            assert resp.status == 404

            resp = await client.get("/api/sessions/w1/loops?all=1", headers=BEARER)
            body = await resp.json()
            assert body["open"] == [] and len(body["all"]) == 1

            # an unknown session is refused the way the rebrief route is:
            # ManagerError, mapped by the middleware
            resp = await client.get("/api/sessions/ghost/loops", headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(scenario())


def test_the_cli_reaches_the_same_routes(monkeypatch, capsys):
    import argparse

    from claude_launcher import cli, cli_loops

    calls = []

    class FakeClient:
        def get(self, path):
            calls.append(("GET", path, None))
            return {
                "session": "s0", "stale": 0,
                "open": [{"id": "loop-1", "what": "w2's answer", "since": "t0",
                          "resume_when": "", "then": "merge", "stale": False}],
            }

        def post(self, path, body=None):
            calls.append(("POST", path, body))
            return {"session": "s0", "loop": {"id": "loop-1", "what": "w2's answer",
                                              "since": "t0", "stale": False}}

    monkeypatch.setattr(cli_loops, "_client", lambda: FakeClient())
    monkeypatch.setenv("CLAUNCH_SESSION", "s0")

    ns = cli.build_parser().parse_args(
        ["loops", "add", "w2's answer", "--then", "merge", "--ref", "issue=i1",
         "--expires-in", "90"]
    )
    assert ns.func(ns) == 0
    ns = cli.build_parser().parse_args(["loops", "ls"])
    assert ns.func(ns) == 0
    ns = cli.build_parser().parse_args(["loops", "close", "loop-1", "--note", "got it"])
    assert ns.func(ns) == 0
    out = capsys.readouterr().out
    assert "[loop-1] w2's answer" in out and "closed loop-1" in out
    assert calls == [
        ("POST", "/api/sessions/s0/loops",
         {"what": "w2's answer", "then": "merge", "expires_in": 90.0,
          "refs": {"issue": "i1"}}),
        ("GET", "/api/sessions/s0/loops", None),
        ("POST", "/api/sessions/s0/loops/loop-1/close", {"note": "got it"}),
    ]
    # a malformed --ref is refused before anything is sent
    assert cli_loops._cmd_add(argparse.Namespace(
        session=None, what="x", resume_when=None, then=None, key=None,
        expires_in=None, ref=["novalue"],
    )) == 2
    assert len(calls) == 3
    # no session at all: told, exit 2, nothing sent
    monkeypatch.delenv("CLAUNCH_SESSION")
    assert cli_loops._cmd_ls(argparse.Namespace(session=None, all=False)) == 2
    assert len(calls) == 3


def test_the_mcp_tools_reach_the_session_routes(monkeypatch):
    calls = []

    class FakeClient:
        def get(self, path):
            calls.append(("GET", path, None))
            return {"session": "s0", "open": [], "stale": 0}

        def post(self, path, body=None):
            calls.append(("POST", path, body))
            return {"session": "s0", "loop": {"id": "loop-1"}}

    monkeypatch.setattr(mesh_mcp, "_client", lambda: FakeClient())
    monkeypatch.setenv("CLAUNCH_SESSION", "s0")

    assert mesh_mcp.call_tool("loops", {})["open"] == []
    assert mesh_mcp.call_tool("loops", {"all": True})["open"] == []
    out = mesh_mcp.call_tool(
        "loop_add",
        {"what": "w2's answer", "then": "merge", "refs": {"issue": "i1"},
         "resume_when": "", "expires_in": 120},
    )
    assert out["loop"]["id"] == "loop-1"
    assert mesh_mcp.call_tool("loop_close", {"id": "loop-1", "note": "got it"})
    with pytest.raises(mesh_mcp.MeshMcpError):
        mesh_mcp.call_tool("loop_close", {})

    assert calls == [
        ("GET", "/api/sessions/s0/loops", None),
        ("GET", "/api/sessions/s0/loops?all=1", None),
        ("POST", "/api/sessions/s0/loops",
         {"what": "w2's answer", "then": "merge", "refs": {"issue": "i1"},
          "expires_in": 120}),
        ("POST", "/api/sessions/s0/loops/loop-1/close", {"note": "got it"}),
    ]
