import ast
import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import artifact_cleanup_mcp as mcp
from claude_launcher.daemon import artifact_cleanup as ac, artifact_cleanup_api


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "basetemp"
    root.mkdir()
    sessions = tmp_path / "sessions"
    for name in ("s1", "s10", "s1-child"):
        (sessions / name / "scratch").mkdir(parents=True)
    monkeypatch.setattr(ac.basetemp, "default_root", lambda: root)
    monkeypatch.setattr(ac.paths, "session_dir", lambda s: sessions / s)
    monkeypatch.setattr(ac.paths, "session_scratch_dir", lambda s: sessions / s / "scratch")
    monkeypatch.setattr(ac, "_foreign_names", lambda: set())
    registry = {s: SimpleNamespace(exited=False, sdef=SimpleNamespace(name=s, cwd=str(tmp_path)))
                for s in ("s1", "s10", "s1-child")}
    manager = SimpleNamespace(get=lambda s: registry[s], list=lambda: list(registry.values()))
    return ac.Cleanup(manager), root, sessions, registry


def completed(path):
    path.mkdir()
    (path / "payload").write_text("test data")
    (path / ac.basetemp.RUN_SUMMARY).write_text(json.dumps({
        "finished_at": "2026-10-12T00:00:00Z", "started_at": "2026-10-11T23:59:00Z",
        "exit_code": 0, "workers": 2, "counts": {"passed": 3},
    }))
    return path


def test_plan_reads_and_apply_deletes_only_owned_completed_runs(setup):
    service, root, sessions, _ = setup
    mine = completed(root / "s1c1012000000")
    others = [completed(root / n) for n in ("s10c1012000000", "s1-childc1012000000")]
    unknown = root / "s1-unfinished"
    unknown.mkdir()
    plan = service.plan("s1")
    assert mine.exists()
    assert len(plan["items"]) == 2
    result = service.apply("s1", plan["plan_id"])
    assert not mine.exists() and all(p.exists() for p in others) and unknown.exists()
    assert (result["removed"], result["cleanup_pending"]) == (1, 1)
    receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
    assert receipt["items"][0]["path"]
    removed = next(x for x in receipt["items"] if x["status"] == "removed")
    assert removed["run"]["counts"] == {"passed": 3}
    assert removed["bytes"] > 0
    with pytest.raises(ac.CleanupError):
        service.apply("s1", plan["plan_id"])


def test_modified_and_foreign_artifacts_are_preserved(setup, monkeypatch):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    plan = service.plan("s1")
    (mine / "payload").write_text("changed user data")
    assert service.apply("s1", plan["plan_id"])["removed"] == 0
    monkeypatch.setattr(ac, "_foreign_names", lambda: {"s1"})
    plan = service.plan("s1")
    assert plan["items"][0]["status"] == "cleanup_pending"
    assert mine.exists()


def test_plan_cannot_be_used_by_another_session_or_after_expiry(setup, monkeypatch):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    plan = service.plan("s1")
    with pytest.raises(ac.CleanupError, match="foreign"):
        service.apply("s10", plan["plan_id"])
    monkeypatch.setattr(ac.time, "time", lambda: plan["created_at"] + 601)
    with pytest.raises(ac.CleanupError, match="expired"):
        service.apply("s1", plan["plan_id"])
    assert mine.exists()


def test_evidence_write_failure_prevents_deletion(setup, monkeypatch):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    plan = service.plan("s1")
    def fail(_):
        raise OSError("disk full")
    monkeypatch.setattr(service, "_save", fail)
    with pytest.raises(OSError, match="disk full"):
        service.apply("s1", plan["plan_id"])
    assert mine.exists()


def test_the_general_test_dir_is_listed_but_never_deleted_mid_life(setup):
    """``<root>/<session>`` ($CLAUNCH_TEST_DIR) shows in the plan, and stays.

    It carries mixed, possibly in-use artifacts (a browser profile) rather
    than one completed run, so the round-close cleanup keeps it pending;
    its lifecycle is the exit reaper's, not this tool's.
    """
    service, root, _, _ = setup
    general = root / "s1"
    (general / "browser-profile").mkdir(parents=True)
    (general / "browser-profile" / "prefs.js").write_text("x", encoding="utf-8")
    plan = service.plan("s1")
    item = next(i for i in plan["items"] if i["path"] == str(general))
    assert item["status"] == "cleanup_pending"
    assert "claunch-run.json" in item["reason"]
    result = service.apply("s1", plan["plan_id"])
    assert general.exists()
    assert all(i["status"] != "removed" for i in result["items"])


def test_deletion_failure_has_real_reason_and_saved_intent(setup, monkeypatch):
    import shutil
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    plan = service.plan("s1")
    def fail(path):
        receipt = json.loads((ac.paths.session_dir("s1") / "cleanup" / (plan["plan_id"] + ".json")).read_text(encoding="utf-8"))
        assert receipt["items"][0]["status"] == "deleting"
        raise PermissionError("denied by filesystem")
    monkeypatch.setattr(shutil, "rmtree", fail)
    result = service.apply("s1", plan["plan_id"])
    assert result["items"][0]["reason"] == "denied by filesystem"
    assert mine.exists()


def link(path, target):
    try:
        path.symlink_to(target, target_is_directory=Path(target).is_dir())
    except OSError:
        pytest.skip("symlink privilege unavailable")
    return path


def test_target_root_link_stays_pending_and_external_files_survive(setup, tmp_path):
    service, root, _, _ = setup
    outside = completed(tmp_path / "external")
    link(root / "s1c1012000000", outside)
    plan = service.plan("s1")
    assert plan["items"][0]["status"] == "cleanup_pending"
    assert "link or reparse point" in plan["items"][0]["reason"]
    assert service.apply("s1", plan["plan_id"])["removed"] == 0
    assert (outside / "payload").exists()


def test_pytest_current_links_to_siblings_are_unlinked_and_tree_removed(setup):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    sibling = mine / "test_a0"
    sibling.mkdir()
    (sibling / "data").write_text("x" * 100)
    link(mine / "test_acurrent", sibling)  # absolute, as pytest makes it
    plan = service.plan("s1")
    item = plan["items"][0]
    assert item["status"] == "ready", item
    # The link adds no file and no bytes: the sibling is counted once.
    assert item["files"] == 3
    assert item["bytes"] == sum(f.stat().st_size for f in mine.rglob("*")
                                if f.is_file() and not f.is_symlink())
    result = service.apply("s1", plan["plan_id"])
    assert result["removed"] == 1 and not mine.exists()


def test_links_out_of_the_tree_are_unlinked_and_their_targets_kept(setup, tmp_path, monkeypatch):
    service, root, _, _ = setup
    outside = completed(tmp_path / "external")
    (outside / "big").write_bytes(b"0" * 4096)
    mine = completed(root / "s1c1012000000")
    link(mine / "dirlink", outside)
    link(mine / "filelink", outside / "big")
    item = service.plan("s1")["items"][0]
    assert item["status"] == "ready", item
    assert item["files"] == 2 and item["bytes"] < 4096
    removed = []
    real = ac._unlink_links
    def spy(path):
        real(path)
        removed.append(sorted(p.name for p in path.iterdir()))
    monkeypatch.setattr(ac, "_unlink_links", spy)
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 1 and not mine.exists()
    # Links were gone before rmtree ran; the targets are untouched.
    assert removed == [[ac.basetemp.RUN_SUMMARY, "payload"]]
    assert (outside / "payload").exists() and (outside / "big").stat().st_size == 4096


def test_link_retargeted_after_plan_is_a_change(setup, tmp_path):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    (mine / "a").mkdir()
    (mine / "b").mkdir()
    current = link(mine / "current", mine / "a")
    plan = service.plan("s1")
    assert plan["items"][0]["status"] == "ready"
    current.unlink()
    current.symlink_to(mine / "b", target_is_directory=True)
    result = service.apply("s1", plan["plan_id"])
    assert result["removed"] == 0
    assert result["items"][0]["reason"] == "artifact changed since plan"
    assert (mine / "current").is_symlink()


def test_linked_run_receipt_is_not_completion_evidence(setup, tmp_path):
    service, root, _, _ = setup
    donor = completed(tmp_path / "donor")
    mine = root / "s1c1012000000"
    mine.mkdir()
    link(mine / ac.basetemp.RUN_SUMMARY, donor / ac.basetemp.RUN_SUMMARY)
    item = service.plan("s1")["items"][0]
    assert item["status"] == "cleanup_pending"
    assert item["reason"] == "test run receipt is not a regular file"


def test_scratch_unknown_browser_is_preserved_and_gone_gitdir_is_removed(setup):
    service, root, sessions, _ = setup
    scratch = sessions / "s1" / "scratch"
    (scratch / "browser").mkdir()
    mine = completed(root / "s1c1012000000")
    nested = mine / "nested"
    nested.mkdir()
    # "elsewhere" resolves next to the file and does not exist: the gitdir is
    # gone, so this is no longer anyone's worktree (claunch-f9ene.1.1 DECISION).
    (nested / ".git").write_text("gitdir: elsewhere")
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 1 and result["cleanup_pending"] == 1
    assert (scratch / "browser").exists() and not mine.exists()


def nested_gitfile(mine, gitdir):
    nested = mine / "nested"
    nested.mkdir()
    (nested / ".git").write_text(f"gitdir: {gitdir}\n")
    return nested


def test_nested_gitfile_pointing_at_an_existing_gitdir_outside_is_preserved(setup, tmp_path):
    service, root, _, _ = setup
    outside = tmp_path / "other-repo" / ".git" / "worktrees" / "nested"
    outside.mkdir(parents=True)
    mine = completed(root / "s1c1012000000")
    nested_gitfile(mine, outside)
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 0 and mine.exists() and outside.exists()
    assert result["items"][0]["reason"].startswith("nested linked worktree:")


def test_nested_gitdir_reaching_outside_through_a_link_in_the_tree_is_preserved(setup, tmp_path):
    service, root, _, _ = setup
    outside = tmp_path / "other-repo"
    (outside / "gitdir").mkdir(parents=True)
    mine = completed(root / "s1c1012000000")
    link(mine / "escape", outside)
    # Textually inside the tree; really the existing gitdir of another repo.
    nested_gitfile(mine, "../escape/gitdir")
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 0 and mine.exists()
    assert "nested linked worktree" in result["items"][0]["reason"]
    assert (outside / "gitdir").exists()


@pytest.mark.parametrize("content", [b"", b"not a gitfile", b"gitdir: ", b"gitdir: \xff\xfe",
                                     b"gitdir: gone\nsecond line\n"])
def test_nested_gitfile_that_names_no_gitdir_is_preserved(setup, content):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    (mine / "nested").mkdir()
    broken = mine / "nested" / ".git"
    broken.write_bytes(content)
    try:
        result = service.apply("s1", service.plan("s1")["plan_id"])
        assert result["removed"] == 0 and mine.exists()
    finally:
        # Left behind, this file keeps the whole basetemp cleanup_pending.
        broken.unlink(missing_ok=True)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_gitdir_that_cannot_be_looked_at_is_not_taken_for_gone(setup, tmp_path):
    service, root, _, _ = setup
    sealed = tmp_path / "sealed"
    (sealed / "gitdir").mkdir(parents=True)
    mine = completed(root / "s1c1012000000")
    nested_gitfile(mine, sealed / "gitdir")
    sealed.chmod(0)
    try:
        result = service.apply("s1", service.plan("s1")["plan_id"])
    finally:
        sealed.chmod(0o755)
    assert result["removed"] == 0 and mine.exists()
    assert "nested linked worktree" in result["items"][0]["reason"]


def test_worktree_fixtures_whose_gitdir_is_in_the_same_tree_are_removed(setup):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    repo = mine / "test_registered_preview0" / "repo"
    repo.mkdir(parents=True)
    git(repo, "init")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test",
        "commit", "--allow-empty", "-m", "init")
    git(repo, "worktree", "add", "--detach", str(mine / "test_registered_preview0" / "wt"))
    assert (mine / "test_registered_preview0" / "wt" / ".git").is_file()
    if os.name == "nt":
        # git writes loose objects read-only; apply() records that as pending and
        # never changes permissions, so the fixture is made removable up front.
        for walked, _, names in os.walk(repo / ".git"):
            for name in names:
                os.chmod(os.path.join(walked, name), 0o666)
    plan = service.plan("s1")
    assert plan["items"][0]["status"] == "ready", plan["items"][0]
    result = service.apply("s1", plan["plan_id"])
    assert result["removed"] == 1 and not mine.exists()


def test_gitdir_rewritten_after_plan_is_a_change_even_with_size_and_mtime_kept(setup, tmp_path):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    gitfile = nested_gitfile(mine, "gone-a") / ".git"
    before = gitfile.stat()
    plan = service.plan("s1")
    assert plan["items"][0]["status"] == "ready"
    gitfile.write_text("gitdir: gone-b\n")
    os.utime(gitfile, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = service.apply("s1", plan["plan_id"])
    assert result["removed"] == 0 and mine.exists()
    assert result["items"][0]["reason"] == "artifact changed since plan"


def test_gitdir_that_appears_outside_after_plan_is_preserved(setup, tmp_path):
    service, root, _, _ = setup
    outside = tmp_path / "late"
    mine = completed(root / "s1c1012000000")
    nested_gitfile(mine, outside)
    plan = service.plan("s1")
    assert plan["items"][0]["status"] == "ready"
    outside.mkdir()
    result = service.apply("s1", plan["plan_id"])
    assert result["removed"] == 0 and mine.exists()
    assert "nested linked worktree" in result["items"][0]["reason"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_windows_junction_is_preserved_without_traversing_it(setup, tmp_path):
    service, root, _, _ = setup
    external = completed(tmp_path / "external")
    target = root / "s1c1012000000"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(external)],
                   capture_output=True, check=True)
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 0
    assert "reparse" in result["items"][0]["reason"]
    assert (external / "payload").exists()


def test_active_session_occupancy_and_late_foreign_owner_prevent_cleanup(setup, monkeypatch):
    service, root, _, registry = setup
    mine = completed(root / "s1c1012000000")
    plan = service.plan("s1")
    registry["s10"].sdef.cwd = str(mine)
    result = service.apply("s1", plan["plan_id"])
    assert result["removed"] == 0 and "session uses" in result["items"][0]["reason"]
    registry["s10"].sdef.cwd = str(root.parent)
    plan = service.plan("s1")
    monkeypatch.setattr(ac, "_foreign_names", lambda: {"s1"})
    assert service.apply("s1", plan["plan_id"])["removed"] == 0
    assert mine.exists()


def test_foreign_registry_read_errors_are_not_treated_as_empty(tmp_path, monkeypatch):
    base = tmp_path / "home"
    foreign = base / "daemons" / "other"
    foreign.mkdir(parents=True)
    monkeypatch.setattr(ac.paths, "base_home", lambda: base)
    monkeypatch.setattr(ac.paths, "daemon_dir", lambda: base / "daemon")
    registry = foreign / "sessions.json"
    registry.write_text(json.dumps([{"def": {"name": "s1"}, "was_running": False}]))
    assert ac._foreign_names() == {"s1"}
    registry.write_text("not json")
    with pytest.raises(ValueError):
        ac._foreign_names()


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_registered_preview_removal_preserves_ref_and_dirty_worktree(setup, tmp_path):
    service, _, sessions, registry = setup
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "init")
    registry["s1"].sdef.cwd = str(repo)
    clean = sessions / "s1" / "scratch" / "s1-preview-clean"
    dirty = sessions / "s1" / "scratch" / "s1-preview-dirty"
    for path in (clean, dirty):
        git(repo, "worktree", "add", "--detach", str(path))
    git(repo, "update-ref", "refs/claunch/preview/test", "HEAD")
    (dirty / "user.txt").write_text("keep me")
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 1 and not clean.exists()
    assert (dirty / "user.txt").read_text() == "keep me"
    git(repo, "show-ref", "--verify", "refs/claunch/preview/test")


def test_preview_worktree_keeps_rejecting_nested_gitfiles(setup, tmp_path):
    service, _, sessions, registry = setup
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "init")
    registry["s1"].sdef.cwd = str(repo)
    preview = sessions / "s1" / "scratch" / "s1-preview-nested"
    git(repo, "worktree", "add", "--detach", str(preview))
    git(repo, "update-ref", "refs/claunch/preview/test", "HEAD")
    nested_gitfile(preview, "gone")  # would pass the basetemp rule
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 0 and preview.exists()
    assert result["items"][0]["reason"] == f"nested linked worktree: {preview / 'nested'}"


@pytest.mark.parametrize("reference", ["absent", "removed_after_plan", "preserved"])
def test_detached_preview_requires_persistent_reachability(setup, tmp_path, reference):
    service, _, sessions, registry = setup
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    commit = ("-c", "user.name=Test", "-c", "user.email=test@example.test",
              "commit", "--allow-empty", "-m")
    git(repo, *commit, "base")
    registry["s1"].sdef.cwd = str(repo)
    preview = sessions / "s1" / "scratch" / "s1-preview-unique"
    git(repo, "worktree", "add", "--detach", str(preview))
    git(preview, *commit, "detached-only")
    head = ac._git(preview, "rev-parse", "HEAD").strip()
    ref = "refs/claunch/preview/unique"
    if reference != "absent":
        git(repo, "update-ref", ref, head)
    plan = service.plan("s1")
    if reference == "absent":
        assert plan["items"][0]["status"] == "cleanup_pending"
    else:
        assert plan["items"][0]["preserving_refs"] == [ref]
    if reference == "removed_after_plan":
        git(repo, "update-ref", "-d", ref)
    result = service.apply("s1", plan["plan_id"])
    if reference == "preserved":
        assert result["removed"] == 1 and not preview.exists()
        assert ac._git(repo, "rev-parse", ref).strip() == head
    else:
        assert result["removed"] == 0 and preview.exists()
        assert "no preserving persistent ref" in result["items"][0]["reason"]


def test_mcp_binds_session_and_rejects_paths(monkeypatch):
    calls = []
    client = SimpleNamespace(post=lambda *a, **kw: calls.append((a, kw)) or {})
    monkeypatch.setattr(mcp.daemon_client, "connect_with_diagnosis", lambda: (client, ""))
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    mcp.call_tool("artifact_cleanup", {})
    mcp.call_tool("artifact_cleanup", {"plan_id": "token"})
    assert calls[1][0][1] == {"session": "s1", "plan_id": "token"}
    for args in ({"path": "C:/"}, {"session": "s2"}, {"plan_id": []}):
        with pytest.raises(mcp.CleanupMcpError):
            mcp.call_tool("artifact_cleanup", args)
    monkeypatch.delenv("CLAUNCH_SESSION")
    with pytest.raises(mcp.CleanupMcpError):
        mcp.call_tool("artifact_cleanup", {})


def test_api_holds_exclusive_window_and_reports_busy(setup, monkeypatch):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    class Window:
        busy = True
        held = False
        async def acquire(self, cls, **kwargs):
            assert cls == "sweep" and kwargs["wait"] == 0 and kwargs["session"] is None
            if self.busy:
                return {"granted": False, "reason": "test running"}
            self.held = True
            return {"granted": True, "grant_id": "cleanup"}
        def release(self, token):
            assert self.held and token == "cleanup"
            self.held = False
    window = Window()
    original = ac.Cleanup.apply
    def checked_apply(self, *args):
        assert window.held
        return original(self, *args)
    monkeypatch.setattr(ac.Cleanup, "apply", checked_apply)
    async def scenario():
        app = web.Application()
        app["manager"], app["window"] = service.manager, window
        artifact_cleanup_api.install(app)
        async with TestClient(TestServer(app)) as client:
            async def post(body):
                return await client.post("/api/artifact-cleanup", json=body)
            assert (await post({"session": "s1", "path": "C:/"})).status == 400
            assert (await post([])).status == 400
            plan = await (await post({"session": "s1"})).json()
            request = {"session": "s1", "plan_id": plan["plan_id"]}
            result = await (await post(request)).json()
            # A refusal is now kept as a schedule, not dropped: no later turn
            # exists for the caller to retry from (claunch-q4cqy.2).
            assert result["status"] == "scheduled" and mine.exists()
            assert result["reason"] == "test running"
            assert service._schedule_record("s1")["last_reason"] == "test running"
            window.busy = False
            result = await (await post(request)).json()
            assert result["removed"] == 1 and not window.held
            # The schedule served its purpose; a later pass finds nothing to do.
            assert service.run_scheduled("s1")["status"] == "none"
    asyncio.run(scenario())


def test_scratch_artifacts_are_listed_whatever_their_name(setup, tmp_path):
    # claunch-30rkn: preview-r2 and bt-preview matched no name rule, so the
    # plan came back empty while 400MB of them sat in the session's scratch.
    service, _, sessions, registry = setup
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "init")
    git(repo, "update-ref", "refs/claunch/preview/test", "HEAD")
    registry["s1"].sdef.cwd = str(repo)
    scratch = sessions / "s1" / "scratch"
    preview = scratch / "preview-r2"
    git(repo, "worktree", "add", "--detach", str(preview))
    (scratch / "bt-preview" / "popen-gw0").mkdir(parents=True)
    completed(scratch / "runs-1")
    (scratch / "notes").mkdir()
    (scratch / "report.md").write_text("kept")
    link(scratch / "linked", preview)
    plan = service.plan("s1")
    rows = {Path(row["path"]).name: row for row in plan["items"]}
    assert set(rows) == {"preview-r2", "bt-preview", "runs-1"}
    assert rows["preview-r2"]["status"] == "ready" and rows["runs-1"]["status"] == "ready"
    assert rows["bt-preview"]["status"] == "cleanup_pending"
    assert ac.basetemp.RUN_SUMMARY in rows["bt-preview"]["reason"]
    result = service.apply("s1", plan["plan_id"])
    assert result["removed"] == 2 and not preview.exists()
    assert (scratch / "bt-preview").exists() and (scratch / "notes").exists()
    git(repo, "show-ref", "--verify", "refs/claunch/preview/test")


def test_scratch_without_a_repository_keeps_the_name_rule(setup):
    service, _, sessions, _ = setup
    scratch = sessions / "s1" / "scratch"
    (scratch / "preview-r3").mkdir()
    assert service.plan("s1")["items"] == []


def test_plan_over_its_time_budget_keeps_the_rest_unmeasured(setup):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    plan = service.plan("s1", budget=-1)
    assert plan["items"][0]["status"] == "cleanup_pending"
    assert "time budget" in plan["items"][0]["reason"]
    assert service.apply("s1", plan["plan_id"])["removed"] == 0 and mine.exists()


def test_fingerprint_names_entries_as_relative_paths(setup, tmp_path, monkeypatch):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    (mine / "a" / "b").mkdir(parents=True)
    (mine / "a" / "b" / "f").write_text("x")
    link(mine / "a" / "link", tmp_path)
    nested_gitfile(mine / "a", "gone")
    fed = []
    real = ac.hashlib.sha256
    class Recorder:
        def __init__(self):
            self.inner = real()
        def update(self, data):
            fed.append(data.decode())
            self.inner.update(data)
        def hexdigest(self):
            return self.inner.hexdigest()
    monkeypatch.setattr(ac.hashlib, "sha256", Recorder)
    ac._snapshot(mine)
    # String slicing replaced Path.relative_to (claunch-30rkn); the names fed
    # to the fingerprint must be the ones relative_to would have produced.
    expected = []
    for walked, dirs, names in os.walk(mine):
        dirs[:] = [d for d in dirs if not (Path(walked) / d).is_symlink()]
        expected += [str((Path(walked) / n).relative_to(mine)) for n in dirs + names]
    expected += [str((mine / "a" / "link").relative_to(mine))]
    named = {ast.literal_eval(line)[0] for line in fed[:-1]}
    assert named == set(expected)


def test_api_plans_once_per_session_and_never_across_sessions(setup, monkeypatch):
    import threading
    service, _, _, _ = setup
    release = threading.Event()
    walks = []
    original = ac.Cleanup.plan
    def slow_plan(self, session, *args):
        walks.append(session)
        if session == "s1":
            assert release.wait(10)
        return original(self, session, *args)
    monkeypatch.setattr(ac.Cleanup, "plan", slow_plan)
    async def scenario():
        app = web.Application()
        app["manager"], app["window"] = service.manager, None
        artifact_cleanup_api.install(app)
        async with TestClient(TestServer(app)) as client:
            async def post(session):
                return await (await client.post("/api/artifact-cleanup", json={"session": session})).json()
            first = asyncio.ensure_future(post("s1"))
            retry = asyncio.ensure_future(post("s1"))
            # s1's walk is stuck; s10's plan is answered regardless.
            other = await asyncio.wait_for(post("s10"), 5)
            assert other["session"] == "s10"
            assert not first.done() and not retry.done()
            release.set()
            a, b = await asyncio.wait_for(asyncio.gather(first, retry), 5)
            assert a["plan_id"] == b["plan_id"]
            assert (await client.post("/api/artifact-cleanup", json={"session": []})).status == 400
    asyncio.run(scenario())
    assert sorted(walks) == ["s1", "s10"]


# ---- scheduled applies (claunch-q4cqy.2) -----------------------------------
#
# An apply refused because the measurement window is busy used to end there: the
# caller's turn was over and auto-release has no later turn. Four sessions in one
# evening left ~1.15 GB that way. The daemon now keeps the request and replans it
# when the window frees up.


def test_a_busy_window_schedules_instead_of_ending(setup, monkeypatch):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")

    class Window:
        busy = True
        async def acquire(self, cls, **kwargs):
            assert cls == "sweep" and kwargs["wait"] == 0
            if self.busy:
                return {"granted": False, "reason": "the window is held by targeted:s9"}
            return {"granted": True, "grant_id": "cleanup"}
        def release(self, token):
            assert token == "cleanup"

    window = Window()

    async def scenario():
        app = web.Application()
        app["manager"], app["window"] = service.manager, window
        artifact_cleanup_api.install(app)
        async with TestClient(TestServer(app)) as client:
            plan = await (await client.post("/api/artifact-cleanup", json={"session": "s1"})).json()
            body = {"session": "s1", "plan_id": plan["plan_id"]}
            result = await (await client.post("/api/artifact-cleanup", json=body)).json()
            assert result["status"] == "scheduled"
            assert "the window is held" in result["reason"]
            assert result["already"] is False  # this call started the clock
            # The plan did not run; the tree is untouched.
            assert mine.exists()
            # The daemon's later pass finds the request and deletes with a free window.
            window.busy = False
            ran = service.run_scheduled("s1")
            assert ran["status"] == "ran" and ran["removed"] == 1
            assert not mine.exists()
            assert service.scheduled() == []

    asyncio.run(scenario())


def test_scheduling_twice_keeps_the_first_clock_and_attempt_count(setup, monkeypatch):
    service, _, _, _ = setup
    monkeypatch.setattr(ac, "SCHEDULE_TTL", 3600.0)
    first = service.schedule("s1", "busy")
    monkeypatch.setattr(ac.Cleanup, "plan", lambda self, session, **kw: (_ for _ in ()).throw(
        ac.CleanupError("still busy")))
    assert service.run_scheduled("s1")["status"] == "cleanup_pending"
    second = service.schedule("s1", "busy again")
    assert second["created_at"] == first["created_at"]
    assert second["expires_at"] == first["expires_at"]
    assert second["attempts"] == 1 and second["last_reason"] == "busy again"


def test_a_schedule_gives_up_at_its_attempt_bound(setup, monkeypatch):
    service, _, _, _ = setup
    monkeypatch.setattr(ac, "SCHEDULE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(ac.Cleanup, "plan", lambda self, session, **kw: (_ for _ in ()).throw(
        ac.CleanupError("still busy")))
    service.schedule("s1", "busy")
    assert service.run_scheduled("s1")["status"] == "cleanup_pending"
    assert service.run_scheduled("s1")["status"] == "cleanup_pending"
    result = service.run_scheduled("s1")
    assert result["status"] == "expired" and result["attempts"] == 2
    assert service._schedule_record("s1") is None


def test_a_schedule_for_an_ended_session_is_abandoned(setup):
    service, root, _, registry = setup
    service.schedule("s1", "busy")
    registry["s1"].exited = True
    result = service.run_scheduled("s1")
    assert result["status"] == "abandoned"
    assert service._schedule_record("s1") is None
    assert service.scheduled() == []


def test_scheduled_lists_live_sessions_only(setup):
    service, _, _, registry = setup
    service.schedule("s1", "busy")
    service.schedule("s10", "busy")
    registry["s10"].exited = True
    assert service.scheduled() == ["s1"]


def test_the_clock_runs_a_due_schedule_when_the_window_frees(setup):
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")

    class Window:
        def __init__(self):
            self.held = False
        async def acquire(self, cls, **kwargs):
            if self.held:
                return {"granted": False, "reason": "busy"}
            self.held = True
            return {"granted": True, "grant_id": "clock"}
        def release(self, token):
            assert self.held and token == "clock"
            self.held = False

    window = Window()
    service.schedule("s1", "the window is held by targeted:s9")

    async def scenario():
        app = {"window": window}
        task = asyncio.ensure_future(
            artifact_cleanup_api.scheduled_clock(app, service, interval=0.01))
        try:
            # The pass runs in a worker thread: wait for the schedule to be
            # dropped, not just for the directory to disappear.
            for _ in range(500):
                if not mine.exists() and service.scheduled() == []:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not mine.exists() and not window.held
        assert service.scheduled() == []

    asyncio.run(scenario())


def test_re_scheduling_after_expiry_starts_a_fresh_clock(setup, monkeypatch):
    # claunch-q4cqy.2 review, finding 1: inheriting an expired record's
    # expires_at answered "scheduled" for a request the daemon would drop on
    # its next pass, so nothing ever ran.
    service, _, _, _ = setup
    monkeypatch.setattr(ac, "SCHEDULE_TTL", 3600.0)
    first = service.schedule("s1", "busy")
    stale = service._schedule_record("s1")
    stale["expires_at"] = time.time() - 1
    stale["attempts"] = 3
    service._write_schedule(stale)
    again = service.schedule("s1", "busy again")
    assert again["expires_at"] > time.time()
    assert again["expires_at"] != first["expires_at"]
    assert again["attempts"] == 0
    assert service.scheduled() == ["s1"]
    assert service.run_scheduled("s1")["status"] == "ran"


def test_scheduled_drops_an_expired_record(setup):
    # Finding 2: nothing else walks the cleanup folder, so an expired record
    # stayed on disk forever.
    service, _, _, _ = setup
    service.schedule("s1", "busy")
    stale = service._schedule_record("s1")
    stale["expires_at"] = time.time() - 1
    service._write_schedule(stale)
    assert service.scheduled() == []
    assert service._schedule_record("s1") is None
    assert not service._schedule_file("s1").exists()


def test_a_refusal_that_merely_mentions_a_session_keeps_the_schedule(setup, monkeypatch):
    # Finding 5: "invalid session name in ..." also contains "session", and
    # matching it abandoned a cleanup that could still run.
    service, _, _, _ = setup
    monkeypatch.setattr(ac.Cleanup, "plan", lambda self, session, **kw: (_ for _ in ()).throw(
        ac.CleanupError("invalid session name in C:/scratch")))
    service.schedule("s1", "busy")
    result = service.run_scheduled("s1")
    assert result["status"] == "cleanup_pending"
    assert service._schedule_record("s1") is not None
    assert service.scheduled() == ["s1"]


def test_a_plan_and_a_second_refusal_report_the_standing_schedule(setup):
    # Finding 3: a caller that sees the standing schedule reports it instead of
    # scheduling a retry of its own.
    service, root, _, _ = setup
    completed(root / "s1c1012000000")
    service.schedule("s1", "the window is held by targeted:s9")
    plan = service.plan("s1")
    assert plan["scheduled"]["last_reason"] == "the window is held by targeted:s9"
    assert plan["scheduled"]["attempts"] == 0

    class Window:
        async def acquire(self, cls, **kwargs):
            return {"granted": False, "reason": "the window is held by targeted:s9"}
        def release(self, token):
            raise AssertionError("an ungranted window is never released")

    async def scenario():
        app = web.Application()
        app["manager"], app["window"] = service.manager, Window()
        artifact_cleanup_api.install(app)
        before = service._schedule_record("s1")["created_at"]
        async with TestClient(TestServer(app)) as client:
            body = {"session": "s1", "plan_id": service.plan("s1")["plan_id"]}
            first = await (await client.post("/api/artifact-cleanup", json=body)).json()
            second = await (await client.post("/api/artifact-cleanup", json=body)).json()
            # Both refusals point at the schedule that was already standing;
            # neither starts a second clock.
            assert first["status"] == "scheduled" and first["already"] is True
            assert second["status"] == "scheduled" and second["already"] is True
            assert service._schedule_record("s1")["created_at"] == before
    asyncio.run(scenario())


def test_cancelling_the_clock_waits_for_a_running_cleanup(setup, monkeypatch):
    # Finding 4: cancellation returned the exclusive grant while the deletion
    # thread was still running, so a test could start on half-deleted files.
    service, root, _, _ = setup
    completed(root / "s1c1012000000")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = ac.Cleanup.run_scheduled

    def slow(self, session):
        started.set()
        try:
            return original(self, session)
        finally:
            release.wait(10)
            finished.set()

    monkeypatch.setattr(ac.Cleanup, "run_scheduled", slow)
    service.schedule("s1", "busy")
    held = {"now": False}

    class Window:
        async def acquire(self, cls, **kwargs):
            held["now"] = True
            return {"granted": True, "grant_id": "clock"}
        def release(self, token):
            # The only assertion that matters: the deletion is over by now.
            assert finished.is_set(), "released before the cleanup thread finished"
            held["now"] = False

    async def scenario():
        task = asyncio.ensure_future(
            artifact_cleanup_api.scheduled_clock({"window": Window()}, service, interval=0.01))
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.05)
        assert held["now"] and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and not held["now"]

    asyncio.run(scenario())


def test_scratch_candidate_holds_the_name_rule_and_the_artifact_probe(setup, tmp_path):
    """The rule lives here once (claunch-bgs76.1).

    ``Cleanup._candidates`` and the Artifacts page's listing both ask this
    function; a caller that restates the names would let the plan and the
    page disagree about the same directory.
    """
    _, _, sessions, _ = setup
    scratch = sessions / "s1" / "scratch"
    (scratch / "pytest-of-me").mkdir()
    (scratch / "browser").mkdir()
    (scratch / "s1-preview-r2").mkdir()
    (scratch / "notes").mkdir()
    # Another session's preview name belongs to that session, not to s1.
    (scratch / "s10-preview-r2").mkdir()
    # claunch-30rkn: no name rule matches these, the artifact probe does.
    (scratch / "bt-preview" / "popen-gw0").mkdir(parents=True)
    completed(scratch / "runs-1")

    taken = {
        path.name for path in scratch.iterdir()
        if ac.scratch_candidate(path, "s1", set())
    }
    assert taken == {"pytest-of-me", "browser", "s1-preview-r2", "bt-preview", "runs-1"}
    assert not ac.scratch_candidate(scratch / "notes", "s1", set())


def test_a_descriptor_exhaustion_reason_says_which_limit(setup, monkeypatch):
    """``[Errno 24]`` alone cannot say whether this daemon was capped.

    On 2026-10-25 every item of a plan came back ``cleanup_pending`` with the
    same ``[Errno 24] Too many open files``, and the reason a reader needs —
    the daemon's own soft limit — was in neither the plan nor the error
    (claunch-wo1zj.1). The hint is appended to the reason at both places a
    reason is recorded: measuring an item, and deleting one.
    """
    service, root, _, _ = setup
    completed(root / "s1c1012000000")

    def starve(path, deadline=None):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(ac, "_snapshot", starve)
    monkeypatch.setattr(ac.open_files, "limits", lambda: (1024, 1048576))

    plan = service.plan("s1")

    assert plan["items"], "nothing to check: the plan had no items"
    for row in plan["items"]:
        assert row["status"] == "cleanup_pending"
        assert "[Errno 24] Too many open files" in row["reason"]
        assert "soft limit of 1024 of 1048576" in row["reason"]


def test_a_reason_that_is_not_descriptor_exhaustion_is_left_alone(setup, monkeypatch):
    """The hint is for ``EMFILE``/``ENFILE`` only, not for every failure."""
    service, root, _, _ = setup
    completed(root / "s1c1012000000")

    def denied(path, deadline=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ac, "_snapshot", denied)

    plan = service.plan("s1")

    assert plan["items"]
    assert all(row["reason"] == "[Errno 13] Permission denied" for row in plan["items"])
