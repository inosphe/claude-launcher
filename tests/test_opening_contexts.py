"""Context items beside the opening task (daemon/opening_contexts.py, gds6-bfu7).

Pinned here: a create request's ``opening_contexts`` is checked before a
session exists (shape, count, sizes, blank items) and refused with the
daemon's reason; the items land as one file each in the session's state
directory with an index, the opening names each with a ``[claunch context]``
line after the task and ahead of uploaded files, and the definition keeps no
copy of their text; the routes list them and page through one; the MCP
``spawn`` tool forwards them and ``context_list``/``context_read`` read them
through the daemon.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import contexts_mcp, lineage, mcp_server, mesh_mcp, profile, store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon import opening_contexts as oc
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)
LOG = "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: boom\n"


def _register():
    store.update(lambda doc: doc.update({"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}))
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _run(body):
    _register()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await body(client, mgr)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# -- checking a request -------------------------------------------------------


def test_nothing_given_is_no_items():
    for raw in (None, "", []):
        assert oc.check(raw) == []


def test_items_are_kept_in_order_with_titles_tidied():
    items = oc.check([{"title": "  pytest   log ", "text": LOG}, "a bare string is its text"])
    assert items == [oc.Item("pytest log", LOG), oc.Item("", "a bare string is its text")]


@pytest.mark.parametrize("raw, said", [
    ("one log", "must be a list"),
    ([{"title": "t"}], "must be text"),
    ([{"title": 3, "text": "x"}], "must be text"),
    ([42], "must be an object"),
    ([{"title": "empty", "text": "  \n "}], "context 1 (empty) is empty"),
    ([{"title": "x" * (oc.MAX_TITLE + 1), "text": "x"}], "a title is at most"),
    ([{"text": "x"}] * (oc.MAX_ITEMS + 1), f"at most {oc.MAX_ITEMS} context items"),
    ([{"title": "big", "text": "x" * (oc.MAX_ITEM_BYTES + 1)}], "attach a larger log as a file"),
    ([{"text": "x" * oc.MAX_ITEM_BYTES}, {"text": "y" * (oc.MAX_TOTAL_BYTES - oc.MAX_ITEM_BYTES + 1)}],
     "together they are at most"),
])
def test_a_bad_request_is_refused_with_its_reason(raw, said):
    with pytest.raises(oc.ContextError, match=re.escape(said)):
        oc.check(raw)


def test_sizes_count_utf8_bytes():
    # 3 bytes a character: under the limit in characters, over it in bytes.
    text = "가" * (oc.MAX_ITEM_BYTES // 3 + 1)
    with pytest.raises(oc.ContextError, match=re.escape("is 256.0 KiB")):
        oc.check([{"text": text}])


@pytest.mark.parametrize("title, want", [
    ("pytest failure log", "pytest-failure-log"),
    ("../../etc/passwd", "etc-passwd"),
    ("에러 로그", "에러-로그"),
    ("", "context"),
    ("***", "context"),
    ("x" * 80, "x" * 40),
])
def test_a_title_becomes_a_safe_file_name_part(title, want):
    assert oc.slug(title) == want


# -- storing and reading -------------------------------------------------------


def test_items_are_stored_listed_and_read_in_windows(home):
    recs = oc.store("c1", [oc.Item("pytest log", LOG), oc.Item("", "line one\nline two")])
    folder = oc.contexts_dir("c1")
    assert [r["file"] for r in recs] == ["01-pytest-log.txt", "02-context.txt"]
    assert (folder / "01-pytest-log.txt").read_text(encoding="utf-8") == LOG
    assert recs[0]["lines"] == 3 and recs[1]["lines"] == 2
    assert recs[0]["path"] == str(folder / "01-pytest-log.txt")
    assert oc.listing("c1") == recs

    whole = oc.read("c1", 1)
    assert whole["text"] == LOG and not whole["truncated"] and whole["next_offset"] is None
    first = oc.read("c1", 1, max_chars=10)
    assert first["text"] == LOG[:10] and first["truncated"] and first["next_offset"] == 10
    rest = oc.read("c1", 1, offset=first["next_offset"])
    assert first["text"] + rest["text"] == LOG

    with pytest.raises(oc.ContextError, match="no context item 3"):
        oc.read("c1", 3)


def test_a_new_session_of_the_same_name_replaces_the_old_items(home):
    oc.store("c2", [oc.Item("a", "1"), oc.Item("b", "2")])
    oc.store("c2", [oc.Item("c", "3")])
    assert [r["title"] for r in oc.listing("c2")] == ["c"]
    assert sorted(p.name for p in oc.contexts_dir("c2").iterdir()) == ["01-c.txt", oc.INDEX]
    oc.store("c2", [])
    assert oc.listing("c2") == [] and not oc.contexts_dir("c2").exists()


def test_a_file_gone_from_disk_is_listed_as_missing(home):
    recs = oc.store("c3", [oc.Item("a", "1")])
    Path(recs[0]["path"]).unlink()
    assert oc.listing("c3")[0]["missing"] is True
    with pytest.raises(oc.ContextError, match="no longer on disk"):
        oc.read("c3", 1)


def test_an_index_naming_a_path_outside_the_folder_is_ignored(home):
    folder = oc.contexts_dir("c4")
    folder.mkdir(parents=True)
    (folder / oc.INDEX).write_text(json.dumps({"items": [
        {"n": 1, "title": "x", "file": "../opening.json"}, "junk"]}), encoding="utf-8")
    assert oc.listing("c4") == []


def test_the_opening_block_names_each_item_and_how_to_read_it(home):
    recs = oc.store("c5", [oc.Item("pytest log", LOG), oc.Item("", "notes")])
    text = oc.block(recs)
    assert "2 context items" in text
    assert "`context_read`" in text and "`context_list`" in text
    assert f"[claunch context] 1. pytest log ({oc.human_size(len(LOG))}, 3 lines) {recs[0]['path']}" in text
    assert f"[claunch context] 2. context 2 (5 B, 1 lines) {recs[1]['path']}" in text
    assert oc.block([]) == ""


# -- the create request ----------------------------------------------------------


def test_a_create_stores_the_items_and_ends_the_opening_with_them(home, tmp_path, monkeypatch):
    openings, definitions = {}, {}
    real_build = harness_mod.build_command

    def spy_build(sdef, **kw):
        openings[sdef.name] = kw.get("opening", "")
        definitions[sdef.name] = sdef
        return real_build(sdef, **kw)

    monkeypatch.setattr(harness_mod, "build_command", spy_build)
    cwd = tmp_path / "repo"
    cwd.mkdir()

    async def body(client, mgr):
        up = await client.post("/api/uploads", params={"name": "trace.json"}, data=b"{}", headers=BEARER)
        file_id = (await up.json())["id"]
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "x1", "profile": "py", "cwd": str(cwd), "task": "fix the failing test",
            "beads": False, "opening_files": [file_id],
            "opening_contexts": [{"title": "pytest log", "text": LOG}, {"text": "spec excerpt"}],
        })
        doc = await resp.json()
        assert resp.status == 201, doc
        kept = [Path(p) for p in doc["opening_contexts"]]
        assert kept == [oc.contexts_dir("x1") / "01-pytest-log.txt", oc.contexts_dir("x1") / "02-context.txt"]
        assert kept[0].read_text(encoding="utf-8") == LOG
        # Not the working directory.
        assert not any(cwd.iterdir())
        opening = openings["x1"]
        task_at = opening.index("fix the failing test")
        ctx_at = opening.index("[claunch context] 1. pytest log")
        file_at = opening.index("[claunch attachment] trace.json")
        assert task_at < ctx_at < file_at
        # The restart record carries no copy of the text.
        assert LOG not in json.dumps(definitions["x1"].to_dict(), ensure_ascii=False)

        listed = await (await client.get("/api/sessions/x1/contexts", headers=BEARER)).json()
        assert [(i["n"], i["title"]) for i in listed["items"]] == [(1, "pytest log"), (2, "")]
        one = await client.get("/api/sessions/x1/contexts/1", headers=BEARER, params={"max_chars": "9"})
        part = await one.json()
        assert one.status == 200 and part["text"] == LOG[:9] and part["next_offset"] == 9
        assert (await client.get("/api/sessions/x1/contexts/7", headers=BEARER)).status == 404
        assert (await client.get("/api/sessions/x1/contexts/one", headers=BEARER)).status == 400

        # The recorded opening (re-inject, Copy) carries the same lines.
        recorded = await (await client.get("/api/sessions/x1/opening", headers=BEARER)).json()
        assert "[claunch context] 2. context 2" in recorded["text"]

    _run(body)


def test_a_bad_item_is_refused_before_a_session_exists(home, tmp_path):
    async def body(client, mgr):
        for items in ("a log", [{"title": "empty", "text": ""}], [{"text": "x"}] * (oc.MAX_ITEMS + 1)):
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "x2", "profile": "py", "cwd": str(tmp_path),
                "task": "look", "beads": False, "opening_contexts": items,
            })
            assert resp.status == 400, (items, await resp.text())
        assert "x2" not in {s.sdef.name for s in mgr.list()}
        assert not oc.contexts_dir("x2").exists()

    _run(body)


def test_a_session_without_items_lists_none(home, tmp_path):
    async def body(client, mgr):
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "x3", "profile": "py", "cwd": str(tmp_path), "task": "look", "beads": False,
        })
        assert resp.status == 201, await resp.text()
        listed = await (await client.get("/api/sessions/x3/contexts", headers=BEARER)).json()
        assert listed == {"session": "x3", "items": []}

    _run(body)


def test_a_root_spawn_carries_the_items_to_its_session_spec():
    assert "opening_contexts" in api_mod.ROOT_SPEC_KEYS


# -- MCP -------------------------------------------------------------------------


def test_the_spawn_tool_offers_and_forwards_the_items(monkeypatch):
    spawn = next(t for t in mesh_mcp.TOOLS if t["name"] == "spawn")
    assert spawn["inputSchema"]["properties"]["opening_contexts"]["type"] == "array"
    sent = {}

    class Client:
        def post(self, path, payload):
            sent.update(path=path, payload=payload)
            return {"ok": True}

    monkeypatch.setattr(mesh_mcp, "_client", lambda: Client())
    monkeypatch.setattr(mesh_mcp, "_session", lambda: "boss")
    items = [{"title": "log", "text": LOG}]
    mesh_mcp._spawn({"task": "fix it", "opening_contexts": items})
    assert sent["path"] == "/api/sessions/boss/children"
    assert sent["payload"]["opening_contexts"] == items


def test_the_context_tools_read_through_the_daemon(monkeypatch):
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"context_list", "context_read"} <= names
    asked = []

    class Client:
        def get(self, path):
            asked.append(path)
            return {"path": path}

    monkeypatch.setattr(contexts_mcp, "_client", lambda: Client())
    monkeypatch.setenv("CLAUNCH_SESSION", "me")
    contexts_mcp.call_tool("context_list", {})
    contexts_mcp.call_tool("context_read", {"n": 2, "offset": 10, "max_chars": 5})
    contexts_mcp.call_tool("context_read", {"n": "1", "session": "other one"})
    assert asked == [
        "/api/sessions/me/contexts",
        "/api/sessions/me/contexts/2?offset=10&max_chars=5",
        "/api/sessions/other%20one/contexts/1",
    ]
    with pytest.raises(contexts_mcp.ContextMcpError, match="needs 'n'"):
        contexts_mcp.call_tool("context_read", {})
    monkeypatch.delenv("CLAUNCH_SESSION")
    with pytest.raises(contexts_mcp.ContextMcpError, match="name a session"):
        contexts_mcp.call_tool("context_list", {})
