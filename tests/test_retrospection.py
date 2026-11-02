"""Session retrospectives as records (claunch-lmnlv): retrospection.py, the
wrap-up kind ``retrospection`` with its ``auto`` confirm, the version-1 ->
version-2 wrap-up schema migration, the read-only /api/retrospection routes,
the ``auto record=`` line ``claunch retro`` heads its block with, and the
improv-worker-v2 input that feeds the run's own retrospective."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import claude_launcher
from claude_launcher import cli, daemon_client, prompt_packs, retrospection, workspaces, wrapup_mcp
from claude_launcher.daemon import retrospection_api, wrapup_api
from claude_launcher.wrapup import store as W

from test_wrapup import FakeManager, FakeSession

RETRO = {"title": "s1 회고: 탭 추가", "body": "RETRO @ run-1\n## A. 세션 회고\n- 목표\n## B. cflow 정의 리뷰\n제안 없음",
         "trigger": "run", "run": "run-1", "workflow": "improv-worker-v2", "issue": "claunch-x1",
         "problems": 2, "proposals": 1}


# -- the record store ------------------------------------------------------

def test_clean_requires_title_and_body_and_checks_the_fields():
    with pytest.raises(retrospection.RetrospectionError, match="title"):
        retrospection.clean({"body": "x"})
    with pytest.raises(retrospection.RetrospectionError, match="body"):
        retrospection.clean({"title": "x"})
    with pytest.raises(retrospection.RetrospectionError, match="trigger"):
        retrospection.clean({"title": "x", "body": "y", "trigger": "cron"})
    with pytest.raises(retrospection.RetrospectionError, match="problems"):
        retrospection.clean({"title": "x", "body": "y", "problems": -1})
    with pytest.raises(retrospection.RetrospectionError, match="proposals"):
        retrospection.clean({"title": "x", "body": "y", "proposals": True})
    out = retrospection.clean({"title": " x ", "body": "y"})
    assert out == {"title": "x", "body": "y", "trigger": "run", "run": "", "workflow": "", "issue": "",
                   "problems": 0, "proposals": 0, "tags": []}


def test_records_are_listed_newest_first_filtered_and_searched(home):
    a = retrospection.create(workspace="alpha", session="s1", payload=RETRO, draft="wd-1", auto=True)
    b = retrospection.create(workspace="beta", session="s2",
                             payload={**RETRO, "title": "s2 회고", "body": "다른 본문", "trigger": "manual"})
    assert a["id"].startswith("rt-") and a["auto"] is True and a["draft"] == "wd-1"
    assert retrospection.db_path().is_file()
    listed = retrospection.search()
    assert [r["id"] for r in listed] == [b["id"], a["id"]] or {r["id"] for r in listed} == {a["id"], b["id"]}
    assert "body" not in listed[0] and listed[0]["chars"] > 0
    assert [r["id"] for r in retrospection.search(workspace="alpha")] == [a["id"]]
    assert [r["id"] for r in retrospection.search(session="s2")] == [b["id"]]
    assert [r["id"] for r in retrospection.search(q="CFLOW 정의")] == [a["id"]]
    assert retrospection.get(a["id"])["body"] == RETRO["body"]
    with pytest.raises(retrospection.RetrospectionError) as e:
        retrospection.get("rt-none")
    assert e.value.status == 404
    facets = retrospection.facets()
    assert facets["workspaces"] == [{"name": "alpha", "count": 1}, {"name": "beta", "count": 1}]
    assert {s["name"] for s in facets["sessions"]} == {"s1", "s2"}


# -- the wrap-up kind --------------------------------------------------------

def test_auto_is_only_for_retrospection_and_only_the_daemon_claims_with_it(tmp_path):
    st = W.WrapupStore(tmp_path / "w.sqlite3")
    with pytest.raises(W.WrapupError, match="only retrospection"):
        st.draft("s1", kind="glossary", payload={"term": "a", "definition": "b"}, workspace="alpha", auto=True)
    with pytest.raises(W.WrapupError, match="retrospection"):
        st.draft("s1", kind="retrospection", payload={"title": "x"}, workspace="alpha")
    with pytest.raises(W.Forbidden):
        st.draft(W.AUTO, kind="retrospection", payload=RETRO, workspace="alpha")
    g = st.draft("s1", kind="glossary", payload={"term": "a", "definition": "b"}, workspace="alpha")
    with pytest.raises(W.Forbidden):
        st.claim(W.AUTO, g["id"], 1)          # the daemon never confirms the user's kinds
    r = st.draft("s1", kind="retrospection", payload=RETRO, workspace="alpha")
    edited = st.update(W.USER, r["id"], 1, payload={**RETRO, "title": "고친 제목"})
    with pytest.raises(W.Forbidden):
        st.claim(W.AUTO, r["id"], edited["revision"])   # touched since filing: the user's now
    with pytest.raises(W.Forbidden):
        st.claim("s1", r["id"], edited["revision"])     # a session still cannot confirm
    assert st.list("s1")["counts"] == {"glossary": 1, "category": 0, "knowledge": 0, "retrospection": 1}


def _v1_db(path: Path) -> None:
    """A wrap-up database as version 1 made it: the kind CHECK names three kinds."""
    db = sqlite3.connect(str(path))
    db.executescript("""
CREATE TABLE drafts (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL CHECK (length(session) > 0),
    workspace TEXT NOT NULL CHECK (length(workspace) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('glossary', 'category', 'knowledge')),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'applying', 'confirmed', 'failed', 'discarded')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    error TEXT NOT NULL DEFAULT '',
    applied TEXT CHECK (applied IS NULL OR json_valid(applied)),
    edited_by_user INTEGER NOT NULL DEFAULT 0 CHECK (edited_by_user IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_by TEXT,
    CHECK ((status = 'confirmed') = (applied IS NOT NULL))
);
CREATE INDEX drafts_session ON drafts (session, status);
CREATE TABLE ended (session TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TRIGGER drafts_revision_advances BEFORE UPDATE ON drafts
WHEN NEW.revision <> OLD.revision + 1
BEGIN SELECT RAISE(ABORT, 'wrapup: a write must advance revision by exactly one'); END;
INSERT INTO drafts (id, session, workspace, kind, payload, reason, status, revision, applied,
                    created_at, updated_at, settled_by)
VALUES ('wd-old1', 's1', 'alpha', 'glossary', '{"term": "a", "definition": "b"}', 'r', 'confirmed', 3,
        '{"id": "g1"}', '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', 'user'),
       ('wd-old2', 's1', 'alpha', 'knowledge', '{"kind": "decision", "title": "t", "body": "b"}', '', 'draft', 1,
        NULL, '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:00+00:00', NULL);
INSERT INTO ended VALUES ('s1', '2026-10-03T00:00:00+00:00');
PRAGMA user_version = 1;
""")
    db.close()


def test_a_version_1_database_is_rebuilt_keeping_rows_triggers_and_index(tmp_path):
    path = tmp_path / "w.sqlite3"
    _v1_db(path)
    st = W.WrapupStore(path)
    db = sqlite3.connect(str(path))
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert {"drafts", "ended", "drafts_session", "drafts_revision_advances", "drafts_identity_fixed"} <= names
    assert "drafts_v1" not in names
    db.close()
    rows = st.list("s1")
    assert [d["id"] for d in rows["drafts"]] == ["wd-old1", "wd-old2"]
    assert rows["drafts"][0]["applied"] == {"id": "g1"} and rows["drafts"][0]["revision"] == 3
    assert rows["ended_at"] == "2026-10-03T00:00:00+00:00"
    # The new kind is accepted, and the revision trigger still guards the table.
    st.draft("s1", kind="retrospection", payload=RETRO, workspace="alpha")
    db = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.IntegrityError, match="advance revision"):
        db.execute("UPDATE drafts SET revision = 9 WHERE id = 'wd-old2'")
    db.close()
    W.WrapupStore(path)       # opening again is a no-op


def test_a_newer_database_is_refused(tmp_path):
    path = tmp_path / "w.sqlite3"
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA user_version = 3")
    db.close()
    with pytest.raises(W.WrapupError, match="schema version 3"):
        W.WrapupStore(path)


# -- the routes ------------------------------------------------------------

def run(tmp_path, scenario):
    workspaces.add(str(tmp_path), "alpha")
    mgr = FakeManager([FakeSession("s1", str(tmp_path), issue="claunch-x1")])

    async def go():
        app = web.Application()
        app["manager"] = mgr
        wrapup_api.install(app, W.WrapupStore(tmp_path / "wrapup.sqlite3"))
        retrospection_api.install(app)
        async with TestClient(TestServer(app)) as client:
            async def call(path, body=None, status=200):
                resp = await client.request("POST" if body is not None else "GET", path, json=body)
                doc = await resp.json()
                assert resp.status == status, doc
                return doc
            await scenario(call, mgr)
    asyncio.run(go())


def test_an_auto_retrospection_is_recorded_without_the_user(home, tmp_path):
    async def scenario(call, mgr):
        d = (await call("/api/wrapup", {"session": "s1", "kind": "retrospection", "payload": RETRO,
                                        "reason": "run-1 회고", "auto": True}))["draft"]
        assert d["status"] == "confirmed" and d["settled_by"] == W.AUTO
        rid = d["applied"]["id"]
        listed = (await call("/api/retrospection?workspace=alpha"))["records"]
        assert [r["id"] for r in listed] == [rid] and listed[0]["auto"] is True
        assert listed[0]["session"] == "s1" and listed[0]["draft"] == d["id"]
        rec = (await call(f"/api/retrospection/{rid}"))["record"]
        assert rec["body"] == RETRO["body"] and rec["problems"] == 2 and rec["proposals"] == 1
        assert (await call("/api/wrapup?session=s1"))["unresolved"] == 0
        assert mgr.get("s1").delivered == []    # its own request: nothing typed back
        facets = await call("/api/retrospection/facets")
        assert facets["workspaces"] == [{"name": "alpha", "count": 1}]
        await call("/api/retrospection/rt-none", status=404)
        await call("/api/retrospection?limit=0", status=400)
        await call("/api/wrapup", {"session": "s1", "kind": "knowledge", "auto": True,
                                   "payload": {"kind": "decision", "title": "t", "body": "b"}}, status=400)
    run(tmp_path, scenario)


def test_without_auto_the_retrospection_waits_for_the_users_confirm(home, tmp_path):
    async def scenario(call, mgr):
        d = (await call("/api/wrapup", {"session": "s1", "kind": "retrospection", "payload": RETRO,
                                        "auto": False}))["draft"]
        assert d["status"] == "draft"
        assert (await call("/api/retrospection"))["records"] == []
        assert (await call("/api/wrapup?session=s1"))["counts"]["retrospection"] == 1
        done = (await call(f"/api/wrapup/{d['id']}/confirm", {"revision": 1}))["draft"]
        assert done["status"] == "confirmed" and done["settled_by"] == W.USER
        listed = (await call("/api/retrospection"))["records"]
        assert len(listed) == 1 and listed[0]["auto"] is False
        assert "retrospective" in mgr.get("s1").delivered[-1]
    run(tmp_path, scenario)


def test_the_daemon_app_registers_the_retrospection_routes(home):
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def build():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        return build_app(mgr, "sekrit", started_at=time.monotonic())

    app = asyncio.run(build())
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    assert {("GET", "/api/retrospection"), ("GET", "/api/retrospection/facets"),
            ("GET", "/api/retrospection/{id}")} <= routes


# -- the MCP tool and the manual door ------------------------------------------

def test_the_mcp_tool_offers_the_kind_and_forwards_auto(monkeypatch):
    sent = []

    class Client:
        def post(self, path, body):
            sent.append((path, body))
            return {"ok": True}

    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    monkeypatch.setattr(wrapup_mcp.daemon_client, "connect_with_diagnosis", lambda: (Client(), None))
    tool = next(t for t in wrapup_mcp.TOOLS if t["name"] == "wrapup_draft")
    assert "retrospection" in tool["inputSchema"]["properties"]["kind"]["enum"]
    assert tool["inputSchema"]["properties"]["auto"]["type"] == "boolean"
    wrapup_mcp.call_tool("wrapup_draft", {"kind": "retrospection", "payload": RETRO, "auto": True})
    assert sent == [("/api/wrapup", {"kind": "retrospection", "payload": RETRO, "auto": True, "session": "s7"})]


def test_with_header_line_puts_the_line_inside_the_header():
    block = prompt_packs.on_demand_block(None, prompt_packs.RETROSPECTIVE)
    out = prompt_packs.with_header_line(block, "retrospection: trigger=manual, auto record=true")
    head, _, body = out.partition("\n---\n")
    assert head.endswith("auto record=true") and head.startswith("---\n# claunch prompt pack: retrospective")
    assert body == block.partition("\n---\n")[2]
    # The pack's record step names the line and the wrap-up kind.
    assert "auto record=" in body and "kind`: `retrospection`" in body


class _Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        return {}

    def post(self, path, body=None):
        self.calls.append(("POST", path))
        return {"delivered": True, "pack": "retrospective"}


def test_claunch_retro_review_asks_for_a_draft_and_print_names_no_value(monkeypatch):
    client = _Client()
    monkeypatch.setattr(daemon_client, "ensure_running", lambda: client)
    assert cli.main(["retro", "s1", "--review"]) == 0
    assert cli.main(["retro", "s1"]) == 0
    assert cli.main(["retro", "s1", "--print", "--review"]) == 0
    assert client.calls == [("POST", "/api/sessions/s1/retrospective?auto=0"),
                            ("POST", "/api/sessions/s1/retrospective"),
                            ("GET", "/api/sessions/s1/retrospective")]


def test_the_retrospective_route_heads_a_sent_block_with_the_auto_line(home, tmp_path):
    from claude_launcher.daemon import api

    class S:
        sdef = type("D", (), {"cwd": str(tmp_path), "name": "s1"})()

        def __init__(self):
            self.typed = []

        async def deliver(self, block):
            self.typed.append(block)
            return True

    session = S()

    class M:
        def get(self, name):
            return session

    async def go():
        app = web.Application()
        app["manager"] = M()
        app.router.add_route("*", "/api/sessions/{name}/retrospective", api.h_session_retrospective)
        async with TestClient(TestServer(app)) as client:
            got = await (await client.get("/api/sessions/s1/retrospective")).json()
            assert "auto record=" not in got["block"].partition("\n---\n")[0]
            sent = await (await client.post("/api/sessions/s1/retrospective")).json()
            assert sent["auto_record"] is True
            review = await (await client.post("/api/sessions/s1/retrospective?auto=0")).json()
            assert review["auto_record"] is False
    asyncio.run(go())
    heads = [b.partition("\n---\n")[0] for b in session.typed]
    assert heads[0].endswith("retrospection: trigger=manual, auto record=true")
    assert heads[1].endswith("retrospection: trigger=manual, auto record=false")


# -- the workflow input ------------------------------------------------------

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"


def test_improv_worker_v2_declares_the_input_and_the_node_passes_it():
    from claude_launcher.cflow2 import frontend
    doc = yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)
    spec = doc["inputs"]["retrospection_auto_record"]
    assert spec["type"] == "bool" and spec["default"] is True and spec["set_by"] == ["user"]
    node = doc["nodes"]["retrospective"]
    assert "${data.inputs.retrospection_auto_record}" in node["prompt"]
    assert "kind=retrospection" in node["prompt"] and "trigger: run" in node["prompt"]
    assert node["returns"]["ok"]["record"] == "str?"
