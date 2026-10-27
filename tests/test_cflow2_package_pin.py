"""claunch-nzrdb: the daemon serves the package's v2 workflows as they were
at its start, and says when the copy on disk has moved on.

The daemon runs an editable install, so its compiler is the code it imported
at start while the package YAML under it follows the checkout. A master
fast-forward that added a key to improv-worker-v2 and taught the compiler
that key in the same merge left the running daemon refusing the definition
("unknown key 'member_executor' in group") until it was restarted.
``layers.pin_package`` copies the package layer at the daemon's start;
``layers.drift_of`` / ``cflow2_web.drift_view`` report what changed since,
which the pickers and the run page show.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

import pytest

from claude_launcher.cflow2 import layers, runtime
from claude_launcher.daemon import __main__ as daemon_main
from claude_launcher.daemon import api as api_mod, cflow2_web, paths
from claude_launcher.daemon.manager import SessionManager

LIB = "cflow: 2\nname: lib\nkind: library\ndescription: shared\n"
FLOW = """\
cflow: 2
name: flow
include: [lib]
nodes:
  w: {title: W, prompt: work}
body: w
"""
OTHER = FLOW.replace("name: flow", "name: other").replace("include: [lib]\n", "")


@pytest.fixture
def package(home, tmp_path, monkeypatch):
    live = tmp_path / "package-v2"
    live.mkdir()
    (live / "lib.yaml").write_text(LIB, encoding="utf-8")
    (live / "flow.yaml").write_text(FLOW, encoding="utf-8")
    (live / "other.yaml").write_text(OTHER, encoding="utf-8")
    monkeypatch.setattr(layers, "live_package_dir", lambda: live)
    monkeypatch.setattr(layers, "_PINNED", None)
    monkeypatch.setattr(layers, "_LIVE", None)
    monkeypatch.setattr(layers, "_DEPS", {})
    monkeypatch.setattr(layers, "_COMPILED", OrderedDict())
    monkeypatch.setattr(cflow2_web, "_LISTS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_LISTS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_BASES", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return live, str(cwd), tmp_path / "pinned"


def test_unpinned_reads_the_package_directory(package):
    live, cwd, _ = package
    assert layers.package_dir() == live
    assert layers.package_drift() is None
    assert layers.drift_of("flow", cwd) == []


def test_pinned_copy_survives_a_change_on_disk(package):
    live, cwd, pinned = package
    assert layers.pin_package(pinned) == pinned
    assert layers.package_dir() == pinned
    assert sorted(p.name for p in pinned.glob("*.yaml")) == ["flow.yaml", "lib.yaml", "other.yaml"]
    # the checkout moves on: a key this compiler does not know
    (live / "flow.yaml").write_text(FLOW.replace("body: w", "body: w\nnew_key: 1"),
                                    encoding="utf-8")
    chart = layers.load("flow", cwd)
    assert chart.name == "flow"
    assert layers.drift_of("flow", cwd) == ["flow.yaml"]
    assert layers.drift_of("other", cwd) == []


def test_a_changed_include_is_drift_of_the_including_workflow(package):
    live, cwd, pinned = package
    layers.pin_package(pinned)
    (live / "lib.yaml").write_text(LIB + "# moved\n", encoding="utf-8")
    assert layers.drift_of("flow", cwd) == ["lib.yaml"]
    assert layers.drift_of("other", cwd) == []


def test_added_and_removed_files(package):
    live, _cwd, pinned = package
    layers.pin_package(pinned)
    (live / "fresh.yaml").write_text(OTHER.replace("name: other", "name: fresh"),
                                     encoding="utf-8")
    (live / "other.yaml").unlink()
    drift = layers.package_drift()
    assert drift["added"] == ["fresh.yaml"]
    assert drift["removed"] == ["other.yaml"]
    assert drift["changed"] == []
    assert drift["pinned_at"] > 0


def test_repin_replaces_the_previous_copy(package):
    live, _cwd, pinned = package
    layers.pin_package(pinned)
    (live / "other.yaml").unlink()
    layers.pin_package(pinned)
    assert not (pinned / "other.yaml").exists()
    assert layers.package_drift()["removed"] == []


def test_startable_and_drift_view_carry_the_drift(package):
    live, cwd, pinned = package
    layers.pin_package(pinned)
    before = {e["name"]: e for e in cflow2_web.startable(cwd)}
    assert "package_drift" not in before["flow"]
    (live / "flow.yaml").write_text(FLOW + "# moved\n", encoding="utf-8")
    after = {e["name"]: e for e in cflow2_web.startable(cwd)}
    assert after["flow"]["package_drift"]["files"] == ["flow.yaml"]
    assert after["flow"]["package_drift"]["pinned_at"] > 0
    assert "package_drift" not in after["other"]
    assert cflow2_web.drift_view("other", cwd) is None
    assert cflow2_web.drift_view("/abs/path.yaml", cwd) is None


def test_a_project_copy_is_not_drift(package, tmp_path):
    live, cwd, pinned = package
    v2 = tmp_path / "proj" / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "other.yaml").write_text(OTHER, encoding="utf-8")
    layers.pin_package(pinned)
    (live / "other.yaml").write_text(OTHER + "# moved\n", encoding="utf-8")
    assert layers.drift_of("other", cwd) == []


def _http(fn):
    """Run ``fn(client, bearer)`` against a real app with no sessions."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await fn(client, {"Authorization": "Bearer sekrit"})
        finally:
            await client.close()

    return asyncio.run(run())


def test_the_daemon_start_pin_reaches_the_pickers_and_the_run_page(package):
    """The daemon's start hook pins into its own directory, and what moves on
    disk afterwards is what the workflow list and the run page report."""
    live, cwd, _ = package
    daemon_main._pin_cflow2_package()
    assert layers.package_dir() == paths.cflow2_package_dir()
    runtime.start(layers.load("flow", cwd), cwd=cwd, scope="s1", caps={})
    (live / "flow.yaml").write_text(FLOW + "# moved\n", encoding="utf-8")
    (live / "fresh.yaml").write_text(OTHER.replace("name: other", "name: fresh"),
                                     encoding="utf-8")

    async def go(client, bearer):
        lst = await (await client.get(f"/api/cflow/workflows?cwd={cwd}",
                                      headers=bearer)).json()
        run = await (await client.get(f"/api/cflow2/run?cwd={cwd}&scope=s1",
                                      headers=bearer)).json()
        return lst, run

    lst, run = _http(go)
    assert lst["package_pin"]["changed"] == ["flow.yaml"]
    assert lst["package_pin"]["added"] == ["fresh.yaml"]
    v2 = {e["name"]: e for e in lst["workflows"] if e.get("version") == 2}
    assert v2["flow"]["package_drift"]["files"] == ["flow.yaml"]
    assert "fresh" not in v2
    assert run["package_drift"]["files"] == ["flow.yaml"]
