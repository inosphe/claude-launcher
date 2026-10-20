"""Files of any kind uploaded from the web UI (daemon/uploads.py, claunch-5gpcf).

Pinned here: an upload is streamed into the daemon's staging area under a
random id with its own (made-safe) name, refused past
``daemon.upload_max_bytes`` without leaving a partial file; a create request
naming the ids moves them into the new session's state directory and ends
its opening with one ``[claunch attachment]`` line per file; the session
line's upload writes straight into a running session's uploads and answers
the line; a thread message keeps its files 30 days, lists them in the block
typed into the session, serves them for download, and shows them expired
once the sweep removed them.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon import paths, threads_api, uploads
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.threads import store as S

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)
TRACE = b'{"traceEvents": []}' * 1000


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


async def _upload(client, data=TRACE, name="capture.json"):
    return await client.post("/api/uploads", params={"name": name}, data=data, headers=BEARER)


# -- names ------------------------------------------------------------------


@pytest.mark.parametrize("raw, want", [
    ("capture.utrace", "capture.utrace"),
    ("C:\\Users\\me\\trace 1.json", "trace 1.json"),
    ("../../etc/passwd", "passwd"),
    ("..", "upload"),
    ("", "upload"),
    ("a<b>:c|d?.pftrace", "a_b__c_d_.pftrace"),
    (".hidden", "hidden"),
])
def test_a_name_is_made_safe(raw, want):
    assert uploads.safe_name(raw) == want


def test_a_long_name_keeps_its_extension():
    name = uploads.safe_name("x" * 300 + ".utrace")
    assert len(name) == 120 and name.endswith(".utrace")


# -- staging ----------------------------------------------------------------


def test_an_upload_is_staged_under_the_daemon_with_its_name(home):
    async def body(client, mgr):
        resp = await _upload(client)
        assert resp.status == 201, await resp.text()
        doc = await resp.json()
        assert doc["name"] == "capture.json" and doc["bytes"] == len(TRACE)
        assert doc["max_bytes"] == uploads.DEFAULT_MAX_BYTES
        staged = uploads.stage_dir() / doc["id"] / "capture.json"
        assert staged.read_bytes() == TRACE
        assert staged.is_relative_to(paths.daemon_dir())

        other = await (await _upload(client)).json()
        assert other["id"] != doc["id"]
        assert (await _upload(client, data=b"")).status == 400

    _run(body)


def test_an_upload_over_the_configured_ceiling_is_refused_and_removed(home):
    store.update(lambda doc: doc.setdefault("daemon", {}).update({"upload_max_bytes": 1024}))

    async def body(client, mgr):
        resp = await _upload(client, data=b"x" * 5000)
        assert resp.status == 413
        assert "daemon.upload_max_bytes" in (await resp.json())["error"]
        folder = uploads.stage_dir()
        assert not folder.exists() or not list(folder.iterdir())

    _run(body)


def test_a_stale_staged_file_is_swept_by_the_next_upload(home, monkeypatch):
    first = uploads.stage_writer("a.bin")
    first.write(b"x")
    first = first.finish()
    monkeypatch.setattr(uploads, "STAGE_TTL_SECONDS", -1)
    second = uploads.stage_writer("b.bin")
    second.write(b"y")
    second.finish()
    names = {p.name for p in uploads.stage_dir().iterdir()}
    assert first.id not in names and second.id in names


# -- create request ---------------------------------------------------------


def test_opening_files_move_to_the_session_and_end_the_opening(home, tmp_path, monkeypatch):
    openings = {}
    real_build = harness_mod.build_command

    def spy_build(sdef, **kw):
        openings[sdef.name] = kw.get("opening", "")
        return real_build(sdef, **kw)

    monkeypatch.setattr(harness_mod, "build_command", spy_build)

    cwd = tmp_path / "repo"
    cwd.mkdir()

    async def body(client, mgr):
        a = (await (await _upload(client, name="frame.utrace")).json())["id"]
        b = (await (await _upload(client, name="cpu.json")).json())["id"]
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "u1", "profile": "py", "cwd": str(cwd),
            "task": "find the slow frame", "beads": False, "opening_files": [a, b],
        })
        doc = await resp.json()
        assert resp.status == 201, doc
        kept = [Path(p) for p in doc["opening_files"]]
        assert kept == [uploads.session_dir("u1") / a / "frame.utrace", uploads.session_dir("u1") / b / "cpu.json"]
        assert kept[0].read_bytes() == TRACE
        assert not (uploads.stage_dir() / a).exists()
        # Not the working directory.
        assert not any(cwd.iterdir())
        opening = openings["u1"]
        assert opening.index("find the slow frame") < opening.index("[claunch attachment] frame.utrace")
        assert f"[claunch attachment] cpu.json ({uploads.human_size(len(TRACE))}) {kept[1]}" in opening

    _run(body)


def test_an_unknown_file_id_is_refused_before_a_session_exists(home, tmp_path):
    async def body(client, mgr):
        for ids in (["0" * 20], ["../../etc"], "abc", ["0" * 20] * (uploads.MAX_FILES + 1)):
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "u2", "profile": "py", "cwd": str(tmp_path),
                "task": "look", "beads": False, "opening_files": ids,
            })
            assert resp.status == 400, (ids, await resp.text())
        assert "u2" not in {s.sdef.name for s in mgr.list()}

    _run(body)


# -- the session line -------------------------------------------------------


def test_the_session_line_upload_goes_straight_into_the_session(home, tmp_path):
    async def body(client, mgr):
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "u3", "profile": "py", "cwd": str(tmp_path), "beads": False,
        })
        assert resp.status == 201, await resp.text()
        resp = await client.post("/api/sessions/u3/uploads", params={"name": "mem.snap"},
                                 data=TRACE, headers=BEARER)
        doc = await resp.json()
        assert resp.status == 201, doc
        path = Path(doc["path"])
        assert path.parent.parent == uploads.session_dir("u3") and path.read_bytes() == TRACE
        assert doc["line"] == uploads.line("mem.snap", len(TRACE), path)
        assert (await client.post("/api/sessions/nope/uploads", params={"name": "x"},
                                  data=b"x", headers=BEARER)).status in (400, 404)
        assert not (uploads.session_dir("nope")).exists()

    _run(body)


# -- threads ----------------------------------------------------------------


class FakeSession:
    def __init__(self, name):
        from types import SimpleNamespace
        self.sdef = SimpleNamespace(name=name)
        self.exited = False
        self.held_at = None
        self.delivered = []

    def draft_open(self):
        return False

    def keyboard_busy(self):
        return False

    def queue_delivery(self, text):
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self):
        self.sessions = {"s1": FakeSession("s1")}

    def get(self, name):
        return self.sessions[name]

    def wake_soon(self, name, *, by="", deliver=""):
        return False


def _stage(name="trace.json", data=TRACE):
    w = uploads.stage_writer(name)
    w.write(data)
    return w.finish().id


def _threads(tmp_path, scenario):
    mgr = FakeManager()
    app = web.Application()
    app["manager"] = mgr
    threads_api.install(app, S.ThreadStore(tmp_path / "threads.sqlite3"))

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client, mgr)

    asyncio.run(go())


def test_thread_files_are_kept_listed_served_and_expire(home, tmp_path):
    async def scenario(client, mgr):
        fid = _stage()
        res = await (await client.post("/api/threads", json={"to": "s1", "title": "Perf", "files": [fid]})).json()
        tid = res["thread"]["id"]
        rec = res["message"]["files"][0]
        kept = uploads.thread_dir() / fid / "trace.json"
        assert rec == {"id": fid, "name": "trace.json", "bytes": len(TRACE), "path": str(kept)}
        assert res["message"]["body"] == "" and kept.read_bytes() == TRACE
        assert not (uploads.stage_dir() / fid).exists()
        block = mgr.sessions["s1"].delivered[-1]
        assert "  > (attachments only)" in block
        assert uploads.line("trace.json", len(TRACE), kept) in block

        resp = await client.get(f"/api/threads/{tid}/files/{fid}/trace.json")
        assert resp.status == 200 and await resp.read() == TRACE
        assert "attachment" in resp.headers["Content-Disposition"]
        assert (await client.get(f"/api/threads/{tid}/files/{fid}/other.json")).status == 404
        assert (await client.get(f"/api/threads/{tid}/files/{fid}/..%2F..%2Fthreads.sqlite3")).status == 404

        old = time.time() - uploads.THREAD_TTL_SECONDS - 60
        os.utime(uploads.thread_dir() / fid, (old, old))
        assert uploads.sweep_thread_files() == [fid]
        got = await (await client.get(f"/api/threads/{tid}")).json()
        assert got["messages"][0]["files"][0]["expired"] is True
        assert "path" not in got["messages"][0]["files"][0]
        assert (await client.get(f"/api/threads/{tid}/files/{fid}/trace.json")).status == 410

    _threads(tmp_path, scenario)


def test_a_refused_thread_post_puts_its_files_back(home, tmp_path):
    async def scenario(client, mgr):
        bad = await client.post("/api/threads/th-nope00/messages", json={"files": ["0" * 20]})
        assert bad.status == 400
        fid = _stage()
        resp = await client.post("/api/threads/th-nope00/messages", json={"text": "x", "files": [fid]})
        assert resp.status == 404
        assert (uploads.stage_dir() / fid / "trace.json").is_file()
        assert not (uploads.thread_dir() / fid).exists()

    _threads(tmp_path, scenario)


def test_a_v4_thread_database_gains_files_and_keeps_its_rows(tmp_path):
    path = tmp_path / "threads.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript("""
    CREATE TABLE threads (id TEXT PRIMARY KEY, session TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', opened_by TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
        agent_read INTEGER NOT NULL DEFAULT 0, qa TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL REFERENCES threads (id),
        session TEXT NOT NULL, author TEXT NOT NULL CHECK (author IN ('user', 'agent', 'system')),
        body TEXT NOT NULL, at TEXT NOT NULL, images TEXT,
        kind TEXT NOT NULL DEFAULT 'message' CHECK (kind IN ('message', 'ask', 'dispatch', 'system')),
        ref TEXT, data TEXT, CHECK (length(body) > 0 OR images IS NOT NULL));
    CREATE UNIQUE INDEX messages_ref ON messages (session, ref) WHERE ref IS NOT NULL;
    INSERT INTO threads VALUES ('th-aaaaaa', 's1', 'Old', 'open', 'user', 7, 0, NULL, 't', 't');
    INSERT INTO messages VALUES (7, 'th-aaaaaa', 's1', 'agent', 'hi', 't', NULL, 'ask', 'r1', '{"a": 1}');
    PRAGMA user_version = 4;
    """)
    db.close()
    st = S.ThreadStore(path)
    got = st.read("th-aaaaaa")["messages"]
    assert [(m["seq"], m["body"], m["kind"], m.get("ref"), m.get("data")) for m in got] == [
        (7, "hi", "ask", "r1", {"a": 1})]
    rec = {"id": "a" * 20, "name": "t.json", "bytes": 3}
    m = st.post(S.USER, "th-aaaaaa", "", files=[rec])["message"]
    assert m["seq"] == 8 and m["files"] == [rec]
    assert st.has_file("th-aaaaaa", "a" * 20, "t.json") and not st.has_file("th-aaaaaa", "a" * 20, "x")
    S.ThreadStore(path)
    with pytest.raises(S.Conflict):
        st.post("s1", "th-aaaaaa", "dup", ref="r1")
