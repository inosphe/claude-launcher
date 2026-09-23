"""A parent run's landing queue: its children's landing requests as run state.

improv-leader used to keep "who asked to land, what is waiting, what was put
off to the next round" in its driving agent's memory, and lost entries when
that memory did. ``landing_queue:`` makes it run state. These tests pin:

* the declaration and its refusals;
* an entry per issue, filed by the daemon for a child's ``enqueue-landing``
  trigger (branch and tip read from the child's checkout, issues from the
  board), renewed on a re-request and unchanged on a repeat;
* the agent's moves (waiting, deferred, rejected) and the one it may not
  make (landed, which git decides);
* the reset inside the move to ``end`` -- landed and rejected dropped, the
  rest carried, a deferred one back as requested -- and the next run of the
  same workflow inheriting what was carried;
* the round-start block and the payload saying what the reset did, with the
  board's disagreements as warning lines.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from claude_launcher import cli_cflow
from claude_launcher.cflow import engine, landing, mcp, model, state as state_mod
from claude_launcher.cflow.model import WorkflowError
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef

LEADER = """
name: lead
recur: true
landing_queue: {target: master}
steps:
  standby:
    instructions: take requests
    next: reflect
  reflect:
    instructions: close the round
"""

WORKER = """
name: work
steps:
  request:
    instructions: ask to land
    triggers:
      - do: enqueue-landing
        at: leave
    next: wait
  wait:
    instructions: wait for the landing
"""

PLAIN = """
name: plain
steps:
  only:
    instructions: nothing queued here
"""


def _git(repo, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _commit(repo, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    (wf / "lead.yaml").write_text(LEADER, encoding="utf-8")
    (wf / "work.yaml").write_text(WORKER, encoding="utf-8")
    (wf / "plain.yaml").write_text(PLAIN, encoding="utf-8")
    _git(d, "init", "-q", "-b", "master")
    _commit(d, "base.txt")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    mcp._seen_run = None
    # The board is read through `br`; each test says what it holds.
    monkeypatch.setattr(landing, "board_statuses", lambda repo, issues: {})
    return d


@pytest.fixture
def feature(proj):
    """A worker's branch with one commit, in a worktree of the leader's repo."""
    wt = proj.parent / "wt"
    _git(proj, "worktree", "add", "-q", "-b", "w1-feature", str(wt), "master")
    tip = _commit(wt, "feature.txt")
    return SimpleNamespace(path=wt, tip=tip, branch="w1-feature")


def _events(kind: str, cwd=None) -> list:
    return [e for e in state_mod.read_journal(cwd) if e.get("event") == kind]


def _queue(payload: dict) -> dict:
    return {e["issue"]: e for e in payload.get("landing_queue") or []}


def _end_round() -> dict:
    engine.report("standing by")
    engine.next_step()
    engine.report("reflected")
    return engine.next_step()


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #
def test_the_declaration_and_its_default_target():
    assert model.parse(LEADER).landing_queue == model.LandingQueueSpec(target="master")
    shorthand = LEADER.replace("landing_queue: {target: master}", "landing_queue: true")
    assert model.parse(shorthand).landing_queue.target == "master"
    assert model.parse(PLAIN).landing_queue is None


@pytest.mark.parametrize(
    "value, match",
    [
        ("landing_queue: 3", "true or a mapping"),
        ("landing_queue: {branch: main}", "unknown key"),
        ("landing_queue: {target: 'a b'}", "branch name"),
    ],
)
def test_declaration_refusals(value, match):
    with pytest.raises(WorkflowError, match=match):
        model.parse(LEADER.replace("landing_queue: {target: master}", value))


def test_a_sub_definition_may_not_keep_a_queue():
    text = "name: side\nkind: subflow\nlanding_queue: true\nsteps:\n  a:\n    instructions: x\n"
    with pytest.raises(WorkflowError, match="landing_queue"):
        model.parse(text)


def test_enqueue_landing_is_a_trigger_action():
    steps = model.parse(WORKER).steps
    assert steps["request"].triggers == (
        model.Trigger(do="enqueue-landing", at="leave"),
    )


# --------------------------------------------------------------------------- #
# entries
# --------------------------------------------------------------------------- #
def test_an_entry_per_issue_and_the_payload_shows_them(proj, feature):
    engine.start("lead")
    out = engine.enqueue_landing(["x-1", "x-2"], feature.branch, feature.tip, by="w1")
    assert out["added"] == ["x-1", "x-2"]
    queue = _queue(engine.status())
    assert set(queue) == {"x-1", "x-2"}
    assert queue["x-1"]["status"] == "requested"
    assert queue["x-1"]["tip"] == feature.tip
    assert queue["x-1"]["branch"] == "w1-feature"
    assert queue["x-1"]["requested_by"] == "w1"
    assert _events("queue_enqueued")[-1]["added"] == ["x-1", "x-2"]


def test_the_same_tip_again_changes_nothing_and_a_new_tip_renews(proj, feature):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    engine.mark_landing("x-1", "deferred", by="lead")
    again = engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    assert again["added"] == [] and again["renewed"] == []
    assert _queue(engine.status())["x-1"]["status"] == "deferred"
    renewed = engine.enqueue_landing(["x-1"], feature.branch, "f" * 40, by="w1")
    assert renewed["renewed"] == ["x-1"]
    entry = _queue(engine.status())["x-1"]
    assert entry["status"] == "requested" and entry["tip"] == "f" * 40


def test_a_workflow_without_a_queue_refuses_entries(proj):
    engine.start("plain")
    with pytest.raises(engine.CflowError, match="landing_queue"):
        engine.enqueue_landing(["x-1"], "b", "a" * 40, by="w1")
    assert "landing_queue" not in engine.status()


# --------------------------------------------------------------------------- #
# the agent's moves, and git's
# --------------------------------------------------------------------------- #
def test_the_agent_moves_an_entry_and_the_journal_says_so(proj, feature):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    out = engine.mark_landing("x-1", "waiting", by="lead", note="this round")
    assert out["was"] == "requested"
    entry = _queue(engine.status())["x-1"]
    assert entry["status"] == "waiting" and entry["note"] == "this round"
    assert entry["set_by"] == "lead"
    assert _events("queue_marked")[-1]["status"] == "waiting"


@pytest.mark.parametrize(
    "issue, status, match",
    [
        ("x-1", "landed", "measured by the daemon"),
        ("x-1", "gone", "not a state"),
        ("x-9", "waiting", "no queue entry"),
    ],
)
def test_refused_moves(proj, feature, issue, status, match):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    with pytest.raises(engine.CflowError, match=match):
        engine.mark_landing(issue, status, by="lead")


def test_git_marks_a_merged_tip_landed_and_it_is_not_moved_again(proj, feature):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    assert engine.refresh_landing()["landed"] == []
    _git(proj, "merge", "-q", "--no-ff", "-m", "land", "w1-feature")
    out = engine.refresh_landing()
    assert out["landed"] == ["x-1"]
    entry = _queue(engine.status())["x-1"]
    assert entry["status"] == "landed" and entry["set_by"] == "daemon"
    with pytest.raises(engine.CflowError, match="has landed"):
        engine.mark_landing("x-1", "rejected", by="lead")


def test_the_mcp_tool_lists_and_moves(proj, feature):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    listed = mcp.call_tool("landing_queue", {})
    assert listed["status"] == "landing_refreshed"
    assert [e["issue"] for e in listed["landing_queue"]] == ["x-1"]
    moved = mcp.call_tool("landing_queue", {"issue": "x-1", "status": "rejected"})
    assert moved["status"] == "landing_marked"
    with pytest.raises(engine.CflowError, match="'status'"):
        mcp.call_tool("landing_queue", {"issue": "x-1"})


# --------------------------------------------------------------------------- #
# the round's end, and the next round
# --------------------------------------------------------------------------- #
def _four_entries(proj, feature):
    engine.enqueue_landing(["landed-1"], feature.branch, feature.tip, by="w1")
    for issue in ("waiting-1", "deferred-1", "rejected-1"):
        engine.enqueue_landing([issue], "other", "e" * 40, by="w2")
    engine.mark_landing("waiting-1", "waiting", by="lead")
    engine.mark_landing("deferred-1", "deferred", by="lead")
    engine.mark_landing("rejected-1", "rejected", by="lead")
    _git(proj, "merge", "-q", "--no-ff", "-m", "land", "w1-feature")


def test_the_end_drops_the_settled_and_carries_the_rest(proj, feature):
    engine.start("lead")
    _four_entries(proj, feature)
    _end_round()
    reset = _events("queue_reset")[-1]
    assert {r["issue"]: r["status"] for r in reset["dropped"]} == {
        "landed-1": "landed", "rejected-1": "rejected",
    }
    assert {r["issue"]: r["status"] for r in reset["carried"]} == {
        "waiting-1": "waiting", "deferred-1": "requested",
    }
    assert [r for r in reset["carried"] if r.get("was")] == [
        {"issue": "deferred-1", "status": "requested", "was": "deferred"}
    ]


def test_the_next_round_inherits_the_carried_queue(proj, feature):
    first = engine.start("lead")
    _four_entries(proj, feature)
    _end_round()
    second = engine.start("lead")
    assert second["run"] != first["run"]
    assert _queue(second) .keys() == {"waiting-1", "deferred-1"}
    assert _queue(second)["deferred-1"]["status"] == "requested"
    assert second["landing_reset"]["run"] == first["run"]
    lines = cflow_clock.landing_lines(second)
    assert any("dropped" in l and "landed-1 (landed)" in l for l in lines)
    assert any("deferred-1 (requested, was deferred)" in l for l in lines)
    assert "landing queue now: 2 entries" in cflow_clock.round_block(
        {**second, "step": "standby"}
    )


def test_a_force_started_round_still_carries_an_unreset_queue(proj, feature):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    engine.mark_landing("x-1", "deferred", by="lead")
    second = engine.start("lead", force=True)
    assert _queue(second)["x-1"]["status"] == "requested"
    assert second["landing_reset"]["carried"] == [{"issue": "x-1", "status": "requested"}]


def test_a_different_workflow_does_not_inherit(proj, feature):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    _end_round()
    assert "landing_queue" not in engine.start("plain")


def test_the_board_disagreeing_is_a_warning_line(proj, feature, monkeypatch):
    monkeypatch.setattr(
        landing, "board_statuses", lambda repo, issues: {"x-1": "closed"}
    )
    engine.start("lead")
    engine.enqueue_landing(["x-1", "x-2"], "other", "e" * 40, by="w1")
    _end_round()
    warnings = _events("queue_reset")[-1]["warnings"]
    assert any("x-1" in w and "closed" in w for w in warnings)
    assert any("x-2" in w and "not on the board" in w for w in warnings)


def test_an_unreadable_board_is_said_not_guessed(proj, feature, monkeypatch):
    monkeypatch.setattr(landing, "board_statuses", lambda repo, issues: None)
    engine.start("lead")
    engine.enqueue_landing(["x-1"], "other", "e" * 40, by="w1")
    _end_round()
    assert _events("queue_reset")[-1]["warnings"] == [
        "the issue board could not be read: queue and board not compared"
    ]


def test_board_warnings_shapes():
    entries = [
        {"issue": "a", "status": "requested"},
        {"issue": "b", "status": "landed"},
        {"issue": "c", "status": "waiting"},
        {"issue": "d", "status": "rejected"},
    ]
    warnings = landing.board_warnings(
        entries, {"a": "open", "b": "in_review", "c": "in_review", "d": "closed"}
    )
    assert warnings == [
        "a: queued (requested) but the board has it open",
        "b: landed, but the board still has it in_review (its assignee closes it)",
    ]


# --------------------------------------------------------------------------- #
# the daemon: a child's trigger, and the git measurement between moves
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, name: str, cwd: str, parent=None, exited=False) -> None:
        self.exited = exited
        self.sdef = SessionDef(name=name, cwd=cwd, parent=parent)
        self.queued: list = []

    def queue_delivery(self, text: str) -> bool:
        self.queued.append(text)
        return True


class _Manager:
    def __init__(self, sessions) -> None:
        self._sessions = {s.sdef.name: s for s in sessions}

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


def _worker_at_request(feature) -> str:
    wcwd = state_mod.resolve_cwd(str(feature.path))
    wf = feature.path / ".claunch" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "work.yaml").write_text(WORKER, encoding="utf-8")
    engine.start("work", cwd=wcwd, scope="w1")
    engine.report("asked", cwd=wcwd, scope="w1")
    engine.next_step(cwd=wcwd, scope="w1")
    return wcwd


def test_a_childs_trigger_files_its_request_on_the_parents_queue(proj, feature, monkeypatch):
    lcwd = state_mod.resolve_cwd(str(proj))
    engine.start("lead", cwd=lcwd, scope="lead")
    wcwd = _worker_at_request(feature)
    monkeypatch.setattr(landing, "in_review_of", lambda repo, session: ["x-1"])
    # The worker may already be gone: the request stands either way.
    clock = cflow_clock.TriggerClock(_Manager([
        _Session("lead", lcwd), _Session("w1", wcwd, parent="lead", exited=True),
    ]))
    [(cwd, scope, action)] = [c for c in clock.scan() if c[2]["do"] == "enqueue-landing"]
    assert (cwd, scope) == (wcwd, "w1")
    performed, detail = asyncio.run(clock._perform(cwd, scope, action))
    assert performed, detail
    assert "added x-1" in detail
    entry = _queue(engine.status(cwd=lcwd, scope="lead"))["x-1"]
    assert entry["tip"] == feature.tip and entry["branch"] == "w1-feature"
    assert entry["requested_by"] == "w1"


@pytest.mark.parametrize(
    "parent, workflow, issues, why",
    [
        (None, "lead", ["x-1"], "no parent session"),
        ("lead", "plain", ["x-1"], "declares no 'landing_queue'"),
        ("lead", "lead", [], "holds no issue in_review"),
        ("lead", "lead", None, "board could not be read"),
    ],
)
def test_a_trigger_that_cannot_file_says_why(proj, feature, monkeypatch, parent, workflow, issues, why):
    lcwd = state_mod.resolve_cwd(str(proj))
    engine.start(workflow, cwd=lcwd, scope="lead")
    wcwd = _worker_at_request(feature)
    monkeypatch.setattr(landing, "in_review_of", lambda repo, session: issues)
    clock = cflow_clock.TriggerClock(_Manager([
        _Session("lead", lcwd), _Session("w1", wcwd, parent=parent),
    ]))
    performed, detail = asyncio.run(
        clock._perform(wcwd, "w1", {"do": "enqueue-landing"})
    )
    assert not performed
    assert why in detail


def test_the_clock_marks_landed_at_most_once_a_minute(proj, feature):
    lcwd = state_mod.resolve_cwd(str(proj))
    engine.start("lead", cwd=lcwd, scope="lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1", cwd=lcwd, scope="lead")
    clock = cflow_clock.TriggerClock(_Manager([_Session("lead", lcwd)]))
    assert clock.refresh_landing(now=100.0) == []
    _git(proj, "merge", "-q", "--no-ff", "-m", "land", "w1-feature")
    assert clock.refresh_landing(now=130.0) == []          # inside the minute
    assert clock.refresh_landing(now=161.0) == [(lcwd, "lead", ["x-1"])]


def test_the_cli_status_shows_the_queue(proj, feature, capsys):
    engine.start("lead")
    engine.enqueue_landing(["x-1"], feature.branch, feature.tip, by="w1")
    cli_cflow._print_landing(engine.status())
    out = capsys.readouterr().out
    assert "x-1 requested  w1-feature @ " + feature.tip[:8] in out
    assert "(by w1)" in out
