"""flow_nudge: a driver raising its run on the dashboard (claunch-g08xz).

A run stopped on something only a person releases (a ``chooser: user``
choice, a human ask, a move request, a limit, a halt, a v1 run's
waiting_selection) used to wait for as long as nobody looked at the driver's
terminal. ``flow_nudge`` files a request the dashboard shows as a card; the
daemon reads what is owed from the run's own files, refuses a nudge for a
run that owes nothing, keeps one card per run, and settles it once the
person acts.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher import mcp_server
from claude_launcher.cflow import engine, state as state_mod
from claude_launcher.cflow2 import layers, mcp as cflow2_mcp, runtime
from claude_launcher.daemon import api as api_mod, cflow2_web, flow_nudge
from claude_launcher.daemon.manager import SessionManager

PICK = """\
cflow: 2
name: pick
nodes:
  a: {title: Step A, prompt: do a}
body:
  seq:
    - id: end-decision
      choose:
        prompt: which way?
        chooser: user
        options:
          left: {then: a}
          right: {then: end}
"""

V1_USER_BRANCH = """
steps:
  landing:
    select:
      prompt: integrate now?
      chooser: user
      options:
        request: {description: "integrate now", next: after}
        hold:    {description: "later", next: after}
  after:
    instructions: continue
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows" / "v2").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "v2" / "pick.yaml").write_text(PICK, encoding="utf-8")
    (d / ".claunch" / "workflows" / "branchy.yaml").write_text(V1_USER_BRANCH,
                                                               encoding="utf-8")
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(cflow2_web, "_RAIL", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(d)
    return d


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _board(owed_by, clock=None, alive=None):
    return flow_nudge.NudgeBoard(read_owed=lambda cwd, scope: owed_by.get((cwd, scope)),
                                 clock=clock or Clock(), alive=alive)


def _owing(*items, run="r1"):
    return {"version": 2, "run": run, "workflow": "w", "items": list(items)}


# --------------------------------------------------------------------------- #
# the board
# --------------------------------------------------------------------------- #

def test_a_run_that_owes_nobody_is_not_nudged():
    board = _board({("/p", "s1"): _owing()})
    with pytest.raises(flow_nudge.NudgeRefused, match="waits on no person"):
        board.submit(session="s1", cwd="/p", scope="s1", message="pick please")
    with pytest.raises(flow_nudge.NudgeRefused, match="no cflow run"):
        board.submit(session="s1", cwd="/q", scope="s1", message="pick please")
    with pytest.raises(flow_nudge.NudgeRefused) as exc:
        _board({("/p", "s1"): _owing("choose: x")}).submit(
            session="s1", cwd="/p", scope="s1", message="  ")
    assert exc.value.status == 400
    assert board.list() == []


def test_one_card_per_run_until_the_window_passes_or_the_wait_changes():
    clock = Clock()
    owed_by = {("/p", "s1"): _owing("choose: end-decision")}
    board = _board(owed_by, clock)
    first = board.submit(session="s1", cwd="/p", scope="s1", message="take the new task")
    assert first["raised"] is True
    assert first["request"]["items"] == ["choose: end-decision"]

    clock.now += 60
    again = board.submit(session="s1", cwd="/p", scope="s1", message="still waiting")
    assert again["raised"] is False and again["request"]["id"] == first["request"]["id"]
    assert [r["message"] for r in board.list()] == ["still waiting"]

    clock.now += flow_nudge.REPEAT_AFTER
    later = board.submit(session="s1", cwd="/p", scope="s1", message="still waiting")
    assert later["raised"] is True and later["request"]["id"] != first["request"]["id"]

    owed_by[("/p", "s1")] = _owing("move: round")
    moved = board.submit(session="s1", cwd="/p", scope="s1", message="approve the move")
    assert moved["raised"] is True
    statuses = {r["id"]: r["status"] for r in board.list()}
    assert statuses == {first["request"]["id"]: flow_nudge.SUPERSEDED,
                        later["request"]["id"]: flow_nudge.SUPERSEDED,
                        moved["request"]["id"]: flow_nudge.PENDING}


def test_a_card_settles_when_the_person_acts_or_the_session_ends():
    owed_by = {("/p", "s1"): _owing("choose: end-decision"),
               ("/p", "s2"): _owing("approval: land")}
    alive = {"s1": True, "s2": True}
    board = _board(owed_by, alive=lambda name: alive[name])
    one = board.submit(session="s1", cwd="/p", scope="s1", message="m")["request"]["id"]
    two = board.submit(session="s2", cwd="/p", scope="s2", message="m")["request"]["id"]
    assert {r["status"] for r in board.list()} == {flow_nudge.PENDING}

    owed_by[("/p", "s1")] = _owing()           # the person picked
    alive["s2"] = False                        # the driver went away
    statuses = {r["id"]: r["status"] for r in board.list()}
    assert statuses == {one: flow_nudge.SETTLED, two: flow_nudge.ENDED}


def test_a_new_run_in_the_slot_settles_the_old_card():
    owed_by = {("/p", "s1"): _owing("choose: x", run="r1")}
    board = _board(owed_by)
    board.submit(session="s1", cwd="/p", scope="s1", message="m")
    owed_by[("/p", "s1")] = _owing("choose: x", run="r2")
    assert [r["status"] for r in board.list()] == [flow_nudge.SETTLED]


# --------------------------------------------------------------------------- #
# what a run owes, read from its files
# --------------------------------------------------------------------------- #

def test_a_v2_user_choice_is_owed_until_the_person_picks(proj):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})
    found = flow_nudge.owed(cwd, "s1")
    assert found["version"] == 2 and found["items"] == ["choose: end-decision"]

    board = flow_nudge.NudgeBoard()
    rid = board.submit(session="s1", cwd=cwd, scope="s1",
                       message="end-decision: take the new task in")["request"]["id"]
    runtime.select(None, "right", caller=runtime.CALLER_USER, cwd=cwd, scope="s1")
    cflow2_web._RAIL.clear()
    assert flow_nudge.owed(cwd, "s1")["items"] == []
    assert [(r["id"], r["status"]) for r in board.list()] == [(rid, flow_nudge.SETTLED)]


def test_a_v1_user_selection_is_owed(proj):
    cwd = str(proj)
    engine.start("branchy")
    found = flow_nudge.owed(cwd, state_mod.DEFAULT_SCOPE)
    assert found["version"] == 1
    assert len(found["items"]) == 1 and found["items"][0].startswith("selection: ")
    assert flow_nudge.owed(cwd, "nobody") is None


# --------------------------------------------------------------------------- #
# where the agent learns of it
# --------------------------------------------------------------------------- #

def test_the_refused_self_pick_and_the_status_point_at_flow_nudge(proj):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s2", caps={})
    with pytest.raises(runtime.FlowError, match="flow_nudge"):
        runtime.select(None, "left", cwd=cwd, scope="s2")
    status = runtime.status(cwd=cwd, scope="s2")
    assert "flow_nudge" in status["next"]
    assert "pick with flow_select" not in status["next"]


def test_a_v1_gate_brief_points_at_flow_nudge(proj):
    engine.start("branchy")
    payload = engine.select("request", "green", by="agent")
    assert "flow_nudge" in payload["how_to_unblock"]


# --------------------------------------------------------------------------- #
# the MCP tool and the daemon routes
# --------------------------------------------------------------------------- #

class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.posts = []

    def post(self, path, body=None, **kw):
        self.posts.append((path, body))
        return self.answer


def test_the_tool_posts_this_sessions_own_run(proj, monkeypatch):
    from claude_launcher import daemon_client

    assert "flow_nudge" in {t["name"] for t in mcp_server.TOOLS}
    client = FakeClient({"raised": True, "note": "raised",
                         "request": {"id": "nudge-1", "items": ["choose: x"]}})
    monkeypatch.setattr(daemon_client, "connect_with_diagnosis", lambda **kw: (client, {}))
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    out = cflow2_mcp.call_tool("flow_nudge", {"message": "pick right"})
    assert out == {"raised": True, "request": "nudge-1", "owed": ["choose: x"],
                   "note": "raised"}
    path, body = client.posts[0]
    assert path == "/api/flow-nudges"
    assert body == {"session": "s7", "cwd": str(proj), "scope": "s7", "message": "pick right"}

    cflow2_mcp.call_tool("flow_nudge", {"message": "m", "side": "stack"})
    assert client.posts[1][1]["scope"].startswith("s7") \
        and client.posts[1][1]["scope"].endswith("+stack")

    with pytest.raises(runtime.FlowError, match="message"):
        cflow2_mcp.call_tool("flow_nudge", {"message": ""})
    monkeypatch.delenv("CLAUNCH_SESSION")
    with pytest.raises(runtime.FlowError, match="CLAUNCH_SESSION"):
        cflow2_mcp.call_tool("flow_nudge", {"message": "m"})


def test_the_routes(proj):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        app["flow_nudge"] = _board({("/p", "s1"): _owing("choose: x")})
        app["flow_nudge"].submit(session="s1", cwd="/p", scope="s1", message="m")
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            unknown = await client.post("/api/flow-nudges", headers=bearer,
                                        json={"session": "ghost", "message": "m"})
            unauth = await client.get("/api/flow-nudges")
            listed = await (await client.get("/api/flow-nudges", headers=bearer)).json()
            return unknown.status, unauth.status, listed
        finally:
            await client.close()

    unknown, unauth, listed = asyncio.run(run())
    assert unknown == 404 and unauth == 401
    assert [(r["session"], r["items"], r["status"]) for r in listed["requests"]] == \
        [("s1", ["choose: x"], "pending")]


# --------------------------------------------------------------------------- #
# a kill of a session standing on its end decision (claunch-g08xz.1)
# --------------------------------------------------------------------------- #

ENDING = """\
cflow: 2
name: ending
nodes:
  a: {title: Step A, prompt: do a}
body:
  seq:
    - id: end-decision
      choose:
        prompt: end this session?
        chooser: user
        options:
          end: {then: end}
          hold: {then: a}
"""


class FakeRequest:
    def __init__(self, app, body):
        self.app = app
        self._body = body

    async def json(self):
        return self._body


def _target(name, cwd):
    from types import SimpleNamespace

    return SimpleNamespace(exited=False, sdef=SimpleNamespace(name=name, cwd=cwd),
                           info=lambda: {"name": name})


@pytest.fixture
def ending(proj):
    (proj / ".claunch" / "workflows" / "v2" / "ending.yaml").write_text(ENDING,
                                                                      encoding="utf-8")
    return proj


def test_the_end_decision_is_found_in_v2_and_v1_runs(ending):
    cwd = str(ending)
    runtime.start(layers.load("ending", cwd), cwd=cwd, scope="w1", caps={})
    found = flow_nudge.end_decision(cwd, "w1")
    assert found["version"] == 2 and found["label"] == "end-decision"
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="w2", caps={})
    assert flow_nudge.end_decision(cwd, "w2") is None   # no 'end' option
    assert flow_nudge.end_decision(cwd, "nobody") is None


def test_a_kill_of_a_session_on_its_end_decision_asks_for_end(ending):
    import json

    cwd = str(ending)
    runtime.start(layers.load("ending", cwd), cwd=cwd, scope="w1", caps={})
    app = {"flow_nudge": flow_nudge.NudgeBoard()}
    by = {"kind": "agent", "name": "lead"}

    resp = asyncio.run(api_mod._end_instead_of_kill(
        FakeRequest(app, {"reason": "its PR merged"}), _target("w1", cwd), by,
        "its PR merged"))
    assert resp.status == 202
    body = json.loads(resp.text)
    assert body["end_requested"] is True and body["kill_request"] is None
    assert body["decision"] == "end-decision" and "skip_end_decision" in body["note"]
    [rec] = app["flow_nudge"].list()
    assert rec["session"] == "w1" and rec["by"] == "lead"
    assert "its PR merged" in rec["message"] and "'end'" in rec["message"]

    skipped = asyncio.run(api_mod._end_instead_of_kill(
        FakeRequest(app, {"skip_end_decision": True}), _target("w1", cwd), by, ""))
    assert skipped is None
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="w2", caps={})
    elsewhere = asyncio.run(api_mod._end_instead_of_kill(
        FakeRequest(app, {}), _target("w2", cwd), by, ""))
    assert elsewhere is None


def test_the_kill_tool_passes_skip_end_decision(monkeypatch):
    from claude_launcher import mesh_mcp

    client = FakeClient({"pending": True})
    monkeypatch.setattr(mesh_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")
    tool = next(t for t in mesh_mcp.TOOLS if t["name"] == "kill")
    assert "skip_end_decision" in tool["inputSchema"]["properties"]
    mesh_mcp.call_tool("kill", {"session": "kid", "reason": "done",
                                "skip_end_decision": True})
    assert client.posts[-1] == ("/api/sessions/lead/children/kid/kill",
                                {"reason": "done", "skip_end_decision": True})


class FakeGate:
    def __init__(self):
        self.filed = []

    def submit(self, **kw):
        self.filed.append(kw)
        return {"id": "kr-1", "target": kw["target"]}


def test_a_forced_kill_files_the_kill_request_even_on_the_end_decision(ending):
    # claunch-g08xz.2: force skips the end decision as it skips the wind-down.
    import json

    cwd = str(ending)
    runtime.start(layers.load("ending", cwd), cwd=cwd, scope="w1", caps={})
    app = {"flow_nudge": flow_nudge.NudgeBoard(), "kill_gate": FakeGate()}
    by = {"kind": "agent", "name": "lead"}

    forced = asyncio.run(api_mod._gated_kill(
        FakeRequest(app, {}), _target("w1", cwd), by, force=True, route="kill",
        reason="stuck"))
    body = json.loads(forced.text)
    assert forced.status == 202 and body["kill_request"] == "kr-1"
    assert "end_requested" not in body and app["flow_nudge"].list() == []
    assert app["kill_gate"].filed[0]["force"] is True

    plain = asyncio.run(api_mod._gated_kill(
        FakeRequest(app, {}), _target("w1", cwd), by, force=False, route="kill"))
    assert json.loads(plain.text)["end_requested"] is True
    assert len(app["kill_gate"].filed) == 1


def _kill_session(monkeypatch, answer, *argv):
    import argparse

    from claude_launcher import cli_sessions, daemon_client

    client = FakeClient(answer)
    monkeypatch.setattr(daemon_client, "ensure_running", lambda: client)
    parser = argparse.ArgumentParser()
    cli_sessions.register(parser.add_subparsers())
    args = parser.parse_args(["kill-session", "w1", *argv])
    assert cli_sessions._cmd_kill_session(args) == 0
    return client


def test_kill_session_cli_skips_the_end_decision_and_reports_the_end_request(
        monkeypatch, capsys):
    client = _kill_session(monkeypatch, {"pending": True}, "--skip-end-decision")
    assert client.posts[-1] == ("/api/sessions/w1/kill",
                                {"reason": "", "skip_end_decision": True})
    client = _kill_session(monkeypatch, {"pending": True})
    assert client.posts[-1] == ("/api/sessions/w1/kill", {"reason": ""})
    capsys.readouterr()

    _kill_session(monkeypatch, {"pending": True, "kill_request": None,
                                "end_requested": True, "decision": "end-decision",
                                "nudge": "nudge-7"})
    out = capsys.readouterr().out
    assert "end-decision" in out and "nudge-7" in out and "None" not in out
    assert "--skip-end-decision" in out
