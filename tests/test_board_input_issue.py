"""TEMPORARY (claunch-z7b0n): a v2 run's ``inputs.issue`` is the issue the
board settled when a session is created or spawned.

During the v1 -> v2 migration a v2 workflow still declares an ``issue``
start input next to the daemon's board link. ``_onboard_and_launch`` folds
an input that came with no board answer into the board answer (so it is
taken or joined, never minted again), refuses an input that contradicts the
board answer, and writes the board's issue -- or "" -- into the run's input.
Remove this file with ``beads.adopt_input_issue`` and the overwrite in
``api._onboard_and_launch``.
"""
from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.cflow2 import layers
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import api as api_mod, beads as beads_mod, cflow2_web
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}
CHILD = "import sys, time\nsys.stdout.write('ready\\n'); sys.stdout.flush()\ntime.sleep(60)\n"

TAKES_ISSUE = """\
cflow: 2
name: takes-issue
inputs:
  goal: {type: text, default: ""}
  issue: {type: string, default: ""}
nodes:
  w: {title: W, prompt: work}
body: w
"""
NO_ISSUE_INPUT = """\
cflow: 2
name: no-issue-input
inputs:
  goal: {type: text, default: ""}
nodes:
  w: {title: W, prompt: work}
body: w
"""
V1 = "name: old\ndescription: v1\nsteps:\n  one:\n    instructions: do one\n"


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    v1 = d / ".claunch" / "workflows"
    (v1 / "v2").mkdir(parents=True)
    (v1 / "old.yaml").write_text(V1, encoding="utf-8")
    (v1 / "v2" / "takes-issue.yaml").write_text(TAKES_ISSUE, encoding="utf-8")
    (v1 / "v2" / "no-issue-input.yaml").write_text(NO_ISSUE_INPUT, encoding="utf-8")
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
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


class FakeBoard:
    """``ensure_issue`` with the decision shape of the real one: a named
    issue (``body["issue"]``) is taken, a task with no name is minted, a
    "no issue" answer is nothing. Records every body it was handed."""

    def __init__(self):
        self.bodies = []

    async def ensure_issue(self, session, *, body, parent=None, manager=None):
        self.bodies.append(dict(body))
        if beads_mod.none_mode(body):
            return None
        named = str(body.get("issue") or "").strip()
        if named:
            return {"issue": named, "created": False, "mode": beads_mod.TAKE,
                    "held_by": None, "why": "named"}
        if str(body.get("task") or "").strip():
            return {"issue": "claunch-minted", "created": True,
                    "mode": beads_mod.MINTED, "held_by": None, "why": "task"}
        return None

    def __getattr__(self, name):  # anything else the app asks of the board
        async def nothing(*a, **k):
            return None
        return nothing


def _create(proj, body):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        board = FakeBoard()
        app["beads"] = board
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "w1", "profile": "py", "cwd": str(proj), **body})
            doc = await resp.json()
            names = [s.sdef.name for s in mgr.list()]
            return resp.status, doc, board.bodies, names
        finally:
            await mgr.shutdown_all()
            await client.close()

    return asyncio.run(run())


def _run_inputs(proj):
    chart = RunStore(str(proj), "w1").load_state()
    return (chart.get("data") or {}).get("inputs")


def test_a_minted_issue_is_the_runs_input(proj):          # (a) Board new
    status, doc, bodies, _ = _create(proj, {
        "task": "do it", "workflow": "takes-issue", "workflow_version": 2})
    assert status == 201, doc
    assert doc["workflow"]["ok"] is True, doc
    assert _run_inputs(proj)["issue"] == "claunch-minted"


def test_the_board_issue_overwrites_the_input(proj):      # (b) Board existing
    status, doc, bodies, _ = _create(proj, {
        "task": "do it", "issue": "claunch-held", "workflow": "takes-issue",
        "workflow_version": 2})
    assert status == 201, doc
    assert _run_inputs(proj)["issue"] == "claunch-held"


def test_an_input_with_no_board_answer_is_adopted_not_minted(proj):  # (c)
    status, doc, bodies, _ = _create(proj, {
        "task": "do it", "workflow": "takes-issue", "workflow_version": 2,
        "cflow_inputs": {"issue": "claunch-typed"}})
    assert status == 201, doc
    assert bodies[0]["issue"] == "claunch-typed"   # the board saw it as named
    assert doc["beads"]["mode"] == beads_mod.TAKE and doc["beads"]["created"] is False
    assert _run_inputs(proj)["issue"] == "claunch-typed"


@pytest.mark.parametrize("board, match", [
    ({"issue": "claunch-other"}, "adopts 'claunch-other'"),
    ({"beads": False}, "asks for none"),
    ({"beads": "none-auto"}, "asks for none"),
    ({"issue_text": "write a new one"}, "issue_text"),
    ({"task": "do it\nissue: claunch-other"}, "names issue 'claunch-other'"),
])
def test_an_input_that_contradicts_the_board_is_refused(proj, board, match):  # (d)
    body = {"task": "do it", "workflow": "takes-issue", "workflow_version": 2,
            "cflow_inputs": {"issue": "claunch-typed"}, **board}
    status, doc, bodies, names = _create(proj, body)
    assert status == 400, doc
    assert match in doc["error"]
    assert bodies == [] and names == []   # refused before the board or a session


def test_the_same_issue_twice_is_not_a_contradiction(proj):
    status, doc, _, _ = _create(proj, {
        "task": "do it", "issue": "claunch-typed", "workflow": "takes-issue",
        "workflow_version": 2, "cflow_inputs": {"issue": "claunch-typed"}})
    assert status == 201, doc
    assert _run_inputs(proj)["issue"] == "claunch-typed"


@pytest.mark.parametrize("none", [False, "none-auto"])
def test_no_issue_leaves_the_input_empty(proj, none):     # (e)
    status, doc, _, _ = _create(proj, {
        "task": "do it", "beads": none, "workflow": "takes-issue",
        "workflow_version": 2})
    assert status == 201, doc
    assert _run_inputs(proj)["issue"] == ""


def test_an_issue_that_could_not_be_settled_leaves_the_input_empty(proj):
    # no task, no name: the board settles nothing, and the run must not keep
    # a value the board did not stand behind
    status, doc, _, _ = _create(proj, {"workflow": "takes-issue", "workflow_version": 2})
    assert status == 201, doc
    assert _run_inputs(proj)["issue"] == ""


def test_v1_no_workflow_and_no_issue_input_are_unchanged(proj):  # (f)
    for extra in ({"workflow": "old"}, {}):
        status, doc, bodies, _ = _create(proj, {"task": "do it", **extra})
        assert status == 201, doc
        assert "issue" not in bodies[0]
    status, doc, bodies, _ = _create(proj, {
        "task": "do it", "workflow": "no-issue-input", "workflow_version": 2})
    assert status == 201, doc
    assert "issue" not in bodies[0]
    assert "issue" not in _run_inputs(proj)


def test_adopt_input_issue_rules():
    body = {"task": "t"}
    beads_mod.adopt_input_issue(body, {"issue": " claunch-x "})
    assert body["issue"] == "claunch-x"
    body = {"task": "t"}
    beads_mod.adopt_input_issue(body, {"issue": ""})
    beads_mod.adopt_input_issue(body, None)
    assert "issue" not in body
    body = {"task": "issue: claunch-x"}
    beads_mod.adopt_input_issue(body, {"issue": "claunch-x"})   # agrees
    assert body["issue"] == "claunch-x"
