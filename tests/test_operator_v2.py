"""Operator v2: independent operators, each with its own project scope.

What has to hold:
  - operators are created, renamed, re-scoped and deleted independently; a
    name is unique, a scope is a list of registered projects or ``all``, and a
    running operator is neither started twice nor deleted;
  - only a session bound to an operator v2 may use the agent routes, and it
    acts as *that* operator: its feed, its inbox, its scope;
  - the scope is enforced on every read and write: poll, refs, dispatch,
    session detail, commits, transcript search and board issues answer 403
    for a session or issue outside it;
  - two operators over different scopes (one project, all projects) keep
    their feeds and their polls apart;
  - the watch switch is recorded in the feed, nudges the bound session, and
    reaches the bot through poll and inbox;
  - the operator answers questions about work from its own history and from
    the in-scope sessions, ended ones included.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import cli_beads, mcp_server, operator_v2_mcp, session_commits
from claude_launcher.daemon import operators_v2, transcript_view


class FakeSession:
    def __init__(self, name, project="default", exited=False, cwd=""):
        self.sdef = SimpleNamespace(name=name, project=project, cwd=cwd, task=f"task of {name}", issue=None)
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

    def snapshot(self, names=None):
        return {"sessions": [{"name": n, **row} for n, row in self.rows.items()
                             if names is None or n in names]}


class FakeManager:
    def __init__(self, sessions):
        self.sessions = sessions
        self.killed = []

    def list(self):
        return list(self.sessions.values())

    def kill(self, name, force=False, by=None):
        self.killed.append(name)
        self.sessions[name].exited = True
        return self.sessions[name]


PROJECTS = ("alpha", "beta", "default")


@pytest.fixture
def world(tmp_path, monkeypatch):
    def require(name):
        if name not in PROJECTS:
            raise operators_v2.projects.ProjectError(f"unknown project {name!r}")
        return SimpleNamespace(name=name)

    monkeypatch.setattr(operators_v2.projects, "require", require)
    monkeypatch.setattr(operators_v2.projects, "names", lambda doc=None: list(PROJECTS))
    monkeypatch.setattr(operators_v2.projects, "default_cwd", lambda name, doc=None: None)
    sessions = {n: FakeSession(n, p, cwd=f"/repo/{p}") for n, p in [
        ("opa", "alpha"), ("opall", "default"), ("a1", "alpha"), ("a2", "alpha"), ("b1", "beta")]}
    ended = FakeSession("a-old", "alpha", exited=True, cwd="/repo/alpha")
    sessions["a-old"] = ended
    manager = FakeManager(sessions)
    observer = FakeObserver()
    ops = operators_v2.OperatorsV2(tmp_path, manager, observer)
    alpha = ops.create({"name": "Alpha", "projects": ["alpha"], "profile": "p"})
    every = ops.create({"name": "Everything", "all": True, "projects": ["alpha"]})
    ops.bind(alpha, "opa")
    ops.bind(every, "opall")
    return SimpleNamespace(ops=ops, sessions=sessions, manager=manager, observer=observer,
                           alpha=alpha, every=every, root=tmp_path)


def run(coro):
    return asyncio.run(coro)


# -- definitions ------------------------------------------------------------
def test_definitions_validate_scope_and_name(world):
    ops = world.ops
    assert world.every["all"] is True and world.every["projects"] == []
    with pytest.raises(web.HTTPConflict):
        ops.create({"name": "alpha", "projects": ["beta"]})  # names are unique, any case
    with pytest.raises(ValueError):
        ops.create({"name": "x", "projects": ["nope"]})
    with pytest.raises(ValueError):
        ops.create({"name": "x", "projects": []})  # a scope is required
    two = ops.create({"name": "Two", "projects": ["beta", "alpha", "beta"]})
    assert two["projects"] == ["beta", "alpha"]


def test_definitions_survive_a_restart(world):
    world.ops.post("opa", {"text": "hello", "request_id": "r1"})
    again = operators_v2.OperatorsV2(world.root, world.manager, world.observer)
    rows = {r["name"]: r for r in again.listing()["operators"]}
    assert set(rows) == {"Alpha", "Everything"}
    assert rows["Alpha"]["projects"] == ["alpha"] and rows["Alpha"]["operator"]["name"] == "opa"
    feed = again.feed(again.get(world.alpha["id"]))
    assert [e["text"] for e in feed if e["kind"] == "post"] == ["hello"]


def test_rescoping_and_watch_are_recorded_and_watch_nudges(world):
    ops = world.ops
    out = run(ops.update(world.alpha["id"], {"projects": ["alpha", "beta"], "watch": False}))
    assert out["projects"] == ["alpha", "beta"] and out["watch"] is False and out["nudge"] == "sent"
    assert "꺼짐" in world.sessions["opa"].sent[-1]
    assert "범위: alpha, beta" in ops.feed(ops.get(world.alpha["id"]))[-1]["text"]
    assert run(ops.poll("opa"))["watch"] is False
    assert ops.inbox("opa")["watch"] is False
    # b1 is in scope now
    assert "b1" in {s["name"] for s in run(ops.poll("opa"))["sessions"]}


def test_a_running_operator_cannot_be_deleted(world):
    with pytest.raises(web.HTTPConflict):
        world.ops.delete(world.alpha["id"])
    world.sessions["opa"].exited = True
    assert world.ops.delete(world.alpha["id"]) == {"deleted": world.alpha["id"]}
    with pytest.raises(web.HTTPNotFound):
        world.ops.get(world.alpha["id"])
    assert not (world.root / "operators" / f"{world.alpha['id']}.json").exists()


# -- identity and scope -----------------------------------------------------
def test_only_a_bound_session_may_act_and_it_acts_as_its_own_operator(world):
    ops = world.ops
    with pytest.raises(web.HTTPForbidden):
        ops.post("a1", {"text": "hi", "request_id": "x"})
    with pytest.raises(web.HTTPForbidden):
        ops.post("ghost", {"text": "hi", "request_id": "x"})
    ops.post("opa", {"text": "from alpha", "request_id": "1"})
    ops.post("opall", {"text": "from all", "request_id": "1"})
    alpha = [e["text"] for e in ops.feed(ops.get(world.alpha["id"])) if e["kind"] == "post"]
    every = [e["text"] for e in ops.feed(ops.get(world.every["id"])) if e["kind"] == "post"]
    assert alpha == ["from alpha"] and every == ["from all"]


def test_poll_sees_only_the_scope_and_never_the_operators(world):
    world.observer.rows = {n: {"events": [{"at": "2026-10-16T00:00:0%d+00:00" % i, "kind": "note", "text": n}]}
                           for i, n in enumerate(["a1", "a2", "b1", "opa", "opall"])}
    alpha = run(world.ops.poll("opa"))
    every = run(world.ops.poll("opall"))
    assert {e["session"] for e in alpha["events"]} == {"a1", "a2"}
    assert {e["session"] for e in every["events"]} == {"a1", "a2", "b1"}
    assert alpha["scope"] == {"all": False, "projects": ["alpha"]}
    # the ended session is not watched
    assert "a-old" not in {s["name"] for s in alpha["sessions"]}


def test_refs_and_dispatch_are_limited_to_the_scope(world):
    ops = world.ops
    with pytest.raises(ValueError):
        ops.post("opa", {"text": "x", "refs": ["b1"], "request_id": "r"})
    with pytest.raises(ValueError):
        ops.post("opa", {"text": "x", "refs": ["opall"], "request_id": "r2"})
    msg = run(ops.message(world.alpha["id"], {"text": "b1에 멈추라고 전해"}))
    assert msg["nudge"] == "sent"
    with pytest.raises(web.HTTPForbidden):
        run(ops.dispatch("opa", {"target": "b1", "text": "stop", "on_behalf_of": msg["id"]}))
    with pytest.raises(web.HTTPForbidden):  # an operator is never a target
        run(ops.dispatch("opall", {"target": "opa", "text": "stop",
                                   "on_behalf_of": run(ops.message(world.every["id"], {"text": "t"}))["id"]}))
    sent = run(ops.dispatch("opa", {"target": "a1", "text": "stop", "on_behalf_of": msg["id"]}))
    assert sent["delivery"] == "sent" and world.sessions["a1"].sent[-1].endswith("stop")
    # a dispatch must name the user's input it relays
    post = ops.post("opa", {"text": "self", "request_id": "own"})
    with pytest.raises(web.HTTPForbidden):
        run(ops.dispatch("opa", {"target": "a1", "text": "go", "on_behalf_of": post["id"]}))
    # an operator cannot cite another operator's message
    other = run(ops.message(world.every["id"], {"text": "x"}))
    with pytest.raises(web.HTTPNotFound):
        run(ops.dispatch("opa", {"target": "a1", "text": "go", "on_behalf_of": other["id"]}))


def test_inbox_and_asks_are_per_operator(world):
    ops = world.ops
    ask = ops.ask("opa", {"text": "배포?", "type": "approve", "request_id": "q"})
    run(ops.message(world.every["id"], {"text": "for everything"}))
    run(ops.answer(world.alpha["id"], ask["id"], {"decision": "approve"}))
    assert [m["kind"] for m in ops.inbox("opa")["messages"]] == ["ask"]
    assert [m["text"] for m in ops.inbox("opall")["messages"]] == ["for everything"]
    assert ops.inbox("opa")["messages"] == []  # read once
    listing = {r["name"]: r for r in ops.listing()["operators"]}
    assert listing["Alpha"]["pending"] == 0


# -- questions about work ---------------------------------------------------
def test_session_detail_and_commits_cover_ended_sessions_in_scope_only(world, monkeypatch):
    calls = []

    def for_session(cwd, name, limit=20):
        calls.append((cwd, name, limit))
        return [{"hash": "abc", "subject": f"work of {name}"}]

    monkeypatch.setattr(session_commits, "for_session", for_session)
    world.observer.rows = {"a-old": {"state": "done", "summary": "finished", "events": []}}
    detail = run(world.ops.session_detail("opa", "a-old"))
    assert detail["category"] == "killed" and detail["summary"] == "finished"
    assert detail["commits"][0]["subject"] == "work of a-old"
    with pytest.raises(web.HTTPForbidden):
        run(world.ops.session_detail("opa", "b1"))
    with pytest.raises(web.HTTPForbidden):
        run(world.ops.commits("opa", "b1"))
    with pytest.raises(web.HTTPNotFound):
        run(world.ops.session_detail("opa", "nobody"))
    assert run(world.ops.commits("opall", "b1", "5"))["commits"]
    assert calls[-1] == ("/repo/beta", "b1", 5)


def test_session_list_filters_by_state(world):
    live = run(world.ops.session_list("opa", {}))
    ended = run(world.ops.session_list("opa", {"state": "ended"}))
    everything = run(world.ops.session_list("opall", {"state": "all", "project": "beta"}))
    assert {s["name"] for s in live["sessions"]} == {"a1", "a2"}
    assert {s["name"] for s in ended["sessions"]} == {"a-old"}
    assert {s["name"] for s in everything["sessions"]} == {"b1"}
    with pytest.raises(ValueError):
        run(world.ops.session_list("opa", {"state": "bogus"}))


def test_work_reads_status_checks_from_the_store_not_the_session_info(monkeypatch):
    """claunch-iw5i8.11: ``session.info()`` carries no status checks (the
    session-list handler attaches them to its own rows), so reading it gave
    every operator an empty list. The answers and the recorded PRs come from
    their stores."""
    from claude_launcher.daemon import api, status_checks

    rows = status_checks.set_entries([{"name": "C", "question": "Committed?"},
                                      {"name": "M", "question": "Merged?"}])
    status_checks.report("a1", [{"id": rows[0]["id"], "answer": "yes"}])
    pr = {"url": "https://h/o/r/pull/7", "number": 7, "status": "merged", "branch": "a1-x"}
    monkeypatch.setattr(api.session_prs, "for_session", lambda cwd, name: [pr] if name == "a1" else [])
    work = run(api._operator_work({}, [FakeSession("a1"), FakeSession("a2")]))
    assert operators_v2.progress_of(work["a1"])["checks"] == [
        {"name": "C", "question": "Committed?", "answer": "yes"},
        {"name": "M", "question": "Merged?", "answer": None}]
    assert work["a1"]["prs"] == [pr] and work["a2"]["prs"] == []
    assert work["a1"]["branch"] == ""      # no checkout: no branch, not an error
    # the poll's progress lines now see an answer change
    before = operators_v2.progress_of(work["a2"])
    status_checks.report("a2", [{"id": rows[1]["id"], "answer": "yes"}])
    after = operators_v2.progress_of(run(api._operator_work({}, [FakeSession("a2")]))["a2"])
    assert operators_v2.progress_changes(before, after) == ["Merged? → yes"]


def test_landing_reads_the_pr_state_in_scope(world):
    def pr(n, status=None):
        return {"number": n, "url": f"https://h/o/r/pull/{n}", "branch": f"b{n}",
                **({"status": status} if status else {})}

    entries = {
        "a1": {"issue": {"id": "x-1", "status": "closed"}, "branch": "a1-x",
               "prs": [pr(1, "closed"), pr(2, "merged")]},
        "a2": {"prs": [pr(3)]},
        "a-old": {"prs": [pr(4, "open"), pr(5)]},
        "b1": {"prs": [pr(6, "merged")]},
    }

    async def work(sessions):
        return {s.sdef.name: entries.get(s.sdef.name) for s in sessions}

    world.ops.work = work
    out = run(world.ops.landing("opa", {"state": "all"}))
    rows = {r["name"]: r for r in out["sessions"]}
    assert set(rows) == {"a1", "a2", "a-old"}           # b1 is outside Alpha's scope
    assert rows["a1"]["landed"] == "merged" and rows["a1"]["issue"]["status"] == "closed"
    assert rows["a1"]["branch"] == "a1-x" and [p["number"] for p in rows["a1"]["prs"]] == [1, 2]
    assert rows["a2"]["landed"] == "unknown"            # a PR with no state on record
    assert rows["a-old"]["landed"] == "open"
    live = run(world.ops.landing("opa", {}))
    assert {r["name"] for r in live["sessions"]} == {"a1", "a2"}
    assert operators_v2.landing_of(None) == {"issue": None, "branch": None, "prs": [], "landed": "no-pr"}
    assert operators_v2.landing_of({"prs": [pr(9, "closed")]})["landed"] == "closed"
    with pytest.raises(ValueError):
        run(world.ops.landing("opa", {"state": "bogus"}))


def test_the_landing_tool_calls_the_landing_route(monkeypatch):
    calls = []

    class Client:
        def get(self, path):
            calls.append(path)
            return {}

    monkeypatch.setenv("CLAUNCH_SESSION", "opa")
    monkeypatch.setattr(operator_v2_mcp.daemon_client, "connect_with_diagnosis", lambda: (Client(), None))
    operator_v2_mcp.call_tool("opv2_landing", {"state": "all", "project": "alpha"})
    assert calls == ["/api/operators/agent/opa/landing?state=all&project=alpha"]


def test_transcript_search_reads_only_the_scope(world, monkeypatch):
    read = []

    def search(name, sdef, *, query, regex=False, limit=20, prose_only=False, **_):
        read.append(name)
        return {"matches": [{"seq": 1, "ts": f"2026-10-16T00:00:0{len(read)}", "text": f"{query} in {name}"}]}

    monkeypatch.setattr(transcript_view, "search", search)
    out = run(world.ops.transcript_search("opa", {"q": "deploy"}))
    assert set(read) == {"a1", "a2", "a-old"}
    assert {m["session"] for m in out["matches"]} == {"a1", "a2", "a-old"}
    with pytest.raises(web.HTTPForbidden):
        run(world.ops.transcript_search("opa", {"q": "deploy", "session": "b1"}))
    with pytest.raises(ValueError):
        run(world.ops.transcript_search("opa", {}))


class FakeBoard:
    def __init__(self, issues):
        self.issues = issues  # (root, id) -> issue

    def available(self):
        return True

    async def root_for(self, cwd):
        return Path(cwd)

    def has_board(self, root):
        return True

    async def show(self, root, issue_id):
        try:
            return self.issues[(str(root), issue_id)]
        except KeyError:
            raise cli_beads.BeadsError(f"no issue {issue_id!r}") from None


def test_issue_is_answered_only_from_a_board_in_scope(world):
    world.sessions["a1"].sdef.issue = "alpha-1"
    board = FakeBoard({("/repo/alpha", "alpha-1"): {"id": "alpha-1", "title": "A", "status": "open", "comments": []},
                       ("/repo/beta", "beta-1"): {"id": "beta-1", "title": "B", "status": "open", "comments": []}})
    world.ops.board = lambda: board
    out = run(world.ops.issue("opa", "alpha-1"))
    assert out["issue"]["title"] == "A" and out["sessions"] == ["a1"]
    with pytest.raises(web.HTTPForbidden):
        run(world.ops.issue("opa", "beta-1"))
    assert run(world.ops.issue("opall", "beta-1"))["issue"]["title"] == "B"


def test_history_answers_what_the_operator_did(world):
    ops = world.ops
    msg = run(ops.message(world.alpha["id"], {"text": "a1 멈춰"}))
    run(ops.dispatch("opa", {"target": "a1", "text": "멈추십시오", "on_behalf_of": msg["id"]}))
    ops.post("opa", {"text": "a2 merged", "refs": ["a2"], "request_id": "m"})
    dispatched = ops.history("opa", {"kind": "dispatch"})["entries"]
    assert [e["target"] for e in dispatched] == ["a1"]
    assert [e["text"] for e in ops.history("opa", {"session": "a2"})["entries"]] == ["a2 merged"]
    assert [e["kind"] for e in ops.history("opa", {"q": "멈춰"})["entries"]] == ["user"]
    assert ops.history("opall", {})["entries"][0]["kind"] == "system"  # its own feed only


# -- asking for a session (claunch-iw5i8.15) -------------------------------
def _gated(world, monkeypatch):
    from claude_launcher.daemon import spawn_gate

    monkeypatch.setattr(operators_v2.projects, "default_cwd", lambda name, doc=None: f"/repo/{name}")
    manager = SimpleNamespace(get=lambda name: world.sessions[name])
    gate = spawn_gate.SpawnGate(manager=manager, path=None,
                                on_settle=world.ops.spawn_settled)
    world.ops.spawn_gate = lambda: gate
    return gate


def test_spawn_files_a_gated_request_for_a_top_level_worker(world, monkeypatch):
    gate = _gated(world, monkeypatch)
    ops = world.ops
    msg = run(ops.message(world.alpha["id"], {"text": "로그인 버그 고쳐줘"}))
    out = run(ops.spawn("opa", {"task": "로그인 버그 수정", "reason": "맡은 세션 없음",
                                "on_behalf_of": msg["id"]}))
    record = out["request"]
    assert record["kind"] == "spawn" and record["status"] == "pending" and record["session"] == "opa"
    # top-level: no parent; the project's workspace on a fresh worktree; the worker workflow
    assert record["spec"] == {"profile": "p", "project": "alpha", "cwd": "/repo/alpha",
                              "task": "로그인 버그 수정", "workflow": "improv-worker-v2", "worktree": ""}
    assert "parent" not in record["spec"]
    assert record["basis"]["operator"] == world.alpha["id"] and record["basis"]["on_behalf_of"] == msg["id"]
    assert "세션 생성 요청" in ops.feed(ops.get(world.alpha["id"]))[-1]["text"]
    assert [r["id"] for r in run(ops.poll("opa"))["requests"]] == [record["id"]]
    # the settlement lands in the operator's feed
    gate.reject(record["id"])
    assert "rejected" in ops.feed(ops.get(world.alpha["id"]))[-1]["text"]


def test_spawn_needs_the_users_word_and_a_project_in_scope(world, monkeypatch):
    _gated(world, monkeypatch)
    ops = world.ops
    post = ops.post("opa", {"text": "self", "request_id": "own"})
    with pytest.raises(web.HTTPForbidden):
        run(ops.spawn("opa", {"task": "t", "reason": "r", "on_behalf_of": post["id"]}))
    msg = run(ops.message(world.alpha["id"], {"text": "go"}))
    with pytest.raises(web.HTTPForbidden):
        run(ops.spawn("opa", {"task": "t", "reason": "r", "on_behalf_of": msg["id"], "project": "beta"}))
    with pytest.raises(ValueError):
        run(ops.spawn("opa", {"task": "t", "on_behalf_of": msg["id"]}))  # no reason
    every = run(ops.message(world.every["id"], {"text": "go"}))
    with pytest.raises(ValueError):  # an all-projects operator must name one
        run(ops.spawn("opall", {"task": "t", "reason": "r", "on_behalf_of": every["id"]}))
    with pytest.raises(web.HTTPForbidden):  # only an operator's bound session
        run(ops.spawn("a1", {"task": "t", "reason": "r", "on_behalf_of": msg["id"]}))


def test_spawn_takes_an_issue_only_from_a_board_in_scope(world, monkeypatch):
    _gated(world, monkeypatch)
    world.ops.board = lambda: FakeBoard({("/repo/alpha", "alpha-1"): {"id": "alpha-1", "comments": []},
                                         ("/repo/beta", "beta-1"): {"id": "beta-1", "comments": []}})
    msg = run(world.ops.message(world.alpha["id"], {"text": "alpha-1 해줘"}))
    out = run(world.ops.spawn("opa", {"task": "t", "reason": "r", "issue": "alpha-1",
                                      "on_behalf_of": msg["id"]}))
    assert out["request"]["spec"]["issue"] == "alpha-1"
    with pytest.raises(web.HTTPForbidden):
        run(world.ops.spawn("opa", {"task": "t", "reason": "r", "issue": "beta-1",
                                    "on_behalf_of": msg["id"]}))


def test_resume_asks_for_an_ended_session_with_a_fallback(world, monkeypatch):
    gate = _gated(world, monkeypatch)
    ops = world.ops
    msg = run(ops.message(world.alpha["id"], {"text": "a-old가 하던 거 이어서"}))
    with pytest.raises(ValueError):  # running: dispatch instead
        run(ops.resume("opa", {"target": "a1", "reason": "r", "on_behalf_of": msg["id"],
                               "fallback": {"task": "t"}}))
    with pytest.raises(ValueError):  # the fallback is required
        run(ops.resume("opa", {"target": "a-old", "reason": "r", "on_behalf_of": msg["id"]}))
    with pytest.raises(web.HTTPForbidden):
        run(ops.resume("opa", {"target": "b1", "reason": "r", "on_behalf_of": msg["id"],
                               "fallback": {"task": "t"}}))
    out = run(ops.resume("opa", {"target": "a-old", "reason": "그 작업의 세션", "on_behalf_of": msg["id"],
                                 "fallback": {"task": "이어서 작업"}}))
    record = out["request"]
    assert record["kind"] == "resume" and record["target"] == "a-old"
    assert record["fallback"]["project"] == "alpha" and record["fallback"]["task"] == "이어서 작업"
    follow = gate.spawn_instead(record["id"])["followup"]
    assert gate.get(follow)["spec"] == record["fallback"]


# -- routes -----------------------------------------------------------------
def _app(world, created):
    app = web.Application()
    app["manager"] = world.manager
    app["observer"] = world.observer

    async def create(request, body):
        created.append(body)
        world.sessions["op-new"] = FakeSession("op-new", body["project"])
        return 201, {"name": "op-new"}

    operators_v2.install(app, create=create)
    app["operators_v2"].root = world.root / "operators"
    app["operators_v2"].cache = None
    return app


def test_routes_crud_start_stop_and_agent_scope(world, monkeypatch):
    created, made = [], []

    async def go():
        client = TestClient(TestServer(_app(world, created)))
        await client.start_server()
        try:
            resp = await client.post("/api/operators", json={"name": "Beta", "projects": ["beta"]})
            assert resp.status == 201
            beta = await resp.json()
            made.append(beta)
            assert (await client.post("/api/operators", json={"name": "beta", "all": True})).status == 409
            assert (await client.post("/api/operators", json={"name": "x", "projects": ["nope"]})).status == 400
            names = {r["name"] for r in (await (await client.get("/api/operators")).json())["operators"]}
            assert names == {"Alpha", "Everything", "Beta"}
            # start: profile required, then one session, then 409
            assert (await client.post(f"/api/operators/{beta['id']}/start", json={})).status == 400
            resp = await client.post(f"/api/operators/{beta['id']}/start", json={"profile": "p", "model": "m"})
            assert resp.status == 201
            assert (await client.post(f"/api/operators/{beta['id']}/start", json={"profile": "p"})).status == 409
            view = await (await client.get(f"/api/operators/{beta['id']}")).json()
            assert view["operator"]["name"] == "op-new" and {s["name"] for s in view["sessions"]} == {"b1"}
            # the new session acts as Beta and sees beta only
            resp = await client.get("/api/operators/agent/op-new/sessions/a1")
            assert resp.status == 403
            resp = await client.get("/api/operators/agent/op-new/sessions/b1")
            assert resp.status == 200
            assert (await client.get("/api/operators/agent/a1/poll")).status == 403
            assert (await client.delete(f"/api/operators/{beta['id']}")).status == 409
            resp = await client.patch(f"/api/operators/{beta['id']}", json={"watch": False})
            assert (await resp.json())["watch"] is False
            stop = await (await client.post(f"/api/operators/{beta['id']}/stop")).json()
            assert stop["stopped"] is True and world.manager.killed == ["op-new"]
            assert (await client.delete(f"/api/operators/{beta['id']}")).status == 200
            assert (await client.get("/api/operators/0000000000ff")).status == 404
        finally:
            await client.close()

    run(go())
    spec = created[0]
    assert spec["project"] == "beta" and spec["beads"] is False and spec["model"] == "m"
    assert "mesh" not in spec and "opv2_inbox" in spec["task"]
    # the session runs the operator-v2 workflow with this operator's id as its input
    assert spec["workflow"] == "operator-v2" == operators_v2.WORKFLOW
    assert spec["cflow_inputs"] == {"operator": made[0]["id"]}


def test_the_v2_tools_are_merged_and_v1_is_gone():
    names = [t["name"] for t in mcp_server.TOOLS]
    v2 = [t["name"] for t in operator_v2_mcp.TOOLS]
    assert all(n.startswith("opv2_") for n in v2) and set(v2) <= set(names)
    assert not [n for n in names if n.startswith("operator_")]
    assert len(set(names)) == len(names)


def test_mcp_tools_need_a_managed_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    with pytest.raises(operator_v2_mcp.OperatorV2McpError):
        operator_v2_mcp.call_tool("opv2_poll", {})
