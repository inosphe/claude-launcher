"""Session wrap-up drafts (claunch-ar8jn): wrapup/store.py, the routes in
daemon/wrapup_api.py and the wrapup_* MCP tools.

A session files glossary, category and knowledge entries as drafts; only the
user's confirm writes them into the workspace stores, a store's refusal
leaves the draft failed and editable, and a session that ends with drafts
still waiting is marked so the dashboard can raise a card."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import category, glossary, knowledge, workspaces, wrapup_mcp
from claude_launcher.daemon import wrapup_api
from claude_launcher.wrapup import store as W


class FakeSession:
    def __init__(self, name, cwd, issue=None, exited=False):
        self.sdef = SimpleNamespace(name=name, cwd=cwd, issue=issue)
        self.exited = exited
        self.held_at = None
        self.failed_at = None
        self.paused_at = None
        self.delivered = []

    def queue_delivery(self, text, on_done=None):
        if self.exited:
            return False
        text = text() if callable(text) else text
        if text:
            self.delivered.append(text)
        if on_done:
            on_done(bool(text))
        return True


class FakeManager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.exit_hooks = []
        self.settle_hooks = []

    def get(self, name):
        return self.sessions[name]

    def wake_soon(self, *_a, **_k):
        return False

    def exit(self, name):
        s = self.sessions[name]
        s.exited = True
        for hook in self.exit_hooks:
            hook(s)


def run(tmp_path, scenario, sessions=None):
    workspaces.add(str(tmp_path), "alpha")
    sessions = sessions or [FakeSession("s1", str(tmp_path), issue="claunch-x1"),
                            FakeSession("s2", str(tmp_path))]
    mgr = FakeManager(sessions)

    async def go():
        app = web.Application()
        app["manager"] = mgr
        wrapup_api.install(app, W.WrapupStore(tmp_path / "wrapup.sqlite3"))
        async with TestClient(TestServer(app)) as client:
            async def call(path, body=None, status=200, method=None):
                method = method or ("POST" if body is not None else "GET")
                resp = await client.request(method, path, json=body)
                doc = await resp.json()
                assert resp.status == status, doc
                return doc
            await scenario(call, mgr)
    asyncio.run(go())


def ws():
    return workspaces.get("alpha")


# -- the store -------------------------------------------------------------

def test_the_store_checks_payloads_when_filed_and_guards_revisions(tmp_path):
    st = W.WrapupStore(tmp_path / "w.sqlite3")
    with pytest.raises(W.WrapupError, match="knowledge"):
        st.draft("s1", kind="knowledge", payload={"kind": "decision", "title": ""}, workspace="alpha")
    with pytest.raises(W.WrapupError, match="exactly one"):
        st.draft("s1", kind="category", payload={"node_id": "n1", "new": {"label": "x"}}, workspace="alpha")
    with pytest.raises(W.Forbidden):
        st.draft(W.USER, kind="glossary", payload={"term": "a", "definition": "b"}, workspace="alpha")
    d = st.draft("s1", kind="glossary", payload={"term": "draft", "definition": "임시본"}, workspace="alpha")
    assert d["status"] == "draft" and d["revision"] == 1
    with pytest.raises(W.Forbidden):
        st.update("s2", d["id"], 1, payload={"term": "x", "definition": "y"})
    with pytest.raises(W.Conflict) as e:
        st.update(W.USER, d["id"], 5, payload={"term": "x", "definition": "y"})
    assert e.value.current["revision"] == 1
    with pytest.raises(W.Forbidden):
        st.claim("s1", d["id"], 1)
    edited = st.update(W.USER, d["id"], 1, payload={"term": "draft", "definition": "고친 정의"})
    assert edited["edited_by_user"] and edited["revision"] == 2
    claimed = st.claim(W.USER, d["id"], 2)
    assert claimed["status"] == "applying"
    with pytest.raises(W.Conflict):
        st.claim(W.USER, d["id"], 3)     # a second confirm while it is written


def test_a_row_left_applying_reopens_as_failed(tmp_path):
    path = tmp_path / "w.sqlite3"
    st = W.WrapupStore(path)
    d = st.draft("s1", kind="glossary", payload={"term": "a", "definition": "b"}, workspace="alpha")
    st.claim(W.USER, d["id"], 1)
    again = W.WrapupStore(path).get(d["id"])
    assert again["status"] == "failed" and "stopped" in again["error"]


def test_ended_marks_only_sessions_with_drafts_and_a_new_draft_clears_it(tmp_path):
    st = W.WrapupStore(tmp_path / "w.sqlite3")
    st.mark_ended("s9")
    assert st.rail() == {}
    st.draft("s1", kind="glossary", payload={"term": "a", "definition": "b"}, workspace="alpha")
    st.mark_ended("s1")
    assert st.rail()["s1"]["ended_at"] and st.rail()["s1"]["unresolved"] == 1
    st.draft("s1", kind="glossary", payload={"term": "c", "definition": "d"}, workspace="alpha")
    assert st.rail()["s1"]["ended_at"] is None


# -- the routes ------------------------------------------------------------

def test_confirm_writes_each_kind_into_its_store(home, tmp_path):
    async def scenario(call, mgr):
        g = (await call("/api/wrapup", {"session": "s1", "kind": "glossary",
                                        "payload": {"term": "임시본", "definition": "확정 전 기록"}}))["draft"]
        k = (await call("/api/wrapup", {"session": "s1", "kind": "knowledge",
                                        "payload": {"kind": "decision", "title": "결정", "body": "이유"}}))["draft"]
        c = (await call("/api/wrapup", {"session": "s1", "kind": "category",
                                        "payload": {"new": {"label": "wrap-up"}}, "reason": "새 주제"}))["draft"]
        assert c["payload"]["items"] == [{"kind": "session", "ref": "s1"}, {"kind": "issue", "ref": "claunch-x1"}]
        # Nothing reached the stores yet.
        assert glossary.search(ws(), "임시본") == []
        assert knowledge.search(ws(), "결정") == []
        assert category.where(ws(), "session", "s1").get("node") is None

        listed = await call("/api/wrapup?session=s1")
        assert listed["unresolved"] == 3 and listed["counts"] == {"glossary": 1, "category": 1, "knowledge": 1}

        await call(f"/api/wrapup/{g['id']}/confirm", {"session": "s1", "revision": 1}, status=403)
        done = (await call(f"/api/wrapup/{g['id']}/confirm", {"revision": 1}))["draft"]
        assert done["status"] == "confirmed" and done["applied"]["term"] == "임시본"
        assert [t["term"] for t in glossary.search(ws(), "임시본")] == ["임시본"]

        res = await call("/api/wrapup/confirm-all", {"target": "s1"})
        assert res["confirmed"] == 2 and res["failed"] == 0
        assert knowledge.search(ws(), "결정")[0]["author"] == "s1"
        assert category.where(ws(), "session", "s1")["node"]["label"] == "wrap-up"
        assert category.where(ws(), "issue", "claunch-x1")["node"]["label"] == "wrap-up"
        assert (await call("/api/wrapup?session=s1"))["unresolved"] == 0

        typed = mgr.get("s1").delivered
        assert typed and all(t.startswith(wrapup_api.PREFIX) for t in typed)
        assert "confirmed 2 of your wrap-up draft(s)" in typed[-1]
    run(tmp_path, scenario)


def test_a_store_refusal_leaves_the_draft_failed_and_editable(home, tmp_path):
    glossary.create(workspaces.add(str(tmp_path), "alpha"), {"term": "taken", "definition": "x"})

    async def scenario(call, mgr):
        d = (await call("/api/wrapup", {"session": "s1", "kind": "glossary",
                                        "payload": {"term": "taken", "definition": "y"}}))["draft"]
        res = await call(f"/api/wrapup/{d['id']}/confirm", {"revision": 1}, status=409)
        failed = res["draft"]
        assert failed["status"] == "failed" and failed["error"]
        assert (await call("/api/wrapup/rail"))["sessions"]["s1"]["unresolved"] == 1
        fixed = (await call(f"/api/wrapup/{d['id']}", {"revision": failed["revision"],
                                                       "payload": {"term": "fresh", "definition": "y"}}))["draft"]
        assert fixed["status"] == "draft"
        assert "edited" in mgr.get("s1").delivered[-1]
        ok = (await call(f"/api/wrapup/{d['id']}/confirm", {"revision": fixed["revision"]}))["draft"]
        assert ok["status"] == "confirmed"
    run(tmp_path, scenario)


def test_discard_and_withdraw_and_who_may_touch_a_draft(home, tmp_path):
    async def scenario(call, mgr):
        await call("/api/wrapup", {"kind": "glossary", "payload": {"term": "a", "definition": "b"}}, status=403)
        await call("/api/wrapup", {"session": "nope", "kind": "glossary",
                                   "payload": {"term": "a", "definition": "b"}}, status=404)
        d = (await call("/api/wrapup", {"session": "s1", "kind": "glossary",
                                        "payload": {"term": "a", "definition": "b"}}))["draft"]
        await call(f"/api/wrapup/{d['id']}/discard", {"session": "s2", "revision": 1}, status=403)
        gone = (await call(f"/api/wrapup/{d['id']}/discard", {"revision": 1}))["draft"]
        assert gone["status"] == "discarded" and gone["settled_by"] == "user"
        assert "discarded" in mgr.get("s1").delivered[-1]
        await call(f"/api/wrapup/{d['id']}/confirm", {"revision": 2}, status=409)
        e = (await call("/api/wrapup", {"session": "s1", "kind": "glossary",
                                        "payload": {"term": "c", "definition": "d"}}))["draft"]
        typed = len(mgr.get("s1").delivered)
        mine = (await call(f"/api/wrapup/{e['id']}/discard", {"session": "s1", "revision": 1}))["draft"]
        assert mine["settled_by"] == "s1" and len(mgr.get("s1").delivered) == typed
    run(tmp_path, scenario)


def test_a_session_that_ends_with_drafts_waiting_is_on_the_rail(home, tmp_path):
    async def scenario(call, mgr):
        await call("/api/wrapup", {"session": "s1", "kind": "knowledge",
                                   "payload": {"kind": "lesson", "title": "t", "body": "b"}})
        row = (await call("/api/wrapup/rail"))["sessions"]["s1"]
        assert row["alive"] and row["ended_at"] is None
        mgr.exit("s1")
        mgr.exit("s2")      # no drafts: no row
        rail = (await call("/api/wrapup/rail"))["sessions"]
        assert set(rail) == {"s1"}
        assert not rail["s1"]["alive"] and rail["s1"]["ended_at"] and rail["s1"]["unresolved"] == 1
        # An ended session can still be settled from the pane; nothing is typed.
        listed = await call("/api/wrapup?session=s1")
        await call(f"/api/wrapup/{listed['drafts'][0]['id']}/confirm", {"revision": 1})
        assert mgr.get("s1").delivered == []
        assert (await call("/api/wrapup/rail"))["sessions"]["s1"]["unresolved"] == 0
    run(tmp_path, scenario)


def test_a_paused_session_is_not_marked_ended_until_settled(home, tmp_path):
    async def scenario(call, mgr):
        await call("/api/wrapup", {"session": "s1", "kind": "glossary",
                                   "payload": {"term": "a", "definition": "b"}})
        mgr.get("s1").paused_at = "now"
        mgr.exit("s1")
        assert (await call("/api/wrapup/rail"))["sessions"]["s1"]["ended_at"] is None
        for hook in mgr.settle_hooks:
            hook(mgr.get("s1"))
        assert (await call("/api/wrapup/rail"))["sessions"]["s1"]["ended_at"]
    run(tmp_path, scenario)


def test_the_daemon_app_registers_the_wrapup_routes(home):
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def build():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        return build_app(mgr, "sekrit", started_at=time.monotonic())

    app = asyncio.run(build())
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    assert {("GET", "/api/wrapup"), ("GET", "/api/wrapup/rail"), ("POST", "/api/wrapup"),
            ("POST", "/api/wrapup/confirm-all"), ("POST", "/api/wrapup/{id}"),
            ("POST", "/api/wrapup/{id}/discard"), ("POST", "/api/wrapup/{id}/confirm")} <= routes


# -- the MCP tools ---------------------------------------------------------

def test_the_mcp_tools_send_the_session_and_cannot_confirm(monkeypatch):
    sent = []

    class Client:
        def get(self, path):
            sent.append(("GET", path, None))
            return {"drafts": []}

        def post(self, path, body):
            sent.append(("POST", path, body))
            return {"ok": True}

    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    monkeypatch.setattr(wrapup_mcp.daemon_client, "connect_with_diagnosis", lambda: (Client(), None))
    names = {t["name"] for t in wrapup_mcp.TOOLS}
    assert names == {"wrapup_draft", "wrapup_update", "wrapup_withdraw", "wrapup_list"}
    wrapup_mcp.call_tool("wrapup_draft", {"kind": "glossary", "payload": {"term": "a", "definition": "b"}})
    wrapup_mcp.call_tool("wrapup_update", {"id": "wd-1", "revision": 2, "payload": {}})
    wrapup_mcp.call_tool("wrapup_withdraw", {"id": "wd-1", "revision": 3})
    wrapup_mcp.call_tool("wrapup_list", {})
    assert [s[1] for s in sent] == ["/api/wrapup", "/api/wrapup/wd-1", "/api/wrapup/wd-1/discard",
                                    "/api/wrapup?session=s7&settled=0"]
    assert all(s[2]["session"] == "s7" for s in sent if s[2] is not None)
    from claude_launcher import mcp_server
    offered = {t["name"] for t in mcp_server.SERVER.tools}
    assert names <= offered
