"""claunch-cb65c.1: a session start compiles its v2 definitions off the
daemon loop, and once.

Creating a session in a new cwd used to compile every v2 layer file twice
(the start list, then each entry again), the chosen one a third time in
preflight and a fourth when the run started -- all on the event loop, the
thread every request and terminal waits on. ``layers`` now keeps compiled
charts by file identity, and ``api._onboard_and_launch`` fills that cache
in a worker thread before the synchronous calls read it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections import OrderedDict

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.cflow2 import frontend, layers
from claude_launcher.daemon import api as api_mod, cflow2_web
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = "import sys, time\nsys.stdout.write('ready\\n'); sys.stdout.flush()\ntime.sleep(60)\n"

FLOW = """\
cflow: 2
name: flow
nodes:
  w: {title: W, prompt: work}
body: w
"""
OTHER = FLOW.replace("name: flow", "name: other")


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    v2 = d / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "flow.yaml").write_text(FLOW, encoding="utf-8")
    (v2 / "other.yaml").write_text(OTHER, encoding="utf-8")
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(layers, "_COMPILED", OrderedDict(), raising=False)
    monkeypatch.setattr(cflow2_web, "_LISTS", {})
    monkeypatch.setattr(cflow2_web, "_DEFS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_LISTS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_BASES", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    store.update(lambda doc: doc.update(
        {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}))
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")
    return d


@pytest.fixture
def compiles(monkeypatch):
    """``(path name, on the main thread)`` for every compile."""
    seen = []
    real = frontend.compile_file

    def counted(path, *a, **kw):
        seen.append((os.path.basename(str(path)),
                     threading.current_thread() is threading.main_thread()))
        return real(path, *a, **kw)

    monkeypatch.setattr(frontend, "compile_file", counted)
    return seen


def _create(proj, body):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "w1", "profile": "py", "cwd": str(proj), "beads": False,
                **body})
            return resp.status, await resp.json()
        finally:
            await mgr.shutdown_all()
            await client.close()

    return asyncio.run(run())


def test_a_v2_session_start_compiles_nothing_on_the_loop(proj, compiles):
    status, doc = _create(proj, {"task": "go", "workflow": "flow",
                                 "workflow_version": 2})
    assert status == 201, doc
    assert doc["workflow"]["ok"] is True and doc["workflow"]["version"] == 2, doc
    assert compiles, "the start compiled nothing at all"
    assert [name for name, main in compiles if main] == []
    # each layer file once: the list, preflight and the run's start share it
    assert sorted(name for name, _ in compiles) == ["flow.yaml", "other.yaml"]


def test_a_second_start_in_another_cwd_reuses_the_compiles(proj, compiles, tmp_path):
    """A new worktree with no project layer of its own sees the same files."""
    from claude_launcher import config

    glob = config.launcher_home() / "workflows" / "v2"
    glob.mkdir(parents=True, exist_ok=True)
    (glob / "shared.yaml").write_text(FLOW.replace("name: flow", "name: shared"),
                                      encoding="utf-8")
    one, two = tmp_path / "wt1", tmp_path / "wt2"
    one.mkdir(), two.mkdir()
    cflow2_web.warm(str(one), "shared")
    cflow2_web.warm(str(two), "shared")
    assert [name for name, _ in compiles] == ["shared.yaml"]
    assert layers.load("shared", str(two)).name == "shared"
    assert len(compiles) == 1


def test_an_edited_layer_file_is_compiled_again(proj, compiles):
    cwd = str(proj)
    assert layers.load("flow", cwd).description == ""
    assert layers.load("flow", cwd) is layers.load("flow", cwd)
    path = proj / ".claunch" / "workflows" / "v2" / "flow.yaml"
    path.write_text(FLOW.replace("name: flow", "name: flow\ndescription: edited"),
                    encoding="utf-8")
    assert layers.load("flow", cwd).description == "edited"
    # another file in the layers changes what an include could resolve to
    before = len(compiles)
    (path.parent / "added.yaml").write_text(OTHER.replace("other", "added"),
                                            encoding="utf-8")
    layers.load("flow", cwd)
    assert len(compiles) == before + 1
