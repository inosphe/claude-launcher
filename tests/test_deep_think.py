"""deep_think (daemon/deep_think.py): a session asks itself a question, a
quick-fork of it answers, and the answer comes back as a merge.

The conversation copy is claude's ``--resume --fork-session`` and the ``py``
harness has no transcript, so -- exactly as test_handoff.py does -- the copy is
stubbed at :func:`spawn.can_fork` / :func:`spawn._fork_parents_conversation`
and everything around it is exercised for real: the fork and its record, the
asynchronous reply, notes, the answer's header, the search record, the
message log, the cap, cancel/extend, and the deadline ladder.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import timedelta

import pytest

from claude_launcher import lineage, mesh_mcp, profile, spawn as spawn_mod, store
from claude_launcher.daemon import deep_think as dt
from claude_launcher.daemon import search_records
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.session import Session

from test_mesh_wire import _Manager, _member

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _daemon_settings(**values):
    store.update(lambda doc: doc.setdefault("daemon", {}).update(values))


async def _serve(mgr, mm):
    from aiohttp import ClientTimeout
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app), timeout=ClientTimeout(total=300))
    await client.start_server()
    return client


async def _wait_for(cond, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def forkable(monkeypatch):
    monkeypatch.setattr(spawn_mod, "can_fork", lambda parent: True)
    monkeypatch.setattr(
        spawn_mod, "_fork_parents_conversation", lambda child, parent, request: None
    )


@pytest.fixture
def typed(monkeypatch):
    seen = []

    async def fake_deliver(self, text, *, force=False):
        seen.append((self.sdef.name, text))
        return True

    monkeypatch.setattr(Session, "deliver", fake_deliver)
    return seen


def _mgr():
    return SessionManager(idle_threshold=0.2, scrollback=200, restore_default=True)


def _run(scenario, tmp_path):
    """Serve a fresh daemon with origin ``a`` and run ``scenario(mgr, mm, client)``."""
    _register_py_harness()

    async def run():
        mgr = _mgr()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            await scenario(mgr, mm, client)
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


async def _ask(client, origin="a", **body):
    body.setdefault("question", "where is the merge delivered?")
    resp = await client.post(f"/api/sessions/{origin}/deep-think", json=body, headers=BEARER)
    return resp.status, await resp.json()


# ---- the blocks ------------------------------------------------------------- #

def test_the_question_block_scopes_the_copy_to_the_question():
    text = dt.compose_question(
        origin="a", fork="a-dt1", question="why X?\nlook at y.py", deadline="2026-10-07T11:00:00+00:00",
    )
    assert text.startswith("---\n# claunch deep-think: QUESTION")
    assert "asked_by: a\n" in text and "id: a-dt1\n" in text
    # not the origin's task to continue
    assert "Answer it and nothing else" in text and "cflow run" in text
    # edits are allowed, announced through notify
    assert '{op: "notify"' in text
    # the answer is the merge
    assert "'handoff' tool" in text and "no length" in text
    assert text.endswith("Question:\nwhy X?\nlook at y.py")


def test_the_answer_block_names_the_question_it_answers():
    block = dt.compose_answer(
        fork="a-dt1", record={"question": "why X?", "asked_at": "t0"}, text="  because Y  ",
    )
    assert block.startswith("---\n# claunch deep-think: ANSWER from a-dt1")
    assert "question: why X?\n" in block and "asked_at: t0\n" in block
    assert block.endswith("---\nbecause Y")


def test_the_record_round_trips_and_is_absent_by_default():
    assert "deep_think" not in SessionDef(name="x").to_dict()
    rec = {"question": "q", "state": "running"}
    copy = SessionDef(name="x-dt1", quick_fork_of="x", deep_think=rec)
    assert SessionDef.from_dict(copy.to_dict()).deep_think == rec
    assert SessionDef.from_dict({"name": "y", "deep_think": {}}).deep_think is None


# ---- ask ---------------------------------------------------------------------- #

def test_ask_forks_the_asker_and_answers_asynchronously(home, tmp_path, forkable, typed):
    async def scenario(mgr, mm, client):
        status, doc = await _ask(client, question="where is the merge delivered?", timeout=600)
        assert status == 201, doc
        assert doc["id"] == "a-dt1" and doc["async"] is True
        assert "NOT in this reply" in doc["note"] and "do not wait" in doc["note"].lower()
        fork = mgr.get("a-dt1")
        assert fork.sdef.quick_fork_of == "a" and fork.sdef.parent == "a"
        rec = fork.sdef.deep_think
        assert rec["question"] == "where is the merge delivered?"
        assert rec["state"] == dt.RUNNING and rec["timeout"] == 600
        # the marker comes first, the question under it
        assert fork.sdef.task.startswith("---\n# claunch quick-fork: --- forked from here ---")
        assert "# claunch deep-think: QUESTION" in fork.sdef.task
        # the asker is not typed into by asking
        assert [n for n, _ in typed if n == "a"] == []
        # the question is already a search record of the fork
        (event,) = [e for e in search_records.rows("a-dt1") if e["id"] == dt.RECORD_ID]
        assert "where is the merge delivered?" in event["text"]
        # status lists it
        resp = await client.get("/api/sessions/a/deep-think", headers=BEARER)
        listed = (await resp.json())["running"]
        assert [r["id"] for r in listed] == ["a-dt1"]

    _run(scenario, tmp_path)


def test_ask_refuses_past_the_cap_an_empty_question_and_a_fork_asking(home, tmp_path, forkable, typed):
    _daemon_settings(deep_think_max=1)

    async def scenario(mgr, mm, client):
        assert (await _ask(client))[0] == 201
        status, doc = await _ask(client)
        assert status == 409 and "a-dt1" in doc["error"]
        assert (await _ask(client, question="  "))[0] == 400
        status, doc = await _ask(client, origin="a-dt1")
        assert status == 400 and "itself a deep_think fork" in doc["error"]
        assert (await _ask(client, timeout=-5))[0] == 400

    _run(scenario, tmp_path)


def test_model_and_effort_ride_the_spawn_body(home, tmp_path, forkable, typed, monkeypatch):
    from claude_launcher.daemon import api

    seen = {}
    real = api.stage_quick_fork

    async def spy(request, parent, body, **kw):
        seen.update(kw.get("spawn_extra") or {})
        return await real(request, parent, body, **{**kw, "spawn_extra": {}})

    monkeypatch.setattr(api, "stage_quick_fork", spy)

    async def scenario(mgr, mm, client):
        status, doc = await _ask(client, model="claude-opus-5-5", effort="max")
        assert status == 201, doc
        assert seen == {"model": "claude-opus-5-5", "effort": "max"}
        rec = mgr.get(doc["id"]).sdef.deep_think
        assert rec["model"] == "claude-opus-5-5" and rec["effort"] == "max"

    _run(scenario, tmp_path)


# ---- notes and the answer ------------------------------------------------------ #

def test_notes_and_the_answer_reach_the_asker_and_the_mesh_log(home, tmp_path, forkable, typed):
    async def scenario(mgr, mm, client):
        mm.create("room")
        await mm.join("room", "a", handle="a")
        await _ask(client, question="why does X fail?")
        resp = await client.post(
            "/api/sessions/a-dt1/deep-think/notify",
            json={"text": "editing src/x.py:10 to add a log line"}, headers=BEARER,
        )
        doc = await resp.json()
        assert resp.status == 200, doc
        assert doc["delivered"] is True and doc["logged_in"] == ["room"]
        note = [t for n, t in typed if n == "a"][-1]
        assert note.startswith("---\n# claunch deep-think: NOTE from a-dt1")
        assert note.endswith("editing src/x.py:10 to add a log line")
        # the answer is the merge, with this module's header
        resp = await client.post(
            "/api/sessions/a-dt1/handoff", json={"text": "X fails because Y (src/x.py:12)"},
            headers=BEARER,
        )
        doc = await resp.json()
        assert resp.status == 200 and doc["target"] == "a", doc
        answer = [t for n, t in typed if n == "a"][-1]
        assert answer.startswith("---\n# claunch deep-think: ANSWER from a-dt1")
        assert "question: why does X fail?" in answer
        assert answer.endswith("X fails because Y (src/x.py:12)")
        await _wait_for(lambda: mgr.get("a-dt1").exited, "the fork to end")
        # both are in the room's log, addressed to nobody so never re-delivered
        logged = [m for m in mm.get("room").messages if m["from"] == "deep-think:a-dt1"]
        assert [m["ref"]["deep_think"]["kind"] for m in logged] == ["note", "answer"]
        assert all(m["to"] == [] for m in logged)
        assert "X fails because Y" in logged[-1]["body"]
        assert mm.get("room").pending("a") == []
        channels = [m for m in mm.list() if m.parent_channel]
        assert channels
        assert all(not any(m.get("ref", {}).get("deep_think")
                           for m in channel.messages) for channel in channels)
        # the record now holds the question and the answer
        (event,) = [e for e in search_records.rows("a-dt1") if e["id"] == dt.RECORD_ID]
        assert "why does X fail?" in event["text"] and "X fails because Y" in event["text"]

    _run(scenario, tmp_path)


def test_a_plain_quick_fork_keeps_its_merge_header(home, tmp_path, forkable, typed):
    async def scenario(mgr, mm, client):
        await client.post("/api/sessions/a/quick-fork", headers=BEARER)
        resp = await client.post("/api/sessions/a-qf1/handoff", json={"text": "did it"}, headers=BEARER)
        assert resp.status == 200
        report = [t for n, t in typed if n == "a"][-1]
        assert report.startswith("---\n# claunch quick-fork: merged from a-qf1")
        # and it cannot send deep_think notes
        resp = await client.post("/api/sessions/a-qf1/deep-think/notify", json={"text": "x"}, headers=BEARER)
        assert resp.status in (400, 404)

    _run(scenario, tmp_path)


# ---- cancel and extend ----------------------------------------------------------- #

def test_cancel_and_extend_belong_to_the_asker(home, tmp_path, forkable, typed):
    async def scenario(mgr, mm, client):
        mgr.create(SessionDef(name="b", harness="py", cwd=str(tmp_path)))
        await _ask(client, timeout=60)
        before = mgr.get("a-dt1").sdef.deep_think["deadline"]
        resp = await client.post("/api/sessions/b/deep-think/a-dt1/extend", json={"timeout": 600}, headers=BEARER)
        assert resp.status == 403
        resp = await client.post("/api/sessions/a/deep-think/a-dt1/extend", json={"timeout": 3600}, headers=BEARER)
        doc = await resp.json()
        assert resp.status == 200, doc
        assert doc["deadline"] > before and mgr.get("a-dt1").sdef.deep_think["state"] == dt.RUNNING
        resp = await client.post("/api/sessions/b/deep-think/a-dt1/cancel", headers=BEARER)
        assert resp.status == 403
        resp = await client.post("/api/sessions/a/deep-think/a-dt1/cancel", headers=BEARER)
        assert resp.status == 200 and (await resp.json())["cancelled"] is True
        await _wait_for(lambda: mgr.get("a-dt1").exited, "the cancelled fork to end")
        resp = await client.post("/api/sessions/a/deep-think/a-dt1/cancel", headers=BEARER)
        assert resp.status == 400

    _run(scenario, tmp_path)


# ---- the deadline ladder ---------------------------------------------------------- #

def test_the_deadline_ladder_asks_the_asker_then_the_fork_then_ends_it(home, tmp_path, forkable, typed):
    _daemon_settings(deep_think_grace=60)

    async def scenario(mgr, mm, client):
        await _ask(client, question="long one", timeout=120)
        app = client.server.app
        fork = mgr.get("a-dt1")
        t0 = dt._parse(fork.sdef.deep_think["asked_at"])
        # before the deadline nothing moves
        assert await dt.sweep(app, now=t0 + timedelta(seconds=60)) == []
        # past it: the asker is asked to extend or cancel
        assert await dt.sweep(app, now=t0 + timedelta(seconds=121)) == [("a-dt1", dt.ASKED)]
        overdue = [t for n, t in typed if n == "a"][-1]
        assert overdue.startswith("---\n# claunch deep-think: OVERDUE a-dt1")
        assert '{op: "extend", id: "a-dt1"' in overdue
        # within the grace nothing more; after it the fork is asked to merge
        assert await dt.sweep(app, now=t0 + timedelta(seconds=150)) == []
        assert await dt.sweep(app, now=t0 + timedelta(seconds=182)) == [("a-dt1", dt.WRAPPING)]
        assert app["handoff"].state("a-dt1")["kind"] == "merge"
        await _wait_for(
            lambda: any(n == "a-dt1" and "MERGE requested" in t for n, t in typed),
            "the merge request in the fork",
        )
        # and one more grace without an answer ends it
        assert await dt.sweep(app, now=t0 + timedelta(seconds=243)) == [("a-dt1", "ended")]
        lost = [t for n, t in typed if n == "a"][-1]
        assert lost.startswith("---\n# claunch deep-think: NO ANSWER from a-dt1")
        await _wait_for(lambda: mgr.get("a-dt1").exited, "the timed-out fork to end")

    _run(scenario, tmp_path)


def test_an_extension_puts_an_overdue_question_back_to_running(home, tmp_path, forkable, typed):
    async def scenario(mgr, mm, client):
        await _ask(client, timeout=60)
        app = client.server.app
        t0 = dt._parse(mgr.get("a-dt1").sdef.deep_think["asked_at"])
        await dt.sweep(app, now=t0 + timedelta(seconds=61))
        assert mgr.get("a-dt1").sdef.deep_think["state"] == dt.ASKED
        await client.post("/api/sessions/a/deep-think/a-dt1/extend", json={"timeout": 600}, headers=BEARER)
        assert mgr.get("a-dt1").sdef.deep_think["state"] == dt.RUNNING

    _run(scenario, tmp_path)


def test_a_fork_whose_asker_is_gone_is_ended(home, tmp_path, forkable, typed):
    async def scenario(mgr, mm, client):
        await _ask(client)
        mgr.kill("a", force=True)
        await _wait_for(lambda: mgr.get("a").exited, "the asker to end")
        assert await dt.sweep(client.server.app) == [("a-dt1", "orphaned")]
        await _wait_for(lambda: mgr.get("a-dt1").exited, "the orphan to end")

    _run(scenario, tmp_path)


# ---- the mesh log -------------------------------------------------------------- #

@pytest.mark.parametrize("has_team", [False, True])
def test_record_only_logs_to_owned_meshes_and_addresses_nobody(has_team):
    mgr = _Manager()
    mgr.add("s1")
    mm = MeshManager(mgr, settle=0.01)
    mm.create("home")
    home = mm.get("home")
    if has_team:
        _member(home, "w1", "s1", role="worker")
    mm.create("parent-pair")
    pair = mm.get("parent-pair")
    pair.parent_channel = ["s1", "s2"]
    _member(pair, "s1", "s1", role="worker")
    _member(pair, "s2", "s2", role="worker")
    assert mm.record_only("s1", sender="deep-think:s1-dt1", body="hello", ref={"deep_think": {"kind": "note"}}) == (["home"] if has_team else [])
    assert not pair.messages
    if not has_team:
        assert not home.messages
        return
    msg = home.messages[-1]
    assert msg["from"] == "deep-think:s1-dt1" and msg["to"] == [] and msg["type"] == "fyi"
    assert msg["body"] == "-> w1 (s1): hello" and msg["ref"]["for"] == "w1"
    assert home.pending("w1") == []
    assert mm.record_only("nobody", sender="x", body="y") == []


# ---- the MCP tool --------------------------------------------------------------- #

class _FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return {"ok": True}

    def get(self, path):
        self.calls.append(("GET", path, None))
        return {"ok": True}


def test_mcp_tool_maps_each_op_to_the_route(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    mesh_mcp.call_tool("deep_think", {"question": "q?", "minutes": 5, "model": "m"})
    mesh_mcp.call_tool("deep_think", {"op": "status"})
    mesh_mcp.call_tool("deep_think", {"op": "notify", "text": "editing x"})
    mesh_mcp.call_tool("deep_think", {"op": "extend", "id": "s9-dt1", "minutes": 10})
    mesh_mcp.call_tool("deep_think", {"op": "cancel", "id": "s9-dt1"})
    assert fake.calls == [
        ("POST", "/api/sessions/s9/deep-think", {"question": "q?", "model": "m", "timeout": 300.0}),
        ("GET", "/api/sessions/s9/deep-think", None),
        ("POST", "/api/sessions/s9/deep-think/notify", {"text": "editing x"}),
        ("POST", "/api/sessions/s9/deep-think/s9-dt1/extend", {"timeout": 600.0}),
        ("POST", "/api/sessions/s9/deep-think/s9-dt1/cancel", {}),
    ]
    for bad in ({"op": "ask"}, {"op": "cancel"}, {"op": "notify"}, {"op": "nope"}, {"question": "q", "minutes": "x"}):
        with pytest.raises(mesh_mcp.MeshMcpError):
            mesh_mcp.call_tool("deep_think", bad)
    names = [t["name"] for t in mesh_mcp.TOOLS]
    assert "deep_think" in names
    desc = next(t for t in mesh_mcp.TOOLS if t["name"] == "deep_think")["description"]
    assert "RETURNS AT ONCE" in desc and "do not wait" in desc
    assert json.dumps(desc)  # serialisable
