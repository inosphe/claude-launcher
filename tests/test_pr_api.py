"""The PR wizard's two routes: the preview the form opens on, and the push.

The engine is covered in ``test_prflow.py``; this pins the wiring the form
depends on -- the preview is the session's own directory read through
``prflow.preview``; the POST hands the form to ``prflow.run`` and, when the
first checkbox is on, types the report into the session; the monitor checkbox is answered
with a warning and no spawn while there is no monitor workflow.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

from claude_launcher import lineage, prflow, profile, store
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

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


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _repo(path):
    def git(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)
    path.mkdir()
    git("init", "-b", "master")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-m", "seed")
    git("remote", "add", "gh", "https://ghe.example.com/o/r.git")
    return path


def test_preview_reads_the_sessions_directory(home, tmp_path):
    _register_py_harness()
    repo = _repo(tmp_path / "repo")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(repo)))
            resp = await client.get("/api/sessions/s1/pr/preview", headers=BEARER)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["repo"] is True
            assert doc["branch"] == "master"
            assert doc["head_subject"] == "seed"
            assert doc["branch_default"].startswith("s1-pr-")
            assert [r["remote"] for r in doc["remotes"]] == ["gh"]
            assert doc["remote"] == "gh"
            # no token, no gh: the preview says which, the form greys its button
            assert isinstance(doc["blockers"], list)

            resp = await client.get("/api/sessions/s1/pr/preview")
            assert resp.status == 401
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_post_runs_the_engine_and_reports_into_the_session(home, tmp_path, monkeypatch):
    _register_py_harness()
    repo = _repo(tmp_path / "repo")
    seen = {}

    def fake_run(cwd, request, *, session=""):
        seen["cwd"], seen["request"], seen["session"] = cwd, dict(request), session
        return {"ok": True, "cwd": cwd, "remote": "gh", "branch": request["branch"],
                "tip": "f" * 40,
                "snapshot": {"sha": "f" * 40, "parent": "f" * 40, "created": False, "files": 0},
                "pr": {"url": "https://ghe.example.com/o/r/pull/1", "number": 1,
                       "existed": False, "isDraft": False},
                "steps": [{"id": "inspect", "ok": True, "detail": ""}]}

    monkeypatch.setattr(prflow, "run", fake_run)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(repo)))
            session = mgr.get("s1")
            delivered = []

            async def capture(text, *, force=False):
                delivered.append(text)
                return True

            session.deliver = capture     # the door the report goes through

            form = {"remote": "gh", "branch": "s1-pr-1", "report": True, "monitor": True}
            resp = await client.post("/api/sessions/s1/pr", json=form, headers=BEARER)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["ok"] is True
            assert doc["pr"]["url"].endswith("/pull/1")
            assert doc["delivered"] is True
            # the form went through untouched, addressed to this session's directory
            assert seen["cwd"] == str(repo) and seen["session"] == "s1"
            assert seen["request"]["branch"] == "s1-pr-1"
            # checkbox 1: the block reached the session, and says the checkout stands
            assert len(delivered) == 1
            assert delivered[0].startswith("---\n# claunch pr:")
            assert "gh/s1-pr-1" in delivered[0]
            assert "checkout was not changed" in delivered[0]
            # checkbox 2: not yet -- said, and nothing spawned
            assert any(w.startswith("monitor:") for w in doc["warnings"])
            assert mgr.children("s1") == []

            # without the first checkbox nothing is typed
            delivered.clear()
            resp = await client.post("/api/sessions/s1/pr",
                                     json={"remote": "gh", "branch": "s1-pr-2"}, headers=BEARER)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["delivered"] is None and delivered == []
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_post_returns_a_failed_step_as_a_step_list(home, tmp_path, monkeypatch):
    _register_py_harness()
    repo = _repo(tmp_path / "repo")

    def fake_run(cwd, request, *, session=""):
        return {"ok": False, "cwd": cwd, "failed": "push", "error": "git push: rejected",
                "steps": [{"id": "inspect", "ok": True, "detail": ""},
                          {"id": "snapshot", "ok": True, "detail": ""},
                          {"id": "push", "ok": False, "detail": "git push: rejected"}]}

    monkeypatch.setattr(prflow, "run", fake_run)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(repo)))
            resp = await client.post("/api/sessions/s1/pr", json={"remote": "gh"}, headers=BEARER)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["ok"] is False and doc["failed"] == "push"
            assert [s["ok"] for s in doc["steps"]] == [True, True, False]
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
