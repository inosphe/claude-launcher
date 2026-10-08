"""The session rail's cflow v2 line (``GET /api/cflow2/rail``).

Each live session whose own directory holds a v2 run under the session's own
name gets one entry, keyed by the session: the workflow, where the run stands
and -- the part the rail exists for -- whether a person owes it something
(``gated``) and what (``owed``). A user choice and a loop limit are owed; a
running task is not. Sessions without a run are left out, and an entry is
recomputed only when the run's state or chart file changed.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import layers, runtime
from claude_launcher.daemon import api as api_mod, cflow2_web
from claude_launcher.daemon.manager import SessionManager

PICK = """\
cflow: 2
name: pick
description: a user choice, then a bounded loop
nodes:
  a: {title: Step A, prompt: do a}
  b: {title: Step B, prompt: do b}
  c: {title: Step C, prompt: do c}
body:
  seq:
    - choose:
        prompt: which way?
        chooser: user
        options:
          left: {then: a}
          right: {then: b}
    - loop: {while: "true", max: 1, do: c}
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    v2 = d / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "pick.yaml").write_text(PICK, encoding="utf-8")
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(cflow2_web, "_RAIL", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(d)
    return d


def _rail(cwd, *names):
    return cflow2_web.rail_entries([(n, cwd) for n in names])


def test_rail_entry_follows_the_run_and_flags_what_a_person_owes(proj):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})

    rows = _rail(cwd, "s1", "s2")
    assert set(rows) == {"s1"}  # s2 has no run here
    r = rows["s1"]
    assert r["workflow"] == "pick" and r["status"] == "running"
    assert r["scope"] == "s1" and r["cwd"] == str(runtime.RunStore(cwd, "s1").cwd)
    assert r["gated"] is True and len(r["owed"]) == 1
    assert r["owed"][0].startswith("choose: ")

    runtime.select(None, "left", cwd=cwd, scope="s1", caller=runtime.CALLER_USER)
    r = _rail(cwd, "s1")["s1"]
    assert r["gated"] is False and r["owed"] == []
    assert r["current"] == ["Step A"]

    task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
    runtime.report(task, cwd=cwd, scope="s1")
    task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
    runtime.report(task, cwd=cwd, scope="s1")
    r = _rail(cwd, "s1")["s1"]
    assert r["status"] == "waiting_approval"
    assert r["gated"] is True
    assert any(o.startswith("loop_limit: ") for o in r["owed"])


def test_rail_entry_is_recomputed_only_when_the_run_files_change(proj, monkeypatch):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})
    calls = []
    real = cflow2_web.rail_entry
    monkeypatch.setattr(cflow2_web, "rail_entry",
                        lambda c, s: calls.append(s) or real(c, s))
    first = _rail(cwd, "s1")
    assert _rail(cwd, "s1") == first and calls == ["s1"]
    runtime.select(None, "left", cwd=cwd, scope="s1", caller=runtime.CALLER_USER)
    assert _rail(cwd, "s1")["s1"]["gated"] is False and calls == ["s1", "s1"]


def test_rail_endpoint_keys_entries_by_live_session(proj, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})
    sessions = [SimpleNamespace(sdef=SimpleNamespace(name=n, cwd=c))
                for n, c in (("s1", cwd), ("s2", cwd), ("s3", ""))]

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        monkeypatch.setattr(mgr, "list", lambda *a, **k: sessions)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            unauth = (await client.get("/api/cflow2/rail")).status
            resp = await client.get("/api/cflow2/rail",
                                    headers={"Authorization": "Bearer sekrit"})
            return unauth, resp.status, await resp.json()
        finally:
            await client.close()

    unauth, status, body = asyncio.run(run())
    assert unauth == 401 and status == 200
    assert set(body["rows"]) == {"s1"}
    assert body["rows"]["s1"]["workflow"] == "pick"
    assert body["rows"]["s1"]["gated"] is True
