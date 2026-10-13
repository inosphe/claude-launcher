import asyncio
import json
import os
from pathlib import Path
import subprocess
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
    receipt = json.loads(Path(result["receipt"]).read_text())
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


def test_deletion_failure_has_real_reason_and_saved_intent(setup, monkeypatch):
    import shutil
    service, root, _, _ = setup
    mine = completed(root / "s1c1012000000")
    plan = service.plan("s1")
    def fail(path):
        receipt = json.loads((ac.paths.session_dir("s1") / "cleanup" / (plan["plan_id"] + ".json")).read_text())
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


def test_scratch_unknown_browser_and_nested_worktree_are_preserved(setup):
    service, root, sessions, _ = setup
    scratch = sessions / "s1" / "scratch"
    (scratch / "browser").mkdir()
    mine = completed(root / "s1c1012000000")
    nested = mine / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: elsewhere")
    result = service.apply("s1", service.plan("s1")["plan_id"])
    assert result["removed"] == 0 and result["cleanup_pending"] == 2
    assert (scratch / "browser").exists() and nested.exists()


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
            assert result["status"] == "cleanup_pending" and mine.exists()
            window.busy = False
            result = await (await post(request)).json()
            assert result["removed"] == 1 and not window.held
    asyncio.run(scenario())
