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
from claude_launcher.daemon import delayed_sweep as daemon_sweep, paths

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
    monkeypatch.setenv("PATH", "/usr/bin")
    q = mod.Queue()
    job, _ = enqueue(q, repo, "a")
    seen = []
    async def launch(*args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(pid=os.getpid(), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    asyncio.run(Clock(SimpleNamespace(), q).tick())
    first, rest = seen[0]["PATH"].split(os.pathsep, 1)
    assert first == str(Path(job["python"]).parent)
    assert rest == "/usr/bin"
