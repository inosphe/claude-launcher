"""The merge pause a Workspaces sync agent holds (claunch-z6da6).

While a ``reposync-merge-push-v2`` run stands in prepare .. execute, the
leader's ``tools/delayed_sweep.py admit`` refuses every candidate but the
sync's own issue. The pause is read off live state, so it ends at receipt,
when the run finishes or is aborted, and when the session ends; a card
bounds it at the card's deadline plus 10 minutes, and before any card the
run's last report plus ``reposync_pause_precard_s`` bounds it.

The parent picker offers this repository's mesh leaders (live or held), and
with no leader only sessions in no mesh, marked as pausing nothing.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_launcher import delayed_sweep as sweep_mod
from claude_launcher.daemon import reposync_agent, reposync_pause as rp

NOW = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)
LOCAL = "d73e6029" + "0" * 32


def iso(when):
    return when.isoformat(timespec="seconds")


def run_state(node="prepare", *, status="running", updated=NOW, workflow=rp.WORKFLOW):
    return {
        "run_id": "r1", "workflow": workflow, "status": status,
        "active": [f"{node}@3"] if node else [], "updated": iso(updated),
        "data": {"inputs": {"assignment": json.dumps({"local": LOCAL, "repo": "x"})}},
    }


def card(session="sync1", *, deadline=NOW + timedelta(minutes=30), submitted=NOW, rid="c1"):
    return {"id": rid, "session": session, "deadline": iso(deadline),
            "submitted_at": iso(submitted), "status": "pending"}


def compute(state, cards=(), precard=1800.0, now=NOW):
    return rp.compute(state, session="sync1", issue="claunch-sync", cards=cards,
                      precard=precard, now=now)


# --------------------------------------------------------------------------- #
# the span and its releases
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("node", ["prepare", "checks", "review", "execute"])
def test_prepare_through_execute_pause(node):
    pause = compute(run_state(node))
    assert pause["node"] == node and not pause["expired"]
    assert pause["local"] == LOCAL and pause["session"] == "sync1"
    assert pause["issue"] == "claunch-sync"


@pytest.mark.parametrize("node", ["inspect", "receipt", None])
def test_inspect_and_receipt_do_not_pause(node):
    assert compute(run_state(node)) is None


@pytest.mark.parametrize("status", ["done", "aborted", "failed"])
def test_a_finished_or_aborted_run_releases(status):
    assert compute(run_state("execute", status=status)) is None


def test_waiting_approval_still_pauses():
    assert compute(run_state("execute", status="waiting_approval"))["node"] == "execute"


def test_another_workflow_does_not_pause():
    # the on_failure recovery worker never pushes; its parent holds the pause
    assert compute(run_state("prepare", workflow="reposync-conflict-v2")) is None


def test_nested_instance_ids_are_read_by_their_last_segment():
    state = run_state(None)
    state["active"] = ["body/seq/prepare@7"]
    assert compute(state)["node"] == "prepare"


# --------------------------------------------------------------------------- #
# the two bounds
# --------------------------------------------------------------------------- #
def test_before_a_card_the_bound_is_the_last_report_plus_the_setting():
    state = run_state("checks", updated=NOW - timedelta(minutes=29))
    pause = compute(state)
    assert pause["bound"] == "progress" and pause["until"] == iso(NOW + timedelta(minutes=1))
    assert not pause["expired"]
    assert compute(state, now=NOW + timedelta(minutes=1))["expired"]
    # a report renews it
    renewed = run_state("checks", updated=NOW)
    assert not compute(renewed, now=NOW + timedelta(minutes=1))["expired"]
    # the 30 minutes is a setting, not a constant
    assert compute(state, precard=3600.0)["until"] == iso(NOW + timedelta(minutes=31))


def test_after_a_card_the_bound_is_its_deadline_plus_ten_minutes():
    deadline = NOW + timedelta(minutes=5)
    pause = compute(run_state("execute", updated=NOW - timedelta(hours=2)),
                    cards=[card(deadline=deadline)])
    assert pause["bound"] == "card" and pause["card"] == "c1"
    assert pause["until"] == iso(deadline + timedelta(minutes=10))
    assert not pause["expired"]  # the stale last report no longer bounds it
    assert compute(run_state("execute"), cards=[card(deadline=deadline)],
                   now=deadline + timedelta(minutes=10))["expired"]


def test_a_replacing_card_moves_the_deadline():
    old = card(rid="old", deadline=NOW + timedelta(minutes=1), submitted=NOW - timedelta(minutes=29))
    new = card(rid="new", deadline=NOW + timedelta(minutes=30), submitted=NOW)
    pause = compute(run_state("review"), cards=[new, old])
    assert pause["card"] == "new"
    assert pause["until"] == iso(NOW + timedelta(minutes=40))


def test_other_sessions_cards_do_not_bound_this_sync():
    pause = compute(run_state("review"), cards=[card(session="someone-else")])
    assert pause["bound"] == "progress"


def test_the_setting_has_a_default_and_reads_live():
    assert rp.precard_seconds({}) == 1800.0
    assert rp.precard_seconds({"reposync_pause_precard_s": 600}) == 600.0
    assert rp.precard_seconds({"reposync_pause_precard_s": "junk"}) == 1800.0


def test_the_setting_is_a_daemon_default_an_agent_cannot_write():
    from claude_launcher import cli_sessions, store
    assert store.DAEMON_DEFAULTS["reposync_pause_precard_s"] == 1800.0
    assert "reposync_pause_precard_s" in cli_sessions._PERSON_ONLY_KEYS
    assert "reposync_pause_precard_s" in cli_sessions._LIVE_KEYS


# --------------------------------------------------------------------------- #
# finding the sync session of a repository
# --------------------------------------------------------------------------- #
def session(name, cwd, *, exited=False, held_at=None, task=None, issue="claunch-sync"):
    task = reposync_agent.marker(cwd) if task is None else task
    return SimpleNamespace(exited=exited, held_at=held_at,
                           sdef=SimpleNamespace(name=name, cwd=cwd, task=task, issue=issue))


def for_repo(sessions, states, *, repo="/repo"):
    common = {"/repo": "C1", "/repo/wt": "C1", "/other": "C2"}
    return rp.for_repo(SimpleNamespace(list=lambda: sessions), repo, cards=[],
                       common_dir=common.get, load_state=lambda cwd, name: states.get(name),
                       precard=1800.0, now=NOW)


def test_for_repo_finds_the_sync_session_by_common_dir():
    got = for_repo([session("sync1", "/repo/wt")], {"sync1": run_state("checks")})
    assert got["paused"] and got["pause"]["session"] == "sync1"


def test_for_repo_ignores_other_repos_plain_sessions_and_ended_sessions():
    states = {n: run_state("checks") for n in ("other", "plain", "gone")}
    got = for_repo([session("other", "/other"), session("plain", "/repo", task="hello"),
                    session("gone", "/repo/wt", exited=True)], states)
    assert got == {"paused": False, "pause": None, "pauses": [], "expired": []}


def test_a_held_sync_session_still_pauses():
    got = for_repo([session("sync1", "/repo/wt", exited=True, held_at="2026-10-20T05:00:00+00:00")],
                   {"sync1": run_state("review")})
    assert got["paused"]


def test_for_repo_reports_an_expired_pause_without_pausing():
    stale = run_state("checks", updated=NOW - timedelta(hours=1))
    got = for_repo([session("sync1", "/repo/wt")], {"sync1": stale})
    assert not got["paused"] and got["expired"][0]["session"] == "sync1"


# --------------------------------------------------------------------------- #
# the leader's admit
# --------------------------------------------------------------------------- #
def load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "delayed_sweep.py"
    spec = importlib.util.spec_from_file_location("delayed_sweep_tool_z6da6", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t")):
        sweep_mod.git(path, *args)
    (path / "base").write_text("base")
    sweep_mod.git(path, "add", ".")
    sweep_mod.git(path, "commit", "-qm", "base")
    return path


def paused(issue="claunch-sync", expired=False):
    pause = compute(run_state("checks"))
    pause["issue"] = issue
    return lambda repo: {"paused": not expired, "pause": None if expired else pause,
                         "expired": [pause] if expired else []}


def test_admit_refuses_an_ordinary_candidate_naming_the_sync_and_its_local_sha(repo, tmp_path):
    tool = load_tool()
    queue = sweep_mod.Queue(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError) as exc:
        tool.admit(queue, repo, "claunch-other", sync_pause=paused())
    assert "sync session sync1" in str(exc.value) and LOCAL in str(exc.value)
    assert "claunch-sync" in str(exc.value)


def test_admit_lets_the_syncs_own_issue_through(repo, tmp_path):
    tool = load_tool()
    queue = sweep_mod.Queue(tmp_path / "jobs.sqlite3")
    tool.admit(queue, repo, "claunch-sync", sync_pause=paused())


def test_admit_checks_every_sync_holding_the_repository(repo, tmp_path):
    tool = load_tool()
    queue = sweep_mod.Queue(tmp_path / "jobs.sqlite3")
    first = compute(run_state("checks"))
    second = dict(first, session="sync2", issue="claunch-sync2")
    answer = {"paused": True, "pause": first, "pauses": [first, second], "expired": []}
    with pytest.raises(ValueError, match="sync2"):
        tool.admit(queue, repo, "claunch-sync", sync_pause=lambda repo: answer)


def test_admit_passes_without_a_pause_or_after_its_bound(repo, tmp_path):
    tool = load_tool()
    queue = sweep_mod.Queue(tmp_path / "jobs.sqlite3")
    tool.admit(queue, repo, "claunch-other", sync_pause=lambda repo: None)
    tool.admit(queue, repo, "claunch-other", sync_pause=paused(expired=True))


def test_admit_still_applies_the_regression_pause_first(repo, tmp_path, monkeypatch):
    tool = load_tool()
    queue = sweep_mod.Queue(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(queue, "admit", lambda repo, issue: (_ for _ in ()).throw(
        ValueError("repository paused by job1")))
    with pytest.raises(ValueError, match="job1"):
        tool.admit(queue, repo, "claunch-sync", sync_pause=paused())


def test_admit_with_no_daemon_has_no_sync_pause(repo, tmp_path):
    # conftest isolates the launcher home: no daemon is announced there
    tool = load_tool()
    assert tool.daemon_sync_pause(repo) is None
    tool.admit(sweep_mod.Queue(tmp_path / "jobs.sqlite3"), repo, "claunch-other")


def test_admit_with_an_unresponsive_daemon_refuses(repo, monkeypatch):
    from claude_launcher import daemon_client
    tool = load_tool()
    monkeypatch.setattr(daemon_client, "connect_with_diagnosis",
                        lambda **kw: (None, {"state": daemon_client.UNRESPONSIVE}))
    with pytest.raises(ValueError, match="defer"):
        tool.daemon_sync_pause(repo)


# --------------------------------------------------------------------------- #
# the agent's instructions
# --------------------------------------------------------------------------- #
CARD = {"repo": "/r", "workspace": "w", "remote": "origin", "branch": "master",
        "action": "rebase-push", "local": LOCAL, "upstream": "b" * 40}


def test_the_sync_agent_is_told_the_pause_and_its_notice_format():
    text = reposync_agent.task(CARD)
    assert "SYNC PAUSE START <repo> local=<sha> run=<run id>" in text
    assert "SYNC PAUSE END <repo> reason=<receipt|failed|aborted>" in text
    assert "replan when prepare begins" in text
    assert "deadline plus 10 minutes" in text


def test_the_recovery_worker_is_not_told_to_pause():
    assert "SYNC PAUSE" not in reposync_agent.task(CARD, mode="on_failure")
