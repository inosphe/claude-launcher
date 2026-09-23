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
  - the workflow and the role stance say the same thing about the tools;
  - the observation mode switches while the operator runs (feed entry, one
    nudge), and in transcript mode operator_transcripts hands the bot each
    running session's new conversation records from a daemon-held cursor,
    starting from the last few, clipped and bounded.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import operator_mcp
from claude_launcher.cflow import model, state as state_mod
from claude_launcher.daemon import mesh_roles, operator_bot, operator_transcript, transcript_view


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
        self.asked = []

    def snapshot(self, names=None):
        self.asked.append(None if names is None else sorted(names))
        return {"sessions": [{"name": n, **row} for n, row in self.rows.items()
                             if names is None or n in names]}


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
    # the paused one's events still arrive, marked and never as needing
    # action; the killed one's history is not read at all
    assert {(e["session"], e["category"], e["needs_action"]) for e in first["events"]} == {
        ("w1", "paused", False)}
    assert {s["name"] for s in first["sessions"]} == {"w1"}
    assert world.observer.asked and all(a == ["w1"] for a in world.observer.asked)
    again = asyncio.run(world.ops.poll("op", first["cursor"]))
    assert again["attention"] == []
    # the panel lists the paused one apart, with no questions counted; the killed one not at all
    rows = world.ops.view("default")["sessions"]
    assert [(r["name"], r["category"], r["questions"]) for r in rows] == [("w1", "paused", 0)]


def test_killed_and_archived_sessions_stay_out_of_the_poll_and_end_once(world):
    # w2 is running at first and then killed: its end is named once, and from
    # then on neither its events nor its row reach the poll or the snapshot
    world.observer.rows = {"w2": {"events": [
        {"id": "e1", "at": "2026-09-23T01:00:00+00:00", "kind": "commit", "text": "commit abc"}]}}
    first = asyncio.run(world.ops.poll("op"))
    assert first["ended"] == [] and {s["name"] for s in first["sessions"]} == {"w1", "w2"}
    world.sessions["w2"].exited = True
    world.observer.rows["w2"]["events"].append(
        {"id": "e2", "at": "2026-09-23T01:10:00+00:00", "kind": "exit", "text": "gone"})
    world.observer.asked.clear()
    second = asyncio.run(world.ops.poll("op", first["cursor"]))
    assert second["ended"] == ["w2"]
    assert second["events"] == [] and {s["name"] for s in second["sessions"]} == {"w1"}
    assert world.observer.asked == [["w1"]]
    assert asyncio.run(world.ops.poll("op", first["cursor"]))["ended"] == []  # said once
    # an archived session is out the same way
    world.sessions["w1"].exited = True
    world.sessions["w1"].archived_at = "2026-09-23T02:00:00+00:00"
    third = asyncio.run(world.ops.poll("op"))
    assert third["ended"] == ["w1"] and third["sessions"] == []


def test_a_first_poll_starts_at_the_latest_events(world):
    many = [{"id": f"e{i}", "at": f"2026-09-23T{i // 60:02d}:{i % 60:02d}:00+00:00", "kind": "action",
             "text": f"event {i}"} for i in range(operator_bot.POLL_LIMIT + 20)]
    world.observer.rows = {"w1": {"events": many}}
    first = asyncio.run(world.ops.poll("op"))
    assert first["more"] is False
    assert [e["text"] for e in first["events"]] == [e["text"] for e in many[-operator_bot.POLL_LIMIT:]]
    assert first["cursor"] == many[-1]["at"]
    # with a cursor, paging forward is unchanged
    paged = asyncio.run(world.ops.poll("op", many[0]["at"]))
    assert paged["more"] is True and paged["events"][0]["text"] == "event 1"


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


def test_reply_to_threads_a_post_under_its_card(world):
    card = world.ops.post("op", {"text": "w1 gate", "refs": ["w1"], "request_id": "c1"})
    reply = world.ops.post("op", {"text": "cleared", "reply_to": card["id"], "request_id": "c2"})
    assert reply["parent"] == card["id"]
    # a reply to a reply joins the same card: a thread is one level deep
    again = world.ops.ask("op", {"text": "land?", "type": "approve", "reply_to": reply["id"], "request_id": "c3"})
    assert again["parent"] == card["id"]
    with pytest.raises(ValueError):
        world.ops.post("op", {"text": "x", "reply_to": "nope", "request_id": "c4"})
    world.ops.state("default")["feed"].append({"id": "u1", "kind": "user", "text": "hi"})
    with pytest.raises(ValueError):
        world.ops.post("op", {"text": "x", "reply_to": "u1", "request_id": "c5"})


def test_track_threads_what_moved_by_another_path(world):
    progress = {"w1": {"checks": [{"name": "C", "question": "커밋 되었는가?", "answer": "no"}],
                       "issue": {"id": "cl-1", "status": "in_progress", "title": "t"}}}

    async def work(sessions):
        return {s.sdef.name: progress[s.sdef.name] for s in sessions if s.sdef.name in progress}

    world.ops.work = work
    world.gates.append({"session": "w1", "step_id": "commit"})
    card = world.ops.post("op", {"text": "w1 waits at commit", "level": "urgent", "refs": ["w1", "w2"],
                                 "request_id": "c1"})
    world.ops.post("op", {"text": "no sessions named", "request_id": "c2"})
    assert asyncio.run(world.ops.track("default")) == []  # the baseline says nothing
    assert asyncio.run(world.ops.track("default")) == []  # throttled, and nothing moved anyway
    # w1: the gate was approved elsewhere, committed, landing requested; w2 paused
    world.gates.clear()
    progress["w1"] = {"checks": [{"name": "C", "question": "커밋 되었는가?", "answer": "yes"}],
                      "issue": {"id": "cl-1", "status": "in_review", "title": "t"}}
    world.sessions["w2"].exited = True
    world.sessions["w2"].paused_at = "2026-09-23T01:00:00+00:00"
    added = asyncio.run(world.ops.track("default", force=True))
    assert [(e["kind"], e["parent"], e["session"], e["text"].split("\n")) for e in added] == [
        ("update", card["id"], "w1", ["게이트 commit 해소", "커밋 되었는가? → yes", "이슈 cl-1: 머지 요청"]),
        ("update", card["id"], "w2", ["상태: 실행 중 → 일시정지"])]
    # in the feed, in time order, after the card
    feed = world.ops.state("default")["feed"]
    assert [e["id"] for e in feed][-2:] == [e["id"] for e in added]
    assert asyncio.run(world.ops.track("default", force=True)) == []  # said once
    # a board that cannot be read is not a change, and the baseline survives a restart
    world.ops.work = None
    assert asyncio.run(world.ops.track("default", force=True)) == []
    restored = operator_bot.Operators(world.root, world.ops.manager, world.observer,
                                      lambda members: [], work)
    progress["w1"]["issue"] = {"id": "cl-1", "status": "closed", "title": "t"}
    assert [e["text"] for e in asyncio.run(restored.track("default"))] == ["이슈 cl-1: 닫힘"]


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
    assert names == {"operator_poll", "operator_post", "operator_ask", "operator_inbox", "operator_dispatch",
                     "operator_transcripts"}
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


def test_poll_answers_without_a_board_or_gates_that_do_not_answer_in_time(world, monkeypatch):
    # 2026-09-23: polls waited behind the board lock past the client's 30s
    # timeout while the restarted daemon swept exited sessions' issues
    monkeypatch.setattr(operator_bot, "READ_TIMEOUT", 0.05)

    async def slow_work(sessions):
        await asyncio.sleep(5)

    def slow_gates(sessions):
        import time
        time.sleep(0.3)
        return [{"session": "w1", "step_id": "late"}]

    world.ops.work, world.ops.gates = slow_work, slow_gates
    out = asyncio.run(world.ops.poll("op"))
    assert out["degraded"] == ["gates", "board"]
    assert not [a for a in out["attention"] if a["kind"] == "cflow_gate"]
    world.ops.work, world.ops.gates = None, lambda members: []
    assert asyncio.run(world.ops.poll("op"))["degraded"] == []


def test_a_restart_is_written_once_per_boot_and_the_next_poll_carries_it(world):
    previous = {"pid": 7, "started_at": "2026-09-23T12:37:52+00:00"}
    current = {"pid": 8, "started_at": "2026-09-23T13:52:08+00:00", "requested": True,
               "requested_by": ["s469"], "requested_at": "2026-09-23T13:51:29+00:00", "requested_via": "cli"}
    (entry,) = world.ops.announce_boot(current, previous)
    assert entry["kind"] == "system" and entry["event"] == "daemon_restart"
    assert "데몬이 재시작되었습니다" in entry["text"] and "pid 7" in entry["text"] and "s469" in entry["text"]
    assert entry["boot"]["previous_started_at"] == previous["started_at"]
    assert world.ops.announce_boot(current, previous) == []  # once per boot
    # a project without an operator gets nothing
    world.ops.state("other")
    world.ops.save("other")
    assert world.ops.announce_boot({**current, "started_at": "2026-09-23T14:00:00+00:00"}, current)[0]["id"] != entry["id"]
    assert all(e.get("event") != "daemon_restart" for e in world.ops.state("other")["feed"])
    first = asyncio.run(world.ops.poll("op"))
    assert first["restart"]["event"] == "daemon_restart"
    assert asyncio.run(world.ops.poll("op", first["cursor"]))["restart"] is None
    # a boot nothing asked for says so, and guesses no cause
    text = operator_bot.restart_text({"started_at": "2026-09-23T15:00:00+00:00"}, current)
    assert "재시작 요청 기록 없음" in text and "원인 미상" in text


def test_watch_boot_waits_for_this_boot_in_the_ledger(world, monkeypatch):
    from claude_launcher.daemon import restart_notice, runtime_state
    ledger = {"boots": [{"started_at": "a"}]}
    monkeypatch.setattr(runtime_state, "read_daemon_json", lambda: {"started_at": "b"})
    monkeypatch.setattr(restart_notice, "read_ledger", lambda: ledger)

    async def later():
        await asyncio.sleep(0.02)
        ledger["boots"].append({"started_at": "b", "requested": False})

    async def run():
        task = asyncio.create_task(later())
        out = await world.ops.watch_boot(attempts=50, delay=0.01)
        await task
        return out

    (entry,) = asyncio.run(run())
    assert entry["boot"] == {"started_at": "b", "previous_started_at": "a", "requested": False,
                             "requested_by": [], "requested_at": None}


def test_a_restart_request_leaves_when_and_how_it_was_asked_in_the_boot_record():
    from claude_launcher.daemon import restart_notice
    restart_notice.record_request(via="cli", session="s469", daemon={"pid": 1, "started_at": "x"})
    restart_notice.note_boot(pid=2, started_at="y")
    boot = restart_notice.read_ledger()["boots"][-1]
    assert boot["requested"] and boot["requested_at"] and boot["requested_via"] == "cli"


def test_mcp_poll_reports_the_recovery_after_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "op")
    monkeypatch.setenv("CLAUNCH_SCRATCH", str(tmp_path))
    from claude_launcher import daemon_client
    up = {"ok": False}

    class Client:
        def get(self, path):
            return {"cursor": "c"}

    monkeypatch.setattr(daemon_client, "connect_with_diagnosis",
                        lambda **kw: (Client(), {}) if up["ok"] else (None, {}))
    monkeypatch.setattr(daemon_client, "unreachable_reason", lambda why: "timed out")
    for _ in range(2):
        with pytest.raises(operator_mcp.OperatorMcpError):
            operator_mcp.call_tool("operator_poll", {"since": "s"})
    up["ok"] = True
    out = operator_mcp.call_tool("operator_poll", {"since": "s"})
    assert out["cursor"] == "c"
    assert out["recovered"]["failures"] == 2 and out["recovered"]["last_error"] == "timed out"
    assert out["recovered"]["first_failed_at"] <= out["recovered"]["recovered_at"]
    assert "recovered" not in operator_mcp.call_tool("operator_poll", {})  # said once


def test_only_gates_a_person_settles_reach_the_operator():
    from claude_launcher.daemon import api
    kind = api._operator_gate_kind
    assert kind({"status": "waiting_approval"}) == "approval"
    assert kind({"status": "waiting_goto"}) == "goto"
    assert kind({"status": "waiting_selection"}) == "selection"
    # a timer, a checklist, a window and a question still with an agent wait on no one here
    for status in ("waiting_timer", "waiting_checklist", "waiting_window", "step", "select"):
        assert kind({"status": status}) == "", status
    assert kind({"status": "waiting_answer", "ask": {"asked": [{"handle": "s1"}]}}) == ""
    # one that reached nobody is the user's: a branch by its options, else an approval
    assert kind({"status": "waiting_answer", "ask": {"asked": [], "kind": "branch",
                                                     "options": [{"name": "pass"}]}}) == "selection"
    assert kind({"status": "waiting_answer", "ask": {"asked": []}}) == "approval"
    assert api._operator_gate_options({"options": [{"name": "a", "description": "x"}, {"bad": 1}]}) == [
        {"name": "a", "description": "x"}]


def _conversation(path, turns):
    with path.open("a", encoding="utf-8") as fh:
        for role, content in turns:
            fh.write(json.dumps({"type": role, "timestamp": "2026-09-23T00:00:00Z",
                                 "message": {"role": role, "content": content}}) + "\n")


@pytest.fixture
def talk(world, tmp_path, monkeypatch):
    files = {n: tmp_path / f"{n}.jsonl" for n in ("w1", "w2")}
    for f in files.values():
        f.touch()
    monkeypatch.setattr(transcript_view, "source_of", lambda sdef, **kw: files.get(sdef.name))
    monkeypatch.setattr(transcript_view.paths, "session_dir", lambda name: tmp_path / "sessions" / name)
    for n in files:
        (tmp_path / "sessions" / n).mkdir(parents=True)
    return files


def test_mode_switches_while_the_operator_runs(world):
    assert world.ops.mode("default") == "events"
    got = asyncio.run(world.ops.set_mode("default", "transcript"))
    assert got["changed"] and got["nudge"] == "sent"
    assert world.ops.mode("default") == "transcript"
    assert world.sessions["op"].sent[-1] == operator_transcript.MODE_NUDGE.format(mode="transcript")
    assert world.ops.state("default")["feed"][-1]["text"] == "관찰 모드: transcript"
    # the same mode again changes nothing and types nothing
    sent = len(world.sessions["op"].sent)
    assert not asyncio.run(world.ops.set_mode("default", "transcript"))["changed"]
    assert len(world.sessions["op"].sent) == sent
    with pytest.raises(ValueError):
        asyncio.run(world.ops.set_mode("default", "screen"))
    # the mode survives a daemon restart
    assert operator_bot.Operators(world.root, world.ops.manager).mode("default") == "transcript"


def test_transcripts_read_new_records_from_a_daemon_held_cursor(world, talk):
    _conversation(talk["w1"], [("user", f"질문 {i}") for i in range(10)])
    _conversation(talk["w2"], [("assistant", [{"type": "thinking", "thinking": "hidden"},
                                              {"type": "text", "text": "done"}])])
    # events mode answers the mode and nothing else
    assert asyncio.run(world.ops.transcripts("op")) == {
        "project": "default", "mode": "events", "transcripts": [], "more": False}
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(world.ops.transcripts("w1"))
    asyncio.run(world.ops.set_mode("default", "transcript"))
    first = asyncio.run(world.ops.transcripts("op"))
    rows = {r["session"]: r for r in first["transcripts"]}
    # a session seen first starts from its last BASELINE records
    assert [r["text"] for r in rows["w1"]["records"]] == [f"질문 {i}" for i in range(4, 10)]
    assert rows["w2"]["records"][0]["text"] == "done"  # thinking left out
    assert asyncio.run(world.ops.transcripts("op"))["transcripts"] == []  # nothing new
    _conversation(talk["w1"], [("assistant", [
        {"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "x" * 500}}]),
        ("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "boom"}])])
    rows = asyncio.run(world.ops.transcripts("op"))["transcripts"]
    assert [r["session"] for r in rows] == ["w1"]
    lines = [r["text"] for r in rows[0]["records"]]
    assert lines[0].startswith("[tool Bash] ") and len(lines[0]) < 260
    assert lines[1] == "[tool error] boom"
    # switching into transcript mode again starts from the tail again
    asyncio.run(world.ops.set_mode("default", "events"))
    asyncio.run(world.ops.set_mode("default", "transcript"))
    assert len({r["session"]: r for r in asyncio.run(world.ops.transcripts("op"))["transcripts"]}["w1"]["records"]) == 6


def test_transcripts_skip_stopped_sessions_and_bound_each_poll(world, talk, monkeypatch):
    world.sessions["w2"].exited = True
    _conversation(talk["w1"], [("user", "a" * 900) for _ in range(40)])
    _conversation(talk["w2"], [("user", "gone")])
    asyncio.run(world.ops.set_mode("default", "transcript"))
    world.ops.state("default")["transcripts"] = {"w1": 0}
    monkeypatch.setattr(operator_transcript, "BUDGET", 5000)
    got = asyncio.run(world.ops.transcripts("op"))
    assert [r["session"] for r in got["transcripts"]] == ["w1"]  # w2 is not running
    assert len(got["transcripts"][0]["records"]) == 5 and got["more"]
    assert world.ops.state("default")["transcripts"]["w1"] == 5
    # a long prose record is clipped, with the length it left out
    _conversation(talk["w1"], [("user", "b" * 4000)])
    world.ops.state("default")["transcripts"]["w1"] = 40
    text = asyncio.run(world.ops.transcripts("op"))["transcripts"][0]["records"][0]["text"]
    assert text.endswith("… (+2500)")


def test_mode_route_and_start_mode(world, monkeypatch):
    monkeypatch.setattr(operator_bot.paths, "daemon_dir", lambda: world.root)
    monkeypatch.setattr(operator_bot.projects, "require", lambda name: SimpleNamespace(name=name))
    created = []

    async def run():
        client = TestClient(TestServer(_app(world, created)))
        await client.start_server()
        try:
            resp = await client.post("/api/operator/mode", json={"project": "default", "mode": "nope"})
            assert resp.status == 400
            resp = await client.post("/api/operator/mode", json={"project": "default", "mode": "transcript"})
            assert (await resp.json())["mode"] == "transcript"
            view = await (await client.get("/api/operator?project=default")).json()
            assert view["mode"] == "transcript"
            resp = await client.get("/api/operator/agent/op/transcripts")
            assert (await resp.json())["mode"] == "transcript"
            resp = await client.post("/api/operator/start",
                                     json={"project": "other", "profile": "p", "mode": "bad"})
            assert resp.status == 400 and not created
            resp = await client.post("/api/operator/start",
                                     json={"project": "other", "profile": "p", "mode": "transcript"})
            assert resp.status == 201
            view = await (await client.get("/api/operator?project=other")).json()
            assert view["mode"] == "transcript"
        finally:
            await client.close()

    asyncio.run(run())
