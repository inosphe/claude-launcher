from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_launcher import delayed_sweep as mod, test_window
from claude_launcher.daemon.delayed_sweep import Clock
from claude_launcher.daemon import delayed_sweep as daemon_sweep, instance_manifest as im, paths

TOOL = Path(__file__).resolve().parents[1] / "tools" / "sweep.py"


@pytest.fixture(autouse=True)
def isolated_instance_paths(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.INSTANCE_BASE_ENV, str(tmp_path / ".home"))
    monkeypatch.setenv(paths.INSTANCE_ENV, "")


def git(repo, *args):
    return mod.git(repo, *args)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "test@example.com"),
                 ("config", "user.name", "test")):
        git(path, *args)
    (path / "base").write_text("base")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    git(path, "branch", "-M", "master")
    return path


def merge(repo, name):
    git(repo, "switch", "-qc", name)
    (repo / name).write_text(name)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", name)
    tip = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "master")
    git(repo, "merge", "--no-ff", "-qm", "merge " + name, name)
    return git(repo, "rev-parse", "HEAD"), tip


def queue(tmp_path):
    return mod.Queue(tmp_path / "jobs.sqlite3")


def enqueue(q, repo, name, issue=None):
    sha, tip = merge(repo, name)
    return q.enqueue(repo, sha, issue or name, "leader", TOOL), tip


def test_each_merge_has_an_immutable_cumulative_checkpoint(repo, tmp_path):
    q = queue(tmp_path)
    first, _ = enqueue(q, repo, "a")
    second, _ = enqueue(q, repo, "b")
    assert git(repo, "rev-parse", first["ref"]) == first["sha"]
    assert git(repo, "rev-parse", second["ref"]) == second["sha"]
    assert git(repo, "ls-tree", "--name-only", first["ref"]).split() == ["a", "base"]
    assert git(repo, "ls-tree", "--name-only", second["ref"]).split() == ["a", "b", "base"]
    assert q.enqueue(repo, first["sha"], "a", "leader", TOOL)["id"] == first["id"]
    restored = mod.Queue(q.database)
    assert len(restored.status(repo)["jobs"]) == 2
    assert restored.claim()["id"] == first["id"]


def test_changed_checkpoint_ref_is_refused(repo, tmp_path):
    q = queue(tmp_path)
    first, _ = enqueue(q, repo, "a")
    second, _ = enqueue(q, repo, "b")
    git(repo, "update-ref", first["ref"], second["sha"])
    with pytest.raises(ValueError, match="ref moved"):
        q.enqueue(repo, first["sha"], "a", "leader", TOOL)


def checkpoint_gate(q, record):
    return subprocess.run(
        [sys.executable, str(TOOL.with_name("delayed_sweep.py")),
         "--database", str(q.database), "check-enqueued", "--env", "CHECKPOINT_RECORD"],
        env={**os.environ, "CHECKPOINT_RECORD": json.dumps(record)},
        capture_output=True, text=True,
    )


def test_checkpoint_gate_checks_the_reported_master_and_merge_second_parent(repo, tmp_path):
    q = queue(tmp_path)
    job, tip = enqueue(q, repo, "a")
    item = {"issue": "a", "tip": tip, "merge_sha": job["sha"]}
    record = {"repo": str(repo), "master": job["sha"], "merges": [item]}
    merge(repo, "b")  # master may advance between integration and the gate
    assert checkpoint_gate(q, record).returncode == 0
    result = checkpoint_gate(q, {**record, "master": "master"})
    assert result.returncode == 2
    assert "full commit SHA" in result.stderr
    result = checkpoint_gate(q, {**record, "merges": [{**item, "tip": job["sha"]}]})
    assert result.returncode == 2
    assert "second parent" in result.stderr


def gate(q, action, record, *extra):
    return subprocess.run(
        [sys.executable, str(TOOL.with_name("delayed_sweep.py")),
         "--database", str(q.database), action, "--json", json.dumps(record), *extra],
        capture_output=True, text=True)


def bundle(repo, tmp_path):
    """One branch merged once, carrying two issues: the incident of gds6 run 20261013-173755."""
    q = queue(tmp_path)
    job, tip = enqueue(q, repo, "a", issue="x.6")
    rows = [{"issue": "x.6", "tip": tip, "merge_sha": job["sha"]},
            {"issue": "x.5", "tip": tip, "merge_sha": job["sha"]}]
    return q, job, tip, rows


def test_two_issue_merge_still_fails_the_hard_gate_and_classifies_as_mismatch(repo, tmp_path):
    q, job, tip, rows = bundle(repo, tmp_path)
    record = {"repo": str(repo), "master": job["sha"], "merges": rows}
    assert checkpoint_gate(q, record).returncode == 2  # the ownership check is intentional
    result = gate(q, "check-enqueued", record, "--classify")
    assert result.returncode == 2
    out = json.loads(result.stdout)
    assert out["class"] == "mismatch"
    assert [r["kind"] for r in out["rows"]] == ["ok", "mismatch"]
    # the suggested regrouping keeps the job's own issue and passes the unchanged gate
    suggested = out["suggested"]
    assert suggested["merges"] == [{"issue": "x.6", "tip": tip, "merge_sha": job["sha"], "also": ["x.5"]}]
    assert suggested["master"] == job["sha"]
    assert checkpoint_gate(q, suggested).returncode == 0
    assert gate(q, "check-enqueued", suggested, "--classify").returncode == 0


def test_classify_conflict_missing_and_invalid_are_not_repaired_by_regrouping(repo, tmp_path):
    q, job, tip, rows = bundle(repo, tmp_path)
    # the job's own issue is not in the report: relabelling would hide a wrong attribution
    wrong = {"repo": str(repo), "merges": [{**rows[0], "issue": "other"}]}
    out = json.loads(gate(q, "check-enqueued", wrong, "--classify").stdout)
    assert out["class"] == "conflict" and "x.6" in out["rows"][0]["problem"]
    # a merge with no job is the old recovery path (enqueue the same SHA again)
    sha, tip2 = merge(repo, "b")
    out = json.loads(gate(q, "check-enqueued", {"repo": str(repo), "merges": [
        {"issue": "b", "tip": tip2, "merge_sha": sha}]}, "--classify").stdout)
    assert out["class"] == "missing"
    # a moved fixed ref or a wrong tip is not a reporting error
    out = json.loads(gate(q, "check-enqueued", {"repo": str(repo), "merges": [
        {**rows[0], "tip": job["sha"]}]}, "--classify").stdout)
    assert out["class"] == "invalid" and "second parent" in out["rows"][0]["problem"]
    other, _ = enqueue(q, repo, "c")
    git(repo, "update-ref", job["ref"], other["sha"])
    out = json.loads(gate(q, "check-enqueued", {"repo": str(repo), "merges": rows[:1]}, "--classify").stdout)
    assert out["class"] == "invalid" and "moved" in out["rows"][0]["problem"]
    assert q.status(repo)["jobs"][0]["issue"] == "x.6"  # classifying wrote nothing


def test_classify_never_creates_jobs_or_refs(repo, tmp_path):
    q, job, tip, rows = bundle(repo, tmp_path)
    before = (json.dumps(q.status(repo), sort_keys=True), git(repo, "for-each-ref"))
    gate(q, "check-enqueued", {"repo": str(repo), "merges": rows}, "--classify")
    assert before == (json.dumps(q.status(repo), sort_keys=True), git(repo, "for-each-ref"))


def test_check_merges_wants_one_row_per_merge_and_the_jobs_issue(repo, tmp_path):
    q, job, tip, rows = bundle(repo, tmp_path)
    result = gate(q, "check-merges", {"repo": str(repo), "merges": rows})
    assert result.returncode == 2 and "reported twice" in result.stderr
    ok = {"repo": str(repo), "merges": [{**rows[0], "also": ["x.5"]}]}
    assert gate(q, "check-merges", ok).returncode == 0
    result = gate(q, "check-merges", {"repo": str(repo), "merges": [{**rows[1], "also": []}]})
    assert result.returncode == 2 and "x.6" in result.stderr
    result = gate(q, "check-merges", {"repo": str(repo), "merges": [{**rows[0], "also": ["x.6"]}]})
    assert result.returncode == 2 and "also" in result.stderr
    # before the enqueue exists the gate owns that question, not this advisory check
    sha, tip2 = merge(repo, "b")
    assert gate(q, "check-merges", {"repo": str(repo), "merges": [
        {"issue": "b", "tip": tip2, "merge_sha": sha}]}).returncode == 0


def test_re_enqueue_under_another_issue_is_still_refused(repo, tmp_path):
    q, job, tip, rows = bundle(repo, tmp_path)
    with pytest.raises(ValueError, match="different verification issue"):
        q.enqueue(repo, job["sha"], "x.5", "leader", TOOL)


def test_checkpoint_gate_refuses_a_queued_merge_removed_from_master(repo, tmp_path):
    q = queue(tmp_path)
    job, tip = enqueue(q, repo, "a")
    git(repo, "reset", "--hard", job["sha"] + "^1")
    # Partial-batch failure reports have no master field; every merge must
    # still be contained in master and match its requested branch tip.
    result = checkpoint_gate(q, {"repo": str(repo), "merges": [
        {"issue": "a", "tip": tip, "merge_sha": job["sha"]}]})
    assert result.returncode == 2
    assert "not contained in master" in result.stderr


def test_pending_record_recovers_the_ref_before_claim(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    git(repo, "update-ref", "-d", job["ref"])
    with q.connect() as db:
        db.execute("UPDATE jobs SET status='pending' WHERE id=?", (job["id"],))
    assert mod.Queue(q.database).claim()["id"] == job["id"]
    assert git(repo, "rev-parse", job["ref"]) == job["sha"]


def test_a_failure_pauses_only_its_repository_and_only_green_repair_resumes(repo, tmp_path):
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    queued, _ = enqueue(q, repo, "normal")
    q.finish(bad["id"], "failed", "receipt", "test_x failed")
    with pytest.raises(ValueError, match="paused"):
        q.admit(repo, "normal")
    assert q.claim() is None  # already-merged ordinary backlog waits for repair
    q.repair(repo, bad["id"], "repair-issue")
    q.admit(repo, "repair-issue")
    repair, _ = enqueue(q, repo, "fix", "repair-issue")
    assert repair["priority"] == 0
    assert q.claim()["id"] == repair["id"]
    q.finish(repair["id"], "retry", detail="window timeout")
    with pytest.raises(ValueError, match="paused"):
        q.admit(repo, "normal")
    q.finish(repair["id"], "passed", "green receipt")
    q.admit(repo, "normal")
    assert q.claim()["id"] == queued["id"]
    # A delayed result about history covered by the green repair is still red,
    # but it must not pause the repaired repository again.
    q.finish(queued["id"], "failed", "historical receipt")
    q.admit(repo, "new-work")


def test_a_failed_repair_sweep_moves_the_pause_and_empties_the_repair_slot(repo, tmp_path):
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    q.finish(bad["id"], "failed", "receipt", "x")
    q.repair(repo, bad["id"], "fix")
    q.admit(repo, "fix")
    repair, _ = enqueue(q, repo, "fix-merge", "fix")
    cleared = q.status(repo)["pauses"][0]["cleared_sha"]
    q.finish(repair["id"], "failed", "receipt", "still red")
    pause = q.status(repo)["pauses"][0]
    assert pause["job"] == repair["id"]
    assert not pause["repair_issue"]
    assert pause["cleared_sha"] == cleared
    q.repair(repo, repair["id"], "fix-2")  # the slot is free for the next repair


def test_an_ordinary_failure_leaves_an_active_pause_unchanged(repo, tmp_path):
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    other, _ = enqueue(q, repo, "other")
    q.finish(bad["id"], "failed", "receipt", "x")
    q.repair(repo, bad["id"], "fix")
    before = q.status(repo)["pauses"][0]
    q.finish(other["id"], "failed", "receipt", "unrelated")
    assert q.status(repo)["pauses"][0] == before
    assert before["job"] == bad["id"] and before["repair_issue"] == "fix"


def test_a_failure_of_a_sha_master_cannot_reach_creates_no_pause(repo, tmp_path):
    q = queue(tmp_path)
    gone, _ = enqueue(q, repo, "gone")
    git(repo, "reset", "-q", "--hard", gone["sha"] + "^1")  # discarded after enqueue
    q.finish(gone["id"], "failed", "receipt", "old conftest defect")
    q.admit(repo, "anything")
    assert q.status(repo)["pauses"] == [] or not q.status(repo)["pauses"][0]["job"]


def test_leader_releases_a_pause_only_for_an_unreachable_sha_with_a_reason(repo, tmp_path):
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    q.finish(bad["id"], "failed", "receipt", "x")  # reachable: paused
    with pytest.raises(ValueError, match="reachable"):
        q.release(repo, bad["id"], "why")
    git(repo, "reset", "-q", "--hard", bad["sha"] + "^1")  # now unreachable
    with pytest.raises(ValueError, match="reason"):
        q.release(repo, bad["id"], " ")
    with pytest.raises(ValueError, match="active failed job"):
        q.release(repo, "nope", "why")
    q.release(repo, bad["id"], "discarded local merge")
    q.admit(repo, "anything")
    assert "discarded local merge" in q.get(bad["id"])["detail"]


def test_unrelated_green_and_invalid_repair_cannot_clear_a_pause(repo, tmp_path):
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    other, _ = enqueue(q, repo, "other")
    q.finish(bad["id"], "failed")
    q.finish(other["id"], "passed")
    with pytest.raises(ValueError, match="paused"):
        q.admit(repo, "new")
    with pytest.raises(ValueError, match="active failed"):
        q.repair(repo, other["id"], "fix")
    q.repair(repo, bad["id"], "fix")
    with pytest.raises(ValueError, match="already assigned"):
        q.repair(repo, bad["id"], "different-fix")


@pytest.mark.parametrize("first_instance", ["", "review-a"])
def test_pause_and_green_repair_are_shared_across_daemon_instances(repo, tmp_path, monkeypatch, first_instance):
    base = tmp_path / "shared-home"
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(base))
    monkeypatch.setenv(paths.INSTANCE_BASE_ENV, str(base))
    monkeypatch.setenv(paths.INSTANCE_ENV, first_instance)
    first = mod.Queue()
    bad, _ = enqueue(first, repo, "bad")
    first.finish(bad["id"], "failed")
    # A named daemon may also have a different home in its manifest.
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(tmp_path / "other-home"))
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-b")
    second = mod.Queue()
    assert first.database == second.database == base / "daemon/delayed-sweeps.sqlite3"
    with pytest.raises(ValueError, match="paused"):
        second.admit(repo, "ordinary")
    second.repair(repo, bad["id"], "repair")
    first.admit(repo, "repair")
    second.admit(repo, "repair")
    with pytest.raises(ValueError, match="already assigned"):
        first.repair(repo, bad["id"], "different-repair")
    repair, _ = enqueue(second, repo, "fix", "repair")
    assert first.claim()["id"] == repair["id"]
    second.finish(repair["id"], "passed")
    first.admit(repo, "ordinary")
    second.admit(repo, "ordinary")


def test_only_the_originating_instance_delivers_the_result(repo, tmp_path, monkeypatch):
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-a")
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    q.finish(bad["id"], "failed")
    notices = []
    async def deliver(notice):
        notices.append(notice)
        return True
    manager = SimpleNamespace(get=lambda owner: SimpleNamespace(exited=False, deliver=deliver))
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-b")
    asyncio.run(Clock(manager, q).tick())
    assert not notices
    assert not q.get(bad["id"])["notified"]
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-a")
    asyncio.run(Clock(manager, q).tick())
    assert len(notices) == 1
    assert bad["id"] in notices[0]
    assert q.get(bad["id"])["notified"]
    asyncio.run(Clock(manager, q).tick())
    assert len(notices) == 1


def test_a_failure_notice_names_the_revert_candidates(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    q.finish(bad["id"], "failed", "receipt", json.dumps(["FAILED tests/test_bad.py::t - x"]))
    notices = []
    async def deliver(notice):
        notices.append(notice)
        return True
    manager = SimpleNamespace(get=lambda owner: SimpleNamespace(exited=False, deliver=deliver))
    asyncio.run(Clock(manager, q).tick())
    assert "revert candidates" in notices[0] and bad["sha"][:12] in notices[0]


def test_retries_back_off_and_park_as_stuck_after_the_limit(repo, tmp_path, monkeypatch):
    # claunch-ersj2: a deterministic "no verdict" retried every 60 s for days,
    # and as the oldest rows those jobs were claimed ahead of every queued one.
    now = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    now[0] += 1  # claim order is created, then id: a tie would fall to the random id
    later, _ = enqueue(q, repo, "b")
    delays = []
    for attempt in range(1, mod.RETRY_LIMIT):
        assert q.claim()["id"] == job["id"]
        q.finish(job["id"], "retry", detail="sweep execution exit 3; no regression verdict")
        row = q.get(job["id"])
        assert row["status"] == "retry" and row["failures"] == attempt
        delays.append(row["available"] - now[0])
        if attempt == 1:
            # While it backs off, the queued job behind it is served.
            assert q.claim()["id"] == later["id"]
            q.finish(later["id"], "passed", "receipt")
        assert q.claim() is None
        now[0] = row["available"] + 1
    assert delays == [mod.retry_delay(n) for n in range(1, mod.RETRY_LIMIT)]
    assert delays[0] == 60.0 and delays == sorted(delays)
    assert q.claim()["id"] == job["id"]
    q.finish(job["id"], "retry", detail="exit 3")
    row = q.get(job["id"])
    assert row["status"] == "stuck"
    assert f"after {mod.RETRY_LIMIT} attempts" in row["detail"] and "exit 3" in row["detail"]
    now[0] += 10 ** 6
    assert q.claim() is None  # stuck is terminal until someone requeues it
    q.requeue(job["id"])
    assert q.get(job["id"])["failures"] == 0
    assert q.claim()["id"] == job["id"]


def test_the_owner_is_told_when_a_job_is_parked_as_stuck(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    with q.connect() as db:
        db.execute("UPDATE jobs SET failures=? WHERE id=?", (mod.RETRY_LIMIT - 1, job["id"]))
    q.finish(job["id"], "retry", detail="uv sync failed")
    notices = []
    async def deliver(notice):
        notices.append(notice)
        return True
    manager = SimpleNamespace(get=lambda owner: SimpleNamespace(exited=False, deliver=deliver))
    window = SimpleNamespace(status=lambda: {"holders": [{}]})
    asyncio.run(Clock(manager, q, window=window).tick())
    assert len(notices) == 1
    assert "status: stuck" in notices[0] and "uv sync failed" in notices[0]
    assert "requeue" in notices[0]


def test_cancel_drops_a_job_and_a_late_runner_result_keeps_the_cancel(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    done, _ = enqueue(q, repo, "b")
    assert q.claim()["id"] == job["id"]
    with pytest.raises(ValueError, match="reason"):
        q.cancel(job["id"], " ")
    q.cancel(job["id"], "repository has no pyproject.toml")
    q.finish(job["id"], "retry", detail="runner finished after the cancel")
    row = q.get(job["id"])
    assert row["status"] == "cancelled"
    assert row["detail"] == "cancelled: repository has no pyproject.toml"
    assert q.claim()["id"] == done["id"]
    q.finish(done["id"], "passed", "receipt")
    with pytest.raises(ValueError, match="verdict"):
        q.cancel(done["id"], "too late")
    with pytest.raises(ValueError, match="stuck or cancelled"):
        q.requeue(done["id"])


def test_cancel_and_requeue_from_the_command_line(repo, tmp_path):
    db = tmp_path / "jobs.sqlite3"
    q = mod.Queue(db)
    job, _ = enqueue(q, repo, "a")
    tool = TOOL.with_name("delayed_sweep.py")
    run = lambda *a: subprocess.run([sys.executable, str(tool), "--database", str(db), *a],
                                    capture_output=True, text=True)
    out = run("cancel", "--job", job["id"], "--reason", "operator")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["status"] == "cancelled"
    out = run("requeue", "--job", job["id"])
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["status"] == "queued"


def test_a_queue_file_without_the_failures_column_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, repo TEXT NOT NULL, common TEXT NOT NULL, "
               "sha TEXT NOT NULL, ref TEXT NOT NULL, issue TEXT NOT NULL, owner TEXT NOT NULL, "
               "tool TEXT NOT NULL, python TEXT NOT NULL, command TEXT, "
               "origin TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, "
               "priority INTEGER NOT NULL DEFAULT -1, created REAL NOT NULL, "
               "available REAL NOT NULL DEFAULT 0, pid INTEGER NOT NULL DEFAULT 0, "
               "attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, detail TEXT NOT NULL DEFAULT '', "
               "notified INTEGER NOT NULL DEFAULT 0, UNIQUE(common, sha))")
    db.commit()
    db.close()
    q = mod.Queue(path)
    with q.connect() as conn:
        assert "failures" in {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}


def test_a_queue_file_without_the_remeasure_columns_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, repo TEXT NOT NULL, common TEXT NOT NULL, "
               "sha TEXT NOT NULL, ref TEXT NOT NULL, issue TEXT NOT NULL, owner TEXT NOT NULL, "
               "tool TEXT NOT NULL, python TEXT NOT NULL, command TEXT, "
               "origin TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, "
               "priority INTEGER NOT NULL DEFAULT -1, created REAL NOT NULL, "
               "available REAL NOT NULL DEFAULT 0, pid INTEGER NOT NULL DEFAULT 0, "
               "attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, detail TEXT NOT NULL DEFAULT '', "
               "notified INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, "
               "UNIQUE(common, sha))")
    db.commit()
    db.close()
    q = mod.Queue(path)
    with q.connect() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"remeasures", "history"} <= columns


def paused_on(q, repo, name="bad"):
    bad, _ = enqueue(q, repo, name)
    q.finish(bad["id"], "failed", "first receipt", "test_x failed")
    return bad


def test_remeasure_keeps_the_pause_and_claims_the_failed_job(repo, tmp_path):
    q = queue(tmp_path)
    bad = paused_on(q, repo)
    other, _ = enqueue(q, repo, "other")
    q.remeasure(repo, bad["id"], "looks intermittent")
    job = q.get(bad["id"])
    assert job["status"] == "queued" and job["remeasures"] == 1 and job["priority"] == 0
    assert json.loads(job["history"])[0]["receipt"] == "first receipt"
    with pytest.raises(ValueError, match="paused"):
        q.admit(repo, "normal")
    assert q.claim()["id"] == bad["id"]  # ahead of the already-merged backlog
    assert q.status(repo)["pauses"][0]["job"] == bad["id"]
    assert q.get(other["id"])["status"] == "queued"


def test_a_passing_remeasure_clears_the_pause_as_intermittent(repo, tmp_path):
    q = queue(tmp_path)
    bad = paused_on(q, repo)
    q.remeasure(repo, bad["id"], "looks intermittent")
    q.claim()
    q.finish(bad["id"], "passed", "green receipt")
    pause = q.status(repo)["pauses"][0]
    assert not pause["job"] and not pause["repair_issue"] and pause["cleared_sha"] == bad["sha"]
    assert "remeasure passed; intermittent" in q.get(bad["id"])["detail"]
    assert "open a follow-up issue" in q.get(bad["id"])["detail"]
    q.admit(repo, "normal")


def test_a_failing_second_measurement_keeps_the_pause_for_the_repair_path(repo, tmp_path):
    q = queue(tmp_path)
    bad = paused_on(q, repo)
    q.remeasure(repo, bad["id"], "looks intermittent")
    q.claim()
    q.finish(bad["id"], "failed", "second receipt", "test_x failed again")
    assert q.status(repo)["pauses"][0]["job"] == bad["id"]
    with pytest.raises(ValueError, match="limit"):
        q.remeasure(repo, bad["id"], "once more")
    with pytest.raises(ValueError, match="stuck or cancelled"):
        q.requeue(bad["id"])
    q.repair(repo, bad["id"], "fix")
    q.admit(repo, "fix")


def test_a_runner_failure_is_measured_again_before_it_pauses(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    q.claim()
    q.finish(job["id"], "failed", "first receipt", "test_x failed", auto_remeasure=True)
    row = q.get(job["id"])
    assert row["status"] == "queued" and row["remeasures"] == 1 and row["notified"] == 1
    assert json.loads(row["history"])[0]["detail"] == "test_x failed"
    assert q.status(repo)["pauses"] == []
    q.admit(repo, "normal")  # merging goes on while the second measurement waits
    assert q.claim()["id"] == job["id"]
    q.finish(job["id"], "passed", "green receipt", auto_remeasure=True)
    row = q.get(job["id"])
    assert row["status"] == "passed" and "remeasure passed; intermittent" in row["detail"]
    assert q.status(repo)["pauses"] == []


def test_a_failure_that_repeats_takes_the_pause(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    q.claim()
    q.finish(job["id"], "failed", "first receipt", "test_x failed", auto_remeasure=True)
    q.claim()
    q.finish(job["id"], "failed", "second receipt", "test_x failed", auto_remeasure=True)
    assert q.get(job["id"])["status"] == "failed"
    assert q.status(repo)["pauses"][0]["job"] == job["id"]
    with pytest.raises(ValueError, match="paused"):
        q.admit(repo, "normal")


def test_suspects_are_the_merges_after_the_last_green_sweep_ranked_by_the_failing_tests(repo, tmp_path):
    q = queue(tmp_path)
    green, _ = enqueue(q, repo, "a")
    q.claim()
    q.finish(green["id"], "passed", "green receipt")
    other, _ = enqueue(q, repo, "other")
    (repo / "tests").mkdir(exist_ok=True)
    git(repo, "switch", "-q", "-c", "mod", "master")
    (repo / "mod.py").write_text("x = 1\n")
    (repo / "tests" / "test_mod.py").write_text("def test_mod():\n    pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "mod")
    git(repo, "switch", "-q", "master")
    git(repo, "merge", "--no-ff", "-qm", "merge mod", "mod")
    bad = q.enqueue(repo, git(repo, "rev-parse", "HEAD"), "mod", "leader", TOOL)
    later, _ = enqueue(q, repo, "later")
    failures = json.dumps(["FAILED tests/test_mod.py::test_mod - boom"])
    q.finish(bad["id"], "failed", "receipt", failures)
    found = q.suspects(bad["id"])
    assert found["base"] == green["sha"]
    assert [m["sha"] for m in found["suspects"]] == [bad["sha"], other["sha"]]
    assert found["suspects"][0]["meets_failing_tests"] == ["mod.py", "tests/test_mod.py"]
    assert found["suspects"][0]["issue"] == "mod" and found["suspects"][1]["meets_failing_tests"] == []
    assert [m["sha"] for m in found["after"]] == [later["sha"]]
    assert found["failing_tests"] == ["tests/test_mod.py"]


def test_suspects_without_a_green_sweep_are_the_latest_merges(repo, tmp_path):
    q = queue(tmp_path)
    first, _ = enqueue(q, repo, "a")
    second, _ = enqueue(q, repo, "b")
    q.claim()
    q.finish(second["id"], "failed", "receipt", "not a failure list")
    found = q.suspects(second["id"])
    assert found["base"] is None and found["failing_tests"] == []
    assert [m["sha"] for m in found["suspects"]] == [second["sha"], first["sha"]]


def test_a_failure_pauses_at_once_without_the_automatic_remeasure(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    q.claim()
    q.finish(job["id"], "failed", "receipt", "test_x failed")
    assert q.status(repo)["pauses"][0]["job"] == job["id"]


def test_remeasure_refuses_what_is_not_the_failed_pause_job(repo, tmp_path):
    q = queue(tmp_path)
    bad = paused_on(q, repo)
    other, _ = enqueue(q, repo, "other")
    with pytest.raises(ValueError, match="reason"):
        q.remeasure(repo, bad["id"], " ")
    with pytest.raises(ValueError, match="active failed job"):
        q.remeasure(repo, other["id"], "why")
    q.repair(repo, bad["id"], "fix")
    with pytest.raises(ValueError, match="repair issue fix"):
        q.remeasure(repo, bad["id"], "why")
    q2 = queue(tmp_path / "second")
    still = paused_on(q2, repo, "again")
    q2.remeasure(repo, still["id"], "why")
    with pytest.raises(ValueError, match="only a failed job"):
        q2.remeasure(repo, still["id"], "twice while queued")


def test_remeasure_from_the_command_line(repo, tmp_path):
    db = tmp_path / "jobs.sqlite3"
    q = mod.Queue(db)
    bad = paused_on(q, repo)
    tool = TOOL.with_name("delayed_sweep.py")
    out = subprocess.run([sys.executable, str(tool), "--database", str(db), "remeasure",
                          "--repo", str(repo), "--job", bad["id"], "--reason", "intermittent"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["status"] == "queued"


def test_another_instance_runs_with_the_recorded_origin_without_duplication(repo, tmp_path, monkeypatch):
    base = tmp_path / "shared-home"
    home = tmp_path / "origin-home"
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(home))
    monkeypatch.setenv(paths.INSTANCE_BASE_ENV, str(base))
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-a")
    first = mod.Queue()
    job, _ = enqueue(first, repo, "a")
    expected_scratch = paths.session_scratch_dir("leader")
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(tmp_path / "other-home"))
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-b")
    seen = []
    async def launch(*args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    second = mod.Queue()
    asyncio.run(Clock(SimpleNamespace(), second).tick())
    args, kwargs = seen[0]
    assert args[0] == job["python"]
    assert args[-1] == str(expected_scratch)
    env = kwargs["env"]
    assert env[paths.INSTANCE_ENV] == "review-a"
    assert env[paths.INSTANCE_BASE_ENV] == str(base)
    assert env["CLAUDE_LAUNCHER_HOME"] == str(home)
    assert env["CLAUNCH_SCRATCH"] == str(expected_scratch)
    assert (expected_scratch / ("delayed-sweep-" + job["id"] + ".log")).exists()
    asyncio.run(Clock(SimpleNamespace(), first).tick())
    assert len(seen) == 1
    assert first.get(job["id"])["attempts"] == 1


def test_runner_env_drops_the_daemons_manifest_bookkeeping(repo, tmp_path, monkeypatch):
    # A named instance's daemon exports its manifest bookkeeping; the suite's
    # first cli.main() would undo it and put the pre-manifest home over the
    # test's throwaway one (claunch-egr6y).
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-a")
    q = mod.Queue()
    enqueue(q, repo, "a")
    for name, value in ((im.APPLIED_ENV, "review-a"), (im.PORT_ENV, "8379"),
                        (im.PREV_ENV, '{"CLAUDE_LAUNCHER_HOME": null}')):
        monkeypatch.setenv(name, value)
    seen = []
    async def launch(*args, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    asyncio.run(Clock(SimpleNamespace(), q).tick())
    env = seen[0]["env"]
    assert env[paths.INSTANCE_ENV] == "review-a"
    for name in (im.APPLIED_ENV, im.PREV_ENV, im.PORT_ENV):
        assert name not in env
    # It runs without a session, but the Window page names who it is for.
    assert "CLAUNCH_SESSION" not in env
    assert env[test_window.WINDOW_REQUESTED_BY_ENV] == "leader"


def test_interrupted_runner_retries_but_live_runner_is_never_duplicated(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    assert q.claim()["id"] == job["id"]
    q.spawned(job["id"], os.getpid())
    assert mod.Queue(q.database).claim() is None
    with q.connect() as db:
        db.execute("UPDATE jobs SET pid=0,available=0 WHERE id=?", (job["id"],))
    assert mod.Queue(q.database).claim()["id"] == job["id"]
    assert q.get(job["id"])["attempts"] == 2


def test_a_repair_can_join_the_window_while_a_background_runner_is_alive(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    background, _ = enqueue(q, repo, "background")
    assert q.claim()["id"] == background["id"]
    q.spawned(background["id"], os.getpid())
    bad, _ = enqueue(q, repo, "bad")
    q.finish(bad["id"], "failed")
    q.repair(repo, bad["id"], "repair")
    repair, _ = enqueue(q, repo, "fix", "repair")
    launched = []
    async def launch(*args, **kwargs):
        launched.append(args)
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    manager = SimpleNamespace(get=lambda owner: SimpleNamespace(exited=True))
    window = SimpleNamespace(status=lambda: {"holders": [{"cls": "targeted"}], "queue": []})
    clock = Clock(manager, q, window=window)
    clock.runner = SimpleNamespace(pid=os.getpid(), returncode=None)
    asyncio.run(clock.tick())
    assert repair["id"] in launched[0]
    assert q.get(repair["id"])["status"] == "running"
    assert q.get(background["id"])["attempts"] == 1
    assert q.claim() is None  # never submit a duplicate live repair


def test_a_paused_repository_does_not_stop_another_repository(repo, tmp_path):
    q = queue(tmp_path)
    bad, _ = enqueue(q, repo, "bad")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
    git(other, "config", "user.name", "test")
    git(other, "config", "user.email", "test@example.com")
    good, _ = enqueue(q, other, "independent")
    q.finish(bad["id"], "failed")
    q.admit(other, "next")
    assert q.claim()["id"] == good["id"]


def test_busy_window_leaves_background_work_in_the_durable_queue(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    window = SimpleNamespace(status=lambda: {"holders": [{"cls": "targeted"}], "queue": []})
    asyncio.run(Clock(SimpleNamespace(), q, window=window).tick())
    assert q.get(job["id"])["status"] == "queued"


def test_runner_tests_the_fixed_sha_after_master_moves_and_releases_checkout(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    merge(repo, "b")
    command = subprocess.list2cmdline([sys.executable, "-c", "print('3 passed in 0.1s')"])
    with q.connect() as db:
        db.execute("UPDATE jobs SET command=? WHERE id=?", (command, job["id"]))
    q.claim()
    grant = test_window.WindowGrant("sweep", "unit", 1, "inherited", 0, None, _owns=False)
    seen = []
    monkeypatch.setattr(test_window, "acquire", lambda cls, **kw: seen.append(kw) or grant)
    mod.run_one(q, job["id"], tmp_path / "scratch")
    result = q.get(job["id"])
    assert result["status"] == "passed", result
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["commit"] == job["sha"]
    assert receipt["commit"] != git(repo, "rev-parse", "master")
    assert seen[0]["priority"] == -1
    assert not (tmp_path / "scratch" / ("delayed-sweep-" + job["id"]) / "1" / "checkout").exists()


def test_runner_never_turns_an_old_red_receipt_into_a_new_regression(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    with q.connect() as db:
        db.execute("UPDATE jobs SET command='unused' WHERE id=?", (job["id"],))
    q.claim()
    # The existing receipt is a real prior judgement. This attempt obtains no
    # window, so its infrastructure failure must stay retry, with no pause.
    # Use sweep's own receipt location rather than assuming its key algorithm.
    import importlib.util
    spec = importlib.util.spec_from_file_location("sweep_for_test", TOOL)
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    path = sweep.receipt_path(repo, job["sha"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"commit": job["sha"], "started_at": "2000-01-01",
                                "counts": {"failed": 1}, "exit_code": 1}))
    def unavailable(*args, **kwargs):
        raise test_window.WindowUnavailable("timeout")
    monkeypatch.setattr(test_window, "acquire", unavailable)
    mod.run_one(q, job["id"], tmp_path / "scratch")
    assert q.get(job["id"])["status"] == "retry"
    assert not q.status(repo)["pauses"]


@pytest.mark.parametrize("setup_fails", [False, True])
def test_default_runner_prepares_snapshot_dependencies_before_testing(repo, tmp_path, monkeypatch, setup_fails):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    tool = tmp_path / "snapshot_sweep.py"
    tool.write_text('''
import json
from datetime import datetime, timezone
from claude_launcher.delayed_sweep import git
def default_command(owner):
    return "uv run --no-sync pytest"
def receipt_path(repo, sha):
    return repo.parent / "receipt.json"
def cmd_run(args):
    assert ".venv" in args.command and "-m pytest" in args.command
    receipt_path(args.repo, "").write_text(json.dumps({
        "commit": git(args.repo, "rev-parse", "HEAD"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"passed": 3}}))
    return 0
def is_green(data):
    return data["counts"] == {"passed": 3}
''')
    with q.connect() as db:
        db.execute("UPDATE jobs SET tool=? WHERE id=?", (str(tool), job["id"]))
    (repo / ".git/info/exclude").write_text(".venv/\n")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "foreign"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "foreign"))
    real_run = subprocess.run
    synced = []
    def run(args, **kwargs):
        if args[:2] == ["uv", "sync"]:
            synced.append(kwargs)
            assert args[2:] == ["--extra", "test"]
            assert "UV_PROJECT_ENVIRONMENT" not in kwargs["env"]
            assert "VIRTUAL_ENV" not in kwargs["env"]
            if setup_fails:
                raise subprocess.CalledProcessError(2, args)
            python = kwargs["cwd"] / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()
            return subprocess.CompletedProcess(args, 0)
        return real_run(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    q.claim()
    mod.run_one(q, job["id"], tmp_path / "scratch")
    assert q.get(job["id"])["status"] == ("retry" if setup_fails else "passed")
    assert not q.status(repo)["pauses"]
    assert "checkout" == synced[0]["cwd"].name
    assert not synced[0]["cwd"].exists()


def test_clock_uses_a_daemon_owned_process_and_persists_its_pid(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    seen = []
    async def launch(*args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setenv("CLAUNCH_SESSION", "leader")
    monkeypatch.setenv(test_window.WINDOW_GRANT_ENV, "parent-grant")
    asyncio.run(Clock(SimpleNamespace(), q).tick())
    assert q.get(job["id"])["pid"] == os.getpid()
    env = seen[0][1]["env"]
    assert "CLAUNCH_SESSION" not in env
    assert test_window.WINDOW_GRANT_ENV not in env


def test_runner_env_has_no_empty_instance_and_always_a_git_identity(repo, tmp_path, monkeypatch):
    home = tmp_path / "bare-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("GIT_CONFIG_GLOBAL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(paths.INSTANCE_ENV, "")
    q = mod.Queue()
    enqueue(q, repo, "a")
    seen = []
    async def launch(*args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    asyncio.run(Clock(SimpleNamespace(), q).tick())
    env = seen[0]
    assert paths.INSTANCE_ENV not in env
    commit = subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo,
                            env=env, capture_output=True, text=True)
    assert commit.returncode == 0, commit.stderr


def test_git_identity_lookup_runs_off_the_event_loop(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    enqueue(q, repo, "a")
    threads = {}
    def ensure(env):
        threads["ensure"] = threading.get_ident()
    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(daemon_sweep, "_ensure_git_identity", ensure)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    async def tick():
        threads["loop"] = threading.get_ident()
        await Clock(SimpleNamespace(), q).tick()
    asyncio.run(tick())
    assert threads["ensure"] != threads["loop"]


def test_runner_path_starts_with_the_interpreters_directory(repo, tmp_path, monkeypatch):
    q = mod.Queue()
    job, _ = enqueue(q, repo, "a")  # runs git, so PATH is narrowed only afterwards
    monkeypatch.setenv("PATH", "/usr/bin")
    seen = []
    async def launch(*args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    asyncio.run(Clock(SimpleNamespace(), q).tick())
    first, rest = seen[0]["PATH"].split(os.pathsep, 1)
    assert first == str(Path(job["python"]).parent)
    assert rest == "/usr/bin"


def test_runner_gets_the_origins_home_and_no_config_file_of_its_own(repo, tmp_path, monkeypatch):
    # Another instance's daemon ran a default-instance job with the origin's
    # home and its own config file; the suite's cli.main created that
    # instance's profiles under the default home (claunch-edy0w). The config
    # file is now <home>/.claunch.yaml, so the home is all the runner gets.
    home = tmp_path / "origin-home"
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(home))
    q = mod.Queue()
    job, _ = enqueue(q, repo, "a")
    assert "config" not in json.loads(job["origin"])
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(tmp_path / "other-home"))
    monkeypatch.setenv(paths.INSTANCE_ENV, "review-b")
    seen = []
    async def launch(*args, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    asyncio.run(Clock(SimpleNamespace(), q).tick())
    env = seen[0]["env"]
    assert env["CLAUDE_LAUNCHER_HOME"] == str(home.resolve())


# --- batches and watching sessions (claunch-yfkt2) ---------------------------------------


def merges(q, repo, names):
    return [enqueue(q, repo, n)[0] for n in names]


def test_a_full_batch_is_swept_once_at_its_newest_checkpoint(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))
    claimed = q.claim(batch=(5, 3600))
    assert claimed["id"] == jobs[-1]["id"]
    assert [q.get(j["id"])["status"] for j in jobs[:-1]] == ["covered"] * 4
    assert {q.get(j["id"])["covered_by"] for j in jobs[:-1]} == {jobs[-1]["id"]}
    q.finish(claimed["id"], "passed", "receipt")
    done = [q.get(j["id"]) for j in jobs[:-1]]
    assert {j["status"] for j in done} == {"passed"}
    assert all(j["notified"] == 1 and j["receipt"] == "receipt" for j in done)


def test_equal_creation_times_keep_the_order_the_merges_were_enqueued_in(repo, tmp_path, monkeypatch):
    # time.time() repeats within one clock tick; the job id is a hash, so a tie broken by id
    # picked a random batch tip, a random status order and a random claim order.
    monkeypatch.setattr(mod.time, "time", lambda: 1000.0)
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))
    assert [j["id"] for j in q.status()["jobs"]] == [j["id"] for j in jobs]
    assert q.claim(batch=(5, 3600))["id"] == jobs[-1]["id"]
    assert [j["id"] for j in q.members(jobs[-1]["id"])] == [j["id"] for j in jobs[:-1]]
    solo = queue(tmp_path / "solo")
    singles = merges(solo, repo, list("fghij"))
    claimed = []
    for _ in singles:
        job = solo.claim()
        claimed.append(job["id"])
        solo.finish(job["id"], "passed", "receipt")
    assert claimed == [j["id"] for j in singles]


def test_a_short_group_waits_for_company_until_the_settle_time(repo, tmp_path):
    q = queue(tmp_path)
    merges(q, repo, ["a", "b"])
    assert q.claim(batch=(5, 3600)) is None
    job = q.claim(batch=(5, 0))
    assert job is not None and len(q.members(job["id"])) == 1


def test_a_batch_takes_at_most_its_size_and_the_rest_form_the_next(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcdefg"))
    first = q.claim(batch=(5, 3600))
    assert first["id"] == jobs[4]["id"]
    q.finish(first["id"], "passed", "r")
    assert q.claim(batch=(5, 3600)) is None  # two merges, settle not reached
    assert q.get(jobs[5]["id"])["formed"] == 0


def test_a_clock_stepping_back_does_not_reorder_the_pool(repo, tmp_path, monkeypatch):
    # The wall clock can step back (VM time sync) while merges are enqueued; the
    # batch must still end on the fifth enqueued merge, not on a later one stamped early.
    ticks = iter([100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 90.0])
    monkeypatch.setattr(mod.time, "time", lambda: next(ticks, 200.0))
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcdefg"))
    assert q.claim(batch=(5, 3600))["id"] == jobs[4]["id"]


def test_a_failed_batch_is_narrowed_by_unbatch_even_while_paused(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abc"))
    tip = q.claim(batch=(3, 0))
    q.finish(tip["id"], "failed", "r", "boom")
    assert q.status(repo)["pauses"][0]["job"] == tip["id"]
    assert [q.get(j["id"])["status"] for j in jobs[:2]] == ["covered", "covered"]
    assert q.unbatch(tip["id"])["released"] == 2
    again = q.claim(batch=(3, 0))
    assert again["id"] == jobs[0]["id"] and again["bisect_of"] == tip["id"]


def test_a_repeated_batch_failure_releases_the_covered_merges_by_itself(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abc"))
    tip = q.claim(batch=(3, 0))
    q.finish(tip["id"], "failed", "r", "boom", auto_remeasure=True)
    assert [q.get(j["id"])["status"] for j in jobs[:2]] == ["covered", "covered"]  # first failure: measured again
    assert q.status(repo)["pauses"] == []
    assert q.claim(batch=(3, 0))["id"] == tip["id"]
    q.finish(tip["id"], "failed", "r2", "boom", auto_remeasure=True)
    assert q.status(repo)["pauses"][0]["job"] == tip["id"]
    assert [q.get(j["id"])["status"] for j in jobs[:2]] == ["queued", "queued"]
    again = q.claim(batch=(3, 0))
    assert again["id"] == jobs[0]["id"] and again["bisect_of"] == tip["id"]


def test_unbatch_refuses_a_tip_that_has_no_verdict(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, ["a", "b"])
    tip = q.claim(batch=(2, 0))
    with pytest.raises(ValueError):
        q.unbatch(tip["id"])


def test_cancelling_a_tip_returns_its_merges_to_the_pool(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, ["a", "b", "c"])
    tip = q.claim(batch=(3, 0))
    q.cancel(tip["id"], "operator")
    assert [q.get(j["id"])["status"] for j in jobs[:2]] == ["queued", "queued"]
    assert all(q.get(j["id"])["covered_by"] is None for j in jobs[:2])


def test_a_repair_is_never_batched(repo, tmp_path):
    q = queue(tmp_path)
    bad = paused_on(q, repo)
    q.repair(repo, bad["id"], "fix")
    fix, _ = enqueue(q, repo, "fix", issue="fix")
    other, _ = enqueue(q, repo, "other")
    assert q.get(fix["id"])["priority"] == 0
    assert q.claim(batch=(5, 0))["id"] == fix["id"]
    assert q.get(fix["id"])["status"] == "running" and q.get(fix["id"])["covered_by"] is None


def test_without_a_batch_policy_every_merge_is_claimed_alone(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, ["a", "b"])
    assert q.claim()["id"] == jobs[0]["id"]


def test_job_state_reports_the_tip_verdict_for_a_covered_merge(repo, tmp_path):
    q = queue(tmp_path)
    jobs = merges(q, repo, ["a", "b"])
    tip = q.claim(batch=(2, 0))
    db = tmp_path / "jobs.sqlite3"
    tool = TOOL.with_name("delayed_sweep.py")
    run = lambda *a: subprocess.run([sys.executable, str(tool), "--database", str(db), *a],
                                    capture_output=True, text=True)
    waiting = run("job-state", "--job", jobs[0]["id"])
    assert waiting.returncode == 1 and json.loads(waiting.stdout)["tip"] == tip["id"]
    q.finish(tip["id"], "passed", "r")
    out = run("job-state", "--job", jobs[0]["id"])
    assert out.returncode == 0 and json.loads(out.stdout)["status"] == "passed"
    assert run("job-state", "--job", tip["id"]).returncode == 0
    assert run("nudge", "--job", jobs[0]["id"]).stdout.startswith("DONE")


def test_nudge_tells_a_waiting_job_to_stay_put(repo, tmp_path):
    q = queue(tmp_path)
    job, _ = enqueue(q, repo, "a")
    db = tmp_path / "jobs.sqlite3"
    out = subprocess.run([sys.executable, str(TOOL.with_name("delayed_sweep.py")), "--database", str(db),
                          "nudge", "--job", job["id"]], capture_output=True, text=True)
    assert out.returncode == 0
    assert out.stdout.split()[0] in ("QUEUED:", "WAITING")
    assert "Nothing needs doing" in out.stdout or "nothing needs doing" in out.stdout


def test_a_queue_file_without_the_batch_columns_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, repo TEXT NOT NULL, common TEXT NOT NULL, "
               "sha TEXT NOT NULL, ref TEXT NOT NULL, issue TEXT NOT NULL, owner TEXT NOT NULL, "
               "tool TEXT NOT NULL, python TEXT NOT NULL, command TEXT, status TEXT NOT NULL, "
               "created REAL NOT NULL, UNIQUE(common, sha))")
    db.commit()
    db.close()
    with mod.Queue(path).connect() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"covered_by", "formed", "child", "bisect_of"} <= columns


def spawner(names, fail=False):
    calls = []
    def spawn(parent, body):
        calls.append((parent, body))
        if fail:
            raise RuntimeError("daemon refused")
        return names[len(calls) - 1]
    return calls, spawn


def live_manager():
    return SimpleNamespace(get=lambda name: SimpleNamespace(exited=False))


def test_the_clock_gives_each_batch_tip_one_watching_child_and_still_sweeps(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))
    calls, spawn = spawner(["w1", "w2"])
    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    clock = Clock(live_manager(), q, batch=True, spawn=spawn, policy=(5, 3600))
    asyncio.run(clock.tick())
    asyncio.run(clock.tick())
    assert len(calls) == 1
    parent, body = calls[0]
    assert parent == "leader" and body["workflow"] == "sweep-job-v2" and body["workflow_version"] == 2
    assert body["cflow_inputs"]["job"] == jobs[-1]["id"] and body["beads"] is False
    assert q.get(jobs[-1]["id"])["child"] == "w1"
    assert q.get(jobs[-1]["id"])["status"] == "running"


def test_a_failed_spawn_is_retried_later_and_does_not_stop_the_sweep(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))
    calls, spawn = spawner([], fail=True)
    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    clock = Clock(live_manager(), q, batch=True, spawn=spawn, policy=(5, 3600))
    asyncio.run(clock.tick())
    asyncio.run(clock.tick())
    assert len(calls) == 1  # backed off
    assert q.get(jobs[-1]["id"])["status"] == "running"
    assert q.get(jobs[-1]["id"])["child"] == ""


def test_an_exited_watcher_is_replaced(repo, tmp_path, monkeypatch):
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))
    q.dispatch(5, 0)
    q.attach_child(jobs[-1]["id"], "gone")
    calls, spawn = spawner(["w2"])
    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    manager = SimpleNamespace(get=lambda name: SimpleNamespace(exited=(name == "gone")))
    asyncio.run(Clock(manager, q, batch=True, spawn=spawn, policy=(5, 3600)).tick())
    assert q.get(jobs[-1]["id"])["child"] == "w2"


def test_batching_follows_the_daemon_settings(monkeypatch):
    assert daemon_sweep.batch_policy({}) == (5, 600.0)
    assert daemon_sweep.batch_policy({"delayed_sweep_batch_size": 0}) is None
    assert daemon_sweep.batch_policy({"delayed_sweep_batch_size": 3,
                                      "delayed_sweep_batch_settle": 60}) == (3, 60.0)
