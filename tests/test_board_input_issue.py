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


def _real_board(proj, *, board=True):
    """The real Board on an in-memory ``br`` (test_beads_daemon.FakeBr), with
    two issues on it. ``board=False`` is a directory with no board."""
    from test_beads_daemon import FakeBr

    br = FakeBr()
    br.add(id="claunch-held", title="held")
    br.add(id="claunch-typed", title="typed")
    if board:
        (proj / ".beads").mkdir(exist_ok=True)
        (proj / ".beads" / "beads.db").write_bytes(b"")
    return br, beads_mod.Board(br, root_for=lambda cwd: proj if board else None)


def _create(proj, body, *, board=True, spawn_from=None):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        br, real = _real_board(proj, board=board)
        app["beads"] = real
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            url, payload = "/api/sessions", {"name": "w1", "profile": "py",
                                             "cwd": str(proj), **body}
            if spawn_from:
                resp = await client.post("/api/sessions", headers=BEARER, json={
                    "name": spawn_from, "profile": "py", "cwd": str(proj),
                    "beads": False})
                assert resp.status == 201, await resp.json()
                url = f"/api/sessions/{spawn_from}/children"
                payload = {"name": "w1", **body}
            resp = await client.post(url, headers=BEARER, json=payload)
            doc = await resp.json()
            names = [s.sdef.name for s in mgr.list()]
            creates = [c for c in br.calls if "create" in c]
            return resp.status, doc, creates, names
        finally:
            await mgr.shutdown_all()
            await client.close()

    return asyncio.run(run())


def _run_inputs(proj):
    chart = RunStore(str(proj), "w1").load_state()
    return (chart.get("data") or {}).get("inputs")


V2 = {"workflow": "takes-issue", "workflow_version": 2}


def test_a_minted_issue_is_the_runs_input(proj):          # (a) Board new
    status, doc, creates, _ = _create(proj, {"task": "do it", **V2})
    assert status == 201, doc
    assert doc["workflow"]["ok"] is True, doc
    assert doc["beads"]["created"] is True and len(creates) == 1
    assert _run_inputs(proj)["issue"] == doc["beads"]["issue"]


def test_the_board_issue_overwrites_the_input(proj):      # (b) Board existing
    status, doc, creates, _ = _create(proj, {"task": "do it", "issue": "claunch-held", **V2})
    assert status == 201, doc
    assert creates == []
    assert _run_inputs(proj)["issue"] == "claunch-held"


def test_an_input_with_no_board_answer_is_adopted_not_minted(proj):  # (c)
    status, doc, creates, _ = _create(proj, {
        "task": "do it", **V2, "cflow_inputs": {"issue": "claunch-typed"}})
    assert status == 201, doc
    assert creates == []                                  # nothing minted
    assert doc["beads"]["issue"] == "claunch-typed"
    assert doc["beads"]["mode"] == beads_mod.TAKE
    assert _run_inputs(proj)["issue"] == "claunch-typed"


@pytest.mark.parametrize("board, match", [
    ({"issue": "claunch-held"}, "adopts 'claunch-held'"),
    ({"beads": False}, "asks for none"),
    ({"beads": "none-auto"}, "asks for none"),
    ({"beads": "existing-pending"}, "asks for none"),
    ({"issue_text": "write a new one"}, "issue_text"),
    ({"task": "do it\nissue: claunch-held"}, "names issue 'claunch-held'"),
])
def test_an_input_that_contradicts_the_board_is_refused(proj, board, match):  # (d)
    body = {"task": "do it", **V2, "cflow_inputs": {"issue": "claunch-typed"}, **board}
    status, doc, creates, names = _create(proj, body)
    assert status == 400, doc
    assert match in doc["error"]
    assert creates == [] and names == []   # refused before the board or a session


def test_the_same_issue_twice_is_not_a_contradiction(proj):
    status, doc, _, _ = _create(proj, {
        "task": "do it", "issue": "claunch-typed", **V2,
        "cflow_inputs": {"issue": "claunch-typed"}})
    assert status == 201, doc
    assert _run_inputs(proj)["issue"] == "claunch-typed"


@pytest.mark.parametrize("none", [False, "none-auto", "existing-pending"])
def test_no_issue_leaves_the_input_empty(proj, none):     # (e)
    status, doc, _, _ = _create(proj, {"task": "do it", "beads": none, **V2})
    assert status == 201, doc
    assert _run_inputs(proj)["issue"] == ""


def test_nothing_to_settle_leaves_the_input_empty(proj):
    status, doc, _, _ = _create(proj, dict(V2))
    assert status == 201, doc
    assert _run_inputs(proj)["issue"] == ""


def test_v1_no_workflow_and_no_issue_input_are_unchanged(proj):  # (f)
    for extra in ({"workflow": "old"}, {}):
        status, doc, creates, _ = _create(proj, {"task": "do it", **extra})
        assert status == 201, doc
        assert doc["beads"]["created"] is True and len(creates) == 1
    status, doc, _, _ = _create(proj, {
        "task": "do it", "workflow": "no-issue-input", "workflow_version": 2})
    assert status == 201, doc
    assert "issue" not in _run_inputs(proj)


# --- W1: a named id the board does not hold is refused, by every path ------


@pytest.mark.parametrize("body", [
    pytest.param({"task": "do it", **V2, "cflow_inputs": {"issue": "claunch-typo"}},
                 id="v2-input"),
    pytest.param({"task": "do it", "issue": "claunch-typo"}, id="board-pick-or-cli"),
])
def test_a_named_issue_not_on_the_board_is_refused(proj, body):
    status, doc, creates, names = _create(proj, body)
    assert status == 400, doc
    assert "'claunch-typo' is not on the board at" in doc["error"]
    assert str(proj) in doc["error"]
    assert creates == [] and names == []


@pytest.mark.parametrize("task", [
    "Known issue: flaky tests on CI, fix the retry",
    "do it\nissue: claunch-typo",
])
def test_a_reference_in_the_task_prose_is_never_refused(proj, task):
    """s88 P1: the task's `issue: <word>` is prose the reference pattern also
    matches. Only the `issue` field refuses; a reference read out of the
    text keeps the old answer -- logged, and the session starts without it
    (nothing named, so nothing is minted from that task either)."""
    status, doc, creates, names = _create(proj, {"task": task})
    assert status == 201, doc
    assert "w1" in names and creates == []


def test_a_spawn_naming_an_issue_not_on_the_board_is_refused(proj):
    # `claunch spawn --issue` and MCP spawn reach the daemon here
    status, doc, creates, names = _create(
        proj, {"task": "do it", "issue": "claunch-typo"}, spawn_from="lead")
    assert status == 400, doc
    assert "'claunch-typo' is not on the board" in doc["error"]
    assert "w1" not in names and creates == []


@pytest.mark.parametrize("how", ["no-board", "auto-off"])
def test_a_board_that_cannot_be_asked_starts_the_session_and_says_so(
        proj, how, monkeypatch):
    from claude_launcher.daemon import harness as harness_mod

    openings = {}
    real = harness_mod.build_command

    def spy(sdef, **kw):
        openings[sdef.name] = kw.get("opening", "")
        return real(sdef, **kw)

    monkeypatch.setattr(harness_mod, "build_command", spy)
    if how == "auto-off":
        store.update(lambda doc: doc.setdefault("daemon", {}).update(
            {"beads_auto_issue": False}))
    status, doc, _, names = _create(
        proj, {"task": "do it", **V2, "cflow_inputs": {"issue": "claunch-typed"}},
        board=(how != "no-board"))
    assert status == 201, doc
    assert "w1" in names
    beads = doc["beads"]
    assert beads["issue"] is None and beads["settled"] is False
    assert beads["input_issue"] == "claunch-typed"
    assert ("no board" in beads["why"]) if how == "no-board" \
        else ("beads_auto_issue is off" in beads["why"])
    assert _run_inputs(proj)["issue"] == ""
    assert "Issue claunch-typed was named for this session but is not linked" \
        in openings["w1"]


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

@pytest.mark.parametrize("how", ["no-board", "auto-off"])
def test_a_board_that_cannot_be_asked_does_not_name_a_prose_reference(
        proj, how, monkeypatch):
    """claunch-z7b0n.1 (s88 N1): with the board out of reach, a word the
    reference pattern read out of the task ("Known issue: flaky") is not
    reported as the session's named issue -- only the `issue` field is."""
    from claude_launcher.daemon import harness as harness_mod

    openings = {}
    real = harness_mod.build_command

    def spy(sdef, **kw):
        openings[sdef.name] = kw.get("opening", "")
        return real(sdef, **kw)

    monkeypatch.setattr(harness_mod, "build_command", spy)
    if how == "auto-off":
        store.update(lambda doc: doc.setdefault("daemon", {}).update(
            {"beads_auto_issue": False}))
    status, doc, _, names = _create(
        proj, {"task": "Known issue: flaky tests on CI, fix the retry", **V2},
        board=(how != "no-board"))
    assert status == 201, doc
    assert "w1" in names
    assert (doc.get("beads") or {}).get("input_issue") is None
    assert "was named for this session but is not linked" not in openings["w1"]
    assert _run_inputs(proj)["issue"] == ""
