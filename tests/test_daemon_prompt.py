"""The daemon prompt: one text per daemon instance, in every session it starts.

The module half (save, backup, log, the length limit, one instance never
seeing another's), the HTTP half (Settings' GET/PUT, the create form's offer,
a create or spawn whose opening carries it unless told not to), and the two
doors agents use: the MCP tool and the CLI's ``--no-daemon-prompt``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import lineage, mesh_mcp, profile, prompt_packs, store, workspaces
from claude_launcher.daemon import daemon_prompt, harness as harness_mod, paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)
HEAD = "# claunch prompt pack: daemon"


# --------------------------------------------------------------------------- #
# module
# --------------------------------------------------------------------------- #

def test_write_keeps_the_text_it_replaced_and_logs_who(tmp_path):
    assert daemon_prompt.read(tmp_path)["enabled"] is False
    assert daemon_prompt.pack(tmp_path) is None

    doc = daemon_prompt.write("  rule one\r\nrule two  ", by="web", base=tmp_path)
    assert doc["text"] == "rule one\nrule two"
    assert doc["enabled"] is True and doc["chars"] == len("rule one\nrule two")
    assert not (tmp_path / daemon_prompt.PREV_FILE).exists()

    daemon_prompt.write("rule three", by="s1", base=tmp_path)
    prev = (tmp_path / daemon_prompt.PREV_FILE).read_text(encoding="utf-8")
    assert prev.strip() == "rule one\nrule two"

    # The same text again is not an edit: nothing logged, backup untouched.
    daemon_prompt.write("rule three\n", by="s2", base=tmp_path)
    log = [json.loads(line) for line in
           (tmp_path / daemon_prompt.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert [e["by"] for e in log] == ["web", "s1"]
    assert log[1]["previous_chars"] == len("rule one\nrule two")

    # Empty turns it off and still keeps what it was.
    doc = daemon_prompt.write("", by="s3", base=tmp_path)
    assert doc["enabled"] is False and daemon_prompt.pack(tmp_path) is None
    assert (tmp_path / daemon_prompt.PREV_FILE).read_text(encoding="utf-8").strip() == "rule three"


def test_too_long_or_not_text_is_refused_and_nothing_changes(tmp_path):
    daemon_prompt.write("keep me", by="web", base=tmp_path)
    with pytest.raises(daemon_prompt.DaemonPromptError):
        daemon_prompt.write("x" * (daemon_prompt.MAX_CHARS + 1), by="web", base=tmp_path)
    with pytest.raises(daemon_prompt.DaemonPromptError):
        daemon_prompt.write(None, by="web", base=tmp_path)
    assert daemon_prompt.text(tmp_path) == "keep me"


def test_each_daemon_instance_has_its_own(home, monkeypatch):
    monkeypatch.delenv(paths.INSTANCE_ENV, raising=False)
    daemon_prompt.write("default's rule", by="web")
    default_path = daemon_prompt.path()

    monkeypatch.setenv(paths.INSTANCE_ENV, "other")
    assert daemon_prompt.path() != default_path
    assert daemon_prompt.text() == ""
    daemon_prompt.write("other's rule", by="web")
    assert daemon_prompt.pack().title == "Daemon prompt (other)"

    monkeypatch.delenv(paths.INSTANCE_ENV)
    assert daemon_prompt.text() == "default's rule"
    assert daemon_prompt.pack().title == "Daemon prompt (default)"


def test_wanted_is_yes_unless_the_request_says_no():
    assert daemon_prompt.wanted({}) is True
    assert daemon_prompt.wanted({"daemon_prompt": True}) is True
    for no in (False, None, 0, "false", "off", "0", ""):
        assert daemon_prompt.wanted({"daemon_prompt": no}) is False, no


def test_the_opening_names_it_as_this_daemons_prompt(tmp_path):
    daemon_prompt.write("be brief", by="web", base=tmp_path)
    p = daemon_prompt.pack(tmp_path)
    block = prompt_packs.opening_block(p.id, p.body, p.source)
    assert "this daemon's own prompt" in block
    assert "be brief" in block
    rec = prompt_packs.records([daemon_prompt.pack(tmp_path)])[0]
    assert rec["id"] == "daemon" and rec["source"] == prompt_packs.DAEMON


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

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


def _spy_openings():
    seen: dict = {}
    real = harness_mod.build_command

    def spy(sdef, **kw):
        seen[sdef.name] = kw.get("opening", "")
        return real(sdef, **kw)

    harness_mod.build_command = spy
    return seen, lambda: setattr(harness_mod, "build_command", real)


def test_settings_routes_read_and_replace_it(home, tmp_path):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr, None)
        try:
            doc = await (await client.get("/api/daemon-prompt", headers=BEARER)).json()
            assert doc["enabled"] is False and doc["instance"] == "default"
            assert doc["path"] == str(paths.daemon_dir() / daemon_prompt.FILE)

            resp = await client.put("/api/daemon-prompt", headers=BEARER,
                                    json={"text": "always cite files"})
            doc = await resp.json()
            assert resp.status == 200 and doc["text"] == "always cite files"
            log = (paths.daemon_dir() / daemon_prompt.LOG_FILE).read_text(encoding="utf-8")
            assert json.loads(log.splitlines()[-1])["by"] == "web"

            resp = await client.put("/api/daemon-prompt", headers=BEARER,
                                    json={"text": "x" * (daemon_prompt.MAX_CHARS + 1)})
            assert resp.status == 400
            assert daemon_prompt.text() == "always cite files"

            offer = await (await client.get(
                "/api/prompt-packs/offer", params={"cwd": str(tmp_path)},
                headers=BEARER,
            )).json()
            assert offer["daemon_prompt"]["id"] == "daemon"
            assert offer["daemon_prompt"]["default"] is True
        finally:
            await client.close()

    asyncio.run(run())


def test_every_session_gets_it_unless_the_request_says_no(home, tmp_path):
    _register_py_harness()
    ws = tmp_path / "proj"
    ws.mkdir()
    workspaces.add(str(ws), "proj")
    daemon_prompt.write("DAEMON RULE: cite files", by="web")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        seen, restore = _spy_openings()
        try:
            # No pack ticked, no role: it goes in anyway, ahead of the task.
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "d1", "profile": "py", "cwd": str(ws),
                "task": "do the thing", "beads": False,
            })
            doc = await resp.json()
            assert resp.status == 201, doc
            opening = seen["d1"]
            assert opening.index("DAEMON RULE") < opening.index("do the thing")
            assert f"{HEAD} -- this daemon's own prompt" in opening
            recorded = mgr.get("d1").sdef.prompt_packs
            assert [r["id"] for r in recorded] == ["daemon"]

            # First of the packs when others are ticked too.
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "d2", "profile": "py", "cwd": str(ws),
                "task": "x", "beads": False, "prompt_packs": ["beads"],
            })
            assert resp.status == 201, await resp.json()
            assert [r["id"] for r in mgr.get("d2").sdef.prompt_packs] == ["daemon", "beads"]

            # Unticked: left out.
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "d3", "profile": "py", "cwd": str(ws),
                "task": "x", "beads": False, "daemon_prompt": False,
            })
            assert resp.status == 201, await resp.json()
            assert "DAEMON RULE" not in seen["d3"]

            # A spawned child too, and its own "no" is honoured.
            resp = await client.post("/api/sessions/d1/children", headers=BEARER,
                                     json={"name": "d4", "task": "help"})
            assert resp.status == 201, await resp.json()
            assert "DAEMON RULE" in seen["d4"]
            resp = await client.post("/api/sessions/d1/children", headers=BEARER,
                                     json={"name": "d5", "task": "help",
                                           "daemon_prompt": False})
            assert resp.status == 201, await resp.json()
            assert "DAEMON RULE" not in seen["d5"]
        finally:
            restore()
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# agents' doors
# --------------------------------------------------------------------------- #

def test_the_mcp_tool_reads_and_writes_through_the_daemon(monkeypatch):
    calls = []

    class FakeClient:
        def get(self, path):
            calls.append(("GET", path, None))
            return {"text": "t"}

        def put(self, path, body):
            calls.append(("PUT", path, body))
            return {"text": body["text"]}

        def post(self, path, body):
            calls.append(("POST", path, body))
            return {}

    monkeypatch.setattr(mesh_mcp, "_client", lambda: FakeClient())
    monkeypatch.setattr(mesh_mcp, "_session", lambda: "s9")
    assert "daemon_prompt" in [t["name"] for t in mesh_mcp.TOOLS]

    mesh_mcp.call_tool("daemon_prompt", {})
    mesh_mcp.call_tool("daemon_prompt", {"op": "set", "text": "new rule"})
    assert calls == [
        ("GET", "/api/daemon-prompt", None),
        ("PUT", "/api/daemon-prompt", {"text": "new rule", "by": "s9"}),
    ]
    with pytest.raises(mesh_mcp.MeshMcpError):
        mesh_mcp.call_tool("daemon_prompt", {"op": "set"})

    # spawn: saying nothing sends nothing; the switch sends the False.
    mesh_mcp._spawn({"task": "go"})
    assert "daemon_prompt" not in calls[-1][2]
    mesh_mcp._spawn({"task": "go", "no_daemon_prompt": True})
    assert calls[-1][2]["daemon_prompt"] is False


def test_the_cli_flag_parses_on_new_session_and_spawn():
    from claude_launcher import cli

    parser = cli.build_parser()
    for cmd in (["new-session"], ["spawn"]):
        assert parser.parse_args(cmd).no_daemon_prompt is False
        assert parser.parse_args(cmd + ["--no-daemon-prompt"]).no_daemon_prompt is True
