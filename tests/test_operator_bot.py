"""The project Operator bot: binding, feed, user input, relay authority, poll.

What has to hold:
  - only the bound operator of a project may post, ask, read input, poll or
    dispatch, and it sees only its own project's sessions;
  - posts and asks are idempotent on request_id, and an ask's answer is
    validated against its type (approve/deny, one of the choices, text);
  - the user's input reaches the operator's terminal as a one-line nudge that
    never carries the input, and operator_inbox returns each input once;
  - a dispatch must name the user message (or answered, not denied, ask) it
    relays — the operator cannot originate work;
  - poll returns Observer events after the cursor, and `attention` carries
    open questions and cflow gates whatever the cursor says — of running
    sessions only: a paused one is named apart and a killed one not at all;
  - each session's progress (status-check answers, board issue) reaches the
    poll as a change once, and the panel shows it;
  - the workflow and the role stance say the same thing about the tools.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import operator_mcp
from claude_launcher.cflow import model, state as state_mod
from claude_launcher.daemon import mesh_roles, operator_bot


class FakeSession:
    def __init__(self, name, project="default", exited=False):
        self.sdef = SimpleNamespace(name=name, project=project, cwd="")
        self.exited = exited
        self.sent = []

    async def deliver(self, text):
        self.sent.append(text)
        return True

    def info(self):
        return {"status": "idle"}


class FakeObserver:
    def __init__(self):
        self.rows = {}

    def snapshot(self):
        return {"sessions": [{"name": n, **row} for n, row in self.rows.items()]}


@pytest.fixture
def world(tmp_path):
    sessions = {n: FakeSession(n, p) for n, p in
                [("op", "default"), ("w1", "default"), ("w2", "default"), ("x1", "other")]}
    manager = SimpleNamespace(list=lambda: list(sessions.values()))
    observer = FakeObserver()
    gates = []
    ops = operator_bot.Operators(tmp_path, manager, observer, lambda members: [
        g for g in gates if g["session"] in {m.sdef.name for m in members}])
    ops.bind("default", "op")
    return SimpleNamespace(ops=ops, sessions=sessions, observer=observer, gates=gates, root=tmp_path)


def test_only_the_bound_operator_may_act(world):
    with pytest.raises(web.HTTPForbidden):
        world.ops.post("w1", {"text": "hi", "request_id": "a"})
    with pytest.raises(web.HTTPNotFound):
        world.ops.post("ghost", {"text": "hi", "request_id": "a"})
    assert world.ops.post("op", {"text": "hi", "request_id": "a"})["role"] == "bot"
    # refs are limited to the operator's own project, and never itself
    with pytest.raises(ValueError):
        world.ops.post("op", {"text": "x", "refs": ["x1"], "request_id": "b"})
    with pytest.raises(ValueError):
        world.ops.post("op", {"text": "x", "refs": ["op"], "request_id": "c"})


def test_post_and_ask_are_idempotent_and_survive_restart(world):
    first = world.ops.post("op", {"text": "merged", "level": "attention", "refs": ["w1"], "request_id": "r1"})
    assert world.ops.post("op", {"text": "merged", "level": "attention", "refs": ["w1"], "request_id": "r1"})["id"] == first["id"]
    with pytest.raises(web.HTTPConflict):
        world.ops.post("op", {"text": "other", "request_id": "r1"})
    ask = world.ops.ask("op", {"text": "배포?", "type": "choice", "choices": ["a", "b"], "request_id": "q1"})
    restored = operator_bot.Operators(world.root, world.ops.manager)
    feed = restored.state("default")["feed"]
    assert [e["id"] for e in feed if e["kind"] in ("post", "ask")] == [first["id"], ask["id"]]
    assert restored.pending("default") == 1


def test_ask_validation(world):
    with pytest.raises(ValueError):
        world.ops.ask("op", {"text": "?", "type": "choice", "choices": ["only"], "request_id": "a"})
    with pytest.raises(ValueError):
        world.ops.ask("op", {"text": "?", "type": "approve", "choices": ["a", "b"], "request_id": "b"})
    with pytest.raises(ValueError):
        world.ops.ask("op", {"text": "?", "type": "vote", "request_id": "c"})


def test_answers_are_typed_nudged_once_and_read_once(world):
    op = world.sessions["op"]
    approve = world.ops.ask("op", {"text": "merge?", "type": "approve", "request_id": "a"})
    choice = world.ops.ask("op", {"text": "which?", "type": "choice", "choices": ["x", "y"], "request_id": "b"})
    text = world.ops.ask("op", {"text": "why?", "type": "text", "request_id": "c"})

    async def run():
        with pytest.raises(ValueError):
            await world.ops.answer("default", approve["id"], {"decision": "maybe"})
        with pytest.raises(ValueError):
            await world.ops.answer("default", choice["id"], {"decision": "z"})
        with pytest.raises(ValueError):
            await world.ops.answer("default", text["id"], {})
        await world.ops.answer("default", approve["id"], {"decision": "deny", "text": "not yet"})
        await world.ops.answer("default", approve["id"], {"decision": "deny", "text": "not yet"})
        with pytest.raises(web.HTTPConflict):
            await world.ops.answer("default", approve["id"], {"decision": "approve"})
        await world.ops.answer("default", choice["id"], {"decision": "y"})
        message = await world.ops.message("default", {"text": "w1에게 테스트 다시 돌리라고 해"})
        return message

    message = asyncio.run(run())
    # the terminal gets nudges, never the user's words
    assert op.sent and all(line.startswith("[Operator]") for line in op.sent)
    assert not any("테스트 다시" in line for line in op.sent)
    inbox = world.ops.inbox("op")["messages"]
    assert [m["id"] for m in inbox] == [approve["id"], choice["id"], message["id"]]
    assert inbox[0]["answer"]["decision"] == "deny"
    assert world.ops.inbox("op")["messages"] == []
    assert world.ops.pending("default") == 1  # the text ask is still open


def test_nudge_without_a_running_operator_keeps_the_input(world):
    world.sessions["op"].exited = True
    entry = asyncio.run(world.ops.message("default", {"text": "hello"}))
    assert entry["nudge"] == "no-operator"
    assert [e["id"] for e in world.ops.unread("default")] == [entry["id"]]


def test_dispatch_relays_only_the_users_instruction(world):
    ask = world.ops.ask("op", {"text": "w2 멈출까?", "type": "approve", "request_id": "a"})
    post = world.ops.post("op", {"text": "note", "request_id": "p"})

    async def run():
        user = await world.ops.message("default", {"text": "w1에게 rebase 하라고 전해"})
        with pytest.raises(web.HTTPForbidden):
            await world.ops.dispatch("op", {"target": "w1", "text": "rebase", "on_behalf_of": post["id"]})
        with pytest.raises(web.HTTPForbidden):  # unanswered ask
            await world.ops.dispatch("op", {"target": "w2", "text": "stop", "on_behalf_of": ask["id"]})
        with pytest.raises(ValueError):  # another project's session
            await world.ops.dispatch("op", {"target": "x1", "text": "rebase", "on_behalf_of": user["id"]})
        with pytest.raises(ValueError):  # itself
            await world.ops.dispatch("op", {"target": "op", "text": "rebase", "on_behalf_of": user["id"]})
        with pytest.raises(web.HTTPForbidden):
            await world.ops.dispatch("w1", {"target": "w2", "text": "rebase", "on_behalf_of": user["id"]})
        sent = await world.ops.dispatch("op", {"target": "w1", "text": "rebase onto master", "on_behalf_of": user["id"]})
        await world.ops.answer("default", ask["id"], {"decision": "deny"})
        with pytest.raises(web.HTTPForbidden):  # a denied ask carries nothing
            await world.ops.dispatch("op", {"target": "w2", "text": "stop", "on_behalf_of": ask["id"]})
        return user, sent

    user, sent = asyncio.run(run())
    assert sent["delivery"] == "sent" and sent["on_behalf_of"] == user["id"]
    assert world.sessions["w1"].sent == ["[Operator relay — 사용자 지시, project default] rebase onto master"]
    assert world.sessions["w2"].sent == [] and world.sessions["x1"].sent == []


def test_poll_cursor_attention_and_project_scope(world):
    world.observer.rows = {
        "w1": {"events": [
            {"id": "e1", "at": "2026-09-23T01:00:00+00:00", "kind": "commit", "text": "commit abc"},
            {"id": "e2", "at": "2026-09-23T01:05:00+00:00", "kind": "action", "text": "choose env",
             "question": True, "answer": None, "choices": ["a", "b"], "needs_action": True}]},
        "x1": {"events": [{"id": "e9", "at": "2026-09-23T01:06:00+00:00", "kind": "merge", "text": "other project"}]},
        "w2": {"events": [], "state": "blocked", "summary": "stuck on lock"},
    }
    world.gates.append({"session": "w2", "status": "waiting_approval", "step_id": "end-gate"})
    world.gates.append({"session": "x1", "status": "waiting_approval", "step_id": "end-gate"})
    first = asyncio.run(world.ops.poll("op"))
    assert [e["text"] for e in first["events"]] == ["commit abc", "choose env"]
    assert first["cursor"] == "2026-09-23T01:05:00+00:00"
    kinds = sorted((a["kind"], a["session"]) for a in first["attention"])
    assert kinds == [("blocked", "w2"), ("cflow_gate", "w2"), ("observer_ask", "w1")]
    again = asyncio.run(world.ops.poll("op", first["cursor"]))
    assert again["events"] == []
    # the open question is still waiting, whatever the cursor
    assert ("observer_ask", "w1") in {(a["kind"], a["session"]) for a in again["attention"]}
    assert {s["name"] for s in again["sessions"]} == {"w1", "w2"}


def test_paused_and_exited_sessions_raise_no_attention(world):
    # w1 is paused (exited + paused_at), w2 was killed: their open questions
    # and blocked state wait on nobody, so nothing of theirs is attention
    world.sessions["w1"].exited = True
    world.sessions["w1"].paused_at = "2026-09-23T01:00:00+00:00"
    world.sessions["w2"].exited = True
    ask = {"at": "2026-09-23T01:05:00+00:00", "text": "which?", "question": True, "answer": None,
           "needs_action": True}
    world.observer.rows = {"w1": {"events": [dict(ask, id="q1")], "state": "blocked"},
                           "w2": {"events": [dict(ask, id="q2")]}}
    first = asyncio.run(world.ops.poll("op"))
    assert first["attention"] == []
    assert first["paused"] == [{"session": "w1", "paused_at": "2026-09-23T01:00:00+00:00"}]
    # the events still arrive, marked with their category and never as needing action
    assert {(e["session"], e["category"], e["needs_action"]) for e in first["events"]} == {
        ("w1", "paused", False), ("w2", "killed", False)}
    again = asyncio.run(world.ops.poll("op", first["cursor"]))
    assert again["attention"] == []
    # the panel lists the paused one apart, with no questions counted; the killed one not at all
    rows = world.ops.view("default")["sessions"]
    assert [(r["name"], r["category"], r["questions"]) for r in rows] == [("w1", "paused", 0)]


def test_poll_reports_progress_changes_once(world):
    progress = {"w1": {"checks": [{"name": "C", "question": "커밋 되었는가?", "answer": "no"}],
                       "issue": {"id": "cl-1", "status": "in_progress", "title": "t"}}}

    async def work(sessions):
        return {s.sdef.name: progress[s.sdef.name] for s in sessions if s.sdef.name in progress}

    world.ops.work = work
    first = asyncio.run(world.ops.poll("op"))
    assert first["progress"] == []  # a first reading is history
    assert {s["name"]: s["issue"] for s in first["sessions"]}["w1"]["status"] == "in_progress"
    progress["w1"] = {"checks": [{"name": "C", "question": "커밋 되었는가?", "answer": "yes"}],
                      "issue": {"id": "cl-1", "status": "in_review", "title": "t"}}
    second = asyncio.run(world.ops.poll("op"))
    assert [(p["session"], p["changes"]) for p in second["progress"]] == [
        ("w1", ["커밋 되었는가? → yes", "이슈 cl-1: 머지 요청"])]
    assert asyncio.run(world.ops.poll("op"))["progress"] == []  # said once
    # the seen state survives a restart, so a restart does not repeat it
    restored = operator_bot.Operators(world.root, world.ops.manager, world.observer, None, work)
    assert asyncio.run(restored.poll("op"))["progress"] == []


def test_view_panel_skips_exited_and_counts_questions(world):
    world.sessions["w2"].exited = True
    world.observer.rows = {"w1": {"events": [{"question": True, "answer": None, "at": "x"}], "summary": "s"}}
    view = world.ops.view("default")
    assert view["operator"]["name"] == "op"
    assert [r["name"] for r in view["sessions"]] == ["w1"]
    assert view["sessions"][0]["questions"] == 1


def _app(world, created):
    app = web.Application()
    app["manager"] = world.ops.manager
    app["observer"] = world.observer

    class Mesh:
        made = []

        def get(self, name):
            if name not in self.made:
                raise KeyError(name)

        def create(self, name, project=""):
            self.made.append(name)

    app["mesh"] = Mesh()

    async def create(request, body):
        created.append(body)
        world.sessions["op2"] = FakeSession("op2", body["project"])
        return 201, {"name": "op2"}

    operator_bot.install(app, create=create, gates=None)
    app["operators"].root = world.root / "operator"
    return app


def test_start_route_binds_one_operator_per_project(world, monkeypatch):
    monkeypatch.setattr(operator_bot.paths, "daemon_dir", lambda: world.root)
    monkeypatch.setattr(operator_bot.projects, "require", lambda name: SimpleNamespace(name=name))
    created = []

    async def run():
        client = TestClient(TestServer(_app(world, created)))
        await client.start_server()
        try:
            # "default" already has a running operator in this fixture's store
            resp = await client.post("/api/operator/start", json={"project": "default", "profile": "p"})
            assert resp.status == 409
            resp = await client.post("/api/operator/start", json={"project": "other"})
            assert resp.status == 400  # profile required
            resp = await client.post("/api/operator/start",
                                     json={"project": "other", "profile": "p:claude", "model": " opus ", "effort": ""})
            assert resp.status == 201
            view = await (await client.get("/api/operator?project=other")).json()
            assert view["operator"]["name"] == "op2"
            # the view names the bot's harness and model, for the header
            assert {"harness", "model"} <= set(view["operator"])
            resp = await client.post("/api/operator/message?project=other", json={"text": "hi"})
            assert resp.status == 200
            resp = await client.post("/api/operator/agent/w1/post", json={"text": "x", "request_id": "a"})
            assert resp.status == 403
        finally:
            await client.close()

    asyncio.run(run())
    assert created[0]["role"] == "operator" and created[0]["workflow"] == "operator"
    assert created[0]["mesh"] == "operator-other" and created[0]["beads"] is False
    # the picked model travels to the create path; an empty effort is the
    # harness default and is not sent
    assert created[0]["profile"] == "p:claude" and created[0]["model"] == "opus"
    assert "effort" not in created[0]


def test_mcp_tools_and_workflow_and_stance_agree():
    names = {t["name"] for t in operator_mcp.TOOLS}
    assert names == {"operator_poll", "operator_post", "operator_ask", "operator_inbox", "operator_dispatch"}
    wf = model.load(dict(state_mod.bundled_workflows())["operator"])
    assert wf.recur_auto and not wf.warnings and not wf.deprecations
    assert wf.steps["watch"].next == "wait"
    assert wf.steps["wait"].timer.then == "watch" and wf.steps["wait"].timer.after == "end"
    watch = " ".join(wf.steps["watch"].instructions.split())
    stance = mesh_roles.resolve(None).roles["operator"].stance
    for tool in names:
        assert tool in watch, tool
        assert tool in stance, tool
    assert "on_behalf_of" in watch


def test_work_reads_checks_and_the_session_s_open_issue():
    from claude_launcher.daemon import api

    class Board:
        def available(self):
            return True

        async def root_for(self, cwd):
            return cwd

        def has_board(self, root):
            return True

        async def issues(self, root):
            return [
                # the stale link: the round it named is closed
                {"id": "cl-old", "status": "closed", "assignee": "w1", "title": "old"},
                {"id": "cl-new", "status": "in_review", "assignee": "w1", "title": "new"},
                {"id": "cl-x", "status": "in_progress", "assignee": "w9", "title": "other"},
            ]

    session = FakeSession("w1")
    session.sdef.cwd, session.sdef.issue, session.sdef.task = "/repo", "cl-old", ""
    session.info = lambda: {"status_checks": [{"name": "C", "question": "커밋?", "answer": "yes"}]}
    out = asyncio.run(api._operator_work({"beads": Board()}, [session]))
    assert out["w1"]["checks"] == [{"name": "C", "question": "커밋?", "answer": "yes"}]
    assert out["w1"]["issue"] == {"id": "cl-new", "status": "in_review", "title": "new"}
    # no board: the checks still arrive, the issue is unknown
    out = asyncio.run(api._operator_work({}, [session]))
    assert out["w1"]["issue"] is None and out["w1"]["checks"]
